"""4 资产回测对比: 3 ETF 基准 vs +513500 vs +513300 vs 两个都加.

统一 START = 2020-11-05 (513300 纳斯达克上市日, 保证 4 配置数据窗口一致)
阈值保持 ±3pp; 等权; 网格 v3 (first_buy)

输出综合对比表: 3pp + 网格 PnL + CAGR + Sharpe + MaxDD

WARNING (2026-10-23 起): 网格出场规则已改为「只卖成本金额」(见
src/domain/grid/grid_position.py:split_take_profit) —— 正常出场时已实现盈亏恒为 0, 浮盈转成
永久持有的零成本份额。所以本脚本的 "网格 PnL" (取自 cum_realized) 会
恒为 0, 输出不再代表网格的真实收益。要看真实收益用
scripts/compare_grid_exit_mode.py。
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
# 3 资产基础 (BASE_3) — 本地常量, 不引用 hongli/
BASE_3 = [
    ("黄金ETF博时",   "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
]

START = "2020-11-05"     # 513300 上市日 (公平起点)
END = "2026-10-09"
INITIAL_CAP = 200_000.0

# 候选第 4 / 第 5 资产
EXTRAS = {
    "513500_标普500ETF博时": ("sh513500", "513500"),
    "513300_纳斯达克ETF华夏": ("sh513300", "513300"),
}

# 颜色 (4 资产用, 5 资产自动分配)
EXTRA_COLORS = {
    "513500_标普500ETF博时": "#5DADE2",     # 蓝
    "513300_纳斯达克ETF华夏": "#48C9B0",     # 青
}


def fetch_all_prices() -> dict[str, pd.DataFrame]:
    out = {}
    for name, sina, em in BASE_3:
        df = fetch_one(sina, em, START, END)
        out[name] = df["close"]
    for name, (sina, em) in EXTRAS.items():
        df = fetch_one(sina, em, START, END)
        out[name] = df["close"]
    return out


def build_universe(prices: dict, members: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({n: prices[n] for n in members}).sort_index().dropna()
    return df


def run_one(prices_df: pd.DataFrame, label: str) -> dict:
    n = len(prices_df.columns)
    print(f"  {label} ({n} 资产, {len(prices_df)} 行)...")

    # 3pp
    pp = three_pp.run_backtest(prices_df, initial_cash=INITIAL_CAP)

    # 网格 v3
    per_asset = []
    for col in prices_df.columns:
        s = prices_df[col]
        r = grid_mod.run_compound_grid(s, pos_yuan=grid_mod.GRID_BASE_YUAN)
        per_asset.append({
            "name": col,
            "cycles": r["cycles"],
            "cum_realized": r["cum_realized"],
        })
    total_pnl = sum(p["cum_realized"] for p in per_asset)
    total_inv = sum(c["total_invested"] for p in per_asset for c in p["cycles"])
    n_trades = sum(len(p["cycles"]) for p in per_asset)
    wins = sum(sum(1 for c in p["cycles"] if c.get("pnl", 0) > 0) for p in per_asset)

    combined_final = pp["final"] + total_pnl
    years = pp["years"]
    combined_cagr = (combined_final / INITIAL_CAP) ** (1 / years) - 1 if years > 0 else 0.0
    return {
        "label": label,
        "n_assets": n,
        "members": list(prices_df.columns),
        "pp": pp,
        "grid_pnl": total_pnl,
        "grid_invested": total_inv,
        "grid_n_trades": n_trades,
        "grid_win_rate": wins / n_trades if n_trades else 0.0,
        "combined_final": combined_final,
        "combined_cagr": combined_cagr,
    }


def main() -> None:
    print(f"拉取 {len(BASE_3) + len(EXTRAS)} 只 ETF ({START} → {END})...")
    prices = fetch_all_prices()
    for n, df in prices.items():
        print(f"  {n:<28} {len(df)} 行  {df.index[0].date()} → {df.index[-1].date()}")

    base_3 = [n for n, _, _ in BASE_3]
    configs = [
        ("3 资产 (基准)", base_3),
        ("+ 513500 标普500", base_3 + ["513500_标普500ETF博时"]),
        ("+ 513300 纳斯达克", base_3 + ["513300_纳斯达克ETF华夏"]),
        ("+ 513500 + 513300", base_3 + ["513500_标普500ETF博时", "513300_纳斯达克ETF华夏"]),
    ]

    print("\n回测各配置 (3pp + 网格 v3)...")
    results = []
    for label, members in configs:
        df = build_universe(prices, members)
        results.append(run_one(df, label))

    # 3pp 单独表
    print()
    print("=" * 100)
    print(f"{'config':<28} {'n':>3} {'final Y':>14} {'CAGR %':>8} {'Sharpe':>7} {'MaxDD %':>9} {'n_reb':>5}")
    print("-" * 100)
    for r in results:
        pp = r["pp"]
        print(f"{r['label']:<28} {r['n_assets']:>3} "
              f"{pp['final']:>14,.0f} {pp['cagr']*100:>7.2f} "
              f"{pp['sharpe']:>7.2f} {pp['max_dd']*100:>8.2f} "
              f"{pp['n_rebalances']:>5}")
    print("=" * 100)

    # 综合 (3pp + 网格)
    print()
    print("=" * 110)
    print(f"{'config':<28} {'3pp_final':>12} {'grid_pnl':>12} {'combined':>14} {'CAGR %':>8} "
          f"{'n_trades':>9} {'win':>5} {'grid_roi %':>10}")
    print("-" * 110)
    for r in results:
        print(f"{r['label']:<28} "
              f"{r['pp']['final']:>12,.0f} {r['grid_pnl']:>+12,.0f} "
              f"{r['combined_final']:>14,.0f} {r['combined_cagr']*100:>7.2f} "
              f"{r['grid_n_trades']:>9} {r['grid_win_rate']*100:>4.0f}% "
              f"{(r['grid_pnl']/r['grid_invested']*100 if r['grid_invested'] else 0):>9.2f}")
    print("=" * 110)

    # Δ vs 基准
    base = results[0]
    print()
    print("Δ vs 3 资产基准 (CAGR / MaxDD / grid PnL):")
    for r in results[1:]:
        print(f"  {r['label']:<28}  "
              f"ΔCAGR = {(r['combined_cagr'] - base['combined_cagr']) * 100:+.2f}pp  "
              f"ΔMaxDD = {(r['pp']['max_dd'] - base['pp']['max_dd']) * 100:+.2f}pp  "
              f"Δgrid_PnL = Y{r['grid_pnl'] - base['grid_pnl']:+,.0f}")


if __name__ == "__main__":
    main()
