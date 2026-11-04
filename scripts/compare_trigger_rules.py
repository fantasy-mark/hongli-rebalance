"""对比 3pp 再平衡的**两种触发口径**, 看「正负偏离拉得太开也触发」是不是改善。

问题 (2026-11-04 用户提出):
    老口径 —— 任一资产的 |w − 1/n| > 3pp 就触发, 整篮子拉回 1/n。
    新口径 —— **也**在「有的正偏、有的反偏, 而且两者差得超过 3pp」时触发。
              即: max Δ − min Δ (正负偏离的**极差**) > 3pp 也算。

两者的关系 (代码里就是 `trigger_rule` 的两个取值):
    "max_dev"            任一 |Δ| > 阈值。**生产口径**
    "max_dev_or_spread"  上面那条 **或** 极差 > 阈值 (用户的新想法)
    当偏离**跨零**时 (有正有负), 极差 ≥ 最大的 |Δ| —— 新口径更敏感;
    当偏离**同号**时 (比如全是低配), 极差 < 最大的 |Δ| —— 靠老口径兜底。
    所以新口径 = 老口径 **加上** 一条, 只会多触发, 不会少触发。

还测了第三种「回正动作」:
    "snap_all" 整篮子拉回 1/n (生产口径)
    "pair"     只把「最正偏」和「最反偏」那一对拉平 (那一对的总额不变,
               钱在两者之间挪), 其余持仓一动不动。

价格口径: **前复权 (qfq)**, 4 只 ETF, 起点 = 513300 上市日 (2020-11-05),
跟 `scripts/compare_4pp_strategies.py` 的窗口一致。

用法:
    python -X utf8 scripts/compare_trigger_rules.py
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

from src.simulation.equal_weight_backtest import run_backtest

# 4 只资产 = 现在的再平衡组合 (纳指 513300 已并入再平衡, 不是只做网格)
ASSETS = [
    ("红利ETF易方达",     "sh515180", "515180"),
    ("黄金ETF博时",       "sz159937", "159937"),
    ("可转债ETF博时",     "sh511380", "511380"),
    ("纳斯达克ETF华夏",   "sh513300", "513300"),
]

START = "2020-11-05"                      # 513300 上市日 (4 资产公共起点)
INITIAL_CAP = 200_000.0
THRESHOLDS = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
#: 单边交易成本 (万分之几) —— 回测本身**不含**成本, 这里用换手量折算一下。
COST_BPS_PER_SIDE = 3.0                    # 0.03% / 边 (ETF 佣金, 无印花税)


def fetch_qfq(sina: str, eastmoney: str) -> pd.Series:
    """取**前复权**收盘价 —— 与仓库的口径一致 (见 memory: qfq-prices)。

    走 `quote_providers.fetch_qfq_history` —— 它就是「东财 qfq → 腾讯 qfq
    → 新浪(不复权, 只兜底)」这条链, 跟图表用的是同一条线。

    ⚠️ 别用不复权: 多年回测里那等于把分红全丢了 (红利 ETF 六年累计分红
    让 CAGR 从 11.95% 掉到 10.03%)。
    """
    from src.infrastructure.quote_providers import fetch_qfq_history
    df = fetch_qfq_history(sina, eastmoney)
    s = df["close"]
    s.name = "close"
    return s[s.index >= pd.Timestamp(START)]


def build_universe() -> pd.DataFrame:
    out: dict[str, pd.Series] = {}
    for name, sina, em in ASSETS:
        out[name] = fetch_qfq(sina, em)
    df = pd.DataFrame(out).sort_index().dropna()
    df.columns = [n for n, _, _ in ASSETS]
    return df


def run_buy_and_hold(prices: pd.DataFrame, initial_cash: float) -> dict:
    """等权建仓一次, 之后不动 —— 「不做再平衡」的对照基线。"""
    n = len(prices.columns)
    first = prices.iloc[0]
    shares = {c: initial_cash * (1.0 / n) / float(first[c]) for c in prices.columns}
    series = pd.Series(
        {d: sum(shares[c] * float(row[c]) for c in prices.columns)
         for d, row in prices.iterrows()})
    series.index = prices.index
    peak = series.cummax()
    max_dd = float(((series - peak) / peak).min())
    rets = series.pct_change().dropna()
    sharpe = 0.0
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * (252 ** 0.5))
    years = (series.index[-1] - series.index[0]).days / 365.25
    final = float(series.iloc[-1])
    return {
        "final": final, "cagr": (final / initial_cash) ** (1 / years) - 1 if years > 0 else 0.0,
        "max_dd": max_dd, "sharpe": sharpe, "n_rebalances": 0, "turnover": 0.0,
        "series": series, "rebalances": [],
    }


def fmt_row(label: str, res: dict, initial_cash: float) -> str:
    ret = res["final"] / initial_cash - 1
    return (f"{label:<26} {res['final']:>12,.0f} {ret * 100:>8.2f}% "
            f"{res['cagr'] * 100:>7.2f}% {res['max_dd'] * 100:>8.2f}% "
            f"{res['sharpe']:>7.2f} {res['n_rebalances']:>6} "
            f"{res['turnover']:>12,.0f}")


HEADER = (f"{'口径':<26} {'期末市值':>12} {'累计收益':>9} {'CAGR':>8} "
          f"{'MaxDD':>9} {'Sharpe':>7} {'调仓':>6} {'换手¥':>12}")


def main() -> None:
    print("取价 (前复权 qfq) …")
    prices = build_universe()
    print(f"  {len(prices)} 个交易日  {prices.index[0].date()} → {prices.index[-1].date()}"
          f"  ({len(prices.columns)} 只: {', '.join(prices.columns)})")

    results: dict[str, dict] = {}

    print("\n" + "=" * 100)
    print("A. 基线")
    print("=" * 100)
    bh = run_buy_and_hold(prices, INITIAL_CAP)
    results["hold"] = bh
    print(HEADER)
    print(fmt_row("买入持有 (不再平衡)", bh, INITIAL_CAP))
    base = run_backtest(prices, initial_cash=INITIAL_CAP, trigger_rule="max_dev")
    results["base3"] = base
    print(fmt_row("3pp max_dev (生产口径)", base, INITIAL_CAP))

    print("\n" + "=" * 100)
    print("B. 阈值扫描 —— 老口径 (max_dev) vs 新口径 (max_dev_or_spread), 动作都是整篮子回正")
    print("=" * 100)
    print(HEADER)
    for th in THRESHOLDS:
        old = run_backtest(prices, initial_cash=INITIAL_CAP,
                           threshold_pp=th, trigger_rule="max_dev")
        new = run_backtest(prices, initial_cash=INITIAL_CAP,
                           threshold_pp=th, trigger_rule="max_dev_or_spread")
        results[f"old_{th}"] = old
        results[f"new_{th}"] = new
        print(fmt_row(f"max_dev @ {th}pp", old, INITIAL_CAP))
        print(fmt_row(f"max_dev_or_spread @ {th}pp", new, INITIAL_CAP))
        print("-" * 100)

    print("\n" + "=" * 100)
    print("C. 只动那一对 (pair 动作): 触发口径同上, 回正时只把最正偏/最反偏那一对拉平")
    print("=" * 100)
    print(HEADER)
    for th in (2.0, 3.0, 4.0):
        pr = run_backtest(prices, initial_cash=INITIAL_CAP, threshold_pp=th,
                          trigger_rule="max_dev_or_spread", rebalance_mode="pair")
        results[f"pair_{th}"] = pr
        print(fmt_row(f"spread + pair @ {th}pp", pr, INITIAL_CAP))

    print("\n" + "=" * 100)
    print("D. 3pp 口径的结论 (生产阈值)")
    print("=" * 100)
    old3, new3, pair3 = results["base3"], results["new_3.0"], results["pair_3.0"]
    for name, r in (("老口径 max_dev (生产)", old3),
                    ("新口径 max_dev_or_spread", new3),
                    ("新口径 + 只动那一对", pair3)):
        d_cagr = (r["cagr"] - old3["cagr"]) * 100
        d_dd = (r["max_dd"] - old3["max_dd"]) * 100
        d_sh = r["sharpe"] - old3["sharpe"]
        cost = r["turnover"] / 2 * COST_BPS_PER_SIDE / 10000
        print(f"  {name:<24} 期末 {r['final']:>12,.0f}  "
              f"CAGR {r['cagr']*100:>6.2f}% ({d_cagr:+.2f}pp)  "
              f"MaxDD {r['max_dd']*100:>7.2f}% ({d_dd:+.2f}pp)  "
              f"Sharpe {r['sharpe']:>5.2f} ({d_sh:+.2f})  "
              f"调仓 {r['n_rebalances']:>3} 次  "
              f"换手 {r['turnover']:>12,.0f}  "
              f"按 {COST_BPS_PER_SIDE}bp/边 成本 ≈ ¥{cost:,.0f} "
              f"({cost / INITIAL_CAP * 100:.2f}% 初始本金)")

    print("\n" + "=" * 100)
    print("E. 调仓日差异 (3pp): 老口径 vs 新口径")
    print("=" * 100)
    old_dates = [r["date"] for r in old3["rebalances"]]
    new_dates = [r["date"] for r in new3["rebalances"]]
    only_new = [d for d in new_dates if d not in old_dates]
    only_old = [d for d in old_dates if d not in new_dates]
    print(f"  老口径调仓 {len(old_dates)} 次, 新口径 {len(new_dates)} 次")
    print(f"  新口径**多**触发的日子 ({len(only_new)}): {only_new}")
    print(f"  新口径**少**触发的日子 ({len(only_old)}): {only_old}")
    print("  多触发那几天, 老口径下的 max|Δ| / 极差:")
    by_date = {r["date"]: r for r in new3["rebalances"]}
    for d in only_new:
        r = by_date[d]
        print(f"    {d}  max|Δ|={r['max_dev']:.2f}pp  极差={r['spread']:.2f}pp  "
              f"最正偏={r['hi']}  最反偏={r['lo']}")

    print("\n" + "=" * 100)
    print("F. 今天 (实盘组合) 在两种口径下会不会触发")
    print("=" * 100)
    hist = json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8"))
    rec = hist[-1]
    total = float(rec["total"])
    ws = [float(w) for w in rec["weights"]]
    names = [p["name"] for p in rec["positions"]]
    target = 1.0 / len(ws)
    devs = [(w - target) * 100 for w in ws]
    max_dev = max(abs(d) for d in devs)
    spread = max(devs) - min(devs)
    print(f"  {rec['date']}  总市值 ¥{total:,.2f}")
    for nm, w, dv in zip(names, ws, devs):
        print(f"    {nm:<18} 权重 {w*100:>6.2f}%   Δ {dv:>+6.2f}pp")
    print(f"  max|Δ| = {max_dev:.2f}pp   极差 = {spread:.2f}pp")
    print(f"  老口径 (max_dev > 3)            → {'触发' if max_dev > 3 else '不触发'}")
    print(f"  新口径 (max_dev>3 或 极差>3)     → "
          f"{'触发' if (max_dev > 3 or spread > 3) else '不触发'}")


if __name__ == "__main__":
    main()
