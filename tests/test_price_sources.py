"""
主备数据源与复权口径对齐测试（离线，不依赖网络）
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional

import pandas as pd

from src.domain.price.entities import AdjustedPriceSeries, AssetCode, AssetType, DataSource, PricePoint
from src.domain.price.sources import (
    PriceAdjustment,
    PriceBasisAligner,
    PriceSourceSelector,
    SourceHealth,
    SourceProfile,
)
from src.infrastructure.price.fallback_provider import FallbackPriceProvider


# ----------------------------------------------------------------------
# 替身
# ----------------------------------------------------------------------

def make_frame(closes, start=date(2024, 1, 1), scale=1.0):
    """构造统一格式 DataFrame"""
    rows = []
    for i, close in enumerate(closes):
        c = float(close) * scale
        rows.append({
            'date': start + timedelta(days=i),
            'open': c, 'high': c * 1.01, 'low': c * 0.99, 'close': c,
            'volume': 1000.0, 'amount': 0.0, 'source': 'fake',
        })
    df = pd.DataFrame(rows).set_index('date')
    return df


def to_series(df, code='515180'):
    data = df.drop(columns=['source'], errors='ignore').reset_index()
    return AdjustedPriceSeries.from_dataframe(
        AssetCode.from_string(code), data.set_index('date'))


class FakeSource:
    """可控的数据源替身"""

    def __init__(self, source_id, adjustment, closes,
                 healthy=True, provides_full_history=True,
                 max_bars=None, scale=1.0):
        self.source_id = source_id
        self.adjustment = adjustment
        self.closes = closes
        self.healthy = healthy
        self.provides_full_history = provides_full_history
        self.max_bars = max_bars
        self.scale = scale
        self.calls: List[Optional[str]] = []

    def fetch(self, code, start_date=None, end_date=None):
        self.calls.append(start_date)
        if not self.healthy:
            raise RuntimeError(f"{self.source_id} down")
        return make_frame(self.closes, scale=self.scale)


# ----------------------------------------------------------------------
# 领域层：数据源选择
# ----------------------------------------------------------------------

class TestSourceSelector(unittest.TestCase):

    def test_prefers_primary_when_healthy(self):
        """主源健康 -> 选主源"""
        sel = PriceSourceSelector.select([
            SourceHealth('eastmoney', True),
            SourceHealth('tencent', True),
        ])
        self.assertEqual(sel.source_id, 'eastmoney')

    def test_falls_back_when_primary_unhealthy(self):
        """主源不可用 -> 选备用源，并记录跳过原因"""
        sel = PriceSourceSelector.select([
            SourceHealth('eastmoney', False, reason='限流'),
            SourceHealth('tencent', True),
        ])
        self.assertEqual(sel.source_id, 'tencent')
        self.assertIn('eastmoney', sel.skipped)

    def test_all_unhealthy(self):
        """全部不可用 -> available=False"""
        sel = PriceSourceSelector.select([
            SourceHealth('eastmoney', False, reason='限流'),
            SourceHealth('tencent', False, reason='超时'),
        ])
        self.assertFalse(sel.available)

    def test_require_full_history_skips_limited_source(self):
        """
        需要全历史时跳过"历史有上限"的源

        否则会把只有 ~640 根的序列写入缓存，静默截断历史。
        """
        healths = [SourceHealth('eastmoney', False, reason='限流'),
                   SourceHealth('tencent', True),
                   SourceHealth('sina', True)]
        profiles = [
            SourceProfile('eastmoney', PriceAdjustment.HFQ, True, None),
            SourceProfile('tencent', PriceAdjustment.HFQ, False, 640),
            SourceProfile('sina', PriceAdjustment.NONE, True, None),
        ]

        sel = PriceSourceSelector.select(
            healths, require_full_history=True, profiles=profiles)

        self.assertEqual(sel.source_id, 'sina')
        # skipped 中会带上跳过原因（如 "tencent(历史仅640根)"）
        self.assertTrue(any(item.startswith('tencent') for item in sel.skipped))

    def test_no_source_satisfies_full_history(self):
        """只有受限历史的源可用 -> 明确报无源可用"""
        sel = PriceSourceSelector.select(
            [SourceHealth('tencent', True)],
            require_full_history=True,
            profiles=[SourceProfile('tencent', PriceAdjustment.HFQ, False, 640)],
        )
        self.assertFalse(sel.available)
        self.assertIn('全历史', sel.reason)


# ----------------------------------------------------------------------
# 领域层：复权口径对齐
# ----------------------------------------------------------------------

class TestBasisAligner(unittest.TestCase):

    def test_same_basis_no_scaling(self):
        """同口径不缩放"""
        cached = to_series(make_frame([10, 11, 12]))
        incoming = to_series(make_frame([10, 11, 12, 13]))

        result = PriceBasisAligner.align(
            cached, incoming, PriceAdjustment.HFQ, PriceAdjustment.HFQ)

        self.assertFalse(result.applied)
        self.assertEqual(result.factor, Decimal("1"))

    def test_qfq_scaled_to_hfq(self):
        """前复权 -> 后复权：按重叠区间倍率换算"""
        cached = to_series(make_frame([10, 11, 12]))          # hfq
        incoming = to_series(make_frame([5, 5.5, 6, 6.5]))    # qfq（正好一半）

        result = PriceBasisAligner.align(
            cached, incoming, PriceAdjustment.HFQ, PriceAdjustment.QFQ)

        self.assertTrue(result.applied)
        self.assertAlmostEqual(float(result.factor), 2.0, places=6)
        # 缩放后应与缓存重叠部分一致
        self.assertEqual(result.overlaps, 3)
        self.assertLess(float(result.max_deviation), 1e-9)

    def test_unadjusted_is_incompatible(self):
        """
        不复权 <-> 复权不可用常数倍率换算

        两者是阶梯关系（每逢除权跳变），强行对齐会引入系统性偏差。
        """
        cached = to_series(make_frame([10, 11, 12]))
        incoming = to_series(make_frame([5, 5.5, 6, 6.5]))

        result = PriceBasisAligner.align(
            cached, incoming, PriceAdjustment.HFQ, PriceAdjustment.NONE)

        self.assertTrue(result.incompatible)
        self.assertFalse(result.applied)

    def test_estimate_factor_median(self):
        """倍率取中位数，抗单点异常"""
        cached = to_series(make_frame([10, 11, 12, 13]))
        incoming = to_series(make_frame([5, 5.5, 6, 6.5]))

        factor, overlaps = PriceBasisAligner.estimate_factor(cached, incoming)
        self.assertEqual(overlaps, 4)
        self.assertAlmostEqual(float(factor), 2.0, places=6)

    def test_no_overlap_returns_none(self):
        """无重叠交易日 -> 无法估算倍率"""
        cached = to_series(make_frame([10, 11, 12]))
        incoming = to_series(make_frame([5, 6], start=date(2024, 3, 1)))

        factor, overlaps = PriceBasisAligner.estimate_factor(cached, incoming)
        self.assertIsNone(factor)
        self.assertEqual(overlaps, 0)

    def test_rescale_keeps_volume(self):
        """缩放只改价格，不改成交量"""
        series = to_series(make_frame([10, 11]))
        scaled = PriceBasisAligner.rescale(series, Decimal("2"))

        self.assertEqual(scaled.prices[0].close, Decimal("20"))
        self.assertEqual(scaled.prices[0].volume, series.prices[0].volume)


# ----------------------------------------------------------------------
# 基础设施层：主备 provider
# ----------------------------------------------------------------------

class TestFallbackProvider(unittest.TestCase):

    def _provider(self, sources, **kwargs):
        return FallbackPriceProvider(sources=sources, health_ttl_seconds=0, **kwargs)

    def test_uses_primary_when_healthy(self):
        """主源可用 -> 用主源"""
        em = FakeSource('eastmoney', PriceAdjustment.HFQ, [10, 11, 12])
        tx = FakeSource('tencent', PriceAdjustment.HFQ, [10, 11, 12])

        result = self._provider([em, tx]).fetch('515180')

        self.assertEqual(result.source_id, 'eastmoney')
        # 备用源只被"探测"调用过一次，未参与正式取数
        self.assertEqual(len(tx.calls), 1)

    def test_falls_back_when_primary_down(self):
        """主源异常 -> 降级备用源"""
        em = FakeSource('eastmoney', PriceAdjustment.HFQ, [10], healthy=False)
        tx = FakeSource('tencent', PriceAdjustment.HFQ, [10, 11, 12])

        result = self._provider([em, tx]).fetch('515180')

        self.assertEqual(result.source_id, 'tencent')
        self.assertEqual(result.rows, 3)
        self.assertTrue(any(not p.healthy and p.source_id == 'eastmoney'
                            for p in result.probes))

    def test_probe_requires_enough_trading_days(self):
        """探测要求至少 N 个交易日"""
        only_one = FakeSource('eastmoney', PriceAdjustment.HFQ, [10])
        provider = self._provider([only_one], probe_trading_days=2)

        healths = provider.probe('515180')
        self.assertFalse(healths[0].healthy)
        self.assertIn('少于要求', healths[0].reason)

    def test_all_sources_down_raises(self):
        """全部不可用 -> 抛错"""
        em = FakeSource('eastmoney', PriceAdjustment.HFQ, [10], healthy=False)
        tx = FakeSource('tencent', PriceAdjustment.HFQ, [10], healthy=False)

        with self.assertRaises(ValueError):
            self._provider([em, tx]).fetch('515180')

    def test_full_history_request_skips_limited_source(self):
        """全历史请求跳过"历史有上限"的源"""
        em = FakeSource('eastmoney', PriceAdjustment.HFQ, [10], healthy=False)
        tx = FakeSource('tencent', PriceAdjustment.HFQ, [10, 11, 12],
                        provides_full_history=False, max_bars=640)
        sina = FakeSource('sina', PriceAdjustment.NONE, [10, 11, 12])

        result = self._provider([em, tx, sina]).fetch('515180')   # start_date=None

        self.assertEqual(result.source_id, 'sina')
        self.assertEqual(result.adjustment, PriceAdjustment.NONE)

    def test_format_is_unified(self):
        """所有源输出统一列"""
        em = FakeSource('eastmoney', PriceAdjustment.HFQ, [10, 11])
        result = self._provider([em]).fetch('515180')

        self.assertEqual(
            list(result.frame.columns),
            ['open', 'high', 'low', 'close', 'volume', 'amount', 'source'])


# ----------------------------------------------------------------------
# 当日 bar 补齐 / 增量包含最后交易日
# ----------------------------------------------------------------------

class TestTodayBarRefresh(unittest.TestCase):
    """
    历史接口盘中通常不含当天，需要实时报价补齐；
    且增量必须**包含**缓存最后交易日（该 bar 可能是盘中价）。
    """

    def test_planner_refreshes_today_bar(self):
        """缓存末条恰好是今天 -> 需要增量刷新（含该日）"""
        from src.domain.price.services import PriceCachePlanner
        from src.domain.price.value_objects import CachePolicy

        today = date(2024, 6, 5)          # 周三
        cached = make_series_like([10, 11, 12], end=today)

        plan = PriceCachePlanner.plan(
            cached, requested_end=today,
            policy=CachePolicy(refresh_today=True), today=today)

        self.assertEqual(plan.mode.value, 'incremental')
        # 起点必须 <= 缓存最后交易日（确保覆盖它）
        self.assertLessEqual(plan.fetch_start, cached.end_date)

    def test_planner_skips_when_refresh_disabled(self):
        """关闭 refresh_today -> 缓存已覆盖则不刷新"""
        from src.domain.price.services import PriceCachePlanner
        from src.domain.price.value_objects import CachePolicy

        today = date(2024, 6, 5)
        cached = make_series_like([10, 11, 12], end=today)

        plan = PriceCachePlanner.plan(
            cached, requested_end=today,
            policy=CachePolicy(refresh_today=False), today=today)

        self.assertEqual(plan.mode.value, 'none')

    def test_planner_history_range_not_refreshed(self):
        """历史区间请求不受 refresh_today 影响"""
        from src.domain.price.services import PriceCachePlanner
        from src.domain.price.value_objects import CachePolicy

        cached = make_series_like([10, 11, 12], end=date(2024, 6, 5))

        plan = PriceCachePlanner.plan(
            cached, requested_end=date(2024, 6, 5),
            policy=CachePolicy(refresh_today=True), today=date(2026, 1, 1))

        self.assertEqual(plan.mode.value, 'none')


class TestTodayBarFill(unittest.TestCase):
    """应用层用实时报价补齐当日 bar"""

    class ProviderWithQuote:
        """历史 + 实时报价替身"""

        def __init__(self, closes, quote_close, prev_close, start=None):
            self.closes = closes
            self.quote_close = quote_close
            self.prev_close = prev_close
            self.start = start or date(2024, 6, 1)
            self.sources = []

        def fetch(self, code, start_date=None, end_date=None):
            from src.infrastructure.price.fallback_provider import FetchedPrices
            frame = make_frame(self.closes, start=self.start)
            return FetchedPrices(frame=frame, source_id='fake',
                                 adjustment=PriceAdjustment.HFQ, probes=())

        def fetch_today_bar(self, code):
            today = date.today()
            frame = pd.DataFrame([{
                'open': self.quote_close, 'high': self.quote_close,
                'low': self.quote_close, 'close': self.quote_close,
                'volume': 1000.0, 'amount': 0.0, 'source': 'realtime',
            }], index=pd.to_datetime([today]))
            return frame, Decimal(str(self.prev_close))

    def _service(self, provider):
        from src.application.price.query_service import PriceQueryService
        from src.domain.price.value_objects import CachePolicy

        return PriceQueryService(
            repository=FakePriceRepository(None),
            cache_policy=CachePolicy(overlap_days=0, refresh_today=True),
            persist_cache=False,
            price_provider=provider,
        ), None

    def test_appends_today_bar_with_basis_factor(self):
        """
        实时报价为不复权，需按"缓存末值 / 报价昨收"换算到缓存口径
        """
        from src.application.price.query_service import PriceQueryService
        from src.domain.price.value_objects import CachePolicy

        today = date.today()
        yesterday = today - timedelta(days=1)
        # 历史到昨天：hfq 20.0；实时报价：不复权 10.0，昨收 10.0 -> 倍率 2.0
        provider = self.ProviderWithQuote(
            closes=[19.0, 20.0], quote_close=10.5, prev_close=10.0,
            start=yesterday - timedelta(days=1))

        repo = FakeRepository()
        service = PriceQueryService(
            repository=repo, cache_policy=CachePolicy(overlap_days=0),
            persist_cache=False, price_provider=provider)

        service.get_adjusted_prices('515180')

        series = repo.find_by_code(None)
        self.assertIsNotNone(series)
        self.assertEqual(series.end_date, today)
        # 倍率 20.0/10.0 = 2.0 -> 当日收盘 10.5*2 = 21.0
        self.assertAlmostEqual(float(series.latest_price.close), 21.0, places=6)

    def test_skips_when_history_already_has_today(self):
        """历史接口已含当日 -> 不重复追加"""
        from src.application.price.query_service import PriceQueryService
        from src.domain.price.value_objects import CachePolicy

        today = date.today()
        provider = self.ProviderWithQuote(
            closes=[19.0, 20.0], quote_close=10.5, prev_close=10.0,
            start=today - timedelta(days=1))    # 历史已到 today

        repo = FakeRepository()
        service = PriceQueryService(
            repository=repo, cache_policy=CachePolicy(overlap_days=0),
            persist_cache=False, price_provider=provider)

        service.get_adjusted_prices('515180')

        series = repo.find_by_code(None)
        self.assertEqual(series.count, 2)
        self.assertEqual(series.end_date, today)


class FakeRepository:
    """内存仓储替身（含口径与完整历史标记）"""

    def __init__(self, series=None):
        self._series = series
        self._history_complete = False
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

    def mark_history_complete(self, asset_code):
        self._history_complete = True

    def is_history_complete(self, asset_code):
        return self._history_complete

    def get_adjustment(self, asset_code):
        return self._adjustment

    def set_adjustment(self, asset_code, adjustment):
        self._adjustment = adjustment


def make_series_like(closes, end=None, code='515180'):
    """构造以 end（默认今天）为最后一个交易日的序列"""
    end = end or date.today()
    start = end - timedelta(days=len(closes) - 1)
    return to_series(make_frame(closes, start=start), code=code)


if __name__ == '__main__':
    unittest.main()
