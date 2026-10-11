"""ETF daily price fetch (Sina, with Eastmoney fallback).

Ported from hongli/backend/scripts/backtest_equal_weight_rebalance.py:fetch_one.
"""
from __future__ import annotations

import logging
import warnings
from typing import Optional, Tuple

import akshare as ak
import pandas as pd

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
    """Eastmoney fallback."""
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


def latest(sina: str, eastmoney: str = "") -> Tuple[float, pd.Timestamp]:
    """Most recent close price and its date."""
    df = fetch_one(sina, eastmoney)
    row = df.iloc[-1]
    return float(row["close"]), df.index[-1]