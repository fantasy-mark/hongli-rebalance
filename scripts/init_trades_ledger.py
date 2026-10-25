"""One-shot bootstrap of `data/trades.json` from the current `data/holdings.json`.

The user's first real investment day is 2026-10-12. The current `holdings.json`
holds the *current* state (post any real trades that happened between 10-12
and today). To bootstrap the trade ledger we synthesize a single
`deposit_rebalance` entry dated 2026-10-12 whose `amount` equals the historical
value of the holdings at that day's close plus the current cash. This makes
the ledger math reproduce the current state without a fictitious buy history.

The initial cash is always routed to `cash_rebalance` (the real money pool);
`cash_grid` starts at 0 (per user decision — virtual pool initialized empty).

Idempotent: re-running overwrites with the same single deposit (id is
deterministic). Run ONCE, locally, before the first `apply-holdings.yml`
Action invocation.

Run:
    python -X utf8 scripts/init_trades_ledger.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.infrastructure.quote_providers import fetch_one          # noqa: E402
HOLDINGS = ROOT / "data" / "holdings.json"
TRADES   = ROOT / "data" / "trades.json"

# Mirror src/domain/portfolio/asset_universe.py:ETF_DEFS for the close-fetch call. We don't import
# the domain layer because we want this bootstrap to work even if a future
# refactor breaks that import surface.
ETF_DEFS = {
    "515180": "sh515180",
    "159937": "sz159937",
    "511380": "sh511380",
    "513300": "sh513300",
}

# 建仓日: 2026-10-12. Hard-coded by user requirement; this is the
# "opening balance" date for the ledger.
INITIAL_DATE = "2026-10-12"


def read_holdings(path: Path) -> tuple[list[dict], float]:
    """Parse data/holdings.json. Returns (positions, cash).

    Accepts both v1 (single `cash` field) and v2 (two-pool `cash_rebalance`
    + `cash_grid`) schemas. The bootstrap routes the rebalance-pool total
    into the synthesized `deposit_rebalance` so the replay produces the
    same state as the current holdings file.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    # v2 schema: sum both pools (grid pool is virtual; for the bootstrap
    # the "deposit" represents total cash put in).
    if "cash_rebalance" in doc or "cash_grid" in doc:
        cash = float(doc.get("cash_rebalance", 0) or 0) + float(doc.get("cash_grid", 0) or 0)
    else:
        # v1 fallback (legacy single-pool holdings).
        cash = float(doc.get("cash", 0) or 0)
    positions: list[dict] = []
    for p in doc.get("positions", []):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", "")).strip()
        code = str(p.get("code", "")).strip().split(".")[0]
        try:
            shares = float(p.get("shares", 0))
        except (ValueError, TypeError):
            continue
        if not name or not code or shares <= 0:
            continue
        positions.append({"name": name, "code": code, "shares": shares})
    return positions, cash


def fetch_close(code: str, date_str: str) -> float:
    """Fetch the close price for `code` on `date_str`. Raises on failure."""
    if code not in ETF_DEFS:
        raise SystemExit(f"ERROR code {code!r} not in ETF_DEFS whitelist; "
                         f"add it before running this script.")
    sina = ETF_DEFS[code]
    df = fetch_one(sina, code, start=date_str, end=date_str)
    if df is None or df.empty:
        raise SystemExit(f"ERROR no price data for {code} on {date_str}")
    return float(df.iloc[-1]["close"])


def write_trades_json(path: Path, trades: list[dict]) -> None:
    doc = {
        "_schema": "trades-v1",
        "_comment": ("Append-only trade ledger. Source of truth for cash "
                     "and positions[].shares. Do not reorder; corrections "
                     "are new entries."),
        "trades": trades,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def write_holdings_v2(path: Path, cash: float, positions: list[dict]) -> None:
    """Rewrite data/holdings.json as schema v2 with two cash pools
    (derived cache). Initial bootstrap routes all cash to cash_rebalance;
    cash_grid starts at 0."""
    doc = {
        "_schema": "v2",
        "_comment": ("REBUILT from data/trades.json. 双池: "
                     "cash_rebalance (真实现金, ETF买卖用) + "
                     "cash_grid (虚拟, 网格记账用). 编辑持仓请用页面"
                     "交易表单 (会同时更新 trades.json); 直接改这个文件"
                     "会被下一次 apply-holdings 覆盖."),
        "cash_rebalance": round(float(cash), 2),
        "cash_grid": 0.0,
        "positions": [
            {"name": p["name"], "code": p["code"], "shares": p["shares"]}
            for p in positions
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    positions, cash = read_holdings(HOLDINGS)
    if not positions:
        raise SystemExit(f"ERROR {HOLDINGS} has no positions; nothing to bootstrap.")

    print(f"建仓日: {INITIAL_DATE}")
    print(f"当前持仓: cash=¥{cash:.2f}, {len(positions)} positions\n")

    etf_total = 0.0
    breakdown: list[tuple[str, str, float, float, float]] = []
    for p in positions:
        close = fetch_close(p["code"], INITIAL_DATE)
        v = close * p["shares"]
        etf_total += v
        breakdown.append((p["name"], p["code"], p["shares"], close, v))
        print(f"  {p['name']:<14} ({p['code']}) shares={p['shares']:>10,.0f}  "
              f"close@{INITIAL_DATE}=¥{close:.4f}  value=¥{v:>12,.2f}")

    deposit_amount = round(etf_total + cash, 2)
    print(f"\n  ETF total @ 建仓日  = ¥{etf_total:>12,.2f}")
    print(f"  + cash              = ¥{cash:>12,.2f}")
    print(f"  = deposit           = ¥{deposit_amount:>12,.2f}\n")

    # Reconstruct the initial portfolio: 1 deposit_rebalance + 1 buy per
    # position, all on INITIAL_DATE.  Replaying these yields:
    #   cash_rebalance = deposit − Σ(shares × close) = 652
    #   cash_grid = 0
    #   positions[i].shares = exact holdings.json values
    trades: list[dict] = [{
        "id": f"{INITIAL_DATE}-deposit_rebalance-001",
        "date": INITIAL_DATE,
        "side": "deposit_rebalance",
        "amount": deposit_amount,
        "note": (f"初始入金 (back-derived from holdings.json × "
                 f"{INITIAL_DATE} close + cash=¥{cash:.2f})"),
    }]
    for i, (name, code, shares, close, _v) in enumerate(breakdown, start=2):
        seq = f"{i:03d}"
        trades.append({
            "id": f"{INITIAL_DATE}-buy-{seq}",
            "date": INITIAL_DATE,
            "side": "buy",
            "code": code,
            "shares": int(shares) if shares == int(shares) else shares,
            "price": close,
            "fee": 0.0,
            "note": f"{name} 建仓买入 @ {INITIAL_DATE} close",
        })

    write_trades_json(TRADES, trades)
    write_holdings_v2(HOLDINGS, cash, positions)

    try:
        rel_t = TRADES.relative_to(ROOT)
        rel_h = HOLDINGS.relative_to(ROOT)
    except ValueError:
        rel_t, rel_h = TRADES, HOLDINGS
    print(f"OK wrote {rel_t}: {len(trades)} trades "
          f"(1 deposit_rebalance ¥{deposit_amount:,.2f} + {len(breakdown)} buys)")
    print(f"OK wrote {rel_h}: schema v2, cash_rebalance=¥{cash:,.2f}, "
          f"cash_grid=¥0.00, {len(positions)} positions")


if __name__ == "__main__":
    main()
