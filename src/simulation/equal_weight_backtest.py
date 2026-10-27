"""3-asset equal-weight rebalance engine (port from hongli verify_3pp_strategy).

Note: despite the filename "three_pp", this engine is **price-agnostic**
about the asset count — `run_backtest` operates on whatever columns are
in the input DataFrame. The production rebalance path in record.py:evaluate
uses 3 assets (黄金/红利/可转债, see app.js:REBALANCE_CODES) and 纳指
(513300) is grid-only. The 4-asset variant is used by research scripts
(analyze_4_assets.py, compare_4pp_strategies.py) for backtest comparison.

Strategy:
  - Day 0: 全部 initial_cash 现金
  - Assets: arbitrary (1/n target weight per column in `prices`)
  - Per day: if any |weight - 1/n| > THRESHOLD_PP → snap ALL back to 1/n
  - Else: do nothing (let weights drift)

This is a SIMPLIFIED, full-snap-back variant of run_strategy() in
backtest_equal_weight_rebalance. The Action uses this for two purposes:
  1. evaluate() on today's single row → trigger / plan
  2. full backtest for chart rendering (called by chart.py)

Inputs:
  prices: DataFrame of ETF closes, aligned (inner join, dropna)
  initial_cash: starting ¥ (default 200,000 to match the v1 history)

Outputs:
  {"series": pd.Series, "rebalances": [...], "final": float, "cagr": float,
   "max_dd": float, "sharpe": float, "n_rebalances": int}
"""
from __future__ import annotations

import pandas as pd

from ..domain.portfolio.rebalance_policy import RebalancePolicy

CASH = 200_000.0
TARGET_W = 1.0 / 3
#: 默认阈值 —— 与生产路径共用**同一个**领域常量 (Phase 5)。
#: 以前这里是本模块自己读环境变量; 现在环境变量的覆盖点收敛到应用层
#: (`AppConfig.from_env`), 回测想要别的阈值就**显式传参**。
THRESHOLD_PP = RebalancePolicy().threshold_pp

# Registry of all 4 ETFs the project tracks (3 rebalance + 1 grid-only QDII).
# Not used by `run_backtest` itself (it takes whatever columns the caller
# passes); kept here as a single source of truth for research scripts that
# need a known code/name list.
ASSETS = [
    ("黄金ETF博时", "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
    ("纳斯达克ETF华夏", "sh513300", "513300"),    # 2020-11-05 上市, grid-only
]


def _max_drawdown(series: pd.Series) -> float:
    peak = series.cummax()
    return float(((series - peak) / peak).min())


def _sharpe(series: pd.Series) -> float:
    rets = series.pct_change().dropna()
    if rets.std() == 0 or len(rets) < 2:
        return 0.0
    return float(rets.mean() / rets.std() * (252 ** 0.5))


def run_backtest(prices: pd.DataFrame, initial_cash: float = CASH,
                 target_w: float | None = None,
                 threshold_pp: float = THRESHOLD_PP) -> dict:
    """Full backtest of the equal-weight rebalance strategy on the given price DataFrame.

    Args:
        prices: DataFrame of aligned closes (any column names), indexed by date.
        initial_cash: starting capital ¥.
        target_w: target weight per asset. Default = 1/len(prices.columns).
                  Pass an explicit value to override (e.g. 0.333 for 3-asset
                  strict equal-weight, 0.4 for a tilted 2-of-5 backtest, etc.).
        threshold_pp: 偏离多少个百分点才触发回正。默认 3。

    Returns:
        Dict with series, rebalances, final, cagr, max_dd, sharpe, n_rebalances.
    """
    asset_names = list(prices.columns)
    if not asset_names:
        raise ValueError("prices must have at least one column")
    if target_w is None:
        target_w = 1.0 / len(asset_names)

    shares = {n: 0.0 for n in asset_names}
    cash = float(initial_cash)
    daily_value: list[float] = []
    rebalances: list[dict] = []

    for d, row in prices.iterrows():
        positions_value = sum(shares[n] * row[n] for n in asset_names)
        total = positions_value + cash
        weights = {n: shares[n] * row[n] / total if total > 0 else 0.0
                   for n in asset_names}

        max_dev = 0.0
        max_asset = None
        triggered = False
        if total > 0:
            for n in asset_names:
                dev = abs(weights[n] - target_w) * 100
                if dev > max_dev:
                    max_dev = dev
                    max_asset = n
                if dev > threshold_pp:
                    triggered = True

        if triggered:
            w_before = dict(weights)
            for n in asset_names:
                shares[n] = total * target_w / row[n]
            cash = 0.0
            rebalances.append({
                "date": d.date().isoformat() if hasattr(d, "date") else str(d),
                "reason": f"{max_asset} 偏离 {max_dev:.2f}pp",
                "weights_before": w_before,
                "total": total,
                "prices": {n: float(row[n]) for n in asset_names},
            })

        positions_eod = sum(shares[n] * row[n] for n in asset_names)
        daily_value.append(positions_eod + cash)

    series = pd.Series(daily_value, index=prices.index)
    final = float(series.iloc[-1])
    years = (series.index[-1] - series.index[0]).days / 365.25
    cagr = (final / initial_cash) ** (1 / years) - 1 if years > 0 else 0.0
    return {
        "final": final,
        "cagr": cagr,
        "years": years,
        "max_dd": _max_drawdown(series),
        "sharpe": _sharpe(series),
        "n_rebalances": len(rebalances),
        "rebalances": rebalances,
        "series": series,
    }
