"""
再平衡策略领域实体

核心聚合根: RebalancePolicy
值对象: RebalanceTrigger, RebalanceConstraints, TransactionCostModel
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Tuple
import uuid


class TriggerType(Enum):
    """触发类型"""
    THRESHOLD = "threshold"  # 阈值触发
    PERIODIC = "periodic"  # 定期触发
    EVENT = "event"  # 事件触发
    COMPOSITE = "composite"  # 组合触发


class PolicyStatus(Enum):
    """策略状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class RebalanceTrigger:
    """
    再平衡触发机制（值对象）
    
    不变量:
    - type 必须有效
    - parameters 不能为空
    """
    type: TriggerType
    parameters: Dict[str, any]
    
    def __post_init__(self):
        if not self.parameters:
            raise ValueError("触发参数不能为空")
        
        # 验证特定类型的参数
        if self.type == TriggerType.THRESHOLD:
            threshold = self.parameters.get('threshold_pp')
            if threshold is None:
                raise ValueError("阈值触发需要threshold_pp参数")
            if not (0 < threshold <= 10):
                raise ValueError(f"阈值必须在0-10之间: {threshold}")
        
        elif self.type == TriggerType.PERIODIC:
            interval = self.parameters.get('interval_days')
            if interval is None:
                raise ValueError("定期触发需要interval_days参数")
            if interval < 1:
                raise ValueError(f"间隔天数必须>=1: {interval}")
    
    def should_trigger(self, current_state: Dict[str, any]) -> bool:
        """
        判断是否应该触发再平衡
        
        Args:
            current_state: 当前状态信息
            
        Returns:
            是否触发
        """
        if self.type == TriggerType.THRESHOLD:
            return self._check_threshold(current_state)
        elif self.type == TriggerType.PERIODIC:
            return self._check_periodic(current_state)
        
        return False
    
    def _check_threshold(self, state: Dict[str, any]) -> bool:
        """检查阈值触发"""
        threshold = Decimal(str(self.parameters['threshold_pp'])) / Decimal("100")
        current_weights = state.get('current_weights', {})
        target_weights = state.get('target_weights', {})
        
        for asset_code, target_weight in target_weights.items():
            current_weight = current_weights.get(asset_code, Decimal("0"))
            deviation = abs(current_weight - target_weight)
            
            if deviation > threshold:
                return True
        
        return False
    
    def _check_periodic(self, state: Dict[str, any]) -> bool:
        """检查定期触发"""
        last_rebalance_date = state.get('last_rebalance_date')
        if not last_rebalance_date:
            return True  # 从未再平衡过，应该触发
        
        interval_days = self.parameters['interval_days']
        days_since = (date.today() - last_rebalance_date).days
        
        return days_since >= interval_days
    
    def to_dict(self) -> Dict[str, any]:
        """转换为字典"""
        return {
            'type': self.type.value,
            'parameters': self.parameters,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, any]) -> 'RebalanceTrigger':
        """从字典创建"""
        return cls(
            type=TriggerType(data['type']),
            parameters=data['parameters'],
        )


@dataclass(frozen=True)
class RebalanceConstraints:
    """
    再平衡约束条件（值对象）
    
    不变量:
    - 所有约束值必须合理
    """
    max_turnover: Decimal = Decimal("0.5")  # 最大换手率50%
    min_trade_size: Decimal = Decimal("1000")  # 最小交易金额1000元
    max_single_asset_change: Decimal = Decimal("0.1")  # 单资产最大调整10%
    
    def __post_init__(self):
        if not (0 < self.max_turnover <= 1):
            raise ValueError(f"max_turnover必须在0-1之间: {self.max_turnover}")
        
        if self.min_trade_size < 0:
            raise ValueError(f"min_trade_size不能为负: {self.min_trade_size}")
        
        if not (0 < self.max_single_asset_change <= 1):
            raise ValueError(f"max_single_asset_change必须在0-1之间: {self.max_single_asset_change}")
    
    def validate_orders(self, orders: List[Dict[str, any]], portfolio_value: Decimal) -> List[str]:
        """
        验证订单是否符合约束
        
        Args:
            orders: 订单列表
            portfolio_value: 组合总值
            
        Returns:
            违规消息列表
        """
        violations = []
        
        total_turnover = Decimal("0")
        
        for order in orders:
            amount = order.get('amount', Decimal("0"))
            
            # 检查最小交易金额
            if amount < self.min_trade_size:
                violations.append(f"订单金额{amount}小于最小交易金额{self.min_trade_size}")
            
            # 计算换手率
            if portfolio_value > 0:
                turnover = amount / portfolio_value
                total_turnover += turnover
            
            # 检查单资产调整幅度
            asset_change = order.get('weight_change', Decimal("0"))
            if abs(asset_change) > self.max_single_asset_change:
                violations.append(
                    f"资产{order.get('asset_code')}调整幅度{asset_change:.2%}"
                    f"超过限制{self.max_single_asset_change:.2%}"
                )
        
        # 检查总换手率
        if total_turnover > self.max_turnover:
            violations.append(
                f"总换手率{total_turnover:.2%}超过限制{self.max_turnover:.2%}"
            )
        
        return violations
    
    def to_dict(self) -> Dict[str, any]:
        """转换为字典"""
        return {
            'max_turnover': float(self.max_turnover),
            'min_trade_size': float(self.min_trade_size),
            'max_single_asset_change': float(self.max_single_asset_change),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, any]) -> 'RebalanceConstraints':
        """从字典创建"""
        return cls(
            max_turnover=Decimal(str(data.get('max_turnover', 0.5))),
            min_trade_size=Decimal(str(data.get('min_trade_size', 1000))),
            max_single_asset_change=Decimal(str(data.get('max_single_asset_change', 0.1))),
        )


@dataclass(frozen=True)
class TransactionCostModel:
    """
    交易成本模型（值对象）
    
    不变量:
    - 所有费率必须>=0
    """
    commission_rate: Decimal = Decimal("0.0003")  # 佣金率0.03%
    slippage_rate: Decimal = Decimal("0.001")  # 滑点率0.1%
    min_commission: Decimal = Decimal("5")  # 最低佣金5元
    
    def __post_init__(self):
        if self.commission_rate < 0:
            raise ValueError(f"commission_rate不能为负: {self.commission_rate}")
        if self.slippage_rate < 0:
            raise ValueError(f"slippage_rate不能为负: {self.slippage_rate}")
        if self.min_commission < 0:
            raise ValueError(f"min_commission不能为负: {self.min_commission}")
    
    def calculate_cost(self, trade_amount: Decimal) -> Decimal:
        """
        计算交易成本
        
        Args:
            trade_amount: 交易金额
            
        Returns:
            总成本
        """
        if trade_amount <= 0:
            return Decimal("0")
        
        # 佣金
        commission = trade_amount * self.commission_rate
        commission = max(commission, self.min_commission)
        
        # 滑点
        slippage = trade_amount * self.slippage_rate
        
        return commission + slippage
    
    def to_dict(self) -> Dict[str, any]:
        """转换为字典"""
        return {
            'commission_rate': float(self.commission_rate),
            'slippage_rate': float(self.slippage_rate),
            'min_commission': float(self.min_commission),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, any]) -> 'TransactionCostModel':
        """从字典创建"""
        return cls(
            commission_rate=Decimal(str(data.get('commission_rate', 0.0003))),
            slippage_rate=Decimal(str(data.get('slippage_rate', 0.001))),
            min_commission=Decimal(str(data.get('min_commission', 5))),
        )


@dataclass
class RebalancePolicy:
    """
    再平衡策略（聚合根）
    
    职责:
    - 管理再平衡配置
    - 判断是否需要再平衡
    - 计算再平衡订单
    """
    name: str
    trigger: RebalanceTrigger
    target_allocation: Dict[str, Decimal]  # 资产代码 -> 目标权重
    constraints: RebalanceConstraints = field(default_factory=RebalanceConstraints)
    cost_model: TransactionCostModel = field(default_factory=TransactionCostModel)
    status: PolicyStatus = PolicyStatus.ACTIVE
    policy_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        # 验证目标权重总和
        total_weight = sum(self.target_allocation.values())
        if abs(total_weight - Decimal("1")) > Decimal("0.01"):
            raise ValueError(f"目标权重总和必须为1，当前为{total_weight}")
        
        # 验证所有权重为正
        for asset_code, weight in self.target_allocation.items():
            if weight <= 0 or weight > 1:
                raise ValueError(f"资产{asset_code}的权重必须在0-1之间: {weight}")
    
    def should_rebalance(self, current_state: Dict[str, any]) -> bool:
        """
        判断是否需要再平衡
        
        Args:
            current_state: 当前状态
                - current_weights: Dict[str, Decimal] 当前权重
                - target_weights: Dict[str, Decimal] 目标权重
                - last_rebalance_date: Optional[date] 上次再平衡日期
                
        Returns:
            是否需要再平衡
        """
        if self.status != PolicyStatus.ACTIVE:
            return False
        
        return self.trigger.should_trigger(current_state)
    
    def calculate_rebalance_orders(
        self,
        current_state: Dict[str, any]
    ) -> Tuple[List[Dict[str, any]], Decimal]:
        """
        计算再平衡订单
        
        Args:
            current_state: 当前状态
                - current_weights: Dict[str, Decimal]
                - portfolio_value: Decimal
                - prices: Dict[str, Decimal]
                
        Returns:
            (订单列表, 预估总成本)
        """
        current_weights = current_state.get('current_weights', {})
        portfolio_value = current_state.get('portfolio_value', Decimal("0"))
        prices = current_state.get('prices', {})
        
        if portfolio_value <= 0:
            return [], Decimal("0")
        
        orders = []
        total_cost = Decimal("0")
        
        # 计算每个资产的调整
        for asset_code, target_weight in self.target_allocation.items():
            current_weight = current_weights.get(asset_code, Decimal("0"))
            weight_diff = target_weight - current_weight
            
            if abs(weight_diff) < Decimal("0.001"):  # 差异小于0.1%忽略
                continue
            
            # 计算交易金额
            trade_amount = portfolio_value * abs(weight_diff)
            
            # 检查最小交易金额
            if trade_amount < self.constraints.min_trade_size:
                continue
            
            # 获取价格
            price = prices.get(asset_code)
            if not price or price <= 0:
                continue
            
            # 计算数量
            quantity = trade_amount / price
            
            # 确定买卖方向
            side = "BUY" if weight_diff > 0 else "SELL"
            
            # 计算成本
            cost = self.cost_model.calculate_cost(trade_amount)
            
            order = {
                'asset_code': asset_code,
                'side': side,
                'quantity': quantity,
                'price': price,
                'amount': trade_amount,
                'weight_change': weight_diff,
                'cost': cost,
            }
            
            orders.append(order)
            total_cost += cost
        
        # 验证约束
        violations = self.constraints.validate_orders(orders, portfolio_value)
        if violations:
            # 简化处理：如果有违规，返回空订单
            # 实际应该优化订单以满足约束
            return [], total_cost
        
        return orders, total_cost
    
    def validate(self) -> List[str]:
        """
        验证策略有效性
        
        Returns:
            错误消息列表
        """
        errors = []
        
        if not self.name:
            errors.append("策略名称不能为空")
        
        # 验证目标权重
        total_weight = sum(self.target_allocation.values())
        if abs(total_weight - Decimal("1")) > Decimal("0.01"):
            errors.append(f"目标权重总和必须为1，当前为{total_weight}")
        
        # 验证触发器
        try:
            test_state = {
                'current_weights': {k: v for k, v in self.target_allocation.items()},
                'target_weights': dict(self.target_allocation),
            }
            self.trigger.should_trigger(test_state)
        except Exception as e:
            errors.append(f"触发器配置无效: {e}")
        
        return errors
    
    def update_target_allocation(self, new_allocation: Dict[str, Decimal]) -> None:
        """更新目标配置"""
        # 验证新配置
        total_weight = sum(new_allocation.values())
        if abs(total_weight - Decimal("1")) > Decimal("0.01"):
            raise ValueError(f"新配置权重总和必须为1，当前为{total_weight}")
        
        self.target_allocation = new_allocation
        self.updated_at = datetime.now()
    
    def to_dict(self) -> Dict[str, any]:
        """转换为字典"""
        return {
            'policy_id': self.policy_id,
            'name': self.name,
            'trigger': self.trigger.to_dict(),
            'target_allocation': {k: float(v) for k, v in self.target_allocation.items()},
            'constraints': self.constraints.to_dict(),
            'cost_model': self.cost_model.to_dict(),
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, any]) -> 'RebalancePolicy':
        """从字典创建"""
        return cls(
            name=data['name'],
            trigger=RebalanceTrigger.from_dict(data['trigger']),
            target_allocation={
                k: Decimal(str(v)) for k, v in data['target_allocation'].items()
            },
            constraints=RebalanceConstraints.from_dict(data.get('constraints', {})),
            cost_model=TransactionCostModel.from_dict(data.get('cost_model', {})),
            status=PolicyStatus(data.get('status', 'active')),
            policy_id=data.get('policy_id', str(uuid.uuid4())),
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else datetime.now(),
            updated_at=datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else datetime.now(),
        )
    
    def __repr__(self) -> str:
        return (
            f"RebalancePolicy(id={self.policy_id}, name={self.name}, "
            f"assets={len(self.target_allocation)}, status={self.status.value})"
        )
