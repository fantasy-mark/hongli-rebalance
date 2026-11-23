"""
缓存模块（数据模块）

包含缓存策略值对象与缓存规划/合并规则：

- CachePolicy / RefreshMode / RefreshPlan / MergeResult  缓存相关值对象
- PriceCachePlanner  根据缓存状态推导刷新计划（全量 / 增量 / 不刷新）
- PriceSeriesMerger  增量合并 + 接缝校验（复权口径变化则全量重建）

均为纯业务规则，不访问网络、不依赖仓储，便于单元测试。
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional

from src.data.models import AdjustedPriceSeries


class RefreshMode(Enum):
    """价格序列刷新方式"""
    NONE = "none"                # 缓存已满足，无需拉取
    INCREMENTAL = "incremental"  # 仅拉取缺失的尾部区间
    FULL = "full"                # 全量拉取（无缓存或需向前扩展）


@dataclass(frozen=True)
class CachePolicy:
    """
    价格缓存策略（值对象）

    不变量:
    - overlap_days >= 0
    - seam_tolerance >= 0
    - min_refresh_interval_hours >= 0

    属性说明:
    - overlap_days: 增量拉取时向回多取的日历天数，用于“接缝校验”
      （后复权价会因分红除权而重算，必须用重叠区间发现口径变化）
    - seam_tolerance: 重叠区间内收盘价的相对偏差阈值，
      超过则判定为后复权口径已变，需全量重建
    - min_refresh_interval_hours: 最小刷新间隔，避免同日重复拉取
      0 表示不限制
    - refresh_today: 缓存末条恰好是"今天"时是否重新拉取
      （该 bar 可能是盘中未收盘数据，需要覆盖为收盘价）
    """
    overlap_days: int = 5
    seam_tolerance: Decimal = Decimal("0.001")
    min_refresh_interval_hours: int = 0
    refresh_today: bool = True

    def __post_init__(self):
        if self.overlap_days < 0:
            raise ValueError(f"overlap_days 不能为负: {self.overlap_days}")
        if self.seam_tolerance < 0:
            raise ValueError(f"seam_tolerance 不能为负: {self.seam_tolerance}")
        if self.min_refresh_interval_hours < 0:
            raise ValueError(
                f"min_refresh_interval_hours 不能为负: {self.min_refresh_interval_hours}"
            )
        if not isinstance(self.seam_tolerance, Decimal):
            object.__setattr__(self, 'seam_tolerance',
                               Decimal(str(self.seam_tolerance)))

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'overlap_days': self.overlap_days,
            'seam_tolerance': float(self.seam_tolerance),
            'min_refresh_interval_hours': self.min_refresh_interval_hours,
        }


@dataclass(frozen=True)
class RefreshPlan:
    """
    缓存刷新计划（值对象）

    由领域服务 PriceCachePlanner 根据缓存状态与请求区间推导得出，
    应用层只负责执行，不再自行判断。

    不变量:
    - mode 为 INCREMENTAL 时 fetch_start 必须有值
    - mode 为 NONE 时 fetch_start 必须为 None
    """
    mode: RefreshMode
    fetch_start: Optional[date] = None
    reason: str = ""

    def __post_init__(self):
        if self.mode is RefreshMode.INCREMENTAL and self.fetch_start is None:
            raise ValueError("增量刷新必须指定 fetch_start")
        if self.mode is RefreshMode.NONE and self.fetch_start is not None:
            raise ValueError("无需刷新时不应指定 fetch_start")

    @property
    def needs_fetch(self) -> bool:
        """是否需要访问数据源"""
        return self.mode is not RefreshMode.NONE

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'mode': self.mode.value,
            'fetch_start': self.fetch_start.isoformat() if self.fetch_start else None,
            'reason': self.reason,
        }


@dataclass(frozen=True)
class MergeResult:
    """
    价格序列合并结果（值对象）

    属性:
    - series: 合并后的序列
    - conflict: 接缝校验是否发现口径冲突（后复权被重算）
    - overlap_points: 参与校验的重叠价格点数量
    - max_deviation: 重叠区间的最大相对偏差
    """
    series: object  # AdjustedPriceSeries（避免循环导入）
    conflict: bool = False
    overlap_points: int = 0
    max_deviation: Optional[Decimal] = None

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'conflict': self.conflict,
            'overlap_points': self.overlap_points,
            'max_deviation': float(self.max_deviation) if self.max_deviation is not None else None,
        }


def is_trading_day(day: date) -> bool:
    """
    是否为潜在交易日

    简化实现：仅排除周六周日。
    法定节假日无法在此判断，由数据源返回空数据的分支兜底
    （拉取后若无新数据，缓存保持不变）。
    """
    return day.weekday() < 5


class PriceCachePlanner:
    """
    价格缓存规划器（领域服务）

    职责:
    - 根据缓存状态与请求区间，推导出刷新计划（全量 / 增量 / 不需要）
    - 不访问网络、不依赖仓储，纯业务规则，便于单元测试
    """

    @staticmethod
    def plan(cached: Optional[AdjustedPriceSeries],
             requested_start: Optional[date] = None,
             requested_end: Optional[date] = None,
             policy: Optional[CachePolicy] = None,
             today: Optional[date] = None,
             last_checked_at: Optional[datetime] = None,
             history_complete: bool = False) -> RefreshPlan:
        """
        推导刷新计划

        Args:
            cached: 已缓存的价格序列，None 表示无缓存
            requested_start: 请求起始日期
            requested_end: 请求结束日期（None 视为到今天）
            policy: 缓存策略
            today: 当前日期（便于测试注入）
            last_checked_at: 上次检查数据源的时刻（用于最小刷新间隔）
            history_complete: 缓存是否已是数据源的完整历史
                （资产上市晚于请求起始日时，无法再向前扩展，
                  不应反复触发全量重拉）

        Returns:
            RefreshPlan
        """
        policy = policy or CachePolicy()
        today = today or date.today()
        target_end = requested_end or today

        # 1. 无缓存 -> 全量
        if cached is None or cached.count == 0:
            return RefreshPlan(
                RefreshMode.FULL,
                requested_start,
                "无缓存，需全量拉取",
            )

        # 2. 最小刷新间隔内已检查过 -> 不再访问数据源
        if (policy.min_refresh_interval_hours > 0
                and last_checked_at is not None):
            elapsed = datetime.now() - last_checked_at
            if elapsed.total_seconds() < policy.min_refresh_interval_hours * 3600:
                return RefreshPlan(
                    RefreshMode.NONE, None,
                    f"距上次刷新未超过 {policy.min_refresh_interval_hours} 小时",
                )

        # 3. 请求区间早于缓存起始日
        if (requested_start is not None
                and cached.start_date is not None
                and requested_start < cached.start_date):
            # 缓存已是完整历史 -> 更早的数据源本就不存在，无需重拉
            if history_complete:
                return RefreshPlan(
                    RefreshMode.NONE, None,
                    f"缓存已是完整历史（自 {cached.start_date}），无需向前扩展",
                )
            return RefreshPlan(
                RefreshMode.FULL,
                requested_start,
                f"缓存起始 {cached.start_date} 晚于请求 {requested_start}，需全量",
            )

        # 4. 缓存尾部已覆盖到目标结束日
        if cached.end_date is not None and cached.end_date >= target_end:
            # 例外：缓存最后一条可能就是"今天"的盘中 bar（未收盘），
            # 需要重新拉取覆盖为收盘价，或由实时接口补齐。
            if (policy.refresh_today
                    and target_end == today          # 仅当请求区间到"今天"
                    and cached.end_date == today     # 且缓存末条就是今天
                    and is_trading_day(today)):
                # 窗口要留出 overlap_days：部分源（如新浪 ETF）比交易所慢，
                # 只拉"今天"这一天会拿到空数据，导致无法对齐口径
                fetch_start = cached.end_date - timedelta(days=policy.overlap_days)
                return RefreshPlan(
                    RefreshMode.INCREMENTAL,
                    fetch_start,
                    f"刷新当日 bar（{cached.end_date} 可能为盘中价）",
                )
            return RefreshPlan(
                RefreshMode.NONE, None,
                f"缓存已覆盖至 {cached.end_date}",
            )

        # 5. 增量：起点必须"包含"缓存最后交易日
        #    该日可能是盘中未收盘的不完整 bar，必须重新拉取并被覆盖；
        #    向回多取 overlap_days 只是为接缝校验提供更多重叠点。
        fetch_start = cached.end_date - timedelta(days=policy.overlap_days)

        return RefreshPlan(
            RefreshMode.INCREMENTAL,
            fetch_start,
            f"增量更新 {fetch_start} ~ {target_end}"
            f"（含已缓存的最后交易日 {cached.end_date}）",
        )


class PriceSeriesMerger:
    """
    价格序列合并器（领域服务）

    职责:
    - 将增量序列合并进缓存序列
    - 通过重叠区间的价格偏差判断后复权口径是否被重算
      （若口径变了，历史价格整体偏移，必须全量重建而不是拼接）

    注：后复权(hfq)价格会在分红除权时整体重算，
        因此“增量拼接”必须先做接缝校验，否则序列会出现价格断层。
    """

    @staticmethod
    def merge(cached: AdjustedPriceSeries,
              update: AdjustedPriceSeries,
              policy: Optional[CachePolicy] = None) -> MergeResult:
        """
        合并增量数据

        Args:
            cached: 缓存序列
            update: 新拉取的序列
            policy: 缓存策略（提供接缝容差）

        Returns:
            MergeResult：含合并结果与是否口径冲突
        """
        policy = policy or CachePolicy()

        overlap_points, max_dev = cached.overlap_close_deviation(update)

        # 无重叠点（例如缓存末端后长期停牌的资产）视为无冲突，直接拼接
        conflict = (
            max_dev is not None
            and max_dev > policy.seam_tolerance
        )

        if conflict:
            # 口径已变：以新拉取的数据为准重建，旧缓存不可用
            return MergeResult(
                series=update,
                conflict=True,
                overlap_points=overlap_points,
                max_deviation=max_dev,
            )

        return MergeResult(
            series=cached.merged_with(update),
            conflict=False,
            overlap_points=overlap_points,
            max_deviation=max_dev,
        )
