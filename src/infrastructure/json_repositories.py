"""JSON repositories — the persistence boundary (BC-A / BC-C / BC-F).

Four files, four different jobs:

| file | role | written by |
|---|---|---|
| `data/trades.json`     | **the ledger** — real fills, the only source of truth | `scripts/apply_holdings.py` (append) |
| `data/holdings.json`   | **derived cache** of the ledger (two cash pools) | rebuilt from the ledger, never hand-edited |
| `data/grid_state.json` | the grid's per-asset state (BC-C) | `scripts/apply_grid.py` (manual ops) + the daily run (window roll) |
| `data/history.json`    | one immutable `DailySnapshot` per day | the daily run |

⚠️ `holdings.json` is a **cache**, not a fact. It used to serve three
roles at once (position cache / fallback source / de-facto asset
universe); the third role now lives in
`src/domain/portfolio/asset_universe.py`.

All writes are atomic (temp file + `os.replace`), so a failed write
leaves the previous valid file in place rather than a half-written
corrupt one.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
HOLDINGS = ROOT / "data" / "holdings.json"   # v2: derived cache of trades.json
TRADES = ROOT / "data" / "trades.json"       # canonical trade ledger
HISTORY = ROOT / "data" / "history.json"
GRID_STATE = ROOT / "data" / "grid_state.json"
GRID_CHART = ROOT / "data" / "grid_chart.png"


# -----------------------------------------------------------------------------
# Holdings (derived cache)
# -----------------------------------------------------------------------------

def read_holdings(json_path: Path | None = None) -> Tuple[list[dict], float, float]:
    """Parse holdings.json.

    Schema (data/holdings.json, v2 with two cash pools):
        {
            "_schema": "v2",
            "cash_rebalance": <number>,    # real money for ETF buy/sell
            "cash_grid": <number>,         # virtual money for grid accounting
            "positions": [
                {"name": "...", "code": "xxxxxx", "shares": <number>},
                ...
            ]
        }

    Backwards-compat: if only legacy `cash` exists, treat as cash_rebalance
    with cash_grid=0 (one-time bootstrap for old backups).  If json is
    missing entirely, falls back to legacy xlsx (5-row layout) which has
    a single cash field; same treatment.

    Returns (positions, cash_rebalance, cash_grid).
    """
    if json_path is None:
        json_path = HOLDINGS
    if not json_path.exists():
        # Backwards-compat: fall back to xlsx
        legacy = json_path.with_suffix(".xlsx")
        if legacy.exists():
            positions, cash = _read_holdings_legacy_xlsx(legacy)
            return positions, cash, 0.0
        return [], 0.0, 0.0
    try:
        with open(json_path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as ex:
        print(f"WARN holdings.json unreadable: {ex}", file=sys.stderr)
        return [], 0.0, 0.0
    # Two-pool schema
    if "cash_rebalance" in doc or "cash_grid" in doc:
        cash_rebalance = float(doc.get("cash_rebalance", 0) or 0)
        cash_grid = float(doc.get("cash_grid", 0) or 0)
    else:
        # Legacy single-pool bootstrap
        cash_rebalance = float(doc.get("cash", 0) or 0)
        cash_grid = 0.0
    holdings: list[dict] = []
    for p in doc.get("positions", []):
        if not isinstance(p, dict): continue
        name = str(p.get("name", "")).strip()
        code = str(p.get("code", "")).strip().split(".")[0]
        if not name or not code: continue
        try:
            shares = float(p.get("shares", 0))
        except (ValueError, TypeError):
            continue
        holdings.append({"name": name, "code": code, "shares": shares})
    return holdings, cash_rebalance, cash_grid


def _read_holdings_legacy_xlsx(xlsx_path: Path) -> Tuple[list[dict], float]:
    """Legacy v1: read 5-row-header xlsx. Kept for backwards compat."""
    import pandas as pd

    df = pd.read_excel(xlsx_path, header=None)
    cash = 0.0
    for i in range(len(df)):
        label = str(df.iloc[i, 0]).strip()
        if label in ("现金", "资产") and not pd.isna(df.iloc[i, 1]):
            try:
                cash = float(df.iloc[i, 1])
                break
            except (ValueError, TypeError):
                continue
    holdings: list[dict] = []
    for i in range(4, len(df)):
        name_v, code_v, shares_v = df.iloc[i, 0], df.iloc[i, 1], df.iloc[i, 2]
        if pd.isna(name_v) or pd.isna(code_v) or pd.isna(shares_v):
            continue
        code = str(code_v).strip().split(".")[0]
        if code.lower() == "code":
            continue
        try:
            shares = float(shares_v)
        except (ValueError, TypeError):
            continue
        holdings.append({"name": str(name_v).strip(), "code": code, "shares": shares})
    return holdings, cash


# -----------------------------------------------------------------------------
# Trade ledger
# -----------------------------------------------------------------------------

def load_trades(json_path: Path | None = None) -> list[dict]:
    """Return the trades list from data/trades.json. Returns [] if missing/unreadable.

    Each trade is one of:
        ETF block (always touches cash_rebalance):
          {side: "buy"  | "sell",                    date, code, shares, price, [fee], [note]}
        Cash block (specific pool):
          {side: "deposit_rebalance"  | "withdraw_rebalance",  date, amount, [note]}
          {side: "deposit_grid"       | "withdraw_grid",       date, amount, [note]}
          {side: "transfer_to_rebalance",  date, amount, [note]}  # grid→rebalance
          {side: "transfer_to_grid",       date, amount, [note]}  # rebalance→grid
    `id` is auto-assigned by the editor; we do not require it here.
    """
    if json_path is None:
        json_path = TRADES
    if not json_path.exists():
        return []
    try:
        doc = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"WARN trades.json unreadable: {ex}", file=sys.stderr)
        return []
    trades = doc.get("trades", [])
    return [t for t in trades if isinstance(t, dict)]


# -----------------------------------------------------------------------------
# Grid state
# -----------------------------------------------------------------------------

def load_grid_state() -> dict:
    """Load grid state from JSON. Returns {asset_name: asset_state} or {}."""
    if not GRID_STATE.exists():
        return {}
    try:
        return json.loads(GRID_STATE.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"WARN grid_state.json unreadable: {ex}", file=sys.stderr)
        return {}


def save_grid_state(state: dict) -> None:
    # Atomic write (temp + replace) — matches scripts/apply_holdings.py and
    # scripts/apply_grid.py. A failed write leaves the previous valid
    # grid_state.json in place rather than a half-written corrupt file.
    tmp = GRID_STATE.with_suffix(GRID_STATE.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, GRID_STATE)


# -----------------------------------------------------------------------------
# History (daily snapshots)
# -----------------------------------------------------------------------------

def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"WARN history.json unreadable: {ex}", file=sys.stderr)
        return []


def save_history(history: list[dict]) -> None:
    HISTORY.write_text(
        json.dumps(history, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


__all__ = [
    "ROOT", "HOLDINGS", "TRADES", "HISTORY", "GRID_STATE", "GRID_CHART",
    "read_holdings", "load_trades", "load_grid_state", "save_grid_state",
    "load_history", "save_history",
]
