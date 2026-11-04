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
  - Per day: 按 `trigger_rule` 判断是否触发 →
      "max_dev"           : 任一 |weight - 1/n| > THRESHOLD_PP
      "max_dev_or_spread" : 上面那条 **或** 正负偏离的极差 > THRESHOLD_PP
  - 触发后按 `rebalance_mode` 回正 →
      "snap_all" : 全部拉回 1/n (生产口径)
      "pair"     : 只把最正偏 / 最反偏那一对拉平
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
#: 默认阈值 —— 与生产路径共用**同一个**领域常量 (Phase 5)。
#: 以前这里是本模块自己读环境变量; 现在环境变量的覆盖点收敛到应用层
#: (`AppConfig.from_env`), 回测想要别的阈值就**显式传参**。
THRESHOLD_PP = RebalancePolicy().threshold_pp

#: 触发口径 —— 「什么算偏离过大」只有这两条, 别再抄第三份。
#:
#:   "max_dev"            任一资产的 |w − 1/n| 超过阈值就触发。**生产口径**
#:                        (与 `RebalancePolicy.evaluate` 一致)。
#:   "max_dev_or_spread"  上面那条 **加上**: 正负偏离的**极差**
#:                        (max Δ − min Δ, 即「最正偏的」跟「最反偏的」差多少)
#:                        超过阈值也触发。捕捉的是「一只偏 +2pp、另一只偏 −2pp」
#:                        这种单看谁都没到 3pp、但篮子已经明显失衡的情形。
TRIGGER_RULES = ("max_dev", "max_dev_or_spread")

#: 回正动作 —— 触发之后动哪些仓位。
#:
#:   "snap_all"  全部资产拉回 1/n, 现金清零。**生产口径**
#:   "pair"      只把「最正偏」和「最反偏」那一对拉平 (这一对的总额不变,
#:               钱在两者之间挪), 其余持仓一动不动。
REBALANCE_MODES = ("snap_all", "pair")

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
                 threshold_pp: float = THRESHOLD_PP,
                 trigger_rule: str = "max_dev",
                 rebalance_mode: str = "snap_all") -> dict:
    """Full backtest of the equal-weight rebalance strategy on the given price DataFrame.

    Args:
        prices: DataFrame of aligned closes (any column names), indexed by date.
        initial_cash: starting capital ¥.
        target_w: target weight per asset. Default = 1/len(prices.columns).
                  Pass an explicit value to override (e.g. 0.333 for 3-asset
                  strict equal-weight, 0.4 for a tilted 2-of-5 backtest, etc.).
        threshold_pp: 偏离多少个百分点才触发回正。默认 3。
        trigger_rule: 触发口径 (见 `TRIGGER_RULES`)。默认 "max_dev" = 生产口径
                      (任一资产偏离超阈值就触发)。
        rebalance_mode: 回正动作 (见 `REBALANCE_MODES`)。默认 "snap_all"
                      = 整篮子拉回 1/n。

    Returns:
        Dict with series, rebalances, final, cagr, max_dd, sharpe, n_rebalances,
        turnover.
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
    turnover = 0.0

    for d, row in prices.iterrows():
        positions_value = sum(shares[n] * row[n] for n in asset_names)
        total = positions_value + cash
        weights = {n: shares[n] * row[n] / total if total > 0 else 0.0
                   for n in asset_names}

        # 偏离 Δ (百分点): Δ = (w − 1/n) × 100
        devs = {n: (weights[n] - target_w) * 100 for n in asset_names}
        max_dev = max(abs(v) for v in devs.values())
        hi = max(asset_names, key=lambda n: devs[n])   # 最正偏的那只
        lo = min(asset_names, key=lambda n: devs[n])   # 最反偏的那只
        spread = devs[hi] - devs[lo]                  # 正负偏离的**极差**
        max_asset = max(asset_names, key=lambda n: abs(devs[n]))
        triggered = False
        if total > 0:
            if trigger_rule == "max_dev":
                triggered = max_dev > threshold_pp
            elif trigger_rule == "max_dev_or_spread":
                # 「也触发」: 任一偏离超阈值 —— **或者** 正负偏离拉得太开。
                # 后者捕捉的是「一只偏 +2pp、另一只偏 −2pp」这种
                # 单看谁都没到 3pp、但篮子其实已经明显失衡的情形。
                triggered = (max_dev > threshold_pp
                             or spread > threshold_pp)
            else:
                raise ValueError(f"unknown trigger_rule {trigger_rule!r}")

        if triggered:
            w_before = dict(weights)
            values_before = {n: shares[n] * row[n] for n in asset_names}
            traded = 0.0
            if (rebalance_mode == "pair" and cash <= 0 and hi != lo):
                # 只把「最正偏」和「最反偏」那一对拉平 —— 这一对的**总额不变**
                # (钱在两者之间挪), 其余持仓一动不动。
                pair_value = values_before[hi] + values_before[lo]
                if pair_value > 0:
                    half = pair_value / 2.0
                    shares[hi] = half / row[hi]
                    shares[lo] = half / row[lo]
                    traded = (abs(half - values_before[hi])
                              + abs(half - values_before[lo]))
                else:
                    for n in asset_names:
                        shares[n] = total * target_w / row[n]
                    cash = 0.0
                    traded = sum(abs(total * target_w - values_before[n])
                                 for n in asset_names)
            else:
                # 整篮子回正: 每只都拉回 1/n, 现金清零
                for n in asset_names:
                    shares[n] = total * target_w / row[n]
                cash = 0.0
                traded = sum(abs(total * target_w - values_before[n])
                             for n in asset_names)
            turnover += traded
            rebalances.append({
                "date": d.date().isoformat() if hasattr(d, "date") else str(d),
                "reason": f"{max_asset} 偏离 {max_dev:.2f}pp",
                "rule": trigger_rule,
                "mode": rebalance_mode,
                "max_dev": max_dev,
                "spread": spread,
                "hi": hi,
                "lo": lo,
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
        "turnover": turnover,
        "series": series,
    }
