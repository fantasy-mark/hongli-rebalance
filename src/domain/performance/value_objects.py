"""
绩效统计领域模型

核心值对象: ReturnStatistics
领域服务:   PerformanceCalculator

这是被再平衡、网格、组合三个策略共用的共享内核，
保证不同策略的绩效指标口径完全一致（之前三处各写一遍，口径已出现漂移）。
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ReturnStatistics:
    """
    绩效统计（值对象，不可变）

    不变量:
    - trading_days >= 0
    - max_drawdown <= 0
    - volatility >= 0

    属性说明:
    - base_value: 计算收益率的基准市值（默认取序列首日）
    - final_value: 序列末日市值
    """
    total_return: Decimal
    annualized_return: Decimal
    volatility: Decimal
    sharpe_ratio: Decimal
    max_drawdown: Decimal
    max_drawdown_start: date
    max_drawdown_end: date
    start_date: date
    end_date: date
    trading_days: int
    base_value: Decimal
    final_value: Decimal

    def __post_init__(self):
        if self.trading_days < 0:
            raise ValueError(f"交易日数不能为负: {self.trading_days}")
        if self.max_drawdown > 0:
            raise ValueError(f"最大回撤应为非正数: {self.max_drawdown}")
        if self.volatility < 0:
            raise ValueError(f"波动率不能为负: {self.volatility}")

    @property
    def years(self) -> Decimal:
        """回测跨越的年数"""
        days = (self.end_date - self.start_date).days
        return Decimal(str(days)) / Decimal("365.25")

    def to_dict(self) -> Dict[str, Any]:
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
            'base_value': float(self.base_value),
            'final_value': float(self.final_value),
        }

    def summary(self) -> str:
        """可读摘要"""
        return (
            f"绩效统计:\n"
            f"  期间: {self.start_date} ~ {self.end_date} ({self.trading_days}个交易日)\n"
            f"  基准市值: {self.base_value:,.2f}  期末市值: {self.final_value:,.2f}\n"
            f"  累计收益: {self.total_return:.2%}\n"
            f"  年化收益: {self.annualized_return:.2%}\n"
            f"  波动率:   {self.volatility:.2%}\n"
            f"  夏普比率: {self.sharpe_ratio:.2f}\n"
            f"  最大回撤: {self.max_drawdown:.2%} "
            f"({self.max_drawdown_start} ~ {self.max_drawdown_end})"
        )


class PerformanceCalculator:
    """
    绩效计算器（领域服务）

    纯函数，无 IO。所有策略共用同一套算法，保证口径一致。
    """

    # 年化交易日数
    TRADING_DAYS_PER_YEAR = 252

    # 默认无风险利率
    DEFAULT_RISK_FREE_RATE = Decimal("0.03")

    @staticmethod
    def calculate(dates: List[date],
                  values: List[Decimal],
                  base_value: Optional[Decimal] = None,
                  risk_free_rate: Optional[Decimal] = None) -> ReturnStatistics:
        """
        计算绩效统计

        Args:
            dates: 与 values 等长的日期序列
            values: 市值序列（正序）
            base_value: 收益率基准，默认取 values[0]
            risk_free_rate: 无风险利率，默认 3%

        Returns:
            ReturnStatistics（数据不足时各项为 0）
        """
        risk_free = (risk_free_rate if risk_free_rate is not None
                     else PerformanceCalculator.DEFAULT_RISK_FREE_RATE)

        if not dates or not values or len(dates) != len(values):
            return PerformanceCalculator._empty()

        start_date = dates[0]
        end_date = dates[-1]
        base = base_value if base_value is not None else values[0]
        final = values[-1]

        total_return = PerformanceCalculator._total_return(final, base)
        annualized = PerformanceCalculator._annualized_return(
            total_return, start_date, end_date)
        volatility = PerformanceCalculator._volatility(values)
        sharpe = PerformanceCalculator._sharpe(annualized, volatility, risk_free)
        max_dd, dd_start, dd_end = PerformanceCalculator._max_drawdown(dates, values)

        return ReturnStatistics(
            total_return=total_return,
            annualized_return=annualized,
            volatility=volatility,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            max_drawdown_start=dd_start,
            max_drawdown_end=dd_end,
            start_date=start_date,
            end_date=end_date,
            trading_days=len(dates),
            base_value=base,
            final_value=final,
        )

    # ------------------------------------------------------------------
    # 内部算法
    # ------------------------------------------------------------------

    @staticmethod
    def _empty() -> ReturnStatistics:
        today = date.today()
        zero = Decimal("0")
        return ReturnStatistics(
            total_return=zero,
            annualized_return=zero,
            volatility=zero,
            sharpe_ratio=zero,
            max_drawdown=zero,
            max_drawdown_start=today,
            max_drawdown_end=today,
            start_date=today,
            end_date=today,
            trading_days=0,
            base_value=zero,
            final_value=zero,
        )

    @staticmethod
    def _total_return(final_value: Decimal, base_value: Decimal) -> Decimal:
        if base_value <= 0:
            return Decimal("0")
        return (final_value - base_value) / base_value

    @staticmethod
    def _annualized_return(total_return: Decimal,
                           start_date: date,
                           end_date: date) -> Decimal:
        days = (end_date - start_date).days
        if days <= 0:
            return Decimal("0")

        years = Decimal(str(days)) / Decimal("365.25")
        if years <= 0:
            return Decimal("0")

        ratio = 1.0 + float(total_return)
        if ratio <= 0:
            return Decimal("-1")

        cagr = ratio ** (1.0 / float(years)) - 1.0
        return Decimal(str(cagr))

    @staticmethod
    def _volatility(values: List[Decimal]) -> Decimal:
        """
        年化波动率（日收益率的样本标准差 * sqrt(252)）
        """
        if len(values) < 2:
            return Decimal("0")

        returns = []
        for i in range(1, len(values)):
            previous = values[i - 1]
            if previous <= 0:
                continue
            returns.append((values[i] - previous) / previous)

        if len(returns) < 2:
            return Decimal("0")

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        daily_vol = Decimal(str(variance)).sqrt()

        return daily_vol * Decimal(str(PerformanceCalculator.TRADING_DAYS_PER_YEAR)).sqrt()

    @staticmethod
    def _sharpe(annualized_return: Decimal,
                volatility: Decimal,
                risk_free_rate: Decimal) -> Decimal:
        if volatility <= 0:
            return Decimal("0")
        return (annualized_return - risk_free_rate) / volatility

    @staticmethod
    def _max_drawdown(dates: List[date],
                      values: List[Decimal]):
        """返回 (最大回撤, 峰值日, 谷底日)"""
        if not values:
            today = date.today()
            return Decimal("0"), today, today

        peak_value = values[0]
        peak_date = dates[0]
        max_dd = Decimal("0")
        dd_start = dates[0]
        dd_end = dates[0]

        for i in range(1, len(values)):
            current = values[i]
            if current > peak_value:
                peak_value = current
                peak_date = dates[i]

            if peak_value <= 0:
                continue

            drawdown = (current - peak_value) / peak_value
            if drawdown < max_dd:
                max_dd = drawdown
                dd_start = peak_date
                dd_end = dates[i]

        return max_dd, dd_start, dd_end
