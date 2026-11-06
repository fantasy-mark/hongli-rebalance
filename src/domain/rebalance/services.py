"""
再平衡领域服务

RebalanceEngine: 再平衡引擎
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

from src.domain.rebalance.entities import RebalancePolicy
from src.domain.execution.entities import TradeOrder, OrderSide, OrderType


@dataclass
class RebalanceResult:
    """再平衡结果"""
    policy_id: str
    triggered: bool
    orders: List[TradeOrder]
    total_cost: Decimal
    estimated_turnover: Decimal
    timestamp: datetime
    
    def summary(self) -> str:
        """生成摘要"""
        if not self.triggered:
            return "未触发再平衡"
        
        return (
            f"再平衡结果\n"
            f"{'='*60}\n"
            f"订单数量: {len(self.orders)}\n"
            f"预估成本: {self.total_cost:.2f}\n"
            f"预估换手率: {float(self.estimated_turnover):.2%}\n"
            f"\n"
            f"订单明细:\n" +
            "\n".join([
                f"  {o.side.value.upper()} {o.quantity} {o.asset_code}"
                for o in self.orders
            ])
        )


class RebalanceEngine:
    """
    再平衡引擎（领域服务）
    
    职责:
    - 检查是否触发再平衡
    - 计算再平衡订单
    - 优化订单以满足约束
    """
    
    @staticmethod
    def check_trigger(policy: RebalancePolicy,
                     current_state: Dict[str, Any]) -> bool:
        """
        检查是否触发再平衡
        
        Args:
            policy: 再平衡策略
            current_state: 当前状态
            
        Returns:
            是否触发
        """
        return policy.should_rebalance(current_state)
    
    @staticmethod
    def calculate_orders(policy: RebalancePolicy,
                        current_state: Dict[str, Any],
                        strategy_id: Optional[str] = None) -> List[TradeOrder]:
        """
        计算再平衡订单
        
        Args:
            policy: 再平衡策略
            current_state: 当前状态
                - current_weights: Dict[str, Decimal]
                - portfolio_value: Decimal
                - prices: Dict[str, Decimal]
            strategy_id: 关联的策略ID
            
        Returns:
            订单列表
        """
        orders_data, total_cost = policy.calculate_rebalance_orders(current_state)
        
        # 转换为TradeOrder对象
        orders = []
        for order_data in orders_data:
            order = TradeOrder(
                asset_code=order_data['asset_code'],
                side=OrderSide.BUY if order_data['side'] == 'BUY' else OrderSide.SELL,
                quantity=order_data['quantity'],
                order_type=OrderType.MARKET,
                strategy_id=strategy_id,
                policy_id=policy.policy_id,
            )
            orders.append(order)
        
        return orders
    
    @staticmethod
    def optimize_orders(orders: List[TradeOrder],
                       constraints: Any,
                       portfolio_value: Decimal) -> List[TradeOrder]:
        """
        优化订单以满足约束
        
        Args:
            orders: 原始订单列表
            constraints: 约束条件
            portfolio_value: 组合总值
            
        Returns:
            优化后的订单列表
        """
        # 简化实现：过滤掉不符合约束的订单
        optimized = []
        
        for order in orders:
            # 检查最小交易金额
            if hasattr(constraints, 'min_trade_size'):
                price = order.current_prices.get(order.asset_code, Decimal("0")) if hasattr(order, 'current_prices') else Decimal("0")
                amount = order.quantity * price
                
                if amount < constraints.min_trade_size:
                    continue
            
            optimized.append(order)
        
        return optimized
    
    @staticmethod
    def simulate_rebalance(policy: RebalancePolicy,
                          hypothetical_state: Dict[str, Any]) -> RebalanceResult:
        """
        模拟再平衡（不实际执行）
        
        Args:
            policy: 再平衡策略
            hypothetical_state: 假设的状态
            
        Returns:
            模拟结果
        """
        # 检查是否触发
        triggered = policy.should_rebalance(hypothetical_state)
        
        if not triggered:
            return RebalanceResult(
                policy_id=policy.policy_id,
                triggered=False,
                orders=[],
                total_cost=Decimal("0"),
                estimated_turnover=Decimal("0"),
                timestamp=datetime.now(),
            )
        
        # 计算订单
        orders = RebalanceEngine.calculate_orders(policy, hypothetical_state)
        
        # 计算总成本和换手率
        total_cost = Decimal("0")
        total_amount = Decimal("0")
        portfolio_value = hypothetical_state.get('portfolio_value', Decimal("0"))
        
        for order in orders:
            # 简化：假设按当前价格成交
            price = hypothetical_state.get('prices', {}).get(order.asset_code, Decimal("0"))
            amount = order.quantity * price
            cost = policy.cost_model.calculate_cost(amount)
            
            total_cost += cost
            total_amount += amount
        
        turnover = total_amount / portfolio_value if portfolio_value > 0 else Decimal("0")
        
        return RebalanceResult(
            policy_id=policy.policy_id,
            triggered=True,
            orders=orders,
            total_cost=total_cost,
            estimated_turnover=turnover,
            timestamp=datetime.now(),
        )
