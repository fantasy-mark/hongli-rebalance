"""
指标类值对象（数据模块）

包含: ReturnMetrics, TechnicalIndicators
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


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
