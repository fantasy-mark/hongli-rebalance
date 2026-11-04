"""把可转债ETF (511380) 换成 **中鼎股份 (000887)** 回测。

用户的问题 (2026-11-04):
    「维持 max_dev > 3pp 不动, 尝试下把可转债ETF更换为 000887 回测」

⚠️ **000887 是深市个股「中鼎股份」**, 不是转债基金 (东财全量基金代码表里
根本没有 000887; 行情接口 secid=0.000887 → 「中鼎股份」)。所以这不是
「换个同类工具」, 是把一篮子里的**债券 ETF 换成了单只股票** —— 风险性质
完全不同, 成本结构也不同 (卖出要交印花税)。

所以本脚本同时回答三件事:
  1. **收益**: 换掉之后收益怎么变 (配对 t 检验 + 逐年)。
  2. **风险**: 单只个股的波动/回撤 vs 债券 ETF (它占组合 1/4)。
  3. **成本**: 个股卖出 0.05% 印花税 (ETF 免) —— 调仓越频繁, 差距越大。

价格口径: **前复权 (qfq)**, 个股用东财 `stock_zh_a_hist(adjust="qfq")`,
与 ETF 那条线**同口径**, 可以直接拼在一张表里比。

用法:
    python -X utf8 scripts/compare_asset_substitutions.py
"""
from __future__ import annotations

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

#: 生产篮子 (4 只) —— 基准
BASE_ASSETS = [
    ("红利ETF易方达",   "sh515180", "515180"),
    ("黄金ETF博时",     "sz159937", "159937"),
    ("可转债ETF博时",   "sh511380", "511380"),
    ("纳斯达克ETF华夏", "sh513300", "513300"),
]

#: 个股: 中鼎股份 (深市主板, 000887)
STOCK = ("中鼎股份", "sz000887", "000887")

START = "2020-11-05"                      # 513300 上市日 (公共起点)
INITIAL_CAP = 200_000.0
FEE_BPS = 3.0                              # 佣金 0.03% / 边
MIN_COMMISSION = 5.0                      # 单笔最低佣金
STAMP_DUTY_BPS = 5.0                      # **个股卖出**印花税 0.05% (ETF 免)


def _etf_series(name: str, sina: str, eastmoney: str) -> pd.Series:
    from src.infrastructure.quote_providers import fetch_qfq_history
    df = fetch_qfq_history(sina, eastmoney)
    s = df["close"]
    s.name = name
    return s


def _stock_series(name: str, sina: str, code: str) -> pd.Series:
    from src.infrastructure.quote_providers import fetch_stock_qfq_history
    df, _source = fetch_stock_qfq_history(code)
    s = df["close"]
    s.name = name
    return s


def build(basket: list[tuple[str, str, str]], is_stock: set[str] | None = None
          ) -> pd.DataFrame:
    """把一组资产拼成对齐的价格表 (内连接 + dropna)。"""
    is_stock = is_stock or set()
    out: dict[str, pd.Series] = {}
    for name, sina, code in basket:
        out[name] = (_stock_series(name, sina, code) if name in is_stock
                     else _etf_series(name, sina, code))
    df = pd.DataFrame(out).sort_index().dropna()
    df = df[df.index >= pd.Timestamp(START)]
    return df


def run(prices: pd.DataFrame, has_stock: bool = False) -> dict:
    """max_dev@3 (生产口径) + 真实成本。个股篮子带卖出印花税。"""
    return run_backtest(
        prices,
        initial_cash=INITIAL_CAP,
        threshold_pp=3.0,
        trigger_rule="max_dev",
        rebalance_mode="snap_all",
        fee_bps=FEE_BPS,
        min_commission=MIN_COMMISSION,
        stamp_duty_bps=STAMP_DUTY_BPS if has_stock else 0.0,
    )


def single_asset_stats(series: pd.Series, label: str) -> None:
    """单只资产自己的波动/回撤 —— 放进 1/4 仓位意味着组合要扛这些。"""
    rets = series.pct_change().dropna()
    peak = series.cummax()
    max_dd = float(((series - peak) / peak).min())
    years = (series.index[-1] - series.index[0]).days / 365.25
    cagr = (float(series.iloc[-1]) / float(series.iloc[0])) ** (1 / years) - 1
    print(f"    {label:<16} 年化 {cagr * 100:>6.2f}%   波动 {rets.std() * (252 ** 0.5) * 100:>6.2f}%"
          f"   MaxDD {max_dd * 100:>7.2f}%")


def yearly(series: pd.Series) -> dict[int, float]:
    out: dict[int, float] = {}
    for year, grp in series.groupby(series.index.year):
        out[int(year)] = float(grp.iloc[-1]) / INITIAL_CAP - 1
    return out


def main() -> None:
    print("取价 (前复权 qfq) …")
    base_px = build(BASE_ASSETS)
    swap_px = build([a if a[0] != "可转债ETF博时" else STOCK for a in BASE_ASSETS],
                    is_stock={STOCK[0]})
    five_px = build(BASE_ASSETS + [STOCK], is_stock={STOCK[0]})
    print(f"  基准篮子   {len(base_px)} 个交易日  {base_px.index[0].date()} → {base_px.index[-1].date()}")
    print(f"  换股篮子   {len(swap_px)} 个交易日  {swap_px.index[0].date()} → {swap_px.index[-1].date()}")
    print(f"  加股篮子   {len(five_px)} 个交易日  {five_px.index[0].date()} → {five_px.index[-1].date()}")
    if len(swap_px) != len(base_px):
        print(f"  ⚠️ 换股篮子的交易日比基准少 {len(base_px) - len(swap_px)} 天 "
              f"(000887 有停牌?) —— 两边不可直接比")

    base = run(base_px)
    swap = run(swap_px, has_stock=True)
    five = run(five_px, has_stock=True)
    base_rets = base["series"].pct_change().dropna()

    print("\n" + "=" * 112)
    print("A. 结果 —— 全部 max_dev@3 (生产口径), 成本: 佣金 3bp/边 + ¥5 最低 + 个股卖出印花税 5bp")
    print("=" * 112)
    hdr = (f"  {'篮子':<34} {'期末市值':>12} {'累计收益':>9} {'CAGR':>8} "
           f"{'MaxDD':>9} {'Sharpe':>7} {'调仓':>6} {'换手¥':>12} {'费用¥':>10}")
    print(hdr)
    for label, r in (("基准 4 只 (可转债ETF)", base),
                     ("换股 4 只 (中鼎股份)", swap),
                     ("加股 5 只 (再加中鼎)", five)):
        ret = r["final"] / INITIAL_CAP - 1
        print(f"  {label:<34} {r['final']:>12,.0f} {ret * 100:>8.2f}% "
              f"{r['cagr'] * 100:>7.2f}% {r['max_dd'] * 100:>8.2f}% {r['sharpe']:>7.2f} "
              f"{r['n_rebalances']:>6} {r['turnover']:>12,.0f} {r['fees']:>10,.0f}")

    print("\n" + "=" * 112)
    print("B. 换股 vs 基准 —— 配对 t 检验 (日收益) + 逐年")
    print("=" * 112)
    for label, r in (("换股 4 只", swap), ("加股 5 只", five)):
        rets = r["series"].pct_change().dropna()
        t, p = stats.ttest_rel(rets, base_rets)
        print(f"  {label}: 期末 Δ ¥{r['final'] - base['final']:>+9,.0f} "
              f"({(r['final'] / base['final'] - 1) * 100:+.2f}%)   "
              f"CAGR Δ {(r['cagr'] - base['cagr']) * 100:+.2f}pp   "
              f"MaxDD Δ {(r['max_dd'] - base['max_dd']) * 100:+.2f}pp   "
              f"配对 t {t:+.2f}  p {p:.3f}")
    ys, yb, yf = yearly(base["series"]), yearly(swap["series"]), yearly(five["series"])
    print(f"\n  {'年份':<8}{'基准':>10}{'换股':>10}{'Δpp':>9}{'加股':>10}{'Δpp':>9}")
    for y in sorted(ys):
        print(f"  {y:<8}{ys[y] * 100:>9.2f}%{yb[y] * 100:>9.2f}%"
              f"{(yb[y] - ys[y]) * 100:>+8.2f}{yf[y] * 100:>9.2f}%"
              f"{(yf[y] - ys[y]) * 100:>+8.2f}")
    # 年度符号检验: 「每年都赢」到底有多强?
    diffs = [yb[y] - ys[y] for y in sorted(ys)]
    wins = sum(1 for d in diffs if d > 0)
    n = len(diffs)
    p_sign = 2 * sum(stats.binom.pmf(k, n, 0.5) for k in range(wins, n + 1))
    print(f"\n  年度符号检验: 换股 {wins}/{n} 年跑赢 (双尾 p = {p_sign:.3f}) —— "
          f"一致性信号, 但 {n} 个观测太少, 不足以定论")
    print(f"  截至 2024 年底: 基准 {ys[2024] * 100:.2f}% vs 换股 {yb[2024] * 100:.2f}% "
          f"(领先 {(yb[2024] - ys[2024]) * 100:+.2f}pp) ⇒ 优势不是只靠 2025-26 两天")

    print("\n" + "=" * 112)
    print("C. 单只资产自己的风险 (它要占组合 1/4)")
    print("=" * 112)
    single_asset_stats(base_px["可转债ETF博时"], "可转债ETF博时")
    single_asset_stats(swap_px["中鼎股份"], "中鼎股份")

    print("\n" + "=" * 112)
    print("D. 成本结构 —— 印花税是换股后多出来的")
    print("=" * 112)
    for label, r in (("基准 4 只", base), ("换股 4 只", swap)):
        sells = sum(1 for rb in r["rebalances"] for l in rb["legs"] if l["side"] == "sell")
        sell_value = sum(l["value"] for rb in r["rebalances"] for l in rb["legs"]
                         if l["side"] == "sell")
        tax = sell_value * STAMP_DUTY_BPS / 10000
        print(f"  {label:<12} 卖出腿 {sells:>4} 条  卖出成交额 ¥{sell_value:>12,.0f}  "
              f"印花税(5bp) ¥{tax:>8,.0f}  总费用 ¥{r['fees']:>8,.0f}")
    print("  (ETF 免印花税 ⇒ 基准那条的印花税是 ¥0; 个股卖出按 0.05% 收)")

    print("\n" + "=" * 112)
    print("E. 触发频次 —— 个股波动大, 3pp 阈值被撞得更频繁")
    print("=" * 112)
    for label, r in (("基准 4 只", base), ("换股 4 只", swap), ("加股 5 只", five)):
        dates = [rb["date"] for rb in r["rebalances"]]
        print(f"  {label:<12} 调仓 {r['n_rebalances']:>3} 次 / {len(base_px)} 个交易日 "
              f"= 每 {len(base_px) / max(r['n_rebalances'], 1):>5.1f} 个交易日一次   "
              f"平均每次换手 ¥{r['turnover'] / max(r['n_rebalances'], 1):>10,.0f}")

    print("\n" + "=" * 112)
    print("F. 数据自检 —— 中鼎股份的 qfq 序列没有「假趋势」, 再平衡有没有加分")
    print("=" * 112)
    from src.infrastructure.quote_providers import fetch_stock_qfq_history
    qdf, _ = fetch_stock_qfq_history("000887")
    raw_q = qdf["close"]
    unadj = None
    try:
        import akshare as ak
        u = ak.stock_zh_a_hist(symbol="000887", period="daily",
                               start_date="20201101", end_date="20991231", adjust="")
        u["日期"] = pd.to_datetime(u["日期"])
        unadj = u.set_index("日期")["收盘"].astype(float)
    except Exception as ex:
        print(f"  (不复权对照取不到: {ex})")
    win = raw_q[raw_q.index >= pd.Timestamp(START)]
    print(f"  qfq 首 {win.index[0].date()} ¥{win.iloc[0]:.4f} → 末 "
          f"{win.index[-1].date()} ¥{win.iloc[-1]:.4f}  ⇒ 累计 "
          f"{(win.iloc[-1] / win.iloc[0] - 1) * 100:+.2f}%")
    if unadj is not None:
        uw = unadj[unadj.index >= pd.Timestamp(START)]
        print(f"  不复权 首 ¥{uw.iloc[0]:.4f} → 末 ¥{uw.iloc[-1]:.4f}  ⇒ 累计 "
              f"{(uw.iloc[-1] / uw.iloc[0] - 1) * 100:+.2f}%  "
              f"(分红贡献 {(win.iloc[-1] / win.iloc[0] - uw.iloc[-1] / uw.iloc[0]) * 100:+.2f}pp)")
    rets = win.pct_change().dropna()
    print(f"  最大单日涨 {rets.max() * 100:+.2f}%  最小 {rets.min() * 100:+.2f}%  "
          f"单日 >|5%| 的天数 {int((rets.abs() > 0.05).sum())} / {len(rets)}")
    top = rets.nlargest(3)
    print("  涨幅最大的三天: " + ", ".join(f"{d.date()} {v * 100:+.1f}%" for d, v in top.items()))
    print("  ⚠️ 单只股票 = 公司经营风险 + 流动性/停牌风险 + 无分散; 这些**回测里看不见**")

    # 再平衡有没有加分? (换股篮子 vs 买入持有)
    def buy_and_hold(px: pd.DataFrame) -> dict:
        n = len(px.columns)
        first = px.iloc[0]
        sh = {c: INITIAL_CAP * (1.0 / n) / float(first[c]) for c in px.columns}
        ser = pd.Series({d: sum(sh[c] * float(row[c]) for c in px.columns)
                         for d, row in px.iterrows()})
        ser.index = px.index
        peak = ser.cummax()
        return {"final": float(ser.iloc[-1]),
                "max_dd": float(((ser - peak) / peak).min())}
    for label, px, r in (("基准 4 只", base_px, base), ("换股 4 只", swap_px, swap)):
        bh = buy_and_hold(px)
        print(f"  {label:<12} 再平衡 ¥{r['final']:>10,.0f} (MaxDD {r['max_dd'] * 100:>6.2f}%)   "
              f"买入持有 ¥{bh['final']:>10,.0f} (MaxDD {bh['max_dd'] * 100:>6.2f}%)   "
              f"再平衡多赚/少赚 ¥{r['final'] - bh['final']:>+9,.0f}")

    print("\n" + "=" * 112)
    print("G. 组合年化波动 + 阈值敏感性 (个股波动大 ⇒ 3pp 这个门槛对换股篮子更关键)")
    print("=" * 112)
    for label, r in (("基准 4 只", base), ("换股 4 只", swap), ("加股 5 只", five)):
        v = r["series"].pct_change().dropna().std() * (252 ** 0.5)
        print(f"  {label:<12} 组合年化波动 {v * 100:>6.2f}%   "
              f"单笔亏损概率 (日波动 {r['series'].pct_change().dropna().std() * 100:.3f}%)")
    print()
    for label, px, has_stock in (("基准 4 只", base_px, False), ("换股 4 只", swap_px, True)):
        print(f"  {label}:")
        for th in (3.0, 3.5, 4.0, 5.0):
            r = run_backtest(px, initial_cash=INITIAL_CAP, threshold_pp=th,
                             trigger_rule="max_dev", rebalance_mode="snap_all",
                             fee_bps=FEE_BPS, min_commission=MIN_COMMISSION,
                             stamp_duty_bps=STAMP_DUTY_BPS if has_stock else 0.0)
            print(f"    @{th}pp  期末 ¥{r['final']:>10,.0f}  CAGR {r['cagr'] * 100:>6.2f}%  "
                  f"MaxDD {r['max_dd'] * 100:>7.2f}%  Sharpe {r['sharpe']:>5.2f}  "
                  f"调仓 {r['n_rebalances']:>3} 次  费用 ¥{r['fees']:>7,.0f}")


if __name__ == "__main__":
    main()
