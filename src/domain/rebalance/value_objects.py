"""
再平衡领域值对象

包含: RebalanceOrder, RebalanceOrderSide
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional


class RebalanceOrderSide(Enum):
    """再平衡订单方向"""
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class RebalanceOrder:
    """
    再平衡订单（值对象）

    再平衡上下文自有的订单表示，不依赖其它限界上下文。
    若需转为统一的执行订单，由应用层的转换器负责。
    """
    asset_code: str
    side: RebalanceOrderSide
    quantity: Decimal
    price: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    strategy_id: Optional[str] = None
    policy_id: Optional[str] = None
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if not self.asset_code:
            raise ValueError("资产代码不能为空")
        if self.quantity <= 0:
            raise ValueError(f"订单数量必须>0: {self.quantity}")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'asset_code': self.asset_code,
            'side': self.side.value,
            'quantity': float(self.quantity),
            'price': float(self.price) if self.price is not None else None,
            'amount': float(self.amount) if self.amount is not None else None,
            'strategy_id': self.strategy_id,
            'policy_id': self.policy_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
