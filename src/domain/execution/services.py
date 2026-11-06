"""
执行领域服务

OrderExecutor: 订单执行器
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional

from src.domain.execution.entities import TradeOrder, ExecutionContext, ExecutionResult


class OrderExecutor:
    """
    订单执行器（领域服务）
    
    职责:
    - 执行单个或批量订单
    - 模拟成交环境
    """
    
    @staticmethod
    def execute_order(order: TradeOrder,
                     context: ExecutionContext) -> ExecutionResult:
        """
        执行单个订单
        
        Args:
            order: 交易订单
            context: 执行上下文
            
        Returns:
            执行结果
        """
        # 检查是否可以执行
        can_execute, reason = context.can_execute(order)
        if not can_execute:
            order.reject(reason)
            return order.execution_result
        
        # 计算执行价格
        execution_price = context.calculate_execution_price(order)
        
        # 计算佣金
        trade_amount = order.quantity * execution_price
        commission = context.calculate_commission(trade_amount)
        
        # 计算滑点
        slippage = trade_amount * context.slippage_rate
        
        # 执行订单
        result = order.execute(execution_price, commission, slippage)
        
        return result
    
    @staticmethod
    def batch_execute(orders: List[TradeOrder],
                     context: ExecutionContext) -> List[ExecutionResult]:
        """
        批量执行订单
        
        Args:
            orders: 订单列表
            context: 执行上下文
            
        Returns:
            执行结果列表
        """
        results = []
        
        for order in orders:
            result = OrderExecutor.execute_order(order, context)
            results.append(result)
            
            # 更新上下文（模拟资金和持仓变化）
            if result.success:
                if order.side.value == 'buy':
                    context.available_cash -= result.total_cost
                    current_pos = context.current_positions.get(order.asset_code, Decimal("0"))
                    context.current_positions[order.asset_code] = current_pos + result.filled_quantity
                else:
                    context.available_cash += (result.filled_quantity * result.average_price - result.commission)
                    current_pos = context.current_positions.get(order.asset_code, Decimal("0"))
                    context.current_positions[order.asset_code] = current_pos - result.filled_quantity
        
        return results
    
    @staticmethod
    def create_market_context(timestamp: datetime,
                             cash: Decimal,
                             positions: Dict[str, Decimal],
                             prices: Dict[str, Decimal],
                             commission_rate: Decimal = Decimal("0.0003"),
                             slippage_rate: Decimal = Decimal("0.001")) -> ExecutionContext:
        """
        创建市场执行上下文
        
        Args:
            timestamp: 时间戳
            cash: 可用现金
            positions: 当前持仓
            prices: 当前价格
            commission_rate: 佣金率
            slippage_rate: 滑点率
            
        Returns:
            执行上下文
        """
        return ExecutionContext(
            timestamp=timestamp,
            available_cash=cash,
            current_positions=positions,
            current_prices=prices,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate,
        )
