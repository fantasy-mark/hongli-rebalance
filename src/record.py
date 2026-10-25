"""Daily double-strategy snapshot: 3pp rebalance + compound grid.

Flow (each Action run):
  1. Read holdings.xlsx (positions + cash)
  2. Fetch latest price for each ETF (3-tier: realtime → spot → history)
  3. Evaluate 3pp rebalance: any |weight - 1/3| > 3pp → trigger
  4. Build a `pool` series (today's 3pp total appended to history.totals)
     and compute recent_min = min(pool[-757:])  →  drives grid layer size
  5. For each asset, advance grid state by one day (JSON-persistent)
  6. Save: data/history.json (record) + data/grid_state.json (per-asset)
  7. Render data/grid_chart.png  (consumed by GitHub Pages <img>)
  8. Emit $GITHUB_OUTPUT for downstream steps
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Tuple

import pandas as pd

from . import grid as grid_mod
from .fetch import latest, fetch_qdii_premium, fetch_kline_history

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "data" / "holdings.json"   # v2: derived cache of trades.json
TRADES = ROOT / "data" / "trades.json"       # canonical trade ledger
HISTORY = ROOT / "data" / "history.json"
GRID_STATE = ROOT / "data" / "grid_state.json"
GRID_CHART = ROOT / "data" / "grid_chart.png"

# Code → (sina_symbol, eastmoney_symbol, display_name, is_qdii)
# QDII flags drive the premium-rate fetch & alert in notify.py.
ETF_DEFS = {
    "515180": ("sh515180", "515180", "红利ETF易方达", False),
    "159937": ("sz159937", "159937", "黄金ETF博时", False),
    "511380": ("sh511380", "511380", "可转债ETF博时", False),
    "513300": ("sh513300", "513300", "纳斯达克ETF华夏", True),   # QDII
}
ASSET_NAMES = [v[2] for v in ETF_DEFS.values()]
NAME_TO_CODE = {v[2]: code for code, v in ETF_DEFS.items()}
QDII_CODES = {code for code, v in ETF_DEFS.items() if v[3]}

# 3pp parameters
TARGET_W = 1.0 / 3
THRESHOLD_PP = float(os.environ.get("THRESHOLD_PP", "3.0"))


# -----------------------------------------------------------------------------
# Holdings / pricing (preserved from v1)
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


# -----------------------------------------------------------------------------
# Trade ledger (data/trades.json) — canonical source for cash + positions[].shares
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


def apply_trade(state: dict, trade: dict) -> None:
    """Mutate `state` in place: state = {"cash_rebalance": float,
    "cash_grid": float, "positions": {code: shares}}.

    Side routing:
        deposit_rebalance  → +amount to cash_rebalance
        deposit_grid       → +amount to cash_grid
        withdraw_rebalance → -amount from cash_rebalance
        withdraw_grid      → -amount from cash_grid
        transfer_to_grid   → -amount from cash_rebalance, +amount to cash_grid
        transfer_to_rebalance → +amount to cash_rebalance, -amount from cash_grid
        buy                → -amount from cash_rebalance, +shares to positions
        sell               → +amount to cash_rebalance, -shares from positions

    Atomic: computes the new state in a scratch dict, validates (no
    negative pool, no oversell), then commits. On failure the original
    `state` is left untouched and ValueError is raised.

    Rounds cash to 2 dp after each trade.
    """
    side = trade["side"]
    # Compute new state in a scratch copy; commit only if validation passes.
    new_state: dict = {
        "cash_rebalance": state.get("cash_rebalance", 0.0),
        "cash_grid":      state.get("cash_grid", 0.0),
        "positions": dict(state.get("positions", {})),
    }
    if side == "deposit_rebalance":
        new_state["cash_rebalance"] += float(trade["amount"])
    elif side == "deposit_grid":
        new_state["cash_grid"] += float(trade["amount"])
    elif side == "withdraw_rebalance":
        new_state["cash_rebalance"] -= float(trade["amount"])
    elif side == "withdraw_grid":
        new_state["cash_grid"] -= float(trade["amount"])
    elif side == "transfer_to_grid":
        amt = float(trade["amount"])
        new_state["cash_rebalance"] -= amt
        new_state["cash_grid"] += amt
    elif side == "transfer_to_rebalance":
        amt = float(trade["amount"])
        new_state["cash_grid"] -= amt
        new_state["cash_rebalance"] += amt
    elif side == "buy":
        code = str(trade["code"])
        sh = float(trade["shares"])
        px = float(trade["price"])
        fee = float(trade.get("fee", 0.0))
        new_state["positions"][code] = new_state["positions"].get(code, 0.0) + sh
        new_state["cash_rebalance"] -= sh * px + fee
    elif side == "sell":
        code = str(trade["code"])
        sh = float(trade["shares"])
        px = float(trade["price"])
        fee = float(trade.get("fee", 0.0))
        held = new_state["positions"].get(code, 0.0)
        if held < sh - 1e-9:
            raise ValueError(
                f"oversell {code}: held={held:.4f}, sell={sh:.4f}")
        new_state["positions"][code] = held - sh
        new_state["cash_rebalance"] += sh * px - fee
    else:
        raise ValueError(f"unknown side: {side!r}")
    new_state["cash_rebalance"] = round(new_state["cash_rebalance"], 2)
    new_state["cash_grid"] = round(new_state["cash_grid"], 2)
    # Defense: any pool going negative is a programming error
    if new_state["cash_rebalance"] < 0 or new_state["cash_grid"] < 0:
        raise ValueError(
            f"cash would go negative: rebalance={new_state['cash_rebalance']:.2f}, "
            f"grid={new_state['cash_grid']:.2f} (after {side})")
    # All validations passed — commit
    state["cash_rebalance"] = new_state["cash_rebalance"]
    state["cash_grid"] = new_state["cash_grid"]
    for c in list(new_state["positions"].keys()):
        v = round(new_state["positions"][c], 2)
        if v <= 0:
            # Drop zero/negative entries; if this was the only position for
            # a code, remove the key entirely.
            if c in state["positions"]:
                del state["positions"][c]
        else:
            state["positions"][c] = v


def rebuild_holdings_from_trades(
    trades: list[dict],
    as_of_date: str | None = None,
    name_lookup: dict[str, str] | None = None,
) -> Tuple[list[dict], float, float]:
    """Replay `trades` chronologically; return (positions, cash_rebalance, cash_grid).

    Only includes codes with shares > 0 in the returned positions list.
    `as_of_date` (ISO yyyy-mm-dd) is inclusive — trades dated after it are
    skipped. `name_lookup` defaults to ETF_DEFS names.
    """
    if name_lookup is None:
        name_lookup = {code: v[2] for code, v in ETF_DEFS.items()}
    state: dict = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {}}
    seen_codes: list[str] = []

    # Sort by (date, original_index) so same-day trades play in append order.
    indexed = list(enumerate(trades))
    indexed.sort(key=lambda kv: (kv[1].get("date", ""), kv[0]))

    for _, t in indexed:
        if as_of_date and t.get("date", "") > as_of_date:
            continue
        side = t.get("side")
        code = t.get("code")
        if side in ("buy", "sell") and code not in seen_codes:
            seen_codes.append(code)
        apply_trade(state, t)

    positions = [
        {"name": name_lookup.get(c, c), "code": c,
         "shares": state["positions"][c]}
        for c in seen_codes
        if state["positions"].get(c, 0.0) > 0
    ]
    return positions, state["cash_rebalance"], state["cash_grid"]


def _read_holdings_legacy_xlsx(xlsx_path: Path) -> Tuple[list[dict], float]:
    """Legacy v1: read 5-row-header xlsx. Kept for backwards compat."""
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


def fetch_positions(holdings: list[dict]) -> Tuple[list[dict], float, list[Tuple[str, str]]]:
    """Fetch latest price for each holding; return (positions, etf_total, errors)."""
    positions: list[dict] = []
    etf_total = 0.0
    errors: list[Tuple[str, str]] = []
    for h in holdings:
        if h["code"] not in ETF_DEFS:
            errors.append((h["code"], "unknown code (not in ETF_DEFS)"))
            continue
        sina, em, _, _ = ETF_DEFS[h["code"]]
        try:
            price, pdate = latest(sina, em)
            value = h["shares"] * price
            etf_total += value
            positions.append({
                "name": h["name"], "code": h["code"], "shares": h["shares"],
                "price": round(price, 4), "date": pdate.date().isoformat(),
                "value": round(value, 2),
            })
            age_days = (pd.Timestamp.today().normalize() - pdate).days
            print(f"  {h['name']:<14} ({h['code']}) ¥{price:>9.4f}  "
                  f"as_of {pdate.date()} (Δ {age_days:+d}d)", file=sys.stderr)
        except Exception as ex:
            errors.append((h["code"], str(ex)))
    return positions, etf_total, errors


# -----------------------------------------------------------------------------
# 3pp evaluation
# -----------------------------------------------------------------------------

def merged_shares_by_code(grid_state: dict) -> dict[str, float]:
    """Map ETF code → shares the grid has merged into the portfolio.

    The grid's take-profit sells back only the principal; the leftover
    shares are handed over to the rebalance portfolio (模拟口径 — they are
    NOT written to trades.json, which records real fills only).  From that
    point on they are ordinary portfolio holdings and must be counted in
    the position's value, the 3pp weight and the total.
    """
    out: dict[str, float] = {}
    for name, asset in (grid_state or {}).items():
        if not isinstance(asset, dict):
            continue
        code = NAME_TO_CODE.get(name)
        if not code:
            continue
        shares = float(asset.get("merged_shares", 0.0) or 0.0)
        if shares > 0:
            out[code] = shares
    return out


def fetch_extra_prices(codes: list[str]) -> Tuple[dict[str, float], list[str]]:
    """Fetch latest prices for codes that carry merged (grid) shares but
    aren't held in the real portfolio — they still need a valuation.

    Returns (price_by_code, errors).
    """
    prices: dict[str, float] = {}
    errors: list[Tuple[str, str]] = []
    for code in codes:
        if code not in ETF_DEFS:
            errors.append((code, "unknown code (not in ETF_DEFS)"))
            continue
        sina, em, _, _ = ETF_DEFS[code]
        try:
            price, _pdate = latest(sina, em)
            prices[code] = float(price)
        except Exception as ex:
            errors.append((code, str(ex)))
    return prices, errors


def merge_grid_shares_into_positions(
    positions: list[dict],
    merged_by_code: dict[str, float],
    extra_prices: dict[str, float] | None = None,
) -> list[dict]:
    """Fold the grid's merged shares into the portfolio positions.

    Returns a new list of position dicts (the input is not mutated).  Each
    position gains:
        merged_shares  — shares handed over by the grid (模拟口径)
        merged_value   — their market value
        total_shares   — real shares + merged shares
    and `value` becomes total_shares × price, so the 3pp weight and the
    portfolio total see the merged exposure.

    A code that has merged shares but no real holding becomes a position
    of its own (shares = 0, all of it merged) — the portfolio genuinely
    holds it.  Its price comes from `extra_prices`; a code we couldn't
    price is skipped (and reported by the caller as an error).
    """
    extra_prices = extra_prices or {}
    out: list[dict] = []
    seen: set[str] = set()
    for p in positions:
        code = str(p.get("code", ""))
        # 份额先定到 2 位小数, 再由它算市值 —— 页面上的"多少份"和"多少钱"
        # 必须自洽 (用户拿显示的份额 × 价格验算要能对上)。
        merged = round(float(merged_by_code.get(code, 0.0) or 0.0), 2)
        seen.add(code)
        real_shares = float(p.get("shares", 0.0))
        price = float(p.get("price", 0.0))
        new = dict(p)
        new["merged_shares"] = merged
        new["total_shares"] = round(real_shares + merged, 2)
        new["merged_value"] = round(merged * price, 2)
        new["value"] = round(new["total_shares"] * price, 2)
        out.append(new)
    # Codes with merged shares but no real holding → new position rows.
    for code, merged in merged_by_code.items():
        if code in seen:
            continue
        price = extra_prices.get(code)
        if not price:
            continue
        name = ETF_DEFS[code][2]
        merged = round(float(merged), 2)
        merged_value = round(merged * float(price), 2)
        out.append({
            "name": name,
            "code": code,
            "shares": 0.0,
            "merged_shares": merged,
            "total_shares": merged,
            "merged_value": merged_value,
            "price": round(float(price), 4),
            "date": "",
            "value": merged_value,
            "merged_only": True,
        })
    return out


def evaluate(positions: list[dict], cash_rebalance: float, cash_grid: float
              ) -> Tuple[float, list[float], bool, list[dict]]:
    """Return (total, weights, triggered, plan). plan is empty when not triggered.

    total = etf_value + cash_rebalance + cash_grid.  The grid pool is
    part of the portfolio (so weights and trigger check include it),
    but the grid engine itself is fully virtual and doesn't touch the
    cash pool — see src/grid.py.

    `positions` must already include the grid's merged shares (see
    merge_grid_shares_into_positions) — they are portfolio holdings now,
    so they have to be in the value the weights are computed on.

    TARGET_W is dynamic: 1/len(positions) — works for 3, 4, 5 asset configs.
    """
    cash_total = cash_rebalance + cash_grid
    total = sum(p["value"] for p in positions) + cash_total
    n = len(positions)
    target_w = 1.0 / n if n > 0 else 0.0
    weights = [p["value"] / total if total > 0 else 0 for p in positions]
    triggered = any(abs(w - target_w) * 100 > THRESHOLD_PP for w in weights)
    plan: list[dict] = []
    if triggered and total > 0:
        for p, w in zip(positions, weights):
            target_value = total * target_w
            plan.append({
                "name": p["name"],
                "code": p["code"],
                "current_value": round(p["value"], 2),
                "target_value": round(target_value, 2),
                "delta_value": round(target_value - p["value"], 2),
            })
    return total, weights, triggered, plan


# -----------------------------------------------------------------------------
# Grid state (JSON-persistent)
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
    import os
    tmp = GRID_STATE.with_suffix(GRID_STATE.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, GRID_STATE)


# -----------------------------------------------------------------------------
# History (JSON-persistent)
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


# -----------------------------------------------------------------------------
# Build & emit
# -----------------------------------------------------------------------------

def write_github_output(date_str: str, total: float, triggered: bool) -> None:
    """Write the `date` / `total` / `trigger` step outputs.

    ⚠️ `trigger=yes` now means "需人工关注" —— either the 3pp rebalance
    fired, or the grid engine has a **suggestion** pending (the engine
    never trades by itself). Only `outputs.date` is consumed downstream.
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"date={date_str}\n")
        f.write(f"total={total:.2f}\n")
        f.write(f"trigger={'yes' if triggered else 'no'}\n")


def build_record(today: str, positions: list[dict],
                 cash_rebalance: float, cash_grid: float,
                 total: float, weights: list[float],
                 triggered_3pp: bool, plan: list[dict],
                 grid_signals: list[dict], grid_state: dict,
                 qdii_premiums: list[dict] | None = None) -> dict:
    etf_total = sum(p["value"] for p in positions)
    n = len(positions)
    target_w = 1.0 / n if n > 0 else 0.0
    # `ops` is an append-only log whose canonical home is
    # data/grid_state.json (the page fetches that file directly for the
    # 复合网格 操作记录 table). Embedding it in every daily record would
    # re-copy the whole log once per day forever, growing history.json
    # without bound. Strip it — nothing reads this copy.
    slim_grid_state = {
        name: ({k: v for k, v in asset.items() if k != "ops"}
               if isinstance(asset, dict) else asset)
        for name, asset in (grid_state or {}).items()
    }
    # 网格归并到再平衡的份额合计 —— 页面/邮件在再平衡那一栏显示它。
    merged_shares_total = sum(float(p.get("merged_shares", 0.0) or 0.0)
                              for p in positions)
    merged_value_total = sum(float(p.get("merged_value", 0.0) or 0.0)
                             for p in positions)
    return {
        "date": today,
        "total": round(total, 2),
        "etf_value": round(etf_total, 2),
        "cash_rebalance": round(cash_rebalance, 2),
        "cash_grid": round(cash_grid, 2),
        "cash_total": round(cash_rebalance + cash_grid, 2),
        "positions": positions,
        "weights": [round(w, 4) for w in weights],
        "target_w": target_w,
        "threshold_pp": THRESHOLD_PP,
        "triggered": triggered_3pp,
        "plan": plan,
        "grid_signals": grid_signals,
        "grid_state": slim_grid_state,
        "merged_shares_total": round(merged_shares_total, 2),
        "merged_value_total": round(merged_value_total, 2),
        "qdii_premiums": qdii_premiums or [],
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def advance_all_grids(state: dict, today: str, prices_by_name: dict[str, float],
                      pool_value: float, recent_min: float) -> tuple[dict, list[dict]]:
    """Advance grid state for all 4 assets, returning (new_state, signals).

    ⚠️ 引擎**不再自动交易** —— 这里只是滚动 120 日窗口 + 算出「建议」。
    `signal` 是**建议提交哪种 op** (open/add/tp), 不是"已成交"。
    """
    layer = grid_mod.layer_yuan(pool_value, recent_min)
    new_state: dict = {}
    signals: list[dict] = []
    for name in ASSET_NAMES:
        asset_state = state.get(name) or grid_mod.fresh_asset_state()
        price = prices_by_name.get(name)
        if price is None:
            new_state[name] = asset_state
            continue
        # 建议基于**追加今天之前**的状态算 —— 今天的低价不能自己当门槛
        adv = grid_mod.advisory(asset_state, float(price), layer)
        new_asset_state, signal = grid_mod.advance_one_day(
            asset_state, today, float(price), layer,
        )
        new_state[name] = new_asset_state
        snap = grid_mod.asset_snapshot(new_asset_state, float(price))
        signals.append({
            "name": name,
            "price": round(float(price), 4),
            "layer_yuan": round(layer, 2),
            # "open" | "add" | "tp" | None —— 建议提交哪种 op, 不是"已成交"
            "signal": signal,
            "in_cycle": snap["in_cycle"],
            "num_buys": snap["num_buys"],
            "avg_cost": snap["avg_cost"],
            # 网格自己的资金占用 = 只算在周期内的层 (出场后本金回来, 占用回落)
            "current_value": snap["current_value"],
            "unrealized_pnl": snap["unrealized_pnl"],
            "return_pct": snap["return_pct"],
            # 已归并到再平衡持仓的份额 (不再是网格的资产) —— 页面在再平衡
            # 那一栏显示, 网格这一栏不重复计入资金占用。
            "merged_shares": snap["merged_shares"],
            "merged_value": snap["merged_value"],
            # 距「最近的那个」触发位还有多远 (正数 = 还没触发, ≤0 = 已越过)
            "trigger_pct": adv["trigger_pct"],
            "trigger_kind": adv["trigger_kind"],
            "trigger_price": adv["trigger_price"],
            # 三个触发位各自的距离 (None = 当前状态下不适用)
            "dist_open_pct": adv["dist_open_pct"],
            "dist_add_pct": adv["dist_add_pct"],
            "dist_tp_pct": adv["dist_tp_pct"],
            # 建议成交价 / 份额 / 金额 (只有 open/add 有金额)
            "suggest_price": adv["suggest_price"],
            "suggest_shares": adv["suggest_shares"],
            "suggest_amount": adv["suggest_amount"],
        })
    return new_state, signals


def main() -> None:
    today = datetime.date.today().isoformat()

    # 1) Universe of codes = holdings.json (cheap registry of what the user
    #    wants tracked).  Shares here are STALE — we only use the code list.
    raw_holdings, _legacy_cash_rebalance, _legacy_cash_grid = read_holdings()

    # 2) Real positions + cash from the trade ledger.  This is the
    #    canonical source for shares/cash in the daily snapshot.
    trades = load_trades()
    positions, cash_rebalance, cash_grid = rebuild_holdings_from_trades(
        trades, as_of_date=today,
    )

    # 3) Defensive fallback: empty trades (the bootstrap moment before
    #    scripts/init_trades_ledger.py has been run).  In that case fall
    #    back to whatever shares are in holdings.json so the snapshot
    #    still produces a meaningful daily record.
    if not positions:
        if not raw_holdings:
            print("ERROR no positions (trades.json empty and holdings.json empty)",
                  file=sys.stderr)
            sys.exit(1)
        positions = [
            {"name": h["name"], "code": h["code"], "shares": h["shares"]}
            for h in raw_holdings
        ]
        print("WARN trades.json empty; falling back to holdings.json shares",
              file=sys.stderr)

    positions, etf_total, errors = fetch_positions(positions)
    for code, msg in errors:
        print(f"WARN {code}: {msg}", file=sys.stderr)
    if not positions:
        print("ERROR all fetches failed", file=sys.stderr)
        sys.exit(1)

    # QDII premium rate: enrich position dicts with premium info
    # (best-effort: failure does not abort the run)
    qdii_top: list[dict] = []      # tracked-but-not-held QDII premiums
    for p in positions:
        if p["code"] in QDII_CODES:
            try:
                prem = fetch_qdii_premium(p["code"])
                if prem:
                    p["premium"] = prem   # {price, iopv, premium_pct, nav_date, ...}
                    flag = "⚠" if abs(prem["premium_pct"]) > 5 else "·"
                    print(
                        f"  QDII {p['name']:<14} premium={prem['premium_pct']:+.2f}%  "
                        f"(市价 {prem['price']:.4f} / IOPV {prem['iopv']:.4f}@{prem['nav_date']})  {flag}",
                        file=sys.stderr,
                    )
            except Exception as ex:
                print(f"WARN QDII premium {p['code']} failed: {ex}", file=sys.stderr)

    # Also fetch premium for QDII codes known to ETF_DEFS but NOT held today —
    # so the email can still warn ("don't buy, premium too high").
    held_codes = {p["code"] for p in positions}
    for code, (sina, em, name, is_qdii) in ETF_DEFS.items():
        if not is_qdii or code in held_codes:
            continue
        try:
            prem = fetch_qdii_premium(code)
            if prem:
                qdii_top.append({
                    "code": code, "name": name, "in_portfolio": False,
                    **prem,
                })
                flag = "⚠" if abs(prem["premium_pct"]) > 5 else "·"
                print(
                    f"  QDII {name:<14} (未持仓) premium={prem['premium_pct']:+.2f}%  "
                    f"(市价 {prem['price']:.4f} / IOPV {prem['iopv']:.4f}@{prem['nav_date']})  {flag}",
                    file=sys.stderr,
                )
        except Exception as ex:
            print(f"WARN QDII unheld premium {code} failed: {ex}", file=sys.stderr)

    # --- Grid-merged shares → portfolio -------------------------------
    # The grid's take-profit hands its leftover shares to the rebalance
    # portfolio.  Those shares are portfolio holdings from then on, so they
    # have to be in the value the 3pp weights are computed on.
    #
    # Two passes: the pre-merge value sizes today's grid layer, the
    # post-merge value is what the record reports. Since the engine no
    # longer trades, `merged_shares` can't change during a run — so the
    # two totals are now always equal. The structure is kept because the
    # manual path (apply_grid.py) *does* change merged shares, and the
    # daily record must see the portfolio the user will actually hold.
    state = load_grid_state()
    merged_before = merged_shares_by_code(state)
    held_codes = {p["code"] for p in positions}
    extra_codes = [c for c in merged_before if c not in held_codes]
    extra_prices, extra_errors = fetch_extra_prices(extra_codes)
    for code, msg in extra_errors:
        print(f"WARN merged-share price {code}: {msg}", file=sys.stderr)
    positions_eff = merge_grid_shares_into_positions(
        positions, merged_before, extra_prices,
    )
    total_pre, _w_pre, _t_pre, _p_pre = evaluate(
        positions_eff, cash_rebalance, cash_grid,
    )

    # 3pp pool for grid layer sizing (history.totals + today)
    history = load_history()
    pool = pd.Series(
        [r["total"] for r in history if "total" in r] + [total_pre],
        index=pd.to_datetime(
            [r["date"] for r in history if "total" in r] + [today]
        ),
    ).sort_index()
    window = grid_mod.GRID_FLOATING_WINDOW + 1
    recent_min = float(pool.tail(window).min()) if len(pool) else 0.0

    # Advance grid
    # Today uses the K-line LOW (intraday low) so the trigger can fire on
    # intraday dips even if the close is above yesterday's close.
    #
    # `positions_eff` (not `positions`) is the source: it also carries the
    # codes the grid tracks but the portfolio doesn't hold — those rows exist
    # only because the grid merged shares into them, and the grid still has
    # to advance on them.
    prices_by_name = {p["name"]: p["price"] for p in positions_eff
                      if p["name"] in ASSET_NAMES}
    lows_by_name: dict[str, float] = {}
    for p in positions_eff:
        if p["name"] not in ASSET_NAMES:
            continue
        code = p["code"]
        kf = fetch_kline_history(code, start=today, end=today)
        if kf is not None and not kf.empty:
            lows_by_name[p["name"]] = float(kf["low"].iloc[-1])
    # If K-line fetch failed for an asset, fall back to the realtime close
    grid_prices = {n: lows_by_name.get(n, prices_by_name.get(n)) for n in ASSET_NAMES}
    new_state, grid_signals = advance_all_grids(
        state, today, grid_prices, total_pre, recent_min,
    )
    # Annotate signals with the (intraday) low that drove the decision so
    # the daily record shows the actual trigger price, not the close.
    for s in grid_signals:
        if s["name"] in lows_by_name:
            s["intraday_low"] = round(lows_by_name[s["name"]], 4)
    save_grid_state(new_state)

    # Re-merge: today's take-profit may have added new merged shares.
    merged_after = merged_shares_by_code(new_state)
    extra_codes = [c for c in merged_after if c not in held_codes]
    if extra_codes:
        extra_prices, extra_errors = fetch_extra_prices(extra_codes)
        for code, msg in extra_errors:
            print(f"WARN merged-share price {code}: {msg}", file=sys.stderr)
    positions_eff = merge_grid_shares_into_positions(
        positions, merged_after, extra_prices,
    )
    total, weights, triggered_3pp, plan = evaluate(
        positions_eff, cash_rebalance, cash_grid,
    )
    positions = positions_eff

    # Persist record
    record = build_record(
        today=today, positions=positions,
        cash_rebalance=cash_rebalance, cash_grid=cash_grid,
        total=total, weights=weights,
        triggered_3pp=triggered_3pp, plan=plan,
        grid_signals=grid_signals, grid_state=new_state,
        qdii_premiums=qdii_top,
    )
    history = [r for r in history if r["date"] != today]
    history.append(record)
    save_history(history)

    # Render chart (best-effort: chart may fail if network blocked; record is already saved)
    try:
        from .chart import render_chart
        render_chart(history, GRID_CHART)
    except Exception as ex:
        print(f"WARN chart render failed: {ex}", file=sys.stderr)

    any_signal = triggered_3pp or any(s["signal"] for s in grid_signals)
    dyn_target = 1.0 / len(positions) if positions else TARGET_W
    print(f"DATE={today} TOTAL={total:.2f} STATUS={'TRIGGER' if any_signal else 'ok'}")
    print(f"  3pp: {'⚠ TRIGGERED' if triggered_3pp else 'ok'} (max Δ {max(abs(w - dyn_target) * 100 for w in weights):.2f}pp)")
    for p, w in zip(positions, weights):
        delta = (w - dyn_target) * 100
        flag = " ⚠" if abs(delta) > THRESHOLD_PP else ""
        merged_tag = (f"  (含归并 {p['merged_shares']:,.0f}份)"
                      if p.get("merged_shares") else "")
        print(f"    {p['name']:<14} {w:>6.2%}  Δ={delta:+.2f}pp{flag}{merged_tag}")
    print(f"  grid layer: ¥{grid_mod.layer_yuan(total, recent_min):,.0f} "
          f"(pool ¥{total:,.0f}, recent_min ¥{recent_min:,.0f})")
    for s in grid_signals:
        # ⚠️ 引擎只给建议, 不会自动下单 —— tag 是"建议提交哪种 op"
        tag = f"建议{s['signal']}" if s["signal"] else "-"
        print(f"    {s['name']:<14} {tag:<8}  layer=¥{s['layer_yuan']:,.0f}  "
              f"in_cycle={s['in_cycle']}  buys={s['num_buys']}  "
              f"unreal={s['unrealized_pnl']:+,.0f}  "
              f"占用=¥{s['current_value']:,.0f}  "
              f"已归并={s['merged_shares']:,.0f}份(¥{s['merged_value']:,.0f})  "
              f"距{s['trigger_kind']}={s['trigger_pct']:+.2f}%")

    write_github_output(today, total, any_signal)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
