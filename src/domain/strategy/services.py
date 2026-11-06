"""
策略领域服务

SignalGenerator: 信号生成器
StrategyBacktester: 策略回测器
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

from src.domain.strategy.entities import TradingStrategy, Action, Signal
from src.domain.price.entities import AdjustedPriceSeries


@dataclass
class BacktestResult:
    """回测结果"""
    strategy_id: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    final_value: Decimal
    total_return: Decimal
    annualized_return: Decimal
    sharpe_ratio: Decimal
    max_drawdown: Decimal
    total_trades: int
    win_rate: Decimal
    trades: List[Dict[str, Any]] = field(default_factory=list)
    
    @property
    def years(self) -> float:
        """年数"""
        return (self.end_date - self.start_date).days / 365.25
    
    def summary(self) -> str:
        """生成摘要"""
        return (
            f"策略回测结果\n"
            f"{'='*60}\n"
            f"回测期间: {self.start_date} ~ {self.end_date}\n"
            f"初始资金: {self.initial_capital:,.0f}\n"
            f"最终市值: {self.final_value:,.0f}\n"
            f"\n"
            f"收益指标:\n"
            f"  累计收益: {float(self.total_return):.2%}\n"
            f"  年化收益: {float(self.annualized_return):.2%}\n"
            f"  夏普比率: {float(self.sharpe_ratio):.2f}\n"
            f"  最大回撤: {float(self.max_drawdown):.2%}\n"
            f"\n"
            f"交易统计:\n"
            f"  总交易次数: {self.total_trades}\n"
            f"  胜率: {float(self.win_rate):.2%}\n"
        )


class SignalGenerator:
    """
    信号生成器（领域服务）
    
    职责:
    - 根据策略和市场数据生成交易信号
    - 无状态，纯函数式
    """
    
    @staticmethod
    def generate_signals(strategy: TradingStrategy, 
                        market_data: Dict[str, Any]) -> List[Action]:
        """
        生成交易信号
        
        Args:
            strategy: 交易策略
            market_data: 市场数据
            
        Returns:
            触发的动作列表
        """
        return strategy.evaluate(market_data)
    
    @staticmethod
    def prepare_market_data(price_series: AdjustedPriceSeries,
                           indicators: Optional[Dict[str, Decimal]] = None) -> Dict[str, Any]:
        """
        准备市场数据
        
        Args:
            price_series: 价格序列
            indicators: 技术指标
            
        Returns:
            市场数据字典
        """
        if not price_series.prices:
            return {}
        
        latest_price = price_series.latest_price
        
        data = {
            'current_price': latest_price.close,
            'open': latest_price.open,
            'high': latest_price.high,
            'low': latest_price.low,
            'volume': latest_price.volume,
            'date': latest_price.date,
        }
        
        # 添加前一日价格（用于交叉判断）
        if len(price_series.prices) >= 2:
            prev_price = price_series.prices[-2]
            data['prev_close'] = prev_price.close
        
        # 添加技术指标
        if indicators:
            data.update(indicators)
        
        return data


class StrategyBacktester:
    """
    策略回测器（领域服务）
    
    职责:
    - 在历史数据上回测策略
    - 计算绩效指标
    """
    
    @staticmethod
    def backtest(strategy: TradingStrategy,
                price_series: AdjustedPriceSeries,
                initial_capital: Decimal = Decimal("1000000"),
                commission_rate: Decimal = Decimal("0.0003")) -> BacktestResult:
        """
        回测策略
        
        Args:
            strategy: 交易策略
            price_series: 历史价格序列
            initial_capital: 初始资金
            commission_rate: 佣金率
            
        Returns:
            回测结果
        """
        if not price_series.prices:
            raise ValueError("价格序列为空")
        
        # 初始化
        cash = initial_capital
        position = Decimal("0")
        portfolio_value = initial_capital
        trades = []
        peak_value = initial_capital
        max_dd = Decimal("0")
        
        # 遍历每个交易日
        for i in range(1, len(price_series.prices)):
            current_price_point = price_series.prices[i]
            prev_price_point = price_series.prices[i-1]
            
            # 准备市场数据
            market_data = {
                'current_price': current_price_point.close,
                'prev_close': prev_price_point.close,
                'open': current_price_point.open,
                'high': current_price_point.high,
                'low': current_price_point.low,
                'volume': current_price_point.volume,
                'date': current_price_point.date,
            }
            
            # 生成信号
            actions = strategy.evaluate(market_data)
            
            # 执行交易（简化：只考虑第一个BUY/SELL动作）
            for action in actions:
                if action.type.value == 'buy' and cash > 0:
                    # 买入
                    buy_amount = cash * (action.target_weight or Decimal("0.25"))
                    quantity = buy_amount / current_price_point.close
                    commission = buy_amount * commission_rate
                    
                    cash -= (buy_amount + commission)
                    position += quantity
                    
                    trades.append({
                        'date': current_price_point.date,
                        'side': 'buy',
                        'price': float(current_price_point.close),
                        'quantity': float(quantity),
                        'amount': float(buy_amount),
                    })
                
                elif action.type.value == 'sell' and position > 0:
                    # 卖出
                    sell_quantity = position * (Decimal("1") - (action.target_weight or Decimal("0")))
                    sell_amount = sell_quantity * current_price_point.close
                    commission = sell_amount * commission_rate
                    
                    cash += (sell_amount - commission)
                    position -= sell_quantity
                    
                    trades.append({
                        'date': current_price_point.date,
                        'side': 'sell',
                        'price': float(current_price_point.close),
                        'quantity': float(sell_quantity),
                        'amount': float(sell_amount),
                    })
            
            # 计算组合价值
            portfolio_value = cash + position * current_price_point.close
            
            # 更新最大回撤
            if portfolio_value > peak_value:
                peak_value = portfolio_value
            
            drawdown = (peak_value - portfolio_value) / peak_value
            if drawdown > max_dd:
                max_dd = drawdown
        
        # 计算绩效指标
        final_value = portfolio_value
        total_return = (final_value - initial_capital) / initial_capital
        
        days = (price_series.prices[-1].date - price_series.prices[0].date).days
        years = days / 365.25
        
        if years > 0:
            annualized_return = Decimal(str(float(final_value / initial_capital) ** (1.0 / years))) - Decimal("1")
        else:
            annualized_return = Decimal("0")
        
        # 简化夏普比率计算
        sharpe_ratio = annualized_return / Decimal("0.15") if annualized_return > 0 else Decimal("0")
        
        # 计算胜率
        winning_trades = sum(1 for t in trades if t['side'] == 'sell')
        win_rate = Decimal(str(winning_trades)) / Decimal(str(len(trades))) if trades else Decimal("0")
        
        return BacktestResult(
            strategy_id=strategy.strategy_id,
            start_date=price_series.prices[0].date,
            end_date=price_series.prices[-1].date,
            initial_capital=initial_capital,
            final_value=final_value,
            total_return=total_return,
            annualized_return=annualized_return,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_dd,
            total_trades=len(trades),
            win_rate=win_rate,
            trades=trades,
        )
