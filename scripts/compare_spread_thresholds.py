"""把**极差阈值调大** (3.5~5pp) 回测 —— 检验「触发少 → 费用少 → 收益改善」。

用户的问题 (2026-11-04):
    「我想调大极差 (比如 3.5~5pp) 回测, 减小交易费用影响, 因为触发频率高了,
      理论回测收益应该会改善」

这个脚本回答三件事:

1. **怎么「调大极差」才真的能减少触发?**
   先看代数。偏离 Δ_i = (w_i − 1/n) × 100。**满仓时 Σw = 1 ⇒ ΣΔ = 0**。
   设 M = max|Δ|, S = 极差 = hi − lo, 资产数 n:
       若 M = hi (一只超配): 其余 n−1 只之和 = −hi ⇒ lo ≤ −hi/(n−1)
           ⇒ S ≥ hi·n/(n−1)
       若 M = −lo: 对称 ⇒ 同样 S ≥ M·n/(n−1)
       反向: hi ≥ S/2 或 −lo ≥ S/2 ⇒ M ≥ S/2
       ⇒ **M·n/(n−1) ≤ S ≤ 2M**
   n = 4 时:
       **"max_dev > 3pp" ⇒ S > 4.0pp** —— 生产口径的 3pp 等价于
       **极差口径的 4pp**(严格说是 {max_dev>3} ⊂ {spread>4})。
       反过来 "spread > 3.5pp" ⇒ M > 1.75pp —— 远比 3pp 的 max_dev 敏感。
   所以 "max_dev"@T ∪ "spread"@X = {S > T·n/(n−1)} ∪ {S > X}
   (n=4, T=3 ⇒ {S > 4} ∪ {S > X})。
   ⇒ 真正能让触发变少的旋钮 = **纯极差规则** ("spread", 丢掉 max_dev 那条)。
   (本脚本 C 节实证这些关系, 不靠嘴说。)
   ⚠️ 唯一的例外是**建仓日**: 全现金时所有权重都是 0 ⇒ Δ 全是 −1/n,
   max_dev = 25pp 但极差 = 0。所以纯极差规则要加一条「现金过半先建仓」。

2. **费用到底吃掉多少?**
   显式建模: **3bp/边 + ¥5 单笔最低佣金** (ETF 免印花税)。
   佣金**从组合里扣** (拿 total − fee 建仓), 所以费用真实拖慢复利,
   不是事后从净值里减一笔 —— 后者会低估费用对**早期**复利的影响。

3. **调大极差到底改善不改善?**
   阈值 2~6pp 扫描 (毛/净两套) + 配对 t 检验 + 逐年对比。

价格口径: **前复权 (qfq)**, 4 只 ETF, 起点 = 513300 上市日 (2020-11-05)。

用法:
    python -X utf8 scripts/compare_spread_thresholds.py
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.ERROR)

import pandas as pd
from scipy import stats

from src.simulation.equal_weight_backtest import run_backtest

# 4 只资产 = 现在的再平衡组合 (纳指 513300 已并入再平衡)
ASSETS = [
    ("红利ETF易方达",     "sh515180", "515180"),
    ("黄金ETF博时",       "sz159937", "159937"),
    ("可转债ETF博时",     "sh511380", "511380"),
    ("纳斯达克ETF华夏",   "sh513300", "513300"),
]

START = "2020-11-05"                      # 513300 上市日 (4 资产公共起点)
INITIAL_CAP = 200_000.0
THRESHOLDS = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]

#: 交易成本 (单边) —— ETF 免印花税, 只有佣金
FEE_BPS = 3.0                              # 0.03% / 边 (ETF 佣金)
MIN_COMMISSION = 5.0                      # 单笔最低佣金 ¥5 (券商常见)


def build_universe() -> pd.DataFrame:
    """4 只 ETF 的**前复权**收盘价 (与图表同一条线)。"""
    from src.infrastructure.quote_providers import fetch_qfq_history
    out: dict[str, pd.Series] = {}
    for name, sina, em in ASSETS:
        df = fetch_qfq_history(sina, em)
        s = df["close"]
        s.name = "close"
        out[name] = s[s.index >= pd.Timestamp(START)]
    df = pd.DataFrame(out).sort_index().dropna()
    df.columns = [n for n, _, _ in ASSETS]
    return df


def run(prices: pd.DataFrame, rule: str, threshold: float,
        spread_pp: float | None = None, net: bool = True) -> dict:
    """跑一套口径。net=True 时计入 3bp/边 + ¥5 最低佣金。"""
    return run_backtest(
        prices,
        initial_cash=INITIAL_CAP,
        threshold_pp=threshold,
        trigger_rule=rule,
        spread_pp=spread_pp,
        fee_bps=FEE_BPS if net else 0.0,
        min_commission=MIN_COMMISSION if net else 0.0,
    )


def fmt(label: str, r: dict) -> str:
    ret = r["final"] / INITIAL_CAP - 1
    return (f"{label:<30} {r['final']:>12,.0f} {ret * 100:>8.2f}% "
            f"{r['cagr'] * 100:>7.2f}% {r['max_dd'] * 100:>8.2f}% "
            f"{r['sharpe']:>7.2f} {r['n_rebalances']:>6} "
            f"{r['turnover']:>12,.0f} {r['fees']:>10,.0f}")


HEADER = (f"{'口径':<30} {'期末市值':>12} {'累计收益':>9} {'CAGR':>8} "
          f"{'MaxDD':>9} {'Sharpe':>7} {'调仓':>6} {'换手¥':>12} {'费用¥':>10}")


def rebalance_dates(r: dict) -> list[str]:
    return [x["date"] for x in r["rebalances"]]


def yearly(series: pd.Series) -> dict[int, float]:
    """每年最后一天的累计收益 (相对期初本金)。"""
    out: dict[int, float] = {}
    for year, grp in series.groupby(series.index.year):
        out[int(year)] = float(grp.iloc[-1]) / INITIAL_CAP - 1
    return out


def main() -> None:
    print("取价 (前复权 qfq) …")
    prices = build_universe()
    print(f"  {len(prices)} 个交易日  {prices.index[0].date()} → {prices.index[-1].date()}"
          f"  ({len(prices.columns)} 只: {', '.join(prices.columns)})")

    base = run(prices, "max_dev", 3.0)
    base_gross = run(prices, "max_dev", 3.0, net=False)
    spread3 = run(prices, "spread", 3.0)
    base_rets = base["series"].pct_change().dropna()

    print("\n" + "=" * 118)
    print("A. 基线 —— 生产口径 max_dev @ 3pp (毛 = 不计费用, 净 = 3bp/边 + ¥5 最低佣金)")
    print("=" * 118)
    print(HEADER)
    print(fmt("max_dev@3 毛 (不计费用)", base_gross))
    print(fmt("max_dev@3 净 (计费用)", base))
    print(f"  费用拖累: 期末 ¥{base_gross['final'] - base['final']:,.0f} "
          f"({(base_gross['final'] / base['final'] - 1) * 100:.3f}%), "
          f"累计佣金 ¥{base['fees']:,.0f}, 平均每笔 ¥{base['fees'] / max(base['n_rebalances'], 1):,.0f}")

    print("\n" + "=" * 118)
    print("B. 阈值扫描 —— 生产口径 max_dev vs **纯极差 spread** (这是唯一能减少触发的旋钮)")
    print("=" * 118)
    print(HEADER)
    results: dict[str, dict] = {}
    for th in THRESHOLDS:
        for rule in ("max_dev", "spread"):
            r = run(prices, rule, th)
            results[f"{rule}@{th:g}"] = r
            print(fmt(f"{rule}@{th} 净", r))
        print("-" * 118)

    print("\n" + "=" * 118)
    print("C. 代数关系的**实证** —— 生产口径 3pp 到底相当于极差多少?")
    print("=" * 118)
    print("  理论 (n=4, 满仓): M·4/3 ≤ S ≤ 2M ⇒ 「max_dev>3」⇒「S>4」")
    md3 = run(prices, "max_dev", 3.0)
    after_first = [r for r in md3["rebalances"][1:]]     # 跳过建仓日 (全现金)
    viol = [r for r in after_first if r["spread"] <= r["max_dev"] * 4 / 3 + 1e-9]
    print(f"    满仓日里违反 S ≥ 4M/3 的样本: {len(viol)} / {len(after_first)}")
    print(f"    max_dev>3 的调仓日 (跳过建仓日) 里 spread 的最小值: "
          f"{min((r['spread'] for r in after_first), default=0):.4f}  "
          f"(建仓日当天: max_dev={md3['rebalances'][0]['max_dev']:.2f}pp, "
          f"极差={md3['rebalances'][0]['spread']:.2f}pp —— 全现金, 权重全是 0)")
    print("\n  「max_dev@3 ∪ spread@X」= {S>4} ∪ {S>X}:")
    for x in (3.0, 3.5, 4.0, 5.0):
        union = run(prices, "max_dev_or_spread", 3.0, spread_pp=x)
        eq = next((t for t in THRESHOLDS if abs(t - x) < 1e-9), None)
        ref = run(prices, "spread", x) if eq is not None else None
        same = (rebalance_dates(union) == rebalance_dates(ref)
                and abs(union["final"] - ref["final"]) < 1e-6) if ref else False
        print(f"    X={x}: 调仓 {union['n_rebalances']:>3} 次  期末 ¥{union['final']:,.0f}"
              f"   ≡ spread@{x}? {same}"
              f"   (spread@{x} 调仓 {ref['n_rebalances'] if ref else '-'} 次, "
              f"期末 ¥{ref['final']:,.0f})")
    print(f"    max_dev_or_spread@3 ≡ spread@3? "
          f"{rebalance_dates(run(prices, 'max_dev_or_spread', 3.0, spread_pp=3.0)) == rebalance_dates(spread3)}")
    extra = set(rebalance_dates(spread3)) - set(rebalance_dates(md3))
    print(f"    max_dev@3 ⊂ spread@3: 纯极差(3pp) 多触发 {len(extra)} 天 "
          f"({', '.join(sorted(extra)[:6])}{' …' if len(extra) > 6 else ''})")

    print("\n" + "=" * 118)
    print("D. 调大极差 vs 生产口径 —— 净收益差 + 配对 t 检验 (日收益, 净 vs 净)")
    print("=" * 118)
    print(f"  {'口径':<22} {'期末(净)':>12} {'Δ vs 基线':>12} {'ΔCAGR':>9} "
          f"{'ΔMaxDD':>9} {'调仓':>6} {'费用¥':>10} {'配对t':>8} {'p值':>9}")
    for key in ("max_dev@2", "max_dev@2.5", "max_dev@3", "max_dev@3.5",
                "max_dev@4", "max_dev@5",
                "spread@2.5", "spread@3", "spread@3.5",
                "spread@4", "spread@4.5", "spread@5", "spread@6"):
        r = results[key]
        rets = r["series"].pct_change().dropna()
        t, p = stats.ttest_rel(rets, base_rets)
        print(f"  {key:<22} {r['final']:>12,.0f} "
              f"{r['final'] - base['final']:>+12,.0f} "
              f"{(r['cagr'] - base['cagr']) * 100:>+8.2f}pp "
              f"{(r['max_dd'] - base['max_dd']) * 100:>+8.2f}pp "
              f"{r['n_rebalances']:>6} {r['fees']:>10,.0f} "
              f"{t:>8.2f} {p:>9.3f}")

    print("\n  **费用能解释多少?** (对比「同一口径的毛 vs 净」与「对基线的净差额」)")
    for key in ("spread@3", "spread@3.5", "spread@4", "spread@4.5", "spread@5"):
        gross = run(prices, "spread", float(key.split("@")[1]), net=False)
        net = results[key]
        fee_effect = gross["final"] - net["final"]          # 同一口径下费用吃掉多少
        delta_vs_base = net["final"] - base["final"]        # 对基线的净差额
        share = (fee_effect / delta_vs_base * 100) if abs(delta_vs_base) > 1 else float("nan")
        print(f"    {key:<14} 自身费用拖累 ¥{fee_effect:>7,.0f}   "
              f"对基线净差额 ¥{delta_vs_base:>+8,.0f}   "
              f"费用能解释 {share:>6.1f}%  (其余全是**调仓时点**的差别)")
    print(f"    全场: 把费用**全部**省掉 (16 套口径里最好的一套) 也只值 "
          f"¥{max(run(prices, k.split('@')[0], float(k.split('@')[1]), net=False)['final'] - results[k]['final'] for k in results):,.0f} 以内")

    print("\n" + "=" * 118)
    print("E. 逐年累计收益 —— 基线 vs 几个极差阈值 (净)")
    print("=" * 118)
    ys = yearly(base["series"])
    years = sorted(ys)
    keys = ["max_dev@3", "spread@3", "spread@3.5", "spread@4", "spread@5"]
    print(f"  {'年份':<8}" + "".join(f"{k:>14}" for k in keys))
    for y in years:
        row = f"  {y:<8}"
        for k in keys:
            row += f"{yearly(results[k]['series'])[y] * 100:>13.2f}%"
        print(row)
    print("\n  逐年**差** (pp, 对基线 max_dev@3):")
    for k in keys[1:]:
        yk = yearly(results[k]["series"])
        diffs = ", ".join(f"{y}: {(yk[y] - ys[y]) * 100:+.2f}" for y in years)
        print(f"    {k:<22} {diffs}")

    print("\n" + "=" * 118)
    print("F. 今天 (实盘组合) 在各口径下会不会触发")
    print("=" * 118)
    hist = json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8"))
    rec = hist[-1]
    ws = [float(w) for w in rec["weights"]]
    names = [p["name"] for p in rec["positions"]]
    target = 1.0 / len(ws)
    devs = [(w - target) * 100 for w in ws]
    max_dev = max(abs(d) for d in devs)
    spread = max(devs) - min(devs)
    print(f"  {rec['date']}  总市值 ¥{float(rec['total']):,.2f}")
    for nm, w, dv in zip(names, ws, devs):
        print(f"    {nm:<18} 权重 {w * 100:>6.2f}%   Δ {dv:>+6.2f}pp")
    print(f"  max|Δ| = {max_dev:.2f}pp   极差 = {spread:.2f}pp")
    for th in (3.0, 3.5, 4.0, 5.0):
        print(f"    @ {th}pp → max_dev 口径 "
              f"{'触发' if max_dev > th else '不触发'}   "
              f"极差口径 {'触发' if spread > th else '不触发'}")


if __name__ == "__main__":
    main()
