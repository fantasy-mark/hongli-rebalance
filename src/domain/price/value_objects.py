"""
价格领域值对象

包含: ReturnMetrics, DateRange, TechnicalIndicators, CachePolicy, RefreshPlan
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple


@dataclass(frozen=True)
class ReturnMetrics:
    """
    收益率指标值对象
    
    所有字段都是不可变的，确保计算结果的一致性
    """
    total_return: Decimal  # 累计收益率
    annualized_return: Decimal  # 年化收益率 (CAGR)
    volatility: Decimal  # 年化波动率
    sharpe_ratio: Decimal  # 夏普比率
    max_drawdown: Decimal  # 最大回撤（负值）
    max_drawdown_start: date  # 最大回撤开始日期
    max_drawdown_end: date  # 最大回撤结束日期
    start_date: date
    end_date: date
    trading_days: int
    
    def __post_init__(self):
        # 验证合理性
        if self.trading_days <= 0:
            raise ValueError("交易日数必须大于0")
        if self.volatility < 0:
            raise ValueError("波动率不能为负")
    
    @property
    def years(self) -> Decimal:
        """年数"""
        return Decimal(str((self.end_date - self.start_date).days)) / Decimal("365.25")
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'total_return': float(self.total_return),
            'annualized_return': float(self.annualized_return),
            'volatility': float(self.volatility),
            'sharpe_ratio': float(self.sharpe_ratio),
            'max_drawdown': float(self.max_drawdown),
            'max_drawdown_start': self.max_drawdown_start.isoformat(),
            'max_drawdown_end': self.max_drawdown_end.isoformat(),
            'start_date': self.start_date.isoformat(),
            'end_date': self.end_date.isoformat(),
            'trading_days': self.trading_days,
            'years': float(self.years),
        }
    
    def summary(self) -> str:
        """生成可读摘要"""
        return (
            f"收益率指标:\n"
            f"  期间: {self.start_date} ~ {self.end_date} ({self.trading_days}个交易日)\n"
            f"  累计收益: {self.total_return:.2%}\n"
            f"  年化收益: {self.annualized_return:.2%}\n"
            f"  波动率: {self.volatility:.2%}\n"
            f"  夏普比率: {self.sharpe_ratio:.2f}\n"
            f"  最大回撤: {self.max_drawdown:.2%} "
            f"({self.max_drawdown_start} ~ {self.max_drawdown_end})"
        )


@dataclass(frozen=True)
class DateRange:
    """
    日期范围值对象
    
    不变量:
    - start <= end
    """
    start: date
    end: date
    
    def __post_init__(self):
        if self.start > self.end:
            raise ValueError(f"起始日期({self.start})不能晚于结束日期({self.end})")
    
    def contains(self, target: date) -> bool:
        """检查是否包含某个日期"""
        return self.start <= target <= self.end
    
    def duration_days(self) -> int:
        """持续天数"""
        return (self.end - self.start).days
    
    def intersects(self, other: 'DateRange') -> bool:
        """检查是否与另一个日期范围有交集"""
        return self.start <= other.end and other.start <= self.end
    
    def intersection(self, other: 'DateRange') -> Optional['DateRange']:
        """计算交集"""
        if not self.intersects(other):
            return None
        
        new_start = max(self.start, other.start)
        new_end = min(self.end, other.end)
        return DateRange(start=new_start, end=new_end)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'duration_days': self.duration_days(),
        }
    
    def __str__(self) -> str:
        return f"{self.start} ~ {self.end}"


@dataclass(frozen=True)
class TechnicalIndicators:
    """
    技术指标值对象
    
    包含常用的技术分析指标
    """
    ma_5: Decimal  # 5日均线
    ma_10: Decimal  # 10日均线
    ma_20: Decimal  # 20日均线
    ma_60: Decimal  # 60日均线
    rsi_14: Decimal  # 14日RSI
    bollinger_upper: Decimal  # 布林带上轨
    bollinger_middle: Decimal  # 布林带中轨
    bollinger_lower: Decimal  # 布林带下轨
    
    def is_above_ma20(self, current_price: Decimal) -> bool:
        """判断是否在20日均线上方"""
        return current_price > self.ma_20
    
    def is_golden_cross(self) -> bool:
        """判断是否金叉（MA5上穿MA20）"""
        return self.ma_5 > self.ma_20
    
    def is_death_cross(self) -> bool:
        """判断是否死叉（MA5下穿MA20）"""
        return self.ma_5 < self.ma_20
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'ma_5': float(self.ma_5),
            'ma_10': float(self.ma_10),
            'ma_20': float(self.ma_20),
            'ma_60': float(self.ma_60),
            'rsi_14': float(self.rsi_14),
            'bollinger_upper': float(self.bollinger_upper),
            'bollinger_middle': float(self.bollinger_middle),
            'bollinger_lower': float(self.bollinger_lower),
        }





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
    """
    overlap_days: int = 5
    seam_tolerance: Decimal = Decimal("0.001")
    min_refresh_interval_hours: int = 0

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
