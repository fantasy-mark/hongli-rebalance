"""ETF price fetch: realtime tick (Eastmoney universal quote) → historical fallback.

All historical close prices use **前复权 (qfq / forward-adjusted)** so the
chart is continuous across dividends / splits — the most recent close is
the truth, and older prices are reduced by future distributions.  This
matches what brokers (同花顺/通达信) display in their default view and
keeps the 收益追踪 chart free of artificial ex-date drops.

Spot tier order (latest → most stale):
  1. Eastmoney `push2.eastmoney.com/api/qt/stock/get` — covers ALL
     on-exchange securities (stocks, ETFs, bond ETFs like 511380).
     Field `f43` is the current price (× 1000). **Unadjusted tick**
     (representative of "right now"; no apply of historical dividends).
  2. Eastmoney `ak.fund_etf_spot_em` — rich spot table but only
     for stock/currency ETFs; does NOT include 511380. **Unadjusted**.
  3. Eastmoney `ak.fund_etf_hist_em` with `adjust="qfq"` — daily closes,
     **前复权**, 全历史 (2010 起)。**日线的首选源**。
  4. Tencent `fqkline` (…,qfq) — 也是前复权, 但**只有最近 ~640 根**日线
     (实测: count 参数再大也只给 641 根; 带日期区间直接 "param error")。
  5. Sina daily-close `ak.fund_etf_hist_sina` — **不复权**!  只当最后兜底
     (Sina 接口没有 adjust 参数), 调用方必须知道口径跟别的源不同。

⚠️ **两条历史序列, 两个口径, 别混用** (2026-11-04 修):

    | 函数                     | 口径     | 用途                             |
    |--------------------------|----------|----------------------------------|
    | `fetch_qfq_history`      | 前复权   | 收益率 / 图表 / 多年回测          |
    | `fetch_stock_qfq_history`| 前复权   | **个股**日线 (ETF 那条路对个股无效)|
    | `fetch_traded_close`     | 不复权   | 账本成本基准 (用户当天**付的**价) |
    | `latest_for_code` (实时) | 不复权   | 今天的市值 (真实成交价)           |

⚠️⚠️ **「前复权」不等于「总收益指数」—— 拿它算收益率会虚高** (2026-11-04 实测):

    东财的 qfq 其实是**加法减分红**:  qfq_t = 不复权价_t − (t 之后还没发的那几笔分红)。
    拿它的**比值**当收益率 ⇒ 虚高。实测 515180 红利ETF 2020-11 → 2026-11:

        东财 qfq         +77.63%
        自算总收益       +59.40%
        官方累计净值     +56.97%   ← 权威 (ak.fund_open_fund_info_em)
        不复权 (分红全丢) +23.39%

    ⇒ **分红标的用东财 qfq 算收益, 六年虚高 20.7pp**。
    ⇒ 黄金/可转债/纳指那三只期间**不发分红**, 所以这个坑一直没暴露 ——
      篮子里唯一暴露的是 515180 红利ETF。
    ⇒ **要算收益率就用真总收益序列**:
        `fetch_stock_total_return_history` (个股, 带股息税)
        `fetch_etf_total_return_history`  (ETF, 个人分红免税)

⚠️ **东财的个股 qfq 可能是坏的** —— 用之前先审计 (`audit_stock_qfq`):
    实测 601088 中国神华: 非除息日里 qfq 日收益与不复权日收益差 >1pp 的有 538 天,
    最大 38.67pp, 还出现过 **+48.66%** 的单日「涨幅」(主板涨停才 10%)。
    600900 / 000887 / 601398 同一项检查是 0.10~0.21pp 均值, 干净。
    ⇒ **别无条件信, 审计不过就剔除** (静默用垃圾数据比没有数据更糟)。

    为什么要分开: 多年回测里用不复权 = 把分红全丢了 —— 红利 ETF 六年累计
    分红让 CAGR 从 **11.95% (qfq)** 掉到 **10.03% (不复权)**。图表的
    「3pp 再平衡」曲线正是拿这条线画的, 之前 `fetch_one` 是「新浪优先」
    → 那条曲线悄悄走了不复权 → 同一张图上, 每日快照 (实时价) 与图表曲线
    两套口径, 数字对不上。

    反过来, 账本的成本基准必须用**不复权**: 用户当天付的就是那个价,
    用 qfq 会把「之后」的分红倒扣回那天 (红利 ETF 2026-10-16 除息,
    10-12 的 qfq 价比当天真实成交价低 ¥0.061/份), 成本就错了。

Note: `ak.fund_etf_hist_sina` is a daily-close endpoint that only
refreshes after the market closes, so an Action triggered at 9:00
Beijing time (before open) would always see the previous trading day's
close.  The spot endpoints give the freshest tick available at any time.
"""
from __future__ import annotations

import datetime
import logging
import warnings
from typing import Optional, Tuple

import akshare as ak
import pandas as pd

from ..domain.shared.money import Price

try:
    import requests
except ImportError:                                  # pragma: no cover
    requests = None                                  # type: ignore

log = logging.getLogger("fetch")
warnings.filterwarnings("ignore")
for _n in ("akshare", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.ERROR)


def _sina(sina: str) -> Optional[pd.DataFrame]:
    """Try Sina ETF history (**不复权**). Returns DataFrame with date index
    & 'close' column or None.

    ⚠️ **不复权** —— 这条线跟 qfq 不是一回事: 除息日之后, 新浪的价格是
    「除息后的真实成交价」, 而 qfq 会把分红倒扣回历史。**多年回测/图表
    不能用它** (红利 ETF 六年累计分红让 CAGR 从 11.95% 掉到 10.03%)。

    只在两个地方用:
      1. qfq 两个源都挂了时的**最后兜底** (调用方要警告);
      2. `fetch_traded_close` —— 账本成本基准要的就是「当天真实成交价」。
    """
    for attempt in range(3):
        try:
            raw = ak.fund_etf_hist_sina(symbol=sina)
            if raw is not None and not raw.empty:
                raw["date"] = pd.to_datetime(raw["date"])
                raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
                df = raw.set_index("date").sort_index()[["close"]]
                return df[~df["close"].isna()]
        except Exception as ex:
            log.warning("Sina %s attempt %d failed: %s", sina, attempt + 1, ex)
    return None


def _eastmoney(eastmoney: str, adjust: str = "qfq") -> Optional[pd.DataFrame]:
    """Eastmoney daily history. Returns 前复权 (qfq) or 不复权 ("") series.

    Args:
        eastmoney: 6 位代码 (如 "515180")。
        adjust: "qfq" = 前复权 (收益率/图表用); "" = 不复权
            (当天真实成交价, 账本成本基准用)。
    """
    try:
        raw = ak.fund_etf_hist_em(
            symbol=eastmoney, period="daily",
            start_date="20100101", end_date="20991231", adjust=adjust,
        )
        if raw is not None and not raw.empty:
            raw["日期"] = pd.to_datetime(raw["日期"])
            raw["收盘"] = pd.to_numeric(raw["收盘"], errors="coerce")
            df = raw.set_index("日期").sort_index()[["收盘"]].rename(columns={"收盘": "close"})
            return df[~df["close"].isna()]
    except Exception as ex:
        log.error("Eastmoney %s failed (adjust=%r): %s", eastmoney, adjust, ex)
    return None


def _tencent_qfq(sina: str) -> Optional[pd.DataFrame]:
    """腾讯 qfq 日线 (只要 close) —— 口径跟东财 qfq 一致。

    ⚠️ 长度限制: 腾讯 `fqkline` 的日线**最多给 ~640 根** (2026-11 实测:
    count 填 1000 / 1600 / 2000 都只给 641 根; 带日期区间会直接
    "param error")。够画近两年, 不够画六年 —— 所以它是**第二顺位**。

    Args:
        sina: 新浪符号 (如 "sh515180") —— 只用它取 6 位代码。
    """
    code6 = str(sina)[-6:]
    df = fetch_kline_history(code6)
    if df is None or df.empty:
        return None
    return df[["close"]]


#: 价格源的名字 —— 图表要把「这次到底用的哪个源」写在脸上。
SOURCE_EASTMONEY_QFQ = "eastmoney_qfq"
SOURCE_TENCENT_QFQ = "tencent_qfq"
SOURCE_SINA_UNADJUSTED = "sina_unadjusted"
SOURCE_EASTMONEY_STOCK_QFQ = "eastmoney_stock_qfq"
SOURCE_STOCK_TOTAL_RETURN = "stock_total_return_own"
SOURCE_FUND_TOTAL_RETURN = "fund_total_return_own"


def _fetch_qfq(sina: str, eastmoney: str) -> Tuple[Optional[pd.DataFrame], str]:
    """按 **前复权** 口径取日线。返回 (df, 源名)。

    顺序: 东财 qfq (全历史) → 腾讯 qfq (~640 根) → 新浪 (**不复权**, 兜底)。
    """
    if eastmoney:
        df = _eastmoney(eastmoney, adjust="qfq")
        if df is not None and not df.empty:
            log.info("价格源: 东财 qfq (%s), %d 根", eastmoney, len(df))
            return df, SOURCE_EASTMONEY_QFQ
    df = _tencent_qfq(sina)
    if df is not None and not df.empty:
        log.info("价格源: 腾讯 qfq (%s), %d 根 (窗口比平时短)", sina, len(df))
        return df, SOURCE_TENCENT_QFQ
    df = _sina(sina)
    if df is not None and not df.empty:
        # 最后兜底 —— 口径**跟别处不一样**, 调用方要负责警告。
        log.warning("价格源: 新浪 **不复权** (%s) —— 口径与 qfq 不一致!", sina)
        return df, SOURCE_SINA_UNADJUSTED
    return None, "none"


def fetch_qfq_history(sina: str, eastmoney: str = "",
                      start: str = "", end: str = "") -> pd.DataFrame:
    """Daily close series — **前复权 (qfq)**, 收益率/图表/回测用这一条。

    ⚠️ 要的是「那天真实成交价」(账本成本基准)? 用 `fetch_traded_close`。

    Args:
        sina: e.g. "sh515180"
        eastmoney: e.g. "515180" (可选, 但没有它就拿不到全历史 qfq)
        start: optional ISO date filter (e.g. "2020-11-05"); inclusive
        end: optional ISO date filter (e.g. "2026-10-13"); inclusive

    Returns:
        DataFrame indexed by date with single 'close' column.
    """
    df, _source = fetch_qfq_history_ex(sina, eastmoney, start, end)
    return df


def fetch_qfq_history_ex(sina: str, eastmoney: str = "",
                         start: str = "", end: str = ""
                         ) -> Tuple[pd.DataFrame, str]:
    """跟 `fetch_qfq_history` 一样, 但**同时返回用了哪个源**。

    图表要靠这个把「这条线不是 qfq」写在脸上 —— 静默的口径不一致比
    没有图更糟。
    """
    df, source = _fetch_qfq(sina, eastmoney)
    if df is None:
        raise RuntimeError(f"no price data for {sina}")
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    return df, source


def fetch_stock_qfq_history(code: str, start: str = "", end: str = "",
                           ) -> Tuple[pd.DataFrame, str]:
    """**个股**日线 (前复权) —— 东财 `stock_zh_a_hist`。

    ⚠️ ETF 那条线 (`ak.fund_etf_hist_em`) 对个股无效, 个股要用这个接口。

    口径与 ETF 的 qfq **完全一致** (东财前复权): 最新价 = 真实收盘价,
    历史价扣掉之后的分红送转 ⇒ 收益率序列可以跟 ETF 直接拼在一张表里比。

    Args:
        code: 6 位代码 (如 "000887" = 中鼎股份)。
        start / end: 可选 ISO 日期过滤 (含端点)。

    Returns:
        (DataFrame indexed by date with 'close' column, 源名)
    """
    try:
        raw = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date="19900101", end_date="20991231", adjust="qfq",
        )
        if raw is not None and not raw.empty:
            raw["日期"] = pd.to_datetime(raw["日期"])
            raw["收盘"] = pd.to_numeric(raw["收盘"], errors="coerce")
            df = (raw.set_index("日期").sort_index()[["收盘"]]
                    .rename(columns={"收盘": "close"}))
            df = df[~df["close"].isna()]
            if start:
                df = df[df.index >= pd.Timestamp(start)]
            if end:
                df = df[df.index <= pd.Timestamp(end)]
            if not df.empty:
                log.info("价格源: 东财个股 qfq (%s), %d 根", code, len(df))
                return df, SOURCE_EASTMONEY_STOCK_QFQ
    except Exception as ex:
        log.error("Eastmoney stock %s failed: %s", code, ex)
    raise RuntimeError(f"no price data for stock {code}")


def _stock_dividend_events(code: str) -> pd.DataFrame:
    """个股的分红送转事件 —— (除权除息日, 每股现金, 每股送转股比)。

    `派息` 是「每 10 股派 X 元」; `送股` / `转增` 同为「每 10 股 X 股」。
    只保留**已实施**的行 (预案/通过的行还没除权, 不能算进收益)。
    """
    raw = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    raw = raw[raw["进度"] == "实施"].copy()
    raw["除权除息日"] = pd.to_datetime(raw["除权除息日"])
    raw = raw[raw["除权除息日"].notna()].sort_values("除权除息日")
    out = pd.DataFrame({
        "ex_date": raw["除权除息日"].values,
        "cash": pd.to_numeric(raw["派息"], errors="coerce").fillna(0.0).values / 10.0,
        "bonus": ((pd.to_numeric(raw["送股"], errors="coerce").fillna(0.0).values
                   + pd.to_numeric(raw["转增"], errors="coerce").fillna(0.0).values)
                  / 10.0),
    })
    return out.reset_index(drop=True)


def fetch_stock_total_return_history(
    code: str, start: str = "", end: str = "",
    dividend_tax_bps: float = 0.0,
) -> Tuple[pd.DataFrame, str]:
    """**自算**个股的总收益序列 (前复权口径) —— 不复权价 × 分红再投。

    ⚠️ **为什么不用东财的 qfq**: 东财 qfq **对某些股票是坏的**。
      实测 601088 (中国神华): 非除息日里 qfq 日收益与不复权日收益差 >1pp 的
      有 **243 天**, 最大差 **21.63pp**, 还出现过 **+48.66%** 的单日「涨幅」
      —— 主板涨停才 10%, 只能是数据错。
      同一项检查: 600900 / 000887 / 601398 是 0.10~0.21pp 均值, 干净。
      ⇒ **东财 qfq 不能无条件信** —— 要么自算, 要么先审计 (见 `audit_stock_qfq`)。

    算法 (标准总收益指数):
        股数在每个除息日变化:
            shares ← shares × (1 + bonus)          # 送转: 份额变多, 价值不变
                    + shares × cash × (1−tax) / P_ex   # 现金分红按除息价再投
        每日组合价值 = shares × P_t
        最后按 qfq 惯例归一化: 末值 = 真实的末日收盘价 ⇒ 可与别的源直接拼接。

    Args:
        code: 6 位代码。
        start / end: 可选 ISO 日期过滤 (含端点)。
        dividend_tax_bps: 股息税 (万分之几)。个人投资者: 持有 ≤1 月 20%,
            1 月~1 年 10%, >1 年 0%。**再平衡会卖旧买新 ⇒ 持股时钟重置**
            ⇒ 长期落在大多落在 10% 档 ⇒ 传 100 就是 10%。

    Returns:
        (DataFrame indexed by date with 'close' column, 源名)
    """
    raw = ak.stock_zh_a_hist(symbol=code, period="daily",
                             start_date="19900101", end_date="20991231", adjust="")
    if raw is None or raw.empty:
        raise RuntimeError(f"no unadjusted price data for stock {code}")
    raw["日期"] = pd.to_datetime(raw["日期"])
    raw["收盘"] = pd.to_numeric(raw["收盘"], errors="coerce")
    px = (raw.set_index("日期").sort_index()["收盘"])
    px = px[~px.isna()]
    if px.empty:
        raise RuntimeError(f"no unadjusted price data for stock {code}")

    tax = float(dividend_tax_bps) / 10000.0
    events = _stock_dividend_events(code)
    shares = 1.0
    factor = pd.Series(1.0, index=px.index)      # 累计份额倍数
    for _i, ev in events.iterrows():
        # 除息日若不是交易日 (停牌), 用**之后**第一个有成交的日子
        pos = px.index.searchsorted(ev["ex_date"])
        if pos >= len(px):
            continue
        ex_idx = px.index[pos]
        p_ex = float(px.iloc[pos])
        if p_ex <= 0:
            continue
        s = factor.iloc[pos - 1] if pos > 0 else 1.0
        factor.iloc[pos:] = (s * (1.0 + float(ev["bonus"]))
                             + s * float(ev["cash"]) * (1.0 - tax) / p_ex)
    series = factor * px
    # qfq 归一化: 末日 = 真实收盘价
    series = series * (float(px.iloc[-1]) / float(series.iloc[-1]))
    df = pd.DataFrame({"close": series})
    df.index.name = "date"
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    log.info("价格源: 自算个股总收益 (%s, 股息税 %.2f%%), %d 根",
             code, tax * 100, len(df))
    return df, SOURCE_STOCK_TOTAL_RETURN


def audit_stock_qfq(code: str, start: str = "", end: str = "",
                    tol_pp: float = 0.5) -> dict:
    """审计东财 qfq 序列**能不能信** —— 拿不复权日收益做对照。

    原理: **除息日以外**, qfq 的日收益必须跟不复权日收益几乎一样
    (前复权只是把过去的价按比例调低, 不改「每天涨多少」)。
    所以拿分红送转表 (权威) 标出除息日, 其余日子逐一比对。

    Returns:
        {"ok": bool, "n_days": int, "mean_err_pp": float, "max_err_pp": float,
         "n_bad_days": int, "worst_date": str, "worst_qfq_ret": float,
         "worst_unadj_ret": float}
    """
    qdf, _ = fetch_stock_qfq_history(code, start=start, end=end)
    raw = ak.stock_zh_a_hist(symbol=code, period="daily",
                             start_date="19900101", end_date="20991231", adjust="")
    raw["日期"] = pd.to_datetime(raw["日期"])
    raw["收盘"] = pd.to_numeric(raw["收盘"], errors="coerce")
    unadj = raw.set_index("日期").sort_index()["收盘"]
    unadj = unadj[~unadj.isna()]

    df = pd.DataFrame({"q": qdf["close"], "u": unadj}).dropna()
    df["rq"] = df["q"].pct_change()
    df["ru"] = df["u"].pct_change()
    df = df.dropna()
    if df.empty:
        return {"ok": False, "n_days": 0, "mean_err_pp": 0.0, "max_err_pp": 0.0,
                "n_bad_days": 0, "worst_date": "", "worst_qfq_ret": 0.0,
                "worst_unadj_ret": 0.0}

    # 除息日 (权威来源: 分红送转表)
    ex_dates: set = set()
    for _i, ev in _stock_dividend_events(code).iterrows():
        pos = df.index.searchsorted(ev["ex_date"])
        if pos < len(df):
            ex_dates.add(df.index[pos])
    normal = df.drop(index=[d for d in ex_dates if d in df.index])
    err = (normal["rq"] - normal["ru"]).abs() * 100.0
    if err.empty:
        return {"ok": False, "n_days": len(df), "mean_err_pp": 0.0,
                "max_err_pp": 0.0, "n_bad_days": 0, "worst_date": "",
                "worst_qfq_ret": 0.0, "worst_unadj_ret": 0.0}
    worst = err.idxmax()
    return {
        "ok": bool(err.max() <= tol_pp),
        "n_days": len(df),
        "n_exdiv_days": len(ex_dates),
        "mean_err_pp": float(err.mean()),
        "max_err_pp": float(err.max()),
        "n_bad_days": int((err > tol_pp).sum()),
        "worst_date": worst.date().isoformat() if hasattr(worst, "date") else str(worst),
        "worst_qfq_ret": float(normal.loc[worst, "rq"]),
        "worst_unadj_ret": float(normal.loc[worst, "ru"]),
    }


#: 全市场基金分红表缓存 —— `ak.fund_fh_em()` 要翻 ~74 页, 别重复打。
_FUND_FH_CACHE: dict = {}


def fund_dividends(code: str, start_year: int = 2010,
                   end_year: int = 2100) -> pd.DataFrame:
    """ETF / 基金的分红记录 —— (除息日期, 每股分红)。

    `ak.fund_fh_em()` 一次拉**全市场全年份**的分红表 (~74 页, 十几秒),
    比逐年翻页快得多 —— 只按年翻会把 91 个年度请求全打一遍。

    ⚠️ **ETF 分红个人不收股息税** (基金层面已扣) —— 跟 A 股个股不一样。
    """
    if "all" not in _FUND_FH_CACHE:
        try:
            _FUND_FH_CACHE["all"] = ak.fund_fh_em()
        except Exception as ex:                          # pragma: no cover
            log.warning("fund_fh_em failed: %s", ex)
            _FUND_FH_CACHE["all"] = pd.DataFrame()
    df = _FUND_FH_CACHE["all"]
    if df is None or df.empty:
        return pd.DataFrame(columns=["ex_date", "cash"])
    rows = []
    for _i, r in df[df["基金代码"] == code].iterrows():
        d = pd.to_datetime(r["除息日期"])
        if pd.isna(d) or not (start_year <= d.year <= end_year):
            continue
        rows.append({"ex_date": d, "cash": float(r["分红"])})
    out = pd.DataFrame(rows, columns=["ex_date", "cash"])
    return out.sort_values("ex_date").reset_index(drop=True)


def _total_return_series(px: pd.Series, events: pd.DataFrame,
                          tax: float) -> pd.Series:
    """总收益指数 = 不复权价 × 累计份额倍数 (分红按除息日收盘价再投)。"""
    factor = pd.Series(1.0, index=px.index)
    if events is not None and not events.empty:
        for _i, ev in events.iterrows():
            pos = px.index.searchsorted(ev["ex_date"])
            if pos >= len(px):
                continue
            p_ex = float(px.iloc[pos])
            if p_ex <= 0:
                continue
            s = factor.iloc[pos - 1] if pos > 0 else 1.0
            bonus = float(ev.get("bonus", 0.0) or 0.0)
            cash = float(ev.get("cash", 0.0) or 0.0)
            factor.iloc[pos:] = (s * (1.0 + bonus)
                                 + s * cash * (1.0 - tax) / p_ex)
    return factor * px


def fetch_etf_total_return_history(
    code: str, sina: str = "", start: str = "", end: str = "",
) -> Tuple[pd.DataFrame, str]:
    """**自算**ETF 的总收益序列 (前复权口径) —— 不复权价 × 分红再投。

    ⚠️ **为什么不能用东财的 qfq 当收益率序列**: 东财的「前复权」其实是
      **加法减分红**口径:  qfq_t = 不复权价_t − (t 之后还没发的那几笔分红)。
      拿它算收益会**虚高** —— 实测 515180 红利ETF 2020-11→2026-11:
          东财 qfq        +77.63%
          自算总收益      +59.40%
          官方累计净值    +56.97%   ← 权威
          不复权          +23.39%
      ⇒ **分红标的用东财 qfq 算收益, 六年虚高 20.7pp**。
      (黄金/可转债/纳指那三只期间不发分红, 所以这个坑一直没暴露。)

    Returns:
        (DataFrame indexed by date with 'close' column, 源名)
    """
    raw = ak.fund_etf_hist_sina(symbol=sina) if sina else None
    if raw is None or raw.empty:
        raise RuntimeError(f"no unadjusted ETF price data for {code}")
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    px = raw.set_index("date").sort_index()["close"]
    px = px[~px.isna()]
    if px.empty:
        raise RuntimeError(f"no unadjusted ETF price data for {code}")

    divs = fund_dividends(code)
    if not divs.empty:
        divs["ex_date"] = pd.to_datetime(divs["ex_date"])
        divs = divs[divs["ex_date"].isin(px.index)
                    | (divs["ex_date"] <= px.index[-1])]
    series = _total_return_series(px, divs, tax=0.0)   # ETF 分红个人免税
    series = series * (float(px.iloc[-1]) / float(series.iloc[-1]))
    df = pd.DataFrame({"close": series})
    df.index.name = "date"
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    log.info("价格源: 自算 ETF 总收益 (%s), %d 根", code, len(df))
    return df, SOURCE_FUND_TOTAL_RETURN


def fetch_traded_close(code: str, date: str) -> float:
    """某个交易日的**真实成交价** (不复权) —— 账本的成本基准用它。

    ⚠️ **不要用 qfq**: qfq 会把「那天之后」的分红倒扣回历史价 —— 用户
    当天付的不是那个数。红利 ETF 2026-10-16 除息: 10-12 的 qfq 价
    ¥1.385, 当天真实成交价 ¥1.446 (差 ¥0.061/份 = 4.4%)。

    Args:
        code: 6 位代码 (如 "515180")。
        date: "YYYY-MM-DD"。

    Raises:
        RuntimeError: 拿不到那天的价格。
    """
    df = _eastmoney(code, adjust="")
    if df is None or df.empty:
        from_asset = _sina(_sina_symbol_for(code))
        df = from_asset
    if df is None or df.empty:
        raise RuntimeError(f"no price data for {code} on {date}")
    day = df[df.index == pd.Timestamp(date)]
    if day.empty:
        raise RuntimeError(f"no price for {code} on {date} "
                           f"(last available {df.index[-1].date()})")
    return float(day["close"].iloc[-1])


def _sina_symbol_for(code: str) -> str:
    """6 位代码 → 新浪符号 ("515180" → "sh515180")。"""
    c = str(code).strip().zfill(6)
    prefix = "sh" if c[0] in ("5", "6", "9") else "sz"
    return prefix + c


# ---------------------------------------------------------------------
# Realtime tick via Eastmoney's universal quote API
# ---------------------------------------------------------------------

def _market_for_code(code: str) -> str:
    """Eastmoney secid prefix: '1' = Shanghai, '0' = Shenzhen.

    5xxxxx, 6xxxxx, 9xxxxx → 上交所;  0xxxxx, 2xxxxx, 3xxxxx, 1xxxxx → 深交所.
    (Bond ETFs like 511380 fall under Shanghai.)
    """
    c = str(code).strip().zfill(6)
    if c[0] in ("5", "6", "9"):
        return "1"
    return "0"


def _eastmoney_universal_quote(code: str) -> Optional[Tuple[float, pd.Timestamp]]:
    """Realtime tick for ANY on-exchange security.

    Endpoint: `push2.eastmoney.com/api/qt/stock/get` with
    `secid={market}.{code}` and `fields=f43,f44,f45,f46,f60,f170`.

    Returns (price, today) or None.  For ETF-class products
    (including 511380 可转债 ETF) `f43` is already in yuan; for
    A-shares it is in 厘 (1/1000 yuan) — we detect via the `fltt`
    echo in the response to handle both correctly.
    """
    if not code or requests is None:
        return None
    market = _market_for_code(code)
    secid = f"{market}.{code}"
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f43,f44,f45,f46,f60,f170",   # price, high, low, open, prev_close, change_pct
        "invt": 2,
        "fltt": 2,
    }
    try:
        r = requests.get(
            url, params=params, timeout=5,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return None
        j = r.json()
        data = (j or {}).get("data") or {}
        raw = data.get("f43")
        if raw is None or raw == "-":
            return None
        # `data["f59"]` is the decimal-precision code: 1→1, 2→2, 3→3 ...
        # `f43` is scaled as price * 10 ** (3 - f59) when f59 is set,
        # but the simpler rule verified empirically for ETFs is:
        #   - codes starting with 5/6/1/9 (incl. bond ETFs like 511380)
        #     return f43 in yuan directly
        #   - A-share codes starting with 0/2/3 return f43 in 厘 (×1000)
        scale = 1.0 if str(code).zfill(6)[0] in ("5", "6", "1", "9") else 1000.0
        price = float(raw) / scale
        return price, pd.Timestamp.now().normalize()
    except Exception as ex:
        log.warning("Eastmoney universal quote %s failed: %s", code, ex)
        return None


def _tencent_quote(code: str) -> Optional[Tuple[float, pd.Timestamp]]:
    """Tencent `qt.gtimg.cn` fallback — used when Eastmoney push2 is throttled.

    Returns (price, today) or None.  Works for any on-exchange ETF/股票
    and is generally more available than push2.eastmoney.com from CN ISPs.
    """
    if not code or requests is None:
        return None
    code6 = str(code).zfill(6)
    prefix = "sh" if code6[0] in ("5", "6", "9") else "sz"
    url = f"https://qt.gtimg.cn/q={prefix}{code6}"
    try:
        r = requests.get(
            url, timeout=5,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        if r.status_code != 200 or b"~" not in r.content:
            return None
        parts = r.content.decode("gbk", errors="ignore").split("~")
        if len(parts) < 6:
            return None
        raw_p = parts[3]
        if not raw_p:
            return None
        return float(raw_p), pd.Timestamp.now().normalize()
    except Exception as ex:
        log.warning("Tencent quote %s failed: %s", code6, ex)
        return None


def _eastmoney_etf_spot(code: str) -> Optional[Tuple[float, pd.Timestamp]]:
    """Realtime tick via `ak.fund_etf_spot_em` (东财 ETF 实时行情).

    NOTE: this endpoint does NOT include bond ETFs (e.g. 511380 可转债ETF博时).
    Use `_eastmoney_universal_quote()` for those.
    """
    if not code:
        return None
    try:
        raw = ak.fund_etf_spot_em()
        if raw is None or raw.empty:
            return None
        raw["_code"] = raw["代码"].astype(str).str.zfill(6)
        match = raw[raw["_code"] == str(code).zfill(6)]
        if match.empty:
            return None
        price = float(match.iloc[0]["最新价"])
        return price, pd.Timestamp.now().normalize()
    except Exception as ex:
        log.warning("Eastmoney ETF spot %s failed: %s", code, ex)
        return None


def latest_history(sina: str, eastmoney: str = "") -> Tuple[float, pd.Timestamp]:
    """Most recent close price from the **qfq** historical series.

    ⚠️ qfq 的**最后一根** = 当天真实收盘价 (前复权就是拿今天当锚), 所以
    拿它当「最近收盘价」没问题。
    """
    df = fetch_qfq_history(sina, eastmoney)
    return float(df.iloc[-1]["close"]), df.index[-1]


# ---------------------------------------------------------------------
# QDII premium rate (溢价率): T-1 NAV vs market price
# ---------------------------------------------------------------------

def fetch_qdii_premium(code: str) -> Optional[dict]:
    """For QDII ETFs (e.g. 513300), compute (price - IOPV) / IOPV.

    Two sources cross-checked:
      - T-1 unit NAV from `api.fund.eastmoney.com/f10/lsjz` (authoritative)
      - Realtime market price from `qt.gtimg.cn` (Tencent, robust to push2 limit)

    Returns dict {price, iopv, premium_pct, nav_date, quote_time, source}
    or None on failure.  QDII IOPV is published ~22:30 Beijing after US
    close, so intraday the "IOPV" we report is the T-1 NAV (the latest
    authoritative reference NAV for today's trading).  The premium is
    always measured against that reference.
    """
    if not code or requests is None:
        return None
    code6 = str(code).strip().zfill(6)

    # 1) T-1 NAV
    nav, nav_date = None, None
    try:
        r = requests.get(
            "https://api.fund.eastmoney.com/f10/lsjz",
            params={"fundCode": code6, "pageIndex": 1, "pageSize": 1},
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"},
        )
        if r.status_code == 200:
            lst = (r.json().get("Data") or {}).get("LSJZList") or []
            if lst:
                nav = float(lst[0]["DWJZ"])
                nav_date = lst[0]["FSRQ"]
    except Exception as ex:
        log.warning("QDII NAV fetch %s failed: %s", code6, ex)

    # 2) Realtime price from Tencent (push2 / qt.gtimg)
    price, quote_time = None, None
    for url in [
        f"https://qt.gtimg.cn/q={('sh' if code6[0] in ('5','6','9') else 'sz')}{code6}",
    ]:
        try:
            r = requests.get(
                url, timeout=5,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            )
            if r.status_code == 200 and b"~" in r.content:
                parts = r.content.decode("gbk", errors="ignore").split("~")
                if len(parts) > 5:
                    raw_p = parts[3]
                    raw_t = parts[30] if len(parts) > 30 else ""
                    if raw_p:
                        price = float(raw_p)
                    if raw_t and len(raw_t) >= 12:
                        # 格式: YYYYMMDDHHMMSS
                        try:
                            quote_time = (
                                f"{raw_t[0:4]}-{raw_t[4:6]}-{raw_t[6:8]} "
                                f"{raw_t[8:10]}:{raw_t[10:12]}:{raw_t[12:14]}"
                            )
                        except Exception:
                            quote_time = raw_t
                break
        except Exception as ex:
            log.warning("Tencent quote %s failed: %s", code6, ex)

    if price is None or nav is None or nav <= 0:
        return None
    premium_pct = (price - nav) / nav * 100.0
    return {
        "code": code6,
        "price": Price(price),
        "iopv": Price(nav),              # T-1 NAV (latest authoritative ref)
        "nav_date": nav_date,
        "premium_pct": round(premium_pct, 2),
        "quote_time": quote_time,
        "source": "tencent+fund.eastmoney",
    }


def latest(sina: str, eastmoney: str = "") -> Tuple[float, pd.Timestamp]:
    """Freshest price available: Eastmoney universal quote → ETF spot
    → Tencent (qt.gtimg) → historical.

    Returns (price, as_of_date).  For the spot paths `as_of_date` is
    tagged with today (the API does not return an explicit timestamp);
    for the historical fallback it is the last bar's date.
    """
    if eastmoney:
        q = _eastmoney_universal_quote(eastmoney)
        if q is not None:
            return q
        s = _eastmoney_etf_spot(eastmoney)
        if s is not None:
            return s
        t = _tencent_quote(eastmoney)
        if t is not None:
            return t
    return latest_history(sina, eastmoney)


def latest_for_code(code: str) -> Tuple[float, datetime.date]:
    """Latest price for an ETF **code** (not a sina symbol).

    The adapter between the asset universe (which the domain speaks) and
    the quote endpoints (which speak sina/eastmoney symbols).  This is
    the `PriceProvider` the domain's valuation service expects — see
    `src/domain/portfolio/valuation.py`.

    Returns (price, as_of_date).
    """
    from ..domain.portfolio.asset_universe import ETF_DEFS

    sina, em, _name, _qdii = ETF_DEFS[code]
    price, pdate = latest(sina, em)
    return float(price), pdate.date()


def fetch_kline_history(code: str, start: str = "", end: str = "") -> Optional[pd.DataFrame]:
    """Daily K-line (含 low) via Tencent `web.ifzq.gtimg.cn`.

    Returns a DataFrame indexed by date with columns: open, close, high, low.
    Used for the grid trigger — the 120-day low window + today's intraday low —
    so it matches what stock software (同花顺/通达信) shows.

    We use Tencent (not akshare Eastmoney) because:
      - TenCent kline is reachable from CN ISPs where push2his.eastmoney.com
        is often throttled or aborted.
      - It returns unadjusted OHLC which is what we need for relative-low
        comparison.
    """
    if requests is None:
        return None
    code6 = str(code).strip().zfill(6)
    prefix = "sh" if code6[0] in ("5", "6", "9") else "sz"
    sym = f"{prefix}{code6}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{sym},day,,,1000,qfq"}
    raw = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://gu.qq.com/"})
            if r.status_code != 200:
                continue
            j = r.json()
            data = j.get("data", {}).get(sym, {})
            klines = data.get("day") or data.get("qfqday") or []
            if klines:
                raw = klines
                break
        except Exception as ex:
            log.warning("tencent kline %s attempt %d failed: %s", sym, attempt + 1, ex)
            import time as _t
            _t.sleep(1.0 * (attempt + 1))
    if not raw:
        return None
    # row: [date, open, close, high, low, volume]
    rows = []
    for k in raw:
        try:
            d = pd.Timestamp(k[0])
            o, c, h, lo = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            rows.append((d, o, c, h, lo))
        except (ValueError, TypeError, IndexError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low"]).set_index("date")
    df = df.sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    return df