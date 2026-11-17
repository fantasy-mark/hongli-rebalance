"""
组合与持仓领域模型（共享内核）

核心值对象: Position（单资产持仓）
核心聚合根: Portfolio（组合：现金 + 各资产持仓）

定位:
这是**再平衡策略**与**网格策略**共用的共享内核。
每个策略持有**自己的 Portfolio 实例**，从而在类型层面实现持仓隔离：
两个策略的现金、持仓、成本、盈亏互不触碰。

职责:
- 统一持仓记账（买入/卖出）并保护不变量
- 提供估值、盈亏、权重计算
- 不关心"为什么交易"（那是策略上下文的事）
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid


@dataclass(frozen=True)
class Position:
    """
    单资产持仓（值对象，不可变）

    不变量:
    - shares >= 0
    - cost_basis >= 0
    """
    asset_code: str
    shares: Decimal = Decimal("0")
    cost_basis: Decimal = Decimal("0")      # 持仓成本（含手续费）
    realized_pnl: Decimal = Decimal("0")     # 该持仓累计已实现盈亏

    def __post_init__(self):
        if not self.asset_code:
            raise ValueError("资产代码不能为空")
        if self.shares < 0:
            raise ValueError(f"持仓数量不能为负: {self.shares}")
        if self.cost_basis < 0:
            raise ValueError(f"持仓成本不能为负: {self.cost_basis}")

    @property
    def average_cost(self) -> Decimal:
        """持仓均价（含费用）"""
        if self.shares <= 0:
            return Decimal("0")
        return self.cost_basis / self.shares

    @property
    def is_open(self) -> bool:
        """是否持仓中"""
        return self.shares > 0

    def market_value(self, price: Optional[Decimal]) -> Decimal:
        """按给定价格计算市值"""
        if price is None or self.shares <= 0:
            return Decimal("0")
        return self.shares * price

    def unrealized_pnl(self, price: Optional[Decimal]) -> Decimal:
        """浮动盈亏 = 市值 - 持仓成本"""
        return self.market_value(price) - self.cost_basis

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'asset_code': self.asset_code,
            'shares': float(self.shares),
            'cost_basis': float(self.cost_basis),
            'realized_pnl': float(self.realized_pnl),
            'average_cost': float(self.average_cost),
        }


@dataclass
class Portfolio:
    """
    组合（聚合根）

    包含现金与各资产持仓。
    **每个策略持有一个独立实例**，以此实现策略间的持仓隔离。

    不变量（由 buy/sell 行为保护）:
    - 持仓数量不能为负
    - 不能卖出超过持仓的数量
    - 持仓成本不能为负
    - 现金通常不为负（allow_overdraft=True 时允许透支，仅记录告警）
    """
    portfolio_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "组合"
    cash: Decimal = Decimal("0")
    positions: Dict[str, Position] = field(default_factory=dict)
    # 允许现金为负（再平衡建仓时手续费可能造成微小透支）
    allow_overdraft: bool = True

    def __post_init__(self):
        if not isinstance(self.cash, Decimal):
            self.cash = Decimal(str(self.cash))
        if self.cash < 0 and not self.allow_overdraft:
            raise ValueError(f"组合现金不能为负: {self.cash}")

    # ------------------------------------------------------------------
    # 持仓访问
    # ------------------------------------------------------------------

    def position_for(self, asset_code: str) -> Position:
        """获取（不存在则返回空仓）"""
        return self.positions.get(asset_code, Position(asset_code=asset_code))

    def shares_of(self, asset_code: str) -> Decimal:
        """某资产的持仓数量"""
        return self.position_for(asset_code).shares

    def cost_basis_of(self, asset_code: str) -> Decimal:
        """某资产的持仓成本"""
        return self.position_for(asset_code).cost_basis

    def is_flat(self, asset_code: str) -> bool:
        """该资产是否空仓（无持仓）"""
        return self.shares_of(asset_code) == 0

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------

    def buy(self, asset_code: str,
            shares: Decimal,
            amount: Decimal,
            cost: Decimal = Decimal("0")) -> Position:
        """
        买入记账

        Args:
            asset_code: 资产代码
            shares:     成交数量
            amount:     成交金额
            cost:       交易费用

        Returns:
            更新后的 Position
        """
        if shares <= 0:
            raise ValueError(f"买入数量必须>0: {shares}")
        if amount < 0 or cost < 0:
            raise ValueError(f"金额与费用不能为负: amount={amount}, cost={cost}")

        outflow = amount + cost
        if not self.allow_overdraft and (self.cash - outflow) < 0:
            raise ValueError(
                f"{asset_code} 买入后现金不足: 现有 {self.cash}，需要 {outflow}"
            )

        current = self.position_for(asset_code)
        updated = Position(
            asset_code=asset_code,
            shares=current.shares + shares,
            cost_basis=current.cost_basis + outflow,
            realized_pnl=current.realized_pnl,
        )

        self.cash -= outflow
        self.positions[asset_code] = updated
        return updated

    def sell(self, asset_code: str,
             shares: Decimal,
             amount: Decimal,
             cost: Decimal = Decimal("0"),
             cost_basis_released: Optional[Decimal] = None) -> Position:
        """
        卖出记账

        Args:
            asset_code: 资产代码
            shares:     成交数量
            amount:     成交金额
            cost:       交易费用
            cost_basis_released: 本次卖出释放的持仓成本
                （不传则按均价比例释放；网格按批次精确释放时传入）

        Returns:
            更新后的 Position
        """
        if shares <= 0:
            raise ValueError(f"卖出数量必须>0: {shares}")
        if amount < 0 or cost < 0:
            raise ValueError(f"金额与费用不能为负: amount={amount}, cost={cost}")

        current = self.position_for(asset_code)
        if current.shares < shares:
            raise ValueError(
                f"{asset_code} 卖出数量超过持仓: 持仓 {current.shares}，卖出 {shares}"
            )

        # 释放的持仓成本：默认按均价比例
        if cost_basis_released is None:
            if current.shares > 0:
                cost_basis_released = current.cost_basis * shares / current.shares
            else:
                cost_basis_released = Decimal("0")

        if cost_basis_released > current.cost_basis:
            cost_basis_released = current.cost_basis

        # 已实现盈亏 = 卖出净额 - 释放的持仓成本
        realized = (amount - cost) - cost_basis_released

        updated = Position(
            asset_code=asset_code,
            shares=current.shares - shares,
            cost_basis=current.cost_basis - cost_basis_released,
            realized_pnl=current.realized_pnl + realized,
        )

        self.cash += (amount - cost)
        self.positions[asset_code] = updated
        return updated

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------

    def market_value(self, prices: Dict[str, Decimal]) -> Decimal:
        """持仓总市值"""
        return sum(
            (p.market_value(prices.get(code)) for code, p in self.positions.items()),
            Decimal("0"),
        )

    def asset_values(self, prices: Dict[str, Decimal]) -> Dict[str, Decimal]:
        """各资产市值"""
        return {
            code: p.market_value(prices.get(code))
            for code, p in self.positions.items()
        }

    def unrealized_pnl(self, prices: Dict[str, Decimal]) -> Decimal:
        """浮动盈亏 = 持仓市值 - 持仓成本"""
        return self.market_value(prices) - self.total_cost_basis()

    def realized_pnl(self) -> Decimal:
        """累计已实现盈亏"""
        return sum((p.realized_pnl for p in self.positions.values()), Decimal("0"))

    def total_cost_basis(self) -> Decimal:
        """持仓总成本"""
        return sum((p.cost_basis for p in self.positions.values()), Decimal("0"))

    def total_value(self, prices: Dict[str, Decimal]) -> Decimal:
        """组合总市值 = 现金 + 持仓市值"""
        return self.cash + self.market_value(prices)

    def weights(self, prices: Dict[str, Decimal]) -> Dict[str, Decimal]:
        """各资产权重"""
        total = self.total_value(prices)
        if total <= 0:
            return {code: Decimal("0") for code in self.positions}

        values = self.asset_values(prices)
        return {code: value / total for code, value in values.items()}

    def position_snapshot(self) -> Dict[str, Decimal]:
        """各资产持仓数量快照"""
        return {code: p.shares for code, p in self.positions.items()}

    # ------------------------------------------------------------------
    # 校验与序列化
    # ------------------------------------------------------------------

    def validate(self) -> List[str]:
        """校验不变量，返回错误列表"""
        errors = []

        if self.cash < 0 and not self.allow_overdraft:
            errors.append(f"组合现金为负: {self.cash}")

        for code, position in self.positions.items():
            if position.shares < 0:
                errors.append(f"{code} 持仓数量为负: {position.shares}")
            if position.cost_basis < 0:
                errors.append(f"{code} 持仓成本为负: {position.cost_basis}")

        return errors

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'portfolio_id': self.portfolio_id,
            'name': self.name,
            'cash': float(self.cash),
            'positions': {code: p.to_dict() for code, p in self.positions.items()},
        }

    def __repr__(self) -> str:
        return (
            f"Portfolio(id={self.portfolio_id}, name={self.name}, "
            f"cash={self.cash:.2f}, positions={len(self.positions)})"
        )
