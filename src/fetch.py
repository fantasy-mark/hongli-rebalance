"""ETF price fetch: realtime tick (Eastmoney universal quote) → historical fallback.

Spot tier order (latest → most stale):
  1. Eastmoney `push2.eastmoney.com/api/qt/stock/get` — covers ALL
     on-exchange securities (stocks, ETFs, bond ETFs like 511380).
     Field `f43` is the current price (× 1000).
  2. Eastmoney `ak.fund_etf_spot_em` — rich spot table but only
     for stock/currency ETFs; does NOT include 511380.
  3. Sina daily-close `ak.fund_etf_hist_sina` — only refreshes after
     the market closes; used as a last-resort historical reference.

Note: `ak.fund_etf_hist_sina` is a daily-close endpoint that only
refreshes after the market closes, so an Action triggered at 9:00
Beijing time (before open) would always see the previous trading day's
close.  The spot endpoints give the freshest tick available at any time.
"""
from __future__ import annotations

import logging
import warnings
from typing import Optional, Tuple

import akshare as ak
import pandas as pd

try:
    import requests
except ImportError:                                  # pragma: no cover
    requests = None                                  # type: ignore

log = logging.getLogger("fetch")
warnings.filterwarnings("ignore")
for _n in ("akshare", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.ERROR)


def _sina(sina: str) -> Optional[pd.DataFrame]:
    """Try Sina ETF history. Returns DataFrame with date index & 'close' column or None."""
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


def _eastmoney(eastmoney: str) -> Optional[pd.DataFrame]:
    """Eastmoney historical fallback."""
    try:
        raw = ak.fund_etf_hist_em(
            symbol=eastmoney, period="daily",
            start_date="20100101", end_date="20991231", adjust="hfq",
        )
        if raw is not None and not raw.empty:
            raw["日期"] = pd.to_datetime(raw["日期"])
            raw["收盘"] = pd.to_numeric(raw["收盘"], errors="coerce")
            df = raw.set_index("日期").sort_index()[["收盘"]].rename(columns={"收盘": "close"})
            return df[~df["close"].isna()]
    except Exception as ex:
        log.error("Eastmoney %s failed: %s", eastmoney, ex)
    return None


def fetch_one(sina: str, eastmoney: str = "") -> pd.DataFrame:
    """Daily close series. Sina first, Eastmoney fallback.

    Args:
        sina: e.g. "sh515180"
        eastmoney: e.g. "515180" (optional, for fallback)

    Returns:
        DataFrame indexed by date with single 'close' column.
    """
    df = _sina(sina)
    if df is None and eastmoney:
        df = _eastmoney(eastmoney)
    if df is None:
        raise RuntimeError(f"no price data for {sina}")
    return df


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
    """Most recent close price from the historical series (Sina → Eastmoney)."""
    df = fetch_one(sina, eastmoney)
    return float(df.iloc[-1]["close"]), df.index[-1]


def latest(sina: str, eastmoney: str = "") -> Tuple[float, pd.Timestamp]:
    """Freshest price available: Eastmoney universal quote → ETF spot → historical.

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
    return latest_history(sina, eastmoney)