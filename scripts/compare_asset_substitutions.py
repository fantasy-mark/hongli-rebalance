"""把可转债ETF (511380) 换成别的资产回测 —— 多候选 + **双价格口径**对比。

用户的问题 (2026-11-04):
    1. 「尝试下把可转债ETF更换为 000887 回测」
    2. 「那换成 600900 再对比下把」

⚠️ **000887 / 600900 都不是转债基金, 是个股**:
    000887 = 中鼎股份 (深市主板)
    600900 = 长江电力 (沪市主板, 高股息水电)
⇒ 这不是「换个同类工具」, 是把一篮子里的**债券 ETF 换成了单只股票**:
   风险性质、成本 (卖出印花税)、税务 (股息税) 全都不一样。

⚠️⚠️ **两种价格口径, 结论会不一样** —— 这是本脚本最重要的产出:

  | 口径 | 含义 | 515180 红利ETF 六年累计 |
  |---|---|---|
  | 东财 qfq (**项目现行**) | 「不复权价 − 之后还没发的分红」(加法) | **+77.63%** |
  | 自算总收益 (**修正**)   | 不复权价 × 分红按除息价再投 | **+59.40%** |
  | 官方累计净值 (权威)     | — | **+56.97%** |
  | 不复权 (分红全丢)       | — | +23.39% |

  ⇒ **东财 qfq 不是总收益指数, 拿来算收益率会虚高**。分红标的 (515180)
    六年虚高 **20.7pp**; 黄金/可转债/纳指期间不发分红, 所以这个坑一直没暴露。
  ⇒ 个股这边更糟: **东财个股 qfq 对 601088 (中国神华) 是坏的** ——
    非除息日里 qfq 日收益与不复权差 >1pp 的有 538 天, 最大 38.67pp,
    还出现过 **+48.66%** 的单日「涨幅」(主板涨停才 10%)。
    ⇒ 本脚本**先审计再用**: `audit_stock_qfq` 不过关的候选直接剔除。

用法:
    python -X utf8 scripts/compare_asset_substitutions.py
"""
from __future__ import annotations

import contextlib
import io
import logging
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.ERROR)


@contextlib.contextmanager
def _quiet():
    """akshare 的 `fund_fh_em` 要翻 74 页, tqdm 进度条太吵 —— 取价时静音。"""
    with contextlib.redirect_stderr(io.StringIO()):
        yield

import pandas as pd
from scipy import stats

from src.infrastructure import quote_providers as qp
from src.simulation.equal_weight_backtest import run_backtest

#: 生产篮子 (4 只) —— 基准
BASE_ASSETS = [
    ("红利ETF易方达",   "sh515180", "515180", False),
    ("黄金ETF博时",     "sz159937", "159937", False),
    ("可转债ETF博时",   "sh511380", "511380", False),
    ("纳斯达克ETF华夏", "sh513300", "513300", False),
]

#: 候选 —— 用来替换掉「可转债ETF博时」的那一只。
#: (名称, 新浪/代码前缀, 东财代码, 是不是个股)
CANDIDATES = [
    ("中鼎股份",   "sz000887",  "000887",  True),
    ("长江电力",   "sh600900",  "600900",  True),
    ("沪深300ETF", "sh510300",  "510300",  False),
    ("中国神华",   "sh601088",  "601088",  True),
    ("工商银行",   "sh601398",  "601398",  True),
]

START = "2020-11-05"                      # 513300 上市日 (公共起点)
INITIAL_CAP = 200_000.0
FEE_BPS = 3.0                              # 佣金 0.03% / 边
MIN_COMMISSION = 5.0                      # 单笔最低佣金
STAMP_DUTY_BPS = 5.0                      # **个股卖出**印花税 0.05% (ETF 免)
STOCK_DIVIDEND_TAX_BPS = 100.0            # 个股股息税 10% (持有 1 月~1 年)

#: 两种价格口径
BASES = [
    ("东财 qfq (项目现行口径)", "eastmoney", 0.0),
    ("自算总收益 (修正口径)", "total_return", STOCK_DIVIDEND_TAX_BPS),
]


# ---------------------------------------------------------------------------
# 取价
# ---------------------------------------------------------------------------
def asset_series(name: str, sina: str, code: str, is_stock: bool,
                 basis: str, tax_bps: float) -> pd.Series:
    if basis == "eastmoney":
        if is_stock:
            df, _ = qp.fetch_stock_qfq_history(code)
        else:
            df = qp.fetch_qfq_history(sina, code)
    else:
        if is_stock:
            df, _ = qp.fetch_stock_total_return_history(code, dividend_tax_bps=tax_bps)
        else:
            df, _ = qp.fetch_etf_total_return_history(code, sina)
    s = df["close"]
    s.name = name
    return s


def build(basket: list[tuple], basis: str, tax_bps: float) -> pd.DataFrame:
    """把一组资产拼成对齐的价格表 (内连接 + dropna)。"""
    out = {name: asset_series(name, sina, code, is_stock, basis, tax_bps)
           for name, sina, code, is_stock in basket}
    df = pd.DataFrame(out).sort_index().dropna()
    return df[df.index >= pd.Timestamp(START)]


def run(prices: pd.DataFrame, has_stock: bool = False, threshold_pp: float = 3.0
        ) -> dict:
    """max_dev@3 (生产口径) + 真实成本。个股篮子带卖出印花税。"""
    return run_backtest(
        prices, initial_cash=INITIAL_CAP, threshold_pp=threshold_pp,
        trigger_rule="max_dev", rebalance_mode="snap_all",
        fee_bps=FEE_BPS, min_commission=MIN_COMMISSION,
        stamp_duty_bps=STAMP_DUTY_BPS if has_stock else 0.0,
    )


def single_asset_stats(series: pd.Series, label: str) -> None:
    rets = series.pct_change().dropna()
    peak = series.cummax()
    max_dd = float(((series - peak) / peak).min())
    years = (series.index[-1] - series.index[0]).days / 365.25
    cagr = (float(series.iloc[-1]) / float(series.iloc[0])) ** (1 / years) - 1
    print(f"    {label:<14} 年化 {cagr * 100:>6.2f}%   波动 {rets.std() * (252 ** 0.5) * 100:>6.2f}%"
          f"   MaxDD {max_dd * 100:>7.2f}%")


def yearly(series: pd.Series) -> dict[int, float]:
    out: dict[int, float] = {}
    for year, grp in series.groupby(series.index.year):
        out[int(year)] = float(grp.iloc[-1]) / INITIAL_CAP - 1
    return out


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


def candidate_sells(result: dict, name: str) -> list[str]:
    """这个候选在换股篮子被卖过几次、什么时候 —— 决定股息税落在哪一档。"""
    out = []
    for rb in result["rebalances"]:
        for l in rb["legs"]:
            if l["name"] == name and l["side"] == "sell":
                out.append(rb["date"])
    return out


# ---------------------------------------------------------------------------
# 个股分红 / 除息 口径审计
# ---------------------------------------------------------------------------
def dividend_audit(code: str, label: str) -> str:
    """拆个股的总收益: 价格涨了多少 vs 分红贡献了多少 (含股息税)。"""
    import akshare as ak

    qdf, _ = qp.fetch_stock_qfq_history(code)
    win = qdf["close"][qdf.index >= pd.Timestamp(START)]
    u = ak.stock_zh_a_hist(symbol=code, period="daily",
                           start_date="20201101", end_date="20991231", adjust="")
    u["日期"] = pd.to_datetime(u["日期"])
    uw = u.set_index("日期")["收盘"].astype(float)
    uw = uw[uw.index >= pd.Timestamp(START)]

    price_ret = float(uw.iloc[-1]) / float(uw.iloc[0]) - 1
    total_ret = float(win.iloc[-1]) / float(win.iloc[0]) - 1
    div_pp = ((1 + total_ret) / (1 + price_ret) - 1) * 100

    dl = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    dl = dl[dl["进度"] == "实施"].copy()
    dl["除权除息日"] = pd.to_datetime(dl["除权除息日"])
    dl = dl[dl["除权除息日"] >= pd.Timestamp(START)]
    gross = float(dl["派息"].sum()) / 10.0
    p0 = float(uw.iloc[0])
    gross_pct = gross / p0 * 100

    gaps = []
    for ds, amt in zip(dl["除权除息日"], dl["派息"] / 10.0):
        loc = uw.index.searchsorted(ds)
        if 0 < loc < len(uw):
            gaps.append(float(uw.iloc[loc]) / float(uw.iloc[loc - 1]) - 1)
    worst_gap = min(gaps) * 100 if gaps else 0.0
    return (f"｜价格 {price_ret * 100:+.2f}%  总收益 {total_ret * 100:+.2f}%"
            f"  分红再投 {div_pp:+.2f}pp"
            f"｜毛分红 {gross:.2f}元/股 ({gross_pct:.1f}% 起始价)"
            f"  10%档税 ≈ -{gross_pct * 0.10:.2f}pp"
            f"｜除息最深跳空 {worst_gap:+.2f}%")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def audit_candidates() -> dict[str, dict]:
    """审计东财个股 qfq —— 数据坏掉的候选**直接剔除**, 不进回测。"""
    print("审计东财个股 qfq (非除息日 qfq 日收益 vs 不复权日收益) …")
    audits: dict[str, dict] = {}
    for name, _sina, code, is_stock in CANDIDATES:
        if not is_stock:
            continue
        a = qp.audit_stock_qfq(code, start=START)
        audits[name] = a
        flag = "✅ 可用" if a["ok"] else ("❌ 数据损坏, 剔除" if a["max_err_pp"] > 5
                                         else "⚠️ 有偏差 (仍在 5pp 内)")
        print(f"  {name:<10} 非除息日均值 {a['mean_err_pp']:>6.3f}pp  最大 {a['max_err_pp']:>6.2f}pp"
              f"  超 0.5pp {a['n_bad_days']:>4} 天  最差 {a['worst_date']}  → {flag}")
    return audits


def main() -> None:
    audits = audit_candidates()
    bad = {n for n, a in audits.items() if a["max_err_pp"] > 5}
    candidates = [c for c in CANDIDATES if c[0] not in bad]

    for basis_title, basis, tax_bps in BASES:
        print("\n" + "#" * 118)
        print(f"# 价格口径: {basis_title}"
              + (f" (个股股息税 {tax_bps / 100:.0f}%)" if basis == "total_return" else ""))
        print("#" * 118)
        print("取价 …")
        with _quiet():
            base_px = build(BASE_ASSETS, basis, tax_bps)
            print(f"  基准篮子 {len(base_px)} 个交易日  {base_px.index[0].date()} → {base_px.index[-1].date()}")

            raw: list[tuple[str, pd.DataFrame, bool]] = [("基准 4 只", base_px, False)]
            for name, sina, code, is_stock in candidates:
                swap_basket = [a if a[0] != "可转债ETF博时" else (name, sina, code, is_stock)
                               for a in BASE_ASSETS]
                raw.append((f"换股 {name}", build(swap_basket, basis, tax_bps), is_stock))
                raw.append((f"加股 {name}",
                            build(BASE_ASSETS + [(name, sina, code, is_stock)], basis, tax_bps),
                            is_stock))

        # 所有篮子切到同一组交易日 (个股有停牌)
        common = base_px.index
        for _l, px, _s in raw:
            common = common.intersection(px.index)
        common = common.sort_values()
        baskets = [(label, px.loc[common], st) for label, px, st in raw]
        print(f"  公共交易日 {len(common)} 天 (因停牌剔除 {len(base_px) - len(common)} 天)")

        results: dict[str, dict] = {}
        for label, px, has_stock in baskets:
            results[label.replace(" ", "")] = run(px, has_stock=has_stock)
        base = results["基准4只"]
        base_rets = base["series"].pct_change().dropna()
        yb = yearly(base["series"])

        print("\n" + "=" * 118)
        print("A. 结果总表 —— 全部 max_dev@3 (生产口径), 佣金 3bp/边 + ¥5 最低 + 个股卖出印花税 5bp")
        print("=" * 118)
        print(f"  {'篮子':<20} {'期末市值':>12} {'累计收益':>9} {'CAGR':>8} {'MaxDD':>9} "
              f"{'Sharpe':>7} {'年化波动':>9} {'调仓':>6} {'费用¥':>10}")
        for label, _px, _st in baskets:
            r = results[label.replace(" ", "")]
            vol = r["series"].pct_change().dropna().std() * (252 ** 0.5)
            print(f"  {label:<20} {r['final']:>12,.0f} {(r['final'] / INITIAL_CAP - 1) * 100:>8.2f}% "
                  f"{r['cagr'] * 100:>7.2f}% {r['max_dd'] * 100:>8.2f}% {r['sharpe']:>7.2f} "
                  f"{vol * 100:>8.2f}% {r['n_rebalances']:>6} {r['fees']:>10,.0f}")

        print("\n" + "=" * 118)
        print("B. vs 基准 —— 配对 t 检验 (日收益) + 年度符号检验")
        print("=" * 118)
        for name, *_ in candidates:
            for tag in ("换股", "加股"):
                r = results[f"{tag}{name}"]
                rets = r["series"].pct_change().dropna()
                t, p = stats.ttest_rel(rets, base_rets)
                yc = yearly(r["series"])
                diffs = [yc[y] - yb[y] for y in sorted(yb)]
                wins = sum(1 for d in diffs if d > 0)
                p_sign = 2 * sum(stats.binom.pmf(k, len(diffs), 0.5)
                                 for k in range(wins, len(diffs) + 1))
                print(f"  {tag} {name:<10} 期末 Δ ¥{r['final'] - base['final']:>+9,.0f} "
                      f"({(r['final'] / base['final'] - 1) * 100:+.2f}%)  "
                      f"CAGR Δ {(r['cagr'] - base['cagr']) * 100:+.2f}pp  "
                      f"MaxDD Δ {(r['max_dd'] - base['max_dd']) * 100:+.2f}pp  "
                      f"t {t:+.2f} (p {p:.3f})  年度 {wins}/{len(diffs)} 赢 (符号 p {p_sign:.3f})")

        if basis != "total_return":
            continue

        # ---- 以下细节只用「修正口径」讲 ----
        print("\n" + "=" * 118)
        print("C. 单只资产自己的风险 (它要占组合 1/4) + 分红/除息口径")
        print("=" * 118)
        single_asset_stats(base_px["可转债ETF博时"], "可转债ETF博时")
        for name, _sina, code, is_stock in candidates:
            if not is_stock:
                continue
            px = next(p for l, p, _s in baskets if l == f"换股 {name}")
            single_asset_stats(px[name], name)
            print(f"      └{dividend_audit(code, name)}")

        print("\n" + "=" * 118)
        print("D. 相关性 —— 篮子里原本**没有 A 股**")
        print("=" * 118)
        smap = {c: base_px[c] for c in base_px.columns}
        for name, *_ in candidates:
            px = next(p for l, p, _s in baskets if l == f"换股 {name}")
            smap[name] = px[name]
        corr = pd.DataFrame(smap).loc[common].pct_change().dropna().corr()
        print(f"  {'':<16}" + "".join(f"{c[:10]:>13}" for c in corr.columns))
        for c in corr.columns:
            print(f"  {c:<16}" + "".join(f"{corr.loc[c, o]:>13.2f}" for o in corr.columns))

        print("\n" + "=" * 118)
        print("E. 成本结构 —— 印花税是换股后多出来的")
        print("=" * 118)
        for label, _px, _st in baskets:
            r = results[label.replace(" ", "")]
            sells = [l for rb in r["rebalances"] for l in rb["legs"] if l["side"] == "sell"]
            sv = sum(l["value"] for l in sells)
            tax = sv * STAMP_DUTY_BPS / 10000
            print(f"  {label:<20} 卖出腿 {len(sells):>4} 条  卖出额 ¥{sv:>12,.0f}  "
                  f"印花税(5bp) ¥{tax:>8,.0f}  总费用 ¥{r['fees']:>8,.0f}  "
                  f"每 {len(common) / max(r['n_rebalances'], 1):>5.1f} 个交易日调一次")

        print("\n" + "=" * 118)
        print("F. 个股被卖过几次 —— 决定股息税落在哪一档 (≤1月 20% / 1月~1年 10% / >1年 0%)")
        print("=" * 118)
        for name, *_ in candidates:
            r = results[f"换股{name}"]
            sells = candidate_sells(r, name)
            print(f"  {name:<12} 被卖 {len(sells):>2} 次" +
                  (f"  日期 {', '.join(sells)}" if sells else "  (整个期间一次没卖 ⇒ 持股 >1 年 ⇒ 股息税 0)"))

        print("\n" + "=" * 118)
        print("G. 再平衡有没有加分 (换股篮子 vs 买入持有)")
        print("=" * 118)
        for label, px, _st in baskets:
            r = results[label.replace(" ", "")]
            bh = buy_and_hold(px)
            print(f"  {label:<20} 再平衡 ¥{r['final']:>10,.0f} ({r['max_dd'] * 100:>6.2f}%)   "
                  f"买入持有 ¥{bh['final']:>10,.0f} ({bh['max_dd'] * 100:>6.2f}%)   "
                  f"Δ ¥{r['final'] - bh['final']:>+9,.0f}")

        print("\n" + "=" * 118)
        print("H. 分期 —— 优势是均匀的, 还是压在某一段?")
        print("=" * 118)
        for tag, d0, d1 in (("2020-11~2023-06", "2020-11-05", "2023-06-30"),
                            ("2023-07~2026-11", "2023-07-01", "2026-11-04")):
            print(f"  {tag}:")
            for name, *_ in candidates:
                seg = results[f"换股{name}"]["series"].loc[d0:d1]
                seg_b = base["series"].loc[d0:d1].reindex(seg.index).ffill()
                print(f"    换股 {name:<12} 自身 {((seg.iloc[-1] / seg.iloc[0]) - 1) * 100:+7.2f}%"
                      f"   基准 {((seg_b.iloc[-1] / seg_b.iloc[0]) - 1) * 100:+7.2f}%"
                      f"   Δ {((seg.iloc[-1] / seg.iloc[0]) / (seg_b.iloc[-1] / seg_b.iloc[0]) - 1) * 100:+7.2f}%")


if __name__ == "__main__":
    main()
