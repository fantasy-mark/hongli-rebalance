"""
交易执行领域实体

核心实体: TradeOrder
值对象: ExecutionResult, ExecutionContext
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Dict, Any, Tuple
import uuid


class OrderSide(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"  # 待执行
    FILLED = "filled"  # 已成交
    PARTIALLY_FILLED = "partially_filled"  # 部分成交
    CANCELLED = "cancelled"  # 已取消
    REJECTED = "rejected"  # 已拒绝


@dataclass(frozen=True)
class ExecutionResult:
    """
    执行结果（值对象）
    
    不变量:
    - filled_quantity >= 0
    - average_price >= 0 (如果filled_quantity > 0)
    """
    success: bool
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    message: str = ""
    
    def __post_init__(self):
        if self.filled_quantity < 0:
            raise ValueError(f"filled_quantity不能为负: {self.filled_quantity}")
        
        if self.filled_quantity > 0 and self.average_price <= 0:
            raise ValueError("成交数量>0时，平均价格必须>0")
    
    @property
    def fill_rate(self) -> Decimal:
        """成交率（需要外部传入订单数量来计算）"""
        # 这个方法需要在使用时传入订单数量
        pass
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'success': self.success,
            'filled_quantity': float(self.filled_quantity),
            'average_price': float(self.average_price),
            'total_cost': float(self.total_cost),
            'slippage': float(self.slippage),
            'commission': float(self.commission),
            'message': self.message,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ExecutionResult':
        """从字典创建"""
        return cls(
            success=data['success'],
            filled_quantity=Decimal(str(data.get('filled_quantity', 0))),
            average_price=Decimal(str(data.get('average_price', 0))),
            total_cost=Decimal(str(data.get('total_cost', 0))),
            slippage=Decimal(str(data.get('slippage', 0))),
            commission=Decimal(str(data.get('commission', 0))),
            message=data.get('message', ''),
        )


@dataclass
class TradeOrder:
    """
    交易订单（实体）
    
    职责:
    - 表示一笔交易指令
    - 跟踪订单状态和执行结果
    """
    asset_code: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    strategy_id: Optional[str] = None
    policy_id: Optional[str] = None
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    executed_at: Optional[datetime] = None
    execution_result: Optional[ExecutionResult] = None
    
    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError(f"订单数量必须>0: {self.quantity}")
        
        if self.side == OrderSide.SELL and self.limit_price is not None:
            if self.limit_price <= 0:
                raise ValueError(f"限价必须>0: {self.limit_price}")
        
        # 限价单必须有价格
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("限价单必须指定limit_price")
    
    @property
    def estimated_value(self) -> Optional[Decimal]:
        """预估订单价值"""
        if self.order_type == OrderType.LIMIT and self.limit_price:
            return self.quantity * self.limit_price
        return None  # 市价单无法预估
    
    def execute(self, execution_price: Decimal, 
                commission: Decimal = Decimal("0"),
                slippage: Decimal = Decimal("0")) -> ExecutionResult:
        """
        模拟执行订单
        
        Args:
            execution_price: 成交价格
            commission: 佣金
            slippage: 滑点
            
        Returns:
            执行结果
        """
        if self.status != OrderStatus.PENDING:
            return ExecutionResult(
                success=False,
                message=f"订单状态为{self.status.value}，无法执行"
            )
        
        # 检查限价
        if self.order_type == OrderType.LIMIT and self.limit_price:
            if self.side == OrderSide.BUY and execution_price > self.limit_price:
                return ExecutionResult(
                    success=False,
                    message=f"买入价{execution_price}超过限价{self.limit_price}"
                )
            elif self.side == OrderSide.SELL and execution_price < self.limit_price:
                return ExecutionResult(
                    success=False,
                    message=f"卖出价{execution_price}低于限价{self.limit_price}"
                )
        
        # 计算总成本
        trade_amount = self.quantity * execution_price
        total_cost = trade_amount + commission + slippage
        
        # 更新订单状态
        self.status = OrderStatus.FILLED
        self.executed_at = datetime.now()
        self.execution_result = ExecutionResult(
            success=True,
            filled_quantity=self.quantity,
            average_price=execution_price,
            total_cost=total_cost,
            slippage=slippage,
            commission=commission,
            message="订单执行成功",
        )
        
        return self.execution_result
    
    def cancel(self) -> bool:
        """取消订单"""
        if self.status == OrderStatus.PENDING:
            self.status = OrderStatus.CANCELLED
            self.execution_result = ExecutionResult(
                success=False,
                message="订单已取消"
            )
            return True
        return False
    
    def reject(self, reason: str) -> None:
        """拒绝订单"""
        self.status = OrderStatus.REJECTED
        self.execution_result = ExecutionResult(
            success=False,
            message=f"订单被拒绝: {reason}"
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'asset_code': self.asset_code,
            'side': self.side.value,
            'quantity': float(self.quantity),
            'order_type': self.order_type.value,
            'limit_price': float(self.limit_price) if self.limit_price else None,
            'strategy_id': self.strategy_id,
            'policy_id': self.policy_id,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'executed_at': self.executed_at.isoformat() if self.executed_at else None,
            'execution_result': self.execution_result.to_dict() if self.execution_result else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradeOrder':
        """从字典创建"""
        order = cls(
            asset_code=data['asset_code'],
            side=OrderSide(data['side']),
            quantity=Decimal(str(data['quantity'])),
            order_type=OrderType(data.get('order_type', 'market')),
            limit_price=Decimal(str(data['limit_price'])) if data.get('limit_price') else None,
            strategy_id=data.get('strategy_id'),
            policy_id=data.get('policy_id'),
            order_id=data.get('order_id', str(uuid.uuid4())),
            status=OrderStatus(data.get('status', 'pending')),
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else datetime.now(),
            executed_at=datetime.fromisoformat(data['executed_at']) if data.get('executed_at') else None,
        )
        
        if data.get('execution_result'):
            order.execution_result = ExecutionResult.from_dict(data['execution_result'])
        
        return order
    
    def __repr__(self) -> str:
        return (
            f"TradeOrder(id={self.order_id}, {self.side.value} {self.quantity} "
            f"{self.asset_code}, status={self.status.value})"
        )


@dataclass(frozen=True)
class ExecutionContext:
    """
    执行上下文（值对象）
    
    包含执行订单所需的环境信息
    """
    timestamp: datetime
    available_cash: Decimal
    current_positions: Dict[str, Decimal]  # 资产代码 -> 持仓数量
    current_prices: Dict[str, Decimal]  # 资产代码 -> 当前价格
    commission_rate: Decimal = Decimal("0.0003")
    slippage_rate: Decimal = Decimal("0.001")
    
    def can_execute(self, order: TradeOrder) -> Tuple[bool, str]:
        """
        检查订单是否可以执行
        
        Returns:
            (是否可以执行, 原因)
        """
        if order.side == OrderSide.BUY:
            # 检查资金是否充足
            price = self.current_prices.get(order.asset_code, Decimal("0"))
            estimated_cost = order.quantity * price
            if estimated_cost > self.available_cash:
                return False, f"资金不足，需要{estimated_cost}，可用{self.available_cash}"
        
        elif order.side == OrderSide.SELL:
            # 检查持仓是否充足
            current_position = self.current_positions.get(order.asset_code, Decimal("0"))
            if current_position < order.quantity:
                return False, f"持仓不足，需要{order.quantity}，当前{current_position}"
        
        return True, ""
    
    def calculate_execution_price(self, order: TradeOrder) -> Decimal:
        """计算执行价格（考虑滑点）"""
        base_price = self.current_prices.get(order.asset_code, Decimal("0"))
        
        if order.order_type == OrderType.LIMIT and order.limit_price:
            return order.limit_price
        
        # 市价单加滑点
        if order.side == OrderSide.BUY:
            return base_price * (Decimal("1") + self.slippage_rate)
        else:
            return base_price * (Decimal("1") - self.slippage_rate)
    
    def calculate_commission(self, trade_amount: Decimal) -> Decimal:
        """计算佣金"""
        commission = trade_amount * self.commission_rate
        # 最低佣金5元
        return max(commission, Decimal("5"))



