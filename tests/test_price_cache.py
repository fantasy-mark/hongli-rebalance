"""
价格缓存与增量同步单元测试（离线，不依赖网络）

覆盖:
- AdjustedPriceSeries 的覆盖判断 / 合并 / 接缝偏差
- PriceCachePlanner 的刷新计划（全量 / 增量 / 不需要 / 最小间隔）
- PriceSeriesMerger 的接缝校验（口径变化则全量重建）
- PriceQueryService 的编排：缓存命中不拉取、增量只拉尾部、失败降级
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Optional

import pandas as pd

from src.domain.price.entities import (
    AdjustedPriceSeries,
    AssetCode,
    AssetType,
    DataSource,
    PricePoint,
)
from src.domain.price.value_objects import (
    CachePolicy,
    RefreshMode,
    RefreshPlan,
)
from src.domain.price.services import PriceCachePlanner, PriceSeriesMerger
from src.application.price.query_service import PriceQueryService


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------

def make_series(closes, start=date(2024, 1, 1), code="515180"):
    """构造价格序列（每日递增日期）"""
    asset_code = AssetCode.from_string(code)
    prices = []
    for i, close in enumerate(closes):
        c = Decimal(str(close))
        prices.append(PricePoint(
            date=start + timedelta(days=i),
            open=c, high=c + Decimal("0.01"), low=c - Decimal("0.01"),
            close=c, volume=1000,
        ))
    return AdjustedPriceSeries(
        asset_code=asset_code,
        asset_type=AssetType.ETF,
        prices=prices,
        source=DataSource.EASTMONEY_HFQ,
    )


class FakePriceRepository:
    """内存仓储替身，记录读写次数"""

    def __init__(self, series: Optional[AdjustedPriceSeries] = None):
        self._series = series
        self.save_count = 0
        self.history_complete = False

    def save(self, series):
        self._series = series
        self.save_count += 1

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
        self.history_complete = True

    def is_history_complete(self, asset_code):
        return self.history_complete


class FakeFetcher:
    """
    主备数据源替身

    形态与 FallbackPriceProvider.fetch 一致：
    返回 FetchedPrices(frame, source_id, adjustment, probes)
    """

    def __init__(self, closes, start=date(2024, 1, 1),
                 adjustment=None, scale=1.0):
        self.closes = closes
        self.start = start
        self.scale = scale
        self.calls: List[Optional[str]] = []
        self.raise_error = False
        if adjustment is None:
            from src.domain.price.sources import PriceAdjustment
            adjustment = PriceAdjustment.HFQ
        self.adjustment = adjustment

    def fetch(self, code, start_date=None, end_date=None,
              preferred_adjustment=None):
        from src.infrastructure.price.fallback_provider import FetchedPrices

        self.calls.append(start_date)
        if self.raise_error:
            raise RuntimeError("network down")

        closes = [c * self.scale for c in self.closes]
        series = make_series(closes, start=self.start, code=code)
        df = series.to_dataframe()

        if start_date:
            df = df[pd.to_datetime(df.index) >= pd.to_datetime(start_date)]
        if end_date:
            df = df[pd.to_datetime(df.index) <= pd.to_datetime(end_date)]

        return FetchedPrices(frame=df, source_id="fake",
                             adjustment=self.adjustment, probes=())


# ----------------------------------------------------------------------
# 聚合根行为
# ----------------------------------------------------------------------

class TestSeriesCovers(unittest.TestCase):

    def test_covers_full_range(self):
        series = make_series([10, 11, 12, 13, 14])
        self.assertTrue(series.covers(date(2024, 1, 1), date(2024, 1, 5)))
        self.assertTrue(series.covers(None, date(2024, 1, 5)))

    def test_not_cover_before_start(self):
        series = make_series([10, 11, 12])
        self.assertFalse(series.covers(date(2023, 12, 1), date(2024, 1, 3)))

    def test_not_cover_after_end(self):
        series = make_series([10, 11, 12])
        self.assertFalse(series.covers(None, date(2024, 2, 1)))

    def test_empty_series_never_covers(self):
        empty = AdjustedPriceSeries(
            asset_code=AssetCode.from_string("515180"),
            asset_type=AssetType.ETF,
            prices=[],
        )
        self.assertFalse(empty.covers(date(2024, 1, 1), date(2024, 1, 2)))


class TestSeriesMerge(unittest.TestCase):

    def test_merge_appends_new_dates(self):
        base = make_series([10, 11, 12])
        update = make_series([10, 11, 12, 13, 14])

        merged = base.merged_with(update)

        self.assertEqual(merged.count, 5)
        self.assertEqual(merged.end_date, date(2024, 1, 5))
        # 原序列不被修改（聚合根封装）
        self.assertEqual(base.count, 3)

    def test_merge_prefers_update_on_overlap(self):
        base = make_series([10, 11, 12])
        update = make_series([9, 11, 12, 13])

        merged = base.merged_with(update)

        self.assertEqual(merged.get_price(date(2024, 1, 1)).close, Decimal("9"))

    def test_merge_rejects_different_asset(self):
        base = make_series([10], code="515180")
        other = make_series([10], code="159937")
        with self.assertRaises(ValueError):
            base.merged_with(other)

    def test_merged_series_stays_sorted(self):
        base = make_series([10, 11, 12])
        update = make_series([10, 11, 12, 13], start=date(2024, 1, 1))
        merged = base.merged_with(update)
        dates = [p.date for p in merged.prices]
        self.assertEqual(dates, sorted(dates))


class TestSeamDeviation(unittest.TestCase):

    def test_no_deviation_when_identical(self):
        base = make_series([10, 11, 12])
        update = make_series([10, 11, 12, 13])
        overlap, dev = base.overlap_close_deviation(update)
        self.assertEqual(overlap, 3)
        self.assertEqual(dev, Decimal("0"))

    def test_detects_rebase(self):
        """后复权口径变化：历史价格整体放大 10%"""
        base = make_series([10, 11, 12])
        update = make_series([11, 12.1, 13.2, 14])
        overlap, dev = base.overlap_close_deviation(update)
        self.assertEqual(overlap, 3)
        self.assertGreater(dev, Decimal("0.09"))

    def test_no_overlap_returns_none(self):
        base = make_series([10, 11])
        update = make_series([20, 21], start=date(2024, 3, 1))
        overlap, dev = base.overlap_close_deviation(update)
        self.assertEqual(overlap, 0)
        self.assertIsNone(dev)


# ----------------------------------------------------------------------
# 领域服务：刷新计划
# ----------------------------------------------------------------------

class TestCachePlanner(unittest.TestCase):

    def setUp(self):
        self.cached = make_series([10, 11, 12])   # 2024-01-01 ~ 01-03
        self.policy = CachePolicy(overlap_days=5, seam_tolerance=Decimal("0.001"))

    def test_no_cache_requires_full(self):
        plan = PriceCachePlanner.plan(None, policy=self.policy)
        self.assertEqual(plan.mode, RefreshMode.FULL)
        self.assertTrue(plan.needs_fetch)

    def test_covered_range_requires_nothing(self):
        plan = PriceCachePlanner.plan(
            self.cached,
            requested_start=date(2024, 1, 1),
            requested_end=date(2024, 1, 3),
            policy=self.policy,
        )
        self.assertEqual(plan.mode, RefreshMode.NONE)
        self.assertFalse(plan.needs_fetch)

    def test_stale_tail_requires_incremental(self):
        plan = PriceCachePlanner.plan(
            self.cached,
            requested_end=date(2024, 1, 10),
            policy=self.policy,
        )
        self.assertEqual(plan.mode, RefreshMode.INCREMENTAL)
        # 从缓存末日向前多取 overlap_days
        self.assertEqual(plan.fetch_start, date(2024, 1, 3) - timedelta(days=5))

    def test_earlier_range_requires_full(self):
        plan = PriceCachePlanner.plan(
            self.cached,
            requested_start=date(2023, 6, 1),
            requested_end=date(2024, 1, 3),
            policy=self.policy,
        )
        self.assertEqual(plan.mode, RefreshMode.FULL)

    def test_min_refresh_interval_skips_fetch(self):
        policy = CachePolicy(overlap_days=5, min_refresh_interval_hours=6)
        plan = PriceCachePlanner.plan(
            self.cached,
            requested_end=date(2024, 1, 10),
            policy=policy,
            last_checked_at=datetime.now() - timedelta(minutes=5),
        )
        self.assertEqual(plan.mode, RefreshMode.NONE)

    def test_min_refresh_interval_expired_allows_fetch(self):
        policy = CachePolicy(overlap_days=5, min_refresh_interval_hours=1)
        plan = PriceCachePlanner.plan(
            self.cached,
            requested_end=date(2024, 1, 10),
            policy=policy,
            last_checked_at=datetime.now() - timedelta(hours=3),
        )
        self.assertEqual(plan.mode, RefreshMode.INCREMENTAL)

    def test_incremental_requires_fetch_start(self):
        with self.assertRaises(ValueError):
            RefreshPlan(RefreshMode.INCREMENTAL, None)

    def test_none_mode_rejects_fetch_start(self):
        with self.assertRaises(ValueError):
            RefreshPlan(RefreshMode.NONE, date(2024, 1, 1))


# ----------------------------------------------------------------------
# 领域服务：合并与接缝校验
# ----------------------------------------------------------------------

class TestSeriesMerger(unittest.TestCase):

    def setUp(self):
        self.policy = CachePolicy(overlap_days=5, seam_tolerance=Decimal("0.001"))

    def test_merge_without_conflict(self):
        cached = make_series([10, 11, 12])
        update = make_series([10, 11, 12, 13])

        result = PriceSeriesMerger.merge(cached, update, self.policy)

        self.assertFalse(result.conflict)
        self.assertEqual(result.series.count, 4)
        self.assertEqual(result.overlap_points, 3)

    def test_conflict_detected_on_rebase(self):
        cached = make_series([10, 11, 12])
        update = make_series([11, 12.1, 13.2, 14])   # 整体 +10%

        result = PriceSeriesMerger.merge(cached, update, self.policy)

        self.assertTrue(result.conflict)
        # 冲突时以新数据为准重建，不做拼接
        self.assertEqual(result.series.count, 4)
        self.assertEqual(result.series.get_price(date(2024, 1, 1)).close,
                         Decimal("11"))

    def test_within_tolerance_not_conflict(self):
        cached = make_series([10, 11, 12])
        update = make_series([10.005, 11.005, 12.005, 13])   # 偏差 0.05%

        result = PriceSeriesMerger.merge(cached, update, self.policy)

        self.assertFalse(result.conflict)


# ----------------------------------------------------------------------
# 应用层编排
# ----------------------------------------------------------------------

class TestPriceQueryServiceCache(unittest.TestCase):
    """验证应用的编排行为（用替身替换仓储与数据源）"""

    def _service(self, repository, provider=None, policy=None):
        """构造服务并注入主备 provider 替身"""
        return PriceQueryService(
            repository=repository,
            cache_policy=policy or CachePolicy(overlap_days=5),
            persist_cache=False,
            price_provider=provider,
        )

    def test_cache_hit_does_not_fetch(self):
        """缓存已覆盖请求区间 -> 不访问数据源"""
        cached = make_series([10, 11, 12])
        repo = FakePriceRepository(cached)
        fetcher = FakeFetcher([10, 11, 12])

        service = self._service(repo, fetcher)
        service.get_adjusted_prices("515180", start_date="2024-01-01",
                                    end_date="2024-01-03")

        self.assertEqual(fetcher.calls, [])

    def test_incremental_fetches_only_tail(self):
        """缓存陈旧 -> 只拉取尾部区间"""
        cached = make_series([10, 11, 12])
        repo = FakePriceRepository(cached)
        fetcher = FakeFetcher([10, 11, 12, 13, 14])

        service = self._service(repo, fetcher)
        service.get_adjusted_prices("515180", end_date="2024-01-05")

        self.assertEqual(len(fetcher.calls), 1)
        # 拉取起点 = 缓存末日 - overlap_days(5)
        self.assertEqual(fetcher.calls[0], "2023-12-29")
        # 缓存已合并到最新
        self.assertEqual(repo.find_by_code(None).count, 5)

    def test_fetch_failure_falls_back_to_cache(self):
        """拉取失败时降级使用缓存，不抛异常"""
        cached = make_series([10, 11, 12])
        repo = FakePriceRepository(cached)
        fetcher = FakeFetcher([10, 11, 12, 13])
        fetcher.raise_error = True

        service = self._service(repo, fetcher)
        df = service.get_adjusted_prices("515180", end_date="2024-01-04")

        self.assertFalse(df.empty)
        self.assertEqual(len(df), 3)      # 仍是缓存内容

    def test_conflict_triggers_full_rebuild(self):
        """接缝校验发现口径变化 -> 全量重建"""
        cached = make_series([10, 11, 12])
        repo = FakePriceRepository(cached)

        # 第一次(增量)返回放大10%的价格，第二次(全量)返回重建后的完整序列
        calls = {'n': 0}

        class RebasingFetcher(FakeFetcher):
            """第一次返回放大 10% 的价格（模拟后复权口径变化）"""
            def fetch(self, code, start_date=None, end_date=None,
                      preferred_adjustment=None):
                from src.infrastructure.price.fallback_provider import FetchedPrices
                calls['n'] += 1
                series = make_series([11, 12.1, 13.2, 14, 15], code=code)
                df = series.to_dataframe()
                if start_date and calls['n'] == 1:
                    df = df[pd.to_datetime(df.index) >= pd.to_datetime(start_date)]
                return FetchedPrices(frame=df, source_id="fake",
                                     adjustment=self.adjustment, probes=())

        fetcher = RebasingFetcher([10])

        service = self._service(repo, fetcher)
        service.get_adjusted_prices("515180", end_date="2024-01-05")

        # 增量 + 全量 至少两次调用
        self.assertGreaterEqual(calls['n'], 2)
        # 最终缓存以重建数据为准
        self.assertEqual(repo.find_by_code(None).count, 5)

    def test_conflict_rebuild_failure_keeps_cache(self):
        """
        全量重建失败时不得用残缺窗口覆盖缓存

        否则历史会被静默截断（缓存只剩增量窗口的几天），
        导致 ATR/前低无法计算、策略不再触发。
        """
        cached = make_series([10, 11, 12])
        repo = FakePriceRepository(cached)

        class FailingRebuildFetcher(FakeFetcher):
            """增量正常返回，但全量重建（start_date=None）失败"""
            def fetch(self, code, start_date=None, end_date=None,
                      preferred_adjustment=None):
                from src.infrastructure.price.fallback_provider import FetchedPrices
                self.calls.append(start_date)
                if start_date is None:
                    raise RuntimeError("rebuild failed")
                series = make_series([11, 12.1, 13.2, 14, 15], code=code)
                df = series.to_dataframe()
                if start_date:
                    df = df[pd.to_datetime(df.index) >= pd.to_datetime(start_date)]
                return FetchedPrices(frame=df, source_id="fake",
                                     adjustment=self.adjustment, probes=())

        fetcher = FailingRebuildFetcher([10])
        service = self._service(repo, fetcher)
        service.get_adjusted_prices("515180", end_date="2024-01-05")

        # 缓存未被残缺窗口覆盖
        self.assertEqual(repo.find_by_code(None).count, cached.count)

    def test_slice_frame_filters_dates(self):
        cached = make_series([10, 11, 12, 13, 14])
        repo = FakePriceRepository(cached)

        service = self._service(repo, FakeFetcher([10]))
        df = service.get_adjusted_prices("515180",
                                         start_date="2024-01-02",
                                         end_date="2024-01-04")

        self.assertEqual(len(df), 3)
        self.assertEqual(df.index.min(), date(2024, 1, 2))


if __name__ == '__main__':
    unittest.main()
