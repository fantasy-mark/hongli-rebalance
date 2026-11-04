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
      "spread"            : 只看资产之间的极差 (max Δ − min Δ) > 阈值
      "max_dev_or_spread" : 上面第一条 **或** 极差 > 阈值
  - 触发后按 `rebalance_mode` 回正 →
      "snap_all" : 全部拉回 1/n (生产口径)
      "pair"     : 只把最正偏 / 最反偏那一对拉平
  - Else: do nothing (let weights drift)

⚠️ **极差 (spread) 与最大偏离 (max_dev) 的代数关系** —— 别把它俩当两个独立的旋钮:

    偏离 Δ_i = (w_i − 1/n) × 100。**满仓时 Σw = 1 ⇒ ΣΔ = 0**
    设 M = max|Δ|, S = 极差 = hi − lo, 资产数 n:
        M = hi 时其余 n−1 只之和 = −hi ⇒ lo ≤ −hi/(n−1) ⇒ S ≥ M·n/(n−1)
        M = −lo 时对称 ⇒ 同样 S ≥ M·n/(n−1)
        反向: hi ≥ S/2 或 −lo ≥ S/2 ⇒ M ≥ S/2 ⇒ S ≤ 2M
    ⇒ **M·n/(n−1) ≤ S ≤ 2M**     (n=4 ⇒ 1.333·M ≤ S ≤ 2M)

    (唯一的例外是**建仓日**: 全现金 ⇒ 权重全是 0 ⇒ M = 100/n 而 S = 0。)

所以 (n = 4):
    "max_dev"@3  ⇒  S > 4.0   —— **生产口径的 3pp 其实就是极差口径的 4pp**
    "max_dev"@T ∪ "spread"@X = {S > T·n/(n−1)} ∪ {S > X}
    "max_dev_or_spread"@T ≡ "spread"@T

⇒ **「保留 3pp 的 max_dev, 只把极差门槛调高」并不会让触发变少**
   (max_dev>3 的日子必然满足 spread>4, 那条分支一直在响)。
   想让触发**变少**, 只能把 max_dev 那条也去掉 (= 纯 "spread" 规则)。

⚠️ **现金对两条规则的影响不一样** (权重分母含真实现金):
    max_dev 会被现金**整体压低** (所有 Δ 一起下移), 极差则**完全不受影响**
    (同时加一个常数不改变 hi − lo)。⇒ 极差口径更稳。

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

#: 触发口径 —— 「什么算偏离过大」只有这三条, 别再抄第四份。
#:
#:   "max_dev"            任一资产的 |w − 1/n| 超过阈值就触发。**生产口径**
#:                        (与 `RebalancePolicy.evaluate` 一致)。
#:   "spread"             **只看极差**: max Δ − min Δ (「最正偏的」跟
#:                        「最反偏的」差多少) 超过阈值就触发。
#:                        捕捉的是「一只偏 +2pp、另一只偏 −2pp」这种单看谁
#:                        都没到 3pp、但篮子已经明显失衡的情形。
#:   "max_dev_or_spread"  上面两条的**并集**。
#:
#: ⚠️ 满仓时 ΣΔ = 0 ⇒ **M·n/(n−1) ≤ spread ≤ 2M** ⇒
#:    "max_dev"@T ⊆ {spread > T·n/(n−1)} (n=4 时 3pp ⇒ 4pp),
#:    且 "max_dev_or_spread"@T ≡ "spread"@T。
#:    (代数证明见模块 docstring) —— 想让触发变少, 只能用纯 "spread"。
TRIGGER_RULES = ("max_dev", "spread", "max_dev_or_spread")

#: 交易成本 —— ETF 买卖**免印花税**, 只有佣金。
#: 默认 0 = 不计成本 (跟历史口径一致); 研究脚本要显式传参。
#:
#:   fee_bps          每边费率 (万分之几)。0.03% = 3bp = 万分之三
#:   min_commission   单笔最低佣金 (券商常见 ¥5)
DEFAULT_FEE_BPS = 0.0
DEFAULT_MIN_COMMISSION = 0.0

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


def _commission(value: float, fee_bps: float, min_commission: float) -> float:
    """单边佣金 —— ETF 免印花税, 只有佣金, 有最低收费。

    费率按**成交额**算, 但券商的最低佣金是硬下限: 一笔 ¥3,000 的成交按
    3bp 只有 ¥0.9, 实际收 ¥5。调仓笔数一多, 最低佣金才是主要成本。
    """
    if value <= 0 or (fee_bps <= 0 and min_commission <= 0):
        return 0.0
    return max(min_commission, abs(value) * fee_bps / 10000.0)


def run_backtest(prices: pd.DataFrame, initial_cash: float = CASH,
                 target_w: float | None = None,
                 threshold_pp: float = THRESHOLD_PP,
                 trigger_rule: str = "max_dev",
                 rebalance_mode: str = "snap_all",
                 spread_pp: float | None = None,
                 fee_bps: float = DEFAULT_FEE_BPS,
                 min_commission: float = DEFAULT_MIN_COMMISSION) -> dict:
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
        spread_pp: **极差分支**的独立阈值。默认 None = 跟 `threshold_pp` 走。
                   ⚠️ 因为 spread ≥ max_dev 恒成立, 把这个值调**高**而
                   `threshold_pp` 不动, 并集**不会变** (见模块 docstring) ——
                   想让触发变少只能用 trigger_rule="spread"。
        fee_bps: 每边佣金费率 (万分之几)。默认 0 = 不计成本。
        min_commission: 单笔最低佣金 ¥。默认 0 = 不计成本。

    Returns:
        Dict with series, rebalances, final, cagr, max_dd, sharpe, n_rebalances,
        turnover, fees.
    """
    asset_names = list(prices.columns)
    if not asset_names:
        raise ValueError("prices must have at least one column")
    if target_w is None:
        target_w = 1.0 / len(asset_names)
    spread_threshold = threshold_pp if spread_pp is None else float(spread_pp)

    shares = {n: 0.0 for n in asset_names}
    cash = float(initial_cash)
    daily_value: list[float] = []
    rebalances: list[dict] = []
    turnover = 0.0
    fees = 0.0

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
            cash_share = cash / total
            if trigger_rule == "max_dev":
                triggered = max_dev > threshold_pp
            elif trigger_rule == "spread":
                # **纯极差**: 只看资产之间拉得多开。
                # ⚠️ 空仓特例: 全现金状态下所有权重都是 0, 极差 = 0 ⇒ 永远
                # 不触发 ⇒ 钱永远铺不出去。所以「现金过半」时先建仓
                # (建完现金清零, 不会再触发这条特例)。
                triggered = (spread > spread_threshold) or (cash_share > 0.5)
            elif trigger_rule == "max_dev_or_spread":
                # 「也触发」: 任一偏离超阈值 —— **或者** 正负偏离拉得太开。
                # 后者捕捉的是「一只偏 +2pp、另一只偏 −2pp」这种
                # 单看谁都没到 3pp、但篮子其实已经明显失衡的情形。
                triggered = (max_dev > threshold_pp
                             or spread > spread_threshold)
            else:
                raise ValueError(f"unknown trigger_rule {trigger_rule!r}")

        if triggered:
            w_before = dict(weights)
            values_before = {n: shares[n] * row[n] for n in asset_names}
            legs: list[dict] = []
            traded = 0.0
            if (rebalance_mode == "pair" and cash <= 0 and hi != lo):
                # 只把「最正偏」和「最反偏」那一对拉平 —— 这一对的**总额不变**
                # (钱在两者之间挪), 其余持仓一动不动。
                pair_value = values_before[hi] + values_before[lo]
                if pair_value > 0:
                    half = pair_value / 2.0
                    legs = [
                        {"name": hi, "side": "sell",
                         "value": abs(half - values_before[hi])},
                        {"name": lo, "side": "buy",
                         "value": abs(half - values_before[lo])},
                    ]
                    # 佣金从这一对里扣 (先按无佣金的 leg 算成交额, 再扣)
                    fee = sum(_commission(l["value"], fee_bps, min_commission)
                              for l in legs)
                    half = (pair_value - fee) / 2.0
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
                target_value = total * target_w
                for n in asset_names:
                    delta = target_value - values_before[n]
                    if abs(delta) > 1e-9:
                        legs.append({"name": n,
                                     "side": "buy" if delta > 0 else "sell",
                                     "value": abs(delta)})
                # 佣金**从组合里扣** (ETF 免印花税): 拿 total − fee 建仓,
                # 这样费用真实地拖慢复利, 而不是事后从净值里减。
                fee = sum(_commission(l["value"], fee_bps, min_commission)
                          for l in legs)
                investable = total - fee
                for n in asset_names:
                    shares[n] = investable * target_w / row[n]
                cash = 0.0
                traded = sum(abs(total * target_w - values_before[n])
                             for n in asset_names)
            for l in legs:
                l["fee"] = _commission(l["value"], fee_bps, min_commission)
            fees += sum(l["fee"] for l in legs)
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
                "legs": legs,
                "fee": sum(l["fee"] for l in legs),
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
        "fees": fees,
        "series": series,
    }
