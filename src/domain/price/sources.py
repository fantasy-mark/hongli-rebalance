"""
价格数据源领域模型

包含:
- PriceAdjustment: 复权口径（后复权 / 前复权 / 不复权）
- SourceHealth:     数据源健康状态（值对象）
- SourceSelection:  数据源选择结果（值对象）
- PriceSourceSelector: 源选择规则（领域服务）
- PriceBasisAligner:  复权口径对齐（领域服务）

设计要点:
不同数据源的复权口径不同（东财/腾讯为后复权，新浪为不复权），
后复权与前复权/不复权之间相差一个**常数倍率**，因此可通过对齐
重叠区间估算倍率后归一，再交给 PriceSeriesMerger 做接缝校验。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Sequence, Tuple

from src.domain.price.entities import AdjustedPriceSeries


class PriceAdjustment(Enum):
    """
    复权口径

    - HFQ:  后复权（历史价格按分红再投资放大，最新价最接近累计收益）
    - QFQ:  前复权（最新价等于真实成交价，历史价格按比例缩小）
    - NONE: 不复权（原始成交价，除权日会出现跳空）

    不变量: 同一缓存序列只能容纳同一口径的数据。
    """
    HFQ = "hfq"
    QFQ = "qfq"
    NONE = "none"

    @property
    def label(self) -> str:
        return {
            PriceAdjustment.HFQ: "后复权",
            PriceAdjustment.QFQ: "前复权",
            PriceAdjustment.NONE: "不复权",
        }[self]

    def is_directly_mergeable(self, other: 'PriceAdjustment') -> bool:
        """是否可直接拼接（同口径）"""
        return self is other

    def can_align_by_factor(self, other: 'PriceAdjustment') -> bool:
        """
        是否可用常数倍率互相换算

        只有"后复权 <-> 前复权"成立：两者都是同一条总收益序列的常数倍缩放。
        **不复权不满足**：它与复权价之间是阶梯关系（每逢除权除息跳变一次），
        跨除权日用常数倍率对齐会产生系统性偏差（实测 640 个交易日后偏差约 7%）。
        """
        adjusted = {PriceAdjustment.HFQ, PriceAdjustment.QFQ}
        return self is other or (self in adjusted and other in adjusted)


@dataclass(frozen=True)
class SourceHealth:
    """
    数据源健康状态（值对象）

    由探测结果构造：拉取最近 N 个交易日的数据，看是否返回可用内容。
    """
    source_id: str
    healthy: bool
    latency_ms: Optional[float] = None
    reason: str = ""
    checked_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'healthy': self.healthy,
            'latency_ms': round(self.latency_ms, 1) if self.latency_ms is not None else None,
            'reason': self.reason,
            'checked_at': self.checked_at.isoformat() if self.checked_at else None,
        }


@dataclass(frozen=True)
class SourceProfile:
    """
    数据源能力描述（值对象）

    与健康状态分离：健康 ≠ 能力满足。
    例如腾讯健康但只有 ~640 根数据，无法满足"全历史"需求。
    """
    source_id: str
    adjustment: PriceAdjustment
    provides_full_history: bool = True
    max_bars: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'adjustment': self.adjustment.value,
            'provides_full_history': self.provides_full_history,
            'max_bars': self.max_bars,
        }


@dataclass(frozen=True)
class SourceSelection:
    """
    数据源选择结果（值对象）

    source_id 为空表示所有源均不可用。
    """
    source_id: Optional[str]
    adjustment: Optional[PriceAdjustment] = None
    reason: str = ""
    skipped: Tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.source_id is not None

    def to_dict(self) -> dict:
        return {
            'source_id': self.source_id,
            'adjustment': self.adjustment.value if self.adjustment else None,
            'reason': self.reason,
            'skipped': list(self.skipped),
        }


class PriceSourceSelector:
    """
    数据源选择器（领域服务）

    规则:
    - 按调用方给出的**优先级顺序**依次检查
    - 第一个健康的源即被选用（主源优先）
    - 不健康的源记录到 skipped，便于日志追溯

    纯函数，无 IO。
    """

    @staticmethod
    def select(healths: Sequence[SourceHealth],
               adjustments: Optional[Sequence[PriceAdjustment]] = None,
               require_full_history: bool = False,
               profiles: Optional[Sequence[SourceProfile]] = None
               ) -> SourceSelection:
        """
        按优先级选择可用数据源

        Args:
            healths: 按优先级排列的健康状态
            adjustments: 与 healths 一一对应的复权口径（可选，向后兼容）
            require_full_history: 是否要求提供全历史
                （缓存为空时若选到只提供 ~640 根的源，会把缓存静默截断）
            profiles: 与 healths 一一对应的能力描述（可选）

        Returns:
            SourceSelection
        """
        if not healths:
            return SourceSelection(source_id=None, reason="未配置任何数据源")

        skipped = tuple(h.source_id for h in healths if not h.healthy)
        reasons = "; ".join(
            f"{h.source_id}: {h.reason}" for h in healths if not h.healthy and h.reason
        )
        incapable = []
        adjustment = None

        for index, health in enumerate(healths):
            if not health.healthy:
                continue

            profile = profiles[index] if profiles is not None and index < len(profiles) else None

            # 能力校验：要求全历史但该源历史有上限 -> 跳过
            if require_full_history and profile is not None                     and not profile.provides_full_history:
                incapable.append(f"{health.source_id}(历史仅{profile.max_bars}根)")
                continue

            adjustment = None
            if profile is not None:
                adjustment = profile.adjustment
            elif adjustments is not None and index < len(adjustments):
                adjustment = adjustments[index]

            note = f"，历史有上限" if (require_full_history is False and profile is not None
                                    and not profile.provides_full_history) else ""
            return SourceSelection(
                source_id=health.source_id,
                adjustment=adjustment,
                reason=f"选用 {health.source_id}（按优先级{note}）",
                skipped=skipped + tuple(incapable),
            )

        if incapable:
            return SourceSelection(
                source_id=None,
                reason=("无满足'全历史'要求的数据源"
                        f"（不可用: {', '.join(incapable)}）"),
                skipped=skipped + tuple(incapable),
            )

        return SourceSelection(
            source_id=None,
            reason=f"所有数据源均不可用{f'：{reasons}' if reasons else ''}",
            skipped=skipped,
        )


@dataclass(frozen=True)
class BasisAlignment:
    """
    复权口径对齐结果（值对象）

    - factor:   把 incoming 价格换算到 cached 口径的乘数
    - overlaps: 参与估算的重叠交易日数
    - max_deviation: 对齐后重叠区间的最大相对偏差
                   （超过阈值说明两个源的数据本身不一致，不可用）
    """
    series: AdjustedPriceSeries
    factor: Decimal
    overlaps: int
    max_deviation: Decimal
    applied: bool
    incompatible: bool = False      # 口径本质上不可换算（如复权 <-> 不复权）

    def to_dict(self) -> dict:
        return {
            'factor': float(self.factor),
            'overlaps': self.overlaps,
            'max_deviation': float(self.max_deviation),
            'applied': self.applied,
            'incompatible': self.incompatible,
        }


class PriceBasisAligner:
    """
    复权口径对齐器（领域服务）

    背景:
    后复权与前复权都是同一条"总收益序列"的常数倍缩放:
        hfq_t = k * qfq_t  （k 为常数）
    因此可用重叠交易日估算 k，把 incoming 换算到 cached 口径后再拼接。

    纯函数，无 IO。
    """

    @staticmethod
    def align(cached: AdjustedPriceSeries,
              incoming: AdjustedPriceSeries,
              cached_adjustment: PriceAdjustment,
              incoming_adjustment: PriceAdjustment,
              factor: Optional[Decimal] = None) -> BasisAlignment:
        """
        把 incoming 对齐到 cached 的口径

        Args:
            cached: 缓存序列
            incoming: 新拉取序列
            cached_adjustment: 缓存口径
            incoming_adjustment: 新数据口径
            factor: 已知倍率（可选）；None 时从重叠区间估算

        Returns:
            BasisAlignment（applied=False 表示口径一致、未做换算）
        """
        # 口径本质上不可换算（复权 <-> 不复权）：拒绝"对齐"
        if not cached_adjustment.can_align_by_factor(incoming_adjustment):
            return BasisAlignment(
                series=incoming,
                factor=Decimal("0"),
                overlaps=0,
                max_deviation=Decimal("0"),
                applied=False,
                incompatible=True,
            )

        # 同口径：无需换算
        if cached_adjustment.is_directly_mergeable(incoming_adjustment):
            overlaps, deviation = cached.overlap_close_deviation(incoming)
            return BasisAlignment(
                series=incoming,
                factor=Decimal("1"),
                overlaps=overlaps,
                max_deviation=deviation if deviation is not None else Decimal("0"),
                applied=False,
            )

        estimated, overlaps = (factor, 0) if factor is not None \
            else PriceBasisAligner.estimate_factor(cached, incoming)

        if estimated is None or estimated <= 0:
            return BasisAlignment(
                series=incoming,
                factor=Decimal("0"),
                overlaps=overlaps,
                max_deviation=Decimal("0"),
                applied=False,
            )

        scaled = PriceBasisAligner.rescale(incoming, estimated)
        _, deviation = cached.overlap_close_deviation(scaled)

        return BasisAlignment(
            series=scaled,
            factor=estimated,
            overlaps=overlaps,
            max_deviation=deviation if deviation is not None else Decimal("0"),
            applied=True,
        )

    @staticmethod
    def estimate_factor(cached: AdjustedPriceSeries,
                        incoming: AdjustedPriceSeries
                        ) -> Tuple[Optional[Decimal], int]:
        """
        用重叠交易日估算倍率 k = cached_close / incoming_close 的中位数

        Returns:
            (倍率, 重叠点数)；无重叠返回 (None, 0)
        """
        incoming_by_date = {p.date: p for p in incoming.prices}

        ratios = []
        for point in cached.prices:
            counterpart = incoming_by_date.get(point.date)
            if counterpart is None or counterpart.close <= 0:
                continue
            ratios.append(point.close / counterpart.close)

        if not ratios:
            return None, 0

        ratios.sort()
        middle = len(ratios) // 2
        if len(ratios) % 2 == 1:
            factor = ratios[middle]
        else:
            factor = (ratios[middle - 1] + ratios[middle]) / Decimal("2")

        return factor, len(ratios)

    @staticmethod
    def rescale(series: AdjustedPriceSeries,
                factor: Decimal) -> AdjustedPriceSeries:
        """把整条序列的价格按倍率缩放（成交量/额不变）"""
        from src.domain.price.entities import PricePoint

        if factor <= 0:
            raise ValueError(f"缩放倍率必须>0: {factor}")

        scaled = [
            PricePoint(
                date=p.date,
                open=p.open * factor,
                high=p.high * factor,
                low=p.low * factor,
                close=p.close * factor,
                volume=p.volume,
                amount=p.amount,
            )
            for p in series.prices
        ]

        return AdjustedPriceSeries(
            asset_code=series.asset_code,
            asset_type=series.asset_type,
            prices=scaled,
            source=series.source,
            calculated_at=series.calculated_at,
        )
