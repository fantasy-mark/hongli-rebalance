"""Full strategy comparison: 3pp-only vs 3pp+grid(v2) vs 3pp+grid(v3).

Combines:
  - 3pp rebalance CAGR/Sharpe/MaxDD (from src.simulation.equal_weight_backtest.run_backtest)
  - Grid v2 (tp_basis=avg_cost)  cycles + realized PnL
  - Grid v3 (tp_basis=first_buy) cycles + realized PnL

Composite metric:
  - Combined CAGR uses 3pp capital as base, grid PnL added on top
    (consistent with plot_strategy_with_grid.py normalization).
  - Combined MaxDD = worse of 3pp MaxDD and (3pp trough + grid loss period)
    → approximated as max(3pp MaxDD, grid drawdown from invested peak).

WARNING (2026-10-23 起): 网格出场规则已改为「只卖成本金额」(见
src/domain/grid/exit_policy.py:cost_only_exit) —— 正常出场时已实现盈亏恒为 0, 浮盈转成
永久持有的零成本份额。所以本脚本的 grid PnL (取自 cum_realized) 会恒为
0, 输出不再代表网格的真实收益。要看真实收益用
scripts/compare_grid_exit_mode.py。

Run:
    python scripts/compare_full_strategies.py
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
from src.simulation import equal_weight_backtest as three_pp
from src.infrastructure.quote_providers import fetch_one
# 3 资产 (跟 v3 一致)
ASSETS = [
    ("黄金ETF博时",   "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
]
START = "2020-01-01"
END = "2026-10-09"
INITIAL_CAP = 200_000.0


def fetch_three_etf() -> pd.DataFrame:
    cols = {}
    for name, sina, em in ASSETS:
        df = fetch_one(sina, em, START, END)
        cols[name] = df["close"]
    out = pd.DataFrame(cols).sort_index().dropna()
    return out


def grid_summary(prices: pd.DataFrame, tp_basis: str) -> dict:
    """Run grid on each column; return aggregate metrics."""
    per_asset = {}
    for name in prices.columns:
        s = prices[name]
        r = grid_mod.run_compound_grid(s, tp_basis=tp_basis, pos_yuan=grid_mod.GRID_BASE_YUAN)
        per_asset[name] = r
    n = sum(len(r["cycles"]) for r in per_asset.values())
    wins = sum(sum(1 for c in r["cycles"] if c.get("pnl", 0) > 0) for r in per_asset.values())
    total_pnl = sum(r["cum_realized"] for r in per_asset.values())
    total_inv = sum(c["total_invested"] for r in per_asset.values() for c in r["cycles"])
    # 网格日度累计已实现 PnL → 折算成 pp (相对于初始 3pp 资本 200k)
    pp_index = total_pnl / INITIAL_CAP * 100
    return dict(
        tp_basis=tp_basis,
        n_trades=n,
        win_rate=wins / n if n else 0.0,
        total_pnl=total_pnl,
        total_invested=total_inv,
        roi_pct=total_pnl / total_inv * 100 if total_inv else 0.0,
        grid_pp=pp_index,
        per_asset=per_asset,
    )


def combined_metrics(three_pp_res: dict, grid: dict) -> dict:
    """Combine 3pp + grid into a single 'strategy' snapshot.

    3pp 单独:  final=3pp_final, MaxDD=3pp_max_dd
    3pp + grid: 累计 + grid.total_pnl
    3pp + grid PP: (3pp_final + total_pnl) / initial - 1
    """
    pp_final = three_pp_res["final"]
    pp_max_dd = three_pp_res["max_dd"]
    pp_cagr = three_pp_res["cagr"]
    pp_sharpe = three_pp_res["sharpe"]
    years = three_pp_res["years"]
    combined_final = pp_final + grid["total_pnl"]
    # 把网格作为额外收益加入 → 综合 CAGR
    combined_cagr = (combined_final / INITIAL_CAP) ** (1 / years) - 1 if years > 0 else 0.0
    return dict(
        final=combined_final,
        cagr=combined_cagr,
        max_dd_pp=pp_max_dd,                # 3pp MaxDD (主要回撤来源)
        max_dd_pct=pp_max_dd * 100,
        grid_pnl=grid["total_pnl"],
        grid_pp=grid["grid_pp"],
    )


def main() -> None:
    print(f"拉取 3 ETF 历史 ({START} → {END})...")
    prices = fetch_three_etf()
    print(f"  对齐后 {len(prices)} 行, 区间 "
          f"{prices.index[0].date()} → {prices.index[-1].date()}")

    # 3pp 单独
    print("\n回测 3pp-only...")
    pp = three_pp.run_backtest(prices, initial_cash=INITIAL_CAP)
    print(f"  final=¥{pp['final']:,.0f}  cagr={pp['cagr']*100:>5.2f}%  "
          f"sharpe={pp['sharpe']:>5.2f}  max_dd={pp['max_dd']*100:>5.2f}%  "
          f"n_reb={pp['n_rebalances']}")

    # 两个 grid 变体
    grids = {}
    for basis in ("avg_cost", "first_buy"):
        print(f"\n回测 grid (tp_basis={basis})...")
        g = grid_summary(prices, basis)
        grids[basis] = g
        print(f"  n_trades={g['n_trades']:>3}  win={g['win_rate']:>5.0%}  "
              f"pnl=¥{g['total_pnl']:>+10,.0f}  roi={g['roi_pct']:>+5.2f}%  "
              f"grid_pp={g['grid_pp']:>+5.2f}")

    # 综合
    print("\n" + "=" * 80)
    print(f"{'metric':<22} {'3pp only':>18} {'+ grid v2 (avg_cost)':>22} {'+ grid v3 (first_buy)':>22}")
    print("-" * 80)
    combined = {b: combined_metrics(pp, g) for b, g in grids.items()}

    rows = [
        ("final ¥",          pp["final"],               combined["avg_cost"]["final"],       combined["first_buy"]["final"]),
        ("CAGR %",           pp["cagr"] * 100,          combined["avg_cost"]["cagr"] * 100,  combined["first_buy"]["cagr"] * 100),
        ("Sharpe",           pp["sharpe"],              pp["sharpe"],                        pp["sharpe"]),  # grid 是额外 PnL, 风险/收益属性同 3pp
        ("3pp MaxDD %",      pp["max_dd"] * 100,        pp["max_dd"] * 100,                  pp["max_dd"] * 100),
        ("grid PnL ¥",       0.0,                       combined["avg_cost"]["grid_pnl"],    combined["first_buy"]["grid_pnl"]),
        ("grid pp 指数",     0.0,                       combined["avg_cost"]["grid_pp"],     combined["first_buy"]["grid_pp"]),
        ("grid trades",      0,                         grids["avg_cost"]["n_trades"],       grids["first_buy"]["n_trades"]),
        ("grid win_rate",    0.0,                       grids["avg_cost"]["win_rate"],       grids["first_buy"]["win_rate"]),
    ]
    for label, a, b, c in rows:
        print(f"{label:<22} {a:>18,.4f} {b:>22,.4f} {c:>22,.4f}")
    print("=" * 80)

    # 网格对比摘要
    print("\n网格 Δ (v3 vs v2):")
    g2, g3 = grids["avg_cost"], grids["first_buy"]
    print(f"  PnL   : ¥{g3['total_pnl'] - g2['total_pnl']:+,.0f}  "
          f"({(g3['total_pnl'] / g2['total_pnl'] - 1) * 100:+.1f}%)")
    print(f"  pp 指数: {g3['grid_pp'] - g2['grid_pp']:+.2f}  →  "
          f"等效年化 +{(g3['grid_pp'] - g2['grid_pp']) / pp['years'] / 100 * 100:.3f}% CAGR (相对 200k 本金)")


if __name__ == "__main__":
    main()
