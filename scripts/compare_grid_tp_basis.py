"""Compare grid TP-basis variants: avg_cost (v2) vs first_buy (v3) on real 3-ETF history.

Output: a side-by-side table of CAGR / Sharpe / max DD / n_trades / win-rate /
avg hold-days / total realized P&L.

WARNING (2026-10-23 起): 网格出场规则已改为「只卖成本金额」(见
src/grid.py:split_take_profit) —— 正常出场时已实现盈亏恒为 0, 浮盈转成
永久持有的零成本份额。所以本脚本的 realized P&L / win-rate 会恒为 0,
输出不再代表网格的真实收益。要看真实收益用
scripts/compare_grid_exit_mode.py。

Run:
    python scripts/compare_grid_tp_basis.py
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

from src import grid as grid_mod
from src.fetch import fetch_one

# 3 资产 (跟 v3 一致)
ASSETS = [
    ("黄金ETF博时",   "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
]
START = "2020-01-01"
END = "2026-10-09"


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


def run_one_tp_basis(tp_basis: str, prices_by_name: dict[str, pd.DataFrame]) -> dict:
    per_asset = []
    for name, sina, em in ASSETS:
        s = prices_by_name[name]["close"].dropna()
        res = grid_mod.run_compound_grid(s, tp_basis=tp_basis, pos_yuan=grid_mod.GRID_BASE_YUAN)
        per_asset.append({
            "name": name,
            "cycles": res["cycles"],
            "cum_realized": res["cum_realized"],
        })
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
        "tp_basis": tp_basis,
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
    for basis in ("avg_cost", "first_buy"):
        print(f"回测 tp_basis={basis}...")
        r = run_one_tp_basis(basis, prices)
        results.append(r)
        print(f"  n_trades={r['n_trades']:>3}  win={r['win_rate']:>6.1%}  "
              f"hold={r['avg_hold_days']:>5.1f}d  "
              f"pnl=¥{r['total_pnl']:>+10,.0f}  roi={r['roi_pct']:>+5.2f}%")

    # 对比表
    print()
    print("=" * 78)
    print(f"{'metric':<24} {'avg_cost (v2)':>20} {'first_buy (v3)':>20} {'Δ':>10}")
    print("-" * 78)
    v2, v3 = results
    rows = [
        ("n_trades",       v2["n_trades"],       v3["n_trades"]),
        ("win_rate",       v2["win_rate"],       v3["win_rate"]),
        ("avg_hold_days",  v2["avg_hold_days"],  v3["avg_hold_days"]),
        ("total_pnl ¥",    v2["total_pnl"],      v3["total_pnl"]),
        ("total_invested ¥", v2["total_invested"], v3["total_invested"]),
        ("roi %",          v2["roi_pct"],        v3["roi_pct"]),
    ]
    for label, a, b in rows:
        delta = b - a
        print(f"{label:<24} {a:>20,.4f} {b:>20,.4f} {delta:>+10,.4f}")
    print("=" * 78)

    # 逐资产展开
    print()
    print("Per-asset 详细:")
    for i, name in enumerate([a[0] for a in ASSETS]):
        print(f"\n  {name}:")
        for r in results:
            cycles = r["per_asset"][i]["cycles"]
            m = grid_metrics(cycles)
            print(f"    {r['tp_basis']:<10}  trades={m['n_trades']:>3}  "
                  f"win={m['win_rate']:>5.0%}  hold={m['avg_hold_days']:>4.0f}d  "
                  f"avg_ret={m['avg_return_pct']:>+5.1f}%  "
                  f"pnl=¥{m['total_pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
