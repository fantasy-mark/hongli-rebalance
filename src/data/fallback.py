"""
主备价格数据源提供者

职责:
1. **使用前探测**：对主源与备用源分别拉取最近 N 个交易日，判断可用性
2. **主源优先**：按优先级选择第一个健康的源（选择规则在 sources.py）
3. **运行期降级**：选中的源在正式拉取时失败，自动顺延到下一个健康源
4. **格式统一**：所有源输出统一列与单位（见 sources.py）

设计分层:
- `src.data.sources.PriceSourceSelector` 负责"选谁"的规则（纯函数）
- 本模块只负责"探测 + 调用 + 降级"等 IO 编排
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

import logging

import pandas as pd

from src.data.sources import (
    PriceAdjustment,
    PriceSourceSelector,
    SourceHealth,
    SourceProfile,
    SourceSelection,
)
from src.data.providers import (
    default_sources,
    PriceSource,
    RealtimeQuoteSource,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedPrices:
    """
    取数结果（含来源与口径信息）

    - frame:      统一格式 DataFrame
    - source_id:  实际提供数据的源
    - adjustment: 该源的价格口径（供领域层决定是否需要对齐）
    - probes:     本次使用前的探测结果（便于日志与排查）
    """
    frame: pd.DataFrame
    source_id: str
    adjustment: PriceAdjustment
    probes: Sequence[SourceHealth] = ()

    @property
    def rows(self) -> int:
        return 0 if self.frame is None else len(self.frame)


class FallbackPriceProvider:
    """
    主备价格数据源提供者

    参数:
        sources: 数据源链（按优先级）
        probe_trading_days: 使用前探测的交易日数（默认 2）
        probe_lookback_days: 探测时回看多少自然日（默认 15）
        health_ttl_seconds: 探测结果缓存时长（默认 300 秒）
        target_adjustment: 下游期望的复权口径（默认后复权）
    """

    def __init__(self,
                 sources: Optional[Sequence[PriceSource]] = None,
                 probe_trading_days: int = 2,
                 probe_lookback_days: int = 15,
                 health_ttl_seconds: int = 300,
                 target_adjustment: PriceAdjustment = PriceAdjustment.HFQ,
                 realtime_source: Optional[PriceSource] = None):
        self.sources: List[PriceSource] = list(sources or default_sources())
        self.probe_trading_days = max(1, probe_trading_days)
        self.probe_lookback_days = max(1, probe_lookback_days)
        self.health_ttl_seconds = max(0, health_ttl_seconds)
        self.target_adjustment = target_adjustment
        # 当日行情补齐用（历史接口盘中通常不含当天）
        self.realtime_source: PriceSource = realtime_source or RealtimeQuoteSource()

        # (code) -> (探测时间, 探测结果)
        self._health_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def fetch(self,
              code: str,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None,
              preferred_adjustment: Optional[PriceAdjustment] = None
              ) -> FetchedPrices:
        """
        取数（主源优先，失败自动降级）

        Args:
            code: 资产代码
            start_date: 起始日期（None 表示全历史）
            end_date: 结束日期
            preferred_adjustment: 优先选择该复权口径的源
                （全量重建时用于保持与缓存同口径）

        Raises:
            ValueError: 所有源均不可用
        """
        probes = self.probe(code)
        # 全历史请求（start_date 为空）必须选能提供全历史的源，
        # 否则缓存会被静默截断（如腾讯仅 ~640 根）
        require_full_history = start_date is None
        selection = self._select(probes,
                                  require_full_history=require_full_history,
                                  preferred_adjustment=preferred_adjustment)

        if not selection.available and require_full_history:
            logger.warning(
                f'{code} 无满足"全历史"要求的数据源，'
                f'改用可用源（缓存可能不完整）: {selection.reason}'
            )
            selection = self._select(probes, require_full_history=False)

        if not selection.available:
            raise ValueError(selection.reason)

        # 优先尝试选中的源，失败则按原优先级顺延
        candidates = self._ordered_candidates(selection, probes,
                                            preferred_adjustment)

        last_error: Optional[BaseException] = None
        for source_id in candidates:
            source = self._source_by_id(source_id)
            if source is None:
                continue
            try:
                frame = source.fetch(code, start_date, end_date)
                if frame is None or frame.empty:
                    raise ValueError(f'{source_id} 返回空数据')

                logger.info(
                    f'{code} 取数成功：{source_id}'
                    f'（{self._adjustment_label(source.adjustment)}，{len(frame)} 行）'
                )
                return FetchedPrices(
                    frame=frame,
                    source_id=source_id,
                    adjustment=source.adjustment,
                    probes=probes,
                )
            except Exception as error:
                last_error = error
                logger.warning(f'{code} 从 {source_id} 取数失败：{error}')
                # 探测期内失败的源标记为不健康，避免短时间内重复命中
                self._mark_unhealthy(code, source_id, str(error))

        raise ValueError(
            f'{code} 取数失败：所有数据源均不可用'
            f'（最后错误：{type(last_error).__name__ if last_error else "unknown"}）'
        )

    def probe(self, code: str) -> List[SourceHealth]:
        """
        探测各数据源可用性

        逐个拉取最近 N 个交易日的数据，校验：
        - 返回非空
        - 行数 >= probe_trading_days
        - 收盘价均为正

        结果按 TTL 缓存，避免每次取数都探测。
        """
        cached = self._health_cache.get(code)
        if cached is not None:
            checked_at, healths = cached
            age = (datetime.now() - checked_at).total_seconds()
            if age < self.health_ttl_seconds:
                return list(healths)

        today = date.today()
        probe_start = (today - timedelta(days=self.probe_lookback_days)).isoformat()

        healths: List[SourceHealth] = []
        for source in self.sources:
            started = datetime.now()
            try:
                frame = source.fetch(code, start_date=probe_start, end_date=today.isoformat())
                latency = (datetime.now() - started).total_seconds() * 1000

                if frame is None or frame.empty:
                    healths.append(SourceHealth(
                        source_id=source.source_id, healthy=False,
                        latency_ms=latency, reason='返回空数据',
                        checked_at=datetime.now()))
                    continue

                if len(frame) < self.probe_trading_days:
                    healths.append(SourceHealth(
                        source_id=source.source_id, healthy=False, latency_ms=latency,
                        reason=f'仅返回 {len(frame)} 行，少于要求的 '
                               f'{self.probe_trading_days} 个交易日',
                        checked_at=datetime.now()))
                    continue

                if bool((frame['close'] <= 0).any()):
                    healths.append(SourceHealth(
                        source_id=source.source_id, healthy=False, latency_ms=latency,
                        reason='存在非正收盘价', checked_at=datetime.now()))
                    continue

                healths.append(SourceHealth(
                    source_id=source.source_id, healthy=True, latency_ms=latency,
                    reason=f'最近 {len(frame)} 个交易日可用',
                    checked_at=datetime.now()))

            except Exception as error:
                latency = (datetime.now() - started).total_seconds() * 1000
                healths.append(SourceHealth(
                    source_id=source.source_id, healthy=False, latency_ms=latency,
                    reason=f'{type(error).__name__}: {str(error)[:60]}',
                    checked_at=datetime.now()))

        for health in healths:
            logger.info(
                f'[探测] {code} {health.source_id}: '
                f'{"可用" if health.healthy else "不可用"}'
                f'（{health.reason}，{health.latency_ms:.0f}ms）'
                if health.latency_ms is not None else health.reason
            )

        self._health_cache[code] = (datetime.now(), healths)
        return list(healths)

    def fetch_today_bar(self, code: str):
        """
        取"当日"这一根 K 线（实时报价源）

        历史 K 线接口盘中通常不含当天，用它把当天补上。

        Returns:
            (frame, prev_close)；不可用时返回 (None, None)
            注意：frame 为**不复权**口径，调用方需换算到缓存口径。
        """
        try:
            frame = self.realtime_source.fetch(code)
            if frame is None or frame.empty:
                return None, None
            prev_close = frame.attrs.get('prev_close')
            return frame, (Decimal(str(prev_close)) if prev_close else None)
        except Exception as error:
            logger.info(f'{code} 当日行情不可用：{error}')
            return None, None

    def invalidate_health(self, code: Optional[str] = None) -> None:
        """清除探测缓存（code 为 None 时清除全部）"""
        if code is None:
            self._health_cache.clear()
        else:
            self._health_cache.pop(code, None)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _select(self, healths: Sequence[SourceHealth],
                require_full_history: bool = False,
                preferred_adjustment: Optional[PriceAdjustment] = None
                ) -> SourceSelection:
        profiles = [self._profile_of(h.source_id) for h in healths]
        profiles = [p for p in profiles if p is not None]
        selection = PriceSourceSelector.select(
            healths, require_full_history=require_full_history, profiles=profiles
        )

        # 若首选源的复权口径与偏好不符，则改选口径匹配的源
        if (preferred_adjustment is not None
                and selection.available
                and selection.adjustment is not preferred_adjustment):
            for index, health in enumerate(healths):
                if not health.healthy:
                    continue
                profile = profiles[index] if index < len(profiles) else None
                if profile is not None and profile.adjustment is preferred_adjustment:
                    return SourceSelection(
                        source_id=health.source_id,
                        adjustment=profile.adjustment,
                        reason=f"选用 {health.source_id}（复权口径匹配）",
                        skipped=selection.skipped,
                    )

        return selection

    def _profile_of(self, source_id: str) -> Optional[SourceProfile]:
        source = self._source_by_id(source_id)
        if source is None:
            return None
        return SourceProfile(
            source_id=source.source_id,
            adjustment=source.adjustment,
            provides_full_history=getattr(source, 'provides_full_history', True),
            max_bars=getattr(source, 'max_bars', None),
        )

    def _ordered_candidates(self, selection: SourceSelection,
                            healths: Sequence[SourceHealth],
                            preferred_adjustment: Optional[PriceAdjustment] = None
                            ) -> List[str]:
        """候选顺序：选中的源优先，其余健康源按原优先级跟随"""
        healthy_ids = [h.source_id for h in healths if h.healthy]
        if selection.source_id in healthy_ids:
            healthy_ids.remove(selection.source_id)
            healthy_ids.insert(0, selection.source_id)

        # 口径偏好：同口径的源优先（保证重建后口径不变）
        if preferred_adjustment is not None:
            def matches(source_id: str) -> bool:
                profile = self._profile_of(source_id)
                return profile is not None and profile.adjustment is preferred_adjustment

            healthy_ids.sort(key=lambda sid: not matches(sid))

        return healthy_ids

    def _source_by_id(self, source_id: str) -> Optional[PriceSource]:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        return None

    def _adjustment_of(self, source_id: str) -> Optional[PriceAdjustment]:
        source = self._source_by_id(source_id)
        return source.adjustment if source else None

    @staticmethod
    def _adjustment_label(adjustment: PriceAdjustment) -> str:
        return adjustment.label

    def _mark_unhealthy(self, code: str, source_id: str, reason: str) -> None:
        cached = self._health_cache.get(code)
        if cached is None:
            return
        checked_at, healths = cached
        updated = [
            SourceHealth(source_id=h.source_id, healthy=False,
                         latency_ms=h.latency_ms, reason=reason[:60],
                         checked_at=datetime.now())
            if h.source_id == source_id else h
            for h in healths
        ]
        self._health_cache[code] = (checked_at, updated)
