"""
最新价必须是"最近交易日"的测试

背景:
东财被限流时，备用源可能返回**口径异常**的数据（如腾讯对个别 ETF 的
后复权价整体缩放 10 倍）。旧逻辑遇到冲突就直接放弃更新，
导致缓存卡在旧日期——页面上的"最新价"其实是几天前的价，
后面的市值、收益全错。

本模块覆盖:
1. 某个源与缓存冲突时，换下一个源重试（而不是放弃）
2. 不复权源可经"重叠区实测倍率"对齐到后复权缓存（只取缓存末端的尾巴）
3. 实时报价可把缓存补到最近交易日（报价自带日期，不必等于"今天"）
"""

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.data.cache import CachePolicy                                  # noqa: E402
from src.data.fallback import FetchedPrices                            # noqa: E402
from src.data.models import (                                           # noqa: E402
    AdjustedPriceSeries,
    AssetCode,
    DataSource,
)
from src.data.service import PriceQueryService                          # noqa: E402
from src.data.sources import PriceAdjustment                            # noqa: E402


# ----------------------------------------------------------------------
# 替身
# ----------------------------------------------------------------------

def make_frame(closes, start=date(2026, 11, 10), source='fake'):
    rows = []
    for i, close in enumerate(closes):
        c = float(close)
        rows.append({
            'date': start + timedelta(days=i),
            'open': c, 'high': c, 'low': c, 'close': c,
            'volume': 1000.0, 'amount': 0.0, 'source': source,
        })
    return pd.DataFrame(rows).set_index('date')


def to_series(df, code='511380'):
    data = df.drop(columns=['source'], errors='ignore').reset_index()
    return AdjustedPriceSeries.from_dataframe(
        AssetCode.from_string(code), data.set_index('date'))


class FakeRepo:
    """内存仓储替身"""

    def __init__(self, series=None):
        self._series = series
        self._adjustment = None

    def save(self, series):
        self._series = series

    def find_by_code(self, asset_code):
        return self._series

    def exists(self, asset_code):
        return self._series is not None

    def delete(self, asset_code):
        self._series = None

    def clear(self):
        self._series = None

    def find_by_date_range(self, asset_code, date_range):
        return self._series

    def get_adjustment(self, asset_code):
        return self._adjustment

    def set_adjustment(self, asset_code, value):
        self._adjustment = value


class MultiSourceProvider:
    """
    多数据源替身

    - sources: [(source_id, adjustment, closes)]，按顺序返回
    - 每个源可单独配置"取数失败"
    """

    def __init__(self, sources, quote=None, quote_prev=None,
                 quote_date=None):
        self.sources = sources
        self.quote = quote
        self.quote_prev = quote_prev
        self.quote_date = quote_date
        self.used: list[str] = []

    def iter_fetches(self, code, start_date=None, end_date=None,
                     preferred_adjustment=None):
        for source_id, adjustment, closes in self.sources:
            self.used.append(source_id)
            yield FetchedPrices(
                frame=make_frame(closes),
                source_id=source_id,
                adjustment=adjustment,
                probes=(),
            )

    def fetch_today_bar(self, code):
        if self.quote is None:
            return None, None
        day = self.quote_date or date.today()
        frame = pd.DataFrame([{
            'open': self.quote, 'high': self.quote, 'low': self.quote,
            'close': self.quote, 'volume': 1000.0, 'amount': 0.0,
            'source': 'realtime',
        }], index=pd.to_datetime([day]))
        return frame, (Decimal(str(self.quote_prev))
                       if self.quote_prev is not None else None)


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------

class TestLatestPriceIsLatestTradingDay(unittest.TestCase):

    def test_conflicting_source_falls_through_to_next_source(self):
        """
        第一个源的数据与缓存冲突 -> 换下一个源，缓存仍能更新

        旧行为：冲突即放弃 -> 缓存卡在旧日期 -> 最新价是旧价
        """
        # 缓存：11-10 ~ 11-12，末日 11-12
        cached = to_series(make_frame([12.0, 12.0, 12.0]))
        repo = FakeRepo(cached)
        repo._adjustment = 'hfq'

        provider = MultiSourceProvider([
            # 坏源：整体缩放 10 倍（腾讯对个别 ETF 的已知问题）
            ('tencent', PriceAdjustment.HFQ, [1.2, 1.2, 1.2, 1.2]),
            # 好源：与缓存同口径
            ('sina', PriceAdjustment.NONE, [12.0, 12.0, 12.0, 12.0]),
        ])

        service = PriceQueryService(
            repository=repo,
            cache_policy=CachePolicy(overlap_days=5),
            persist_cache=False,
            price_provider=provider,
        )
        service.get_adjusted_prices('511380')

        updated = repo.find_by_code(None)
        self.assertIsNotNone(updated)
        # 缓存必须被更新到更晚的日期（而不是停在 11-12）
        self.assertGreater(updated.end_date, date(2026, 11, 12))
        # 坏源被尝试过，然后才换到好源
        self.assertEqual(provider.used, ['tencent', 'sina'])

    def test_unadjusted_source_aligned_to_cache_basis(self):
        """
        不复权源可对齐到后复权缓存（重叠区实测倍率，且只取尾巴）

        不复权与复权在未跨除权日时是常数倍关系，实测偏差足够小即可用。
        """
        cached = to_series(make_frame([12.0, 12.0, 12.0]))
        repo = FakeRepo(cached)
        repo._adjustment = 'hfq'

        provider = MultiSourceProvider([
            # 不复权：约为缓存的一半（实测倍率 2.0）
            ('sina', PriceAdjustment.NONE, [6.0, 6.0, 6.0, 6.0]),
        ])

        service = PriceQueryService(
            repository=repo,
            cache_policy=CachePolicy(overlap_days=5),
            persist_cache=False,
            price_provider=provider,
        )
        service.get_adjusted_prices('511380')

        updated = repo.find_by_code(None)
        self.assertIsNotNone(updated)
        # 新增的尾巴按实测倍率换算回后复权口径
        self.assertGreater(updated.end_date, date(2026, 11, 12))
        self.assertAlmostEqual(
            float(updated.latest_price.close), 12.0, places=6)

    def test_realtime_quote_fills_latest_trading_day(self):
        """
        历史接口滞后时，实时报价把缓存补到最近交易日

        真实链路：新浪（不复权）先对齐到后复权缓存（得到实测倍率），
        再用实时报价补上当天那根 bar——报价自带日期，不必等于"今天"。
        """
        cached = to_series(make_frame(
            [12.0] * 11, start=date(2026, 11, 10)))   # 末日 2026-11-20
        repo = FakeRepo(cached)
        repo._adjustment = 'hfq'

        quote_day = date(2026, 11, 23)
        provider = MultiSourceProvider(
            sources=[
                # 新浪（不复权）：与缓存重叠，实测倍率 = 12.0 / 11.6
                ('sina', PriceAdjustment.NONE, [11.6] * 11),
            ],
            quote=11.9,          # 当日不复权收盘
            quote_prev=11.6,     # 报价昨收（= 缓存末日的不复权价）
            quote_date=quote_day,
        )

        service = PriceQueryService(
            repository=repo,
            cache_policy=CachePolicy(overlap_days=5),
            persist_cache=False,
            price_provider=provider,
        )
        service.get_adjusted_prices('511380')

        updated = repo.find_by_code(None)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.end_date, quote_day)
        # 倍率 = 缓存末日(后复权 12.0) / 报价昨收(11.6) -> 当日收盘 11.9 * 倍率
        expected = 11.9 * (12.0 / 11.6)
        self.assertAlmostEqual(
            float(updated.latest_price.close), expected, places=6)

    def test_quote_skipped_when_factor_does_not_match_known_ratio(self):
        """
        缓存末日与报价日不相邻 -> 不补齐（用已知倍率核对不上）

        若缓存末日比报价日的前一交易日还早一天，
        "报价昨收"对不上缓存末日，算出的倍率会把价格写错——宁可不补。
        """
        cached = to_series(make_frame(
            [12.0] * 11, start=date(2026, 11, 10)))   # 末日 2026-11-20
        repo = FakeRepo(cached)
        repo._adjustment = 'hfq'

        provider = MultiSourceProvider(
            sources=[
                ('sina', PriceAdjustment.NONE, [11.6] * 11),
            ],
            quote=11.9,
            quote_prev=11.2,        # 报价昨收是别的交易日的价 -> 对不上
            quote_date=date(2026, 11, 23),
        )

        service = PriceQueryService(
            repository=repo,
            cache_policy=CachePolicy(overlap_days=5),
            persist_cache=False,
            price_provider=provider,
        )
        service.get_adjusted_prices('511380')

        updated = repo.find_by_code(None)
        self.assertEqual(updated.end_date, date(2026, 11, 20))

    def test_quote_not_appended_when_gap_too_large(self):
        """
        缓存末日与报价日相隔太久 -> 不补齐（宁可不补，也不能写错价）
        """
        cached = to_series(make_frame([12.0, 12.0, 12.0]))   # 末日 11-12
        repo = FakeRepo(cached)
        repo._adjustment = 'hfq'

        provider = MultiSourceProvider(
            sources=[],
            quote=11.9, quote_prev=11.6,
            quote_date=date(2026, 11, 23),
        )

        service = PriceQueryService(
            repository=repo,
            cache_policy=CachePolicy(overlap_days=0),
            persist_cache=False,
            price_provider=provider,
        )
        service.get_adjusted_prices('511380')

        updated = repo.find_by_code(None)
        # 缓存末日是 2026-11-12，报价日 2026-11-23，相隔 11 天 -> 不补
        self.assertEqual(updated.end_date, date(2026, 11, 12))

    def test_stale_cache_is_reported_with_its_date(self):
        """
        get_latest_price 必须带上价格对应的日期（供页面标出）
        """
        cached = to_series(make_frame([12.0, 12.0, 12.0]))
        repo = FakeRepo(cached)
        repo._adjustment = 'hfq'

        provider = MultiSourceProvider(sources=[])
        service = PriceQueryService(
            repository=repo,
            cache_policy=CachePolicy(overlap_days=0),
            persist_cache=False,
            price_provider=provider,
        )
        info = service.get_latest_price('511380')
        self.assertEqual(info['date'], date(2026, 11, 12))
        self.assertEqual(float(info['close']), 12.0)


if __name__ == '__main__':
    unittest.main()
