"""
交易内核共享模型

核心值对象: LotSize（最小交易单位）

再平衡与网格两个策略都需要"按整手取整"这条市场规则，
过去各写一份实现（_round_to_lot_size / floor_to_lot），容易漂移。
此处提取为共享内核，两个上下文共同引用。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict


@dataclass(frozen=True)
class LotSize:
    """
    最小交易单位（值对象，不可变）

    A 股 / ETF 均为 100 股一手。

    不变量:
    - shares >= 1

    示例:
        LotSize(100).floor(Decimal("6666.67"))  -> Decimal("6600")
    """
    shares: int = 100

    def __post_init__(self):
        if self.shares < 1:
            raise ValueError(f"最小交易单位必须>=1: {self.shares}")

    def floor(self, quantity: Decimal) -> Decimal:
        """
        向下取整到最小交易单位的整数倍

        Args:
            quantity: 原始数量

        Returns:
            取整后的数量（不足一手时为 0）
        """
        if quantity <= 0:
            return Decimal("0")

        lots = int(quantity) // self.shares
        return Decimal(str(lots * self.shares))

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {'shares': self.shares}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LotSize':
        """从字典创建"""
        return cls(shares=int(data.get('shares', 100)))


# 常用默认值：A 股一手 = 100 股
DEFAULT_LOT_SIZE = LotSize(100)
