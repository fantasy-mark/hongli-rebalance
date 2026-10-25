"""Compare grid TRIGGER variants:
  A. 滚动全局最低 (current daily 行为: last_120d_min = min(history, today))
  C. 120 日窗口最低  (与 run_compound_grid 一致: past 120 trading days)

Other params identical (v3): step_pct=3, tp_pct=3, tp_basis=first_buy, base ¥10000.

Output: side-by-side n_trades / win_rate / hold-days / total P&L / ROI% /
        per-asset breakdown.

Run:
    python scripts/compare_grid_trigger.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    import logging
    logging.getLogger(_n).setLevel(logging.ERROR)

import pandas as pd

from src.simulation import grid_simulator as grid_mod
from src.infrastructure.quote_providers import fetch_one
ASSETS = [
    ("黄金ETF博时",   "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
]
START = "2020-01-01"
END = "2026-10-16"
LOOKBACK = grid_mod.GRID_LOOKBACK  # 120
STEP = grid_mod.GRID_STEP_PCT       # 3.0
TP = grid_mod.GRID_TP_PCT            # 3.0
TP_BASIS = grid_mod.GRID_TP_BASIS    # "first_buy"


# -----------------------------------------------------------------------------
# Custom runners: we replicate run_compound_grid but swap the trigger rule.
# Everything else (pyramid, take-profit) is identical.
# -----------------------------------------------------------------------------

def _run_trigger(prices: pd.Series, mode: str, lookback: int = LOOKBACK,
                 step_pct: float = STEP, tp_pct: float = TP,
                 tp_basis: str = TP_BASIS, pos_yuan: float = grid_mod.GRID_BASE_YUAN
                 ) -> dict:
    in_cycle = False
    entries: list[dict] = []
    cycles: list[dict] = []
    cum_realized = 0.0
    history: list[dict] = []
    prices_sorted = prices.sort_index()

    for i, (date, price) in enumerate(prices_sorted.items()):
        price = float(price)
        if not in_cycle:
            if i > 0:
                if mode == "global":  # A: rolling global min (from start)
                    past_min = prices_sorted.iloc[:i].min()
                elif mode == "window":  # C: past N trading days
                    window = prices_sorted.iloc[max(0, i - lookback):i]
                    past_min = window.min() if len(window) else float("inf")
                elif mode == "window_buffer":  # C + 3% 缓冲: 低于窗口最低 3% 才开
                    window = prices_sorted.iloc[max(0, i - lookback):i]
                    past_min = window.min() * (1 - step_pct / 100) if len(window) else float("inf")
                else:
                    raise ValueError(f"unknown mode: {mode}")
                if price < past_min:
                    sh = pos_yuan / price
                    entries.append({"date": date, "price": price, "shares": sh})
                    in_cycle = True
        else:
            last_price = entries[-1]["price"]
            if price <= last_price * (1 - step_pct / 100):
                sh = pos_yuan / price
                entries.append({"date": date, "price": price, "shares": sh})
            total_shares = sum(e["shares"] for e in entries)
            total_invested = sum(e["shares"] * e["price"] for e in entries)
            if total_shares > 0:
                avg_cost = total_invested / total_shares
                if tp_basis == "first_buy" and entries:
                    tp_target = entries[0]["price"] * (1 + tp_pct / 100)
                else:
                    tp_target = avg_cost * (1 + tp_pct / 100)
                if price >= tp_target:
                    exit_value = total_shares * price
                    pnl = exit_value - total_invested
                    cum_realized += pnl
                    cycles.append({
                        "entries": entries,
                        "exit_date": date,
                        "exit_price": price,
                        "num_buys": len(entries),
                        "avg_cost": avg_cost,
                        "total_shares": total_shares,
                        "total_invested": total_invested,
                        "exit_value": exit_value,
                        "pnl": pnl,
                        "return_pct": pnl / total_invested * 100 if total_invested else 0.0,
                    })
                    in_cycle = False
                    entries = []
        history.append({"date": date, "cum_realized": cum_realized})
    return {"cycles": cycles, "cum_realized": cum_realized, "history": history}


# -----------------------------------------------------------------------------
# Reporting (mirrors compare_grid_tp_basis.py for apples-to-apples)
# -----------------------------------------------------------------------------

def grid_metrics(cycles: list[dict]) -> dict:
    if not cycles:
        return dict(n_trades=0, win_rate=0.0, avg_hold_days=0.0,
                    avg_return_pct=0.0, total_pnl=0.0)
    n = len(cycles)
    wins = sum(1 for c in cycles if c.get("pnl", 0) > 0)
    hold = []
    for c in cycles:
        try:
            d0 = pd.Timestamp(c["entries"][0]["date"])
            d1 = pd.Timestamp(c["exit_date"])
            hold.append((d1 - d0).days)
        except Exception:
            pass
    return dict(
        n_trades=n,
        win_rate=wins / n,
        avg_hold_days=sum(hold) / len(hold) if hold else 0.0,
        avg_return_pct=sum(c.get("return_pct", 0.0) for c in cycles) / n,
        total_pnl=sum(c.get("pnl", 0.0) for c in cycles),
    )


def run_mode(mode: str, label: str, prices: dict[str, pd.DataFrame]) -> dict:
    per_asset = []
    for name, sina, em in ASSETS:
        s = prices[name]["close"].dropna()
        res = _run_trigger(s, mode)
        per_asset.append({"name": name, "cycles": res["cycles"],
                          "cum_realized": res["cum_realized"]})
    total_pnl = sum(p["cum_realized"] for p in per_asset)
    total_invested = 0.0
    n_trades = 0
    wins = 0
    hold = []
    for p in per_asset:
        m = grid_metrics(p["cycles"])
        n_trades += m["n_trades"]
        wins += int(m["n_trades"] * m["win_rate"])
        hold.append(m["avg_hold_days"])
        for c in p["cycles"]:
            total_invested += c.get("total_invested", 0.0)
    return {
        "label": label,
        "mode": mode,
        "total_pnl": total_pnl,
        "total_invested": total_invested,
        "n_trades": n_trades,
        "win_rate": wins / n_trades if n_trades else 0.0,
        "avg_hold_days": sum(hold) / len(hold) if hold else 0.0,
        "roi_pct": total_pnl / total_invested * 100 if total_invested else 0.0,
        "per_asset": per_asset,
    }


def main() -> None:
    print(f"拉取 3 ETF 历史 ({START} → {END})...")
    prices = {}
    for name, sina, em in ASSETS:
        prices[name] = fetch_one(sina, em, START, END)
        print(f"  {name:<14} ({sina})  {len(prices[name])} 行")

    print()
    results = []
    for mode, label in [("global",        "A 全局滚动最低"),
                         ("window",        "C 120日窗口最低"),
                         ("window_buffer", "C+3% 窗口最低-3%")]:
        print(f"回测 {label}...")
        r = run_mode(mode, label, prices)
        results.append(r)
        print(f"  n_trades={r['n_trades']:>3}  win={r['win_rate']:>6.1%}  "
              f"hold={r['avg_hold_days']:>5.1f}d  "
              f"pnl=¥{r['total_pnl']:>+10,.0f}  roi={r['roi_pct']:>+5.2f}%")

    print()
    print("=" * 95)
    print(f"{'metric':<22} {'A 全局':>18} {'C 窗口':>18} {'C+3%':>18} {'C+3% - C':>14}")
    print("-" * 95)
    a, c, cb = results
    rows = [
        ("n_trades",          a["n_trades"],          c["n_trades"],          cb["n_trades"]),
        ("win_rate",          a["win_rate"],          c["win_rate"],          cb["win_rate"]),
        ("avg_hold_days",     a["avg_hold_days"],     c["avg_hold_days"],     cb["avg_hold_days"]),
        ("total_pnl ¥",       a["total_pnl"],         c["total_pnl"],         cb["total_pnl"]),
        ("total_invested ¥",  a["total_invested"],    c["total_invested"],    cb["total_invested"]),
        ("roi %",             a["roi_pct"],           c["roi_pct"],           cb["roi_pct"]),
    ]
    for label, x, y, z in rows:
        print(f"{label:<22} {x:>18,.4f} {y:>18,.4f} {z:>18,.4f} {z - y:>+14,.4f}")
    print("=" * 95)

    print("\nPer-asset 详细:")
    for i, (name, _, _) in enumerate(ASSETS):
        print(f"\n  {name}:")
        for r in results:
            cycles = r["per_asset"][i]["cycles"]
            m = grid_metrics(cycles)
            print(f"    {r['label']:<14}  trades={m['n_trades']:>3}  "
                  f"win={m['win_rate']:>5.0%}  hold={m['avg_hold_days']:>4.0f}d  "
                  f"avg_ret={m['avg_return_pct']:>+5.1f}%  "
                  f"pnl=¥{m['total_pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
