"""3-asset equal-weight rebalance engine (port from hongli verify_3pp_strategy).

Strategy:
  - Day 0: 全部 initial_cash 现金
  - Assets: 3 ETFs (黄金/红利/可转债) at 1/3 each
  - Per day: if any |weight - 1/3| > THRESHOLD_PP → snap ALL back to 1/3
  - Else: do nothing (let weights drift)

This is a SIMPLIFIED, full-snap-back variant of run_strategy() in
backtest_equal_weight_rebalance. The Action uses this for two purposes:
  1. evaluate() on today's single row → trigger / plan
  2. full backtest for chart rendering (called by chart.py)

Inputs:
  prices: DataFrame of 3 ETF closes, aligned (inner join, dropna)
  initial_cash: starting ¥ (default 200,000 to match the v1 history)

Outputs:
  {"series": pd.Series, "rebalances": [...], "final": float, "cagr": float,
   "max_dd": float, "sharpe": float, "n_rebalances": int}
"""
from __future__ import annotations

import os
from typing import Tuple

import pandas as pd

CASH = 200_000.0
TARGET_W = 1.0 / 3
THRESHOLD_PP = float(os.environ.get("THRESHOLD_PP", "3.0"))

ASSETS = [
    ("黄金ETF博时", "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
    ("纳斯达克ETF华夏", "sh513300", "513300"),    # 2020-11-05 上市
]
ASSET_NAMES = [n for n, _, _ in ASSETS]


def _max_drawdown(series: pd.Series) -> float:
    peak = series.cummax()
    return float(((series - peak) / peak).min())


def _sharpe(series: pd.Series) -> float:
    rets = series.pct_change().dropna()
    if rets.std() == 0 or len(rets) < 2:
        return 0.0
    return float(rets.mean() / rets.std() * (252 ** 0.5))


def run_backtest(prices: pd.DataFrame, initial_cash: float = CASH,
                 target_w: float | None = None) -> dict:
    """Full backtest of the equal-weight rebalance strategy on the given price DataFrame.

    Args:
        prices: DataFrame of aligned closes (any column names), indexed by date.
        initial_cash: starting capital ¥.
        target_w: target weight per asset. Default = 1/len(prices.columns).
                  Pass an explicit value (e.g. 0.25 for 4-asset, ±3pp tolerance)
                  to override.

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
                if dev > THRESHOLD_PP:
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


def evaluate_today(positions: list[dict], cash: float) -> Tuple[float, list[float], bool, list[dict]]:
    """One-shot rebalance check for today's snapshot (used by record.py).

    Args:
        positions: list of {"name", "code", "shares", "price", "value", ...}
        cash: total cash balance (¥).

    Returns:
        (total, weights, triggered, plan)
        plan = [{"name", "code", "current_value", "target_value", "delta_value"}, ...]
    """
    total = sum(p["value"] for p in positions) + cash
    n = len(positions)
    target_w = 1.0 / n if n > 0 else 0.0
    weights = [p["value"] / total if total > 0 else 0.0 for p in positions]
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
