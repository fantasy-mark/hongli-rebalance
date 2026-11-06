"""
交易策略领域实体

核心聚合根: TradingStrategy
值对象: TradingRule, Condition, Action
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Dict, Any, Optional
import uuid


class SignalType(Enum):
    """信号类型"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    REBALANCE = "rebalance"


@dataclass(frozen=True)
class Signal:
    """交易信号（值对象）"""
    timestamp: datetime
    asset_code: str
    signal_type: SignalType
    strength: Decimal = Decimal("0.5")
    confidence: Decimal = Decimal("0.5")
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp.isoformat(),
            'asset_code': self.asset_code,
            'signal_type': self.signal_type.value,
            'strength': float(self.strength),
            'confidence': float(self.confidence),
            'metadata': self.metadata,
        }


class StrategyStatus(Enum):
    """策略状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ConditionType(Enum):
    """条件类型"""
    PRICE_BREAKOUT = "price_breakout"  # 价格突破
    PRICE_PULLBACK = "price_pullback"  # 价格回调
    INDICATOR_CROSS = "indicator_cross"  # 指标交叉
    INDICATOR_LEVEL = "indicator_level"  # 指标水平
    TIME_BASED = "time_based"  # 时间条件
    CUSTOM = "custom"  # 自定义


class ComparisonOperator(Enum):
    """比较运算符"""
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    EQ = "=="
    NEQ = "!="
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"


class ActionType(Enum):
    """动作类型"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    REBALANCE = "rebalance"


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True)
class Condition:
    """
    交易条件（值对象）
    
    不变量:
    - type 必须有效
    - operator 必须有效
    - parameters 不能为空
    """
    type: ConditionType
    operator: ComparisonOperator
    parameters: Dict[str, Any]
    
    def __post_init__(self):
        if not self.parameters:
            raise ValueError("条件参数不能为空")
    
    def evaluate(self, market_data: Dict[str, Any]) -> bool:
        """
        评估条件是否满足
        
        Args:
            market_data: 市场数据字典
            
        Returns:
            是否满足条件
        """
        # 简化实现，实际应根据type和operator进行复杂评估
        if self.type == ConditionType.PRICE_BREAKOUT:
            current_price = market_data.get('current_price', 0)
            threshold = self.parameters.get('threshold', 0)
            
            if self.operator == ComparisonOperator.GT:
                return current_price > threshold
            elif self.operator == ComparisonOperator.GTE:
                return current_price >= threshold
            # ... 其他运算符
        
        elif self.type == ConditionType.INDICATOR_CROSS:
            # 指标交叉逻辑
            fast_value = market_data.get('fast_indicator', 0)
            slow_value = market_data.get('slow_indicator', 0)
            prev_fast = market_data.get('prev_fast_indicator', 0)
            prev_slow = market_data.get('prev_slow_indicator', 0)
            
            if self.operator == ComparisonOperator.CROSS_ABOVE:
                return prev_fast <= prev_slow and fast_value > slow_value
            elif self.operator == ComparisonOperator.CROSS_BELOW:
                return prev_fast >= prev_slow and fast_value < slow_value
        
        return False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'type': self.type.value,
            'operator': self.operator.value,
            'parameters': self.parameters,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Condition':
        """从字典创建"""
        return cls(
            type=ConditionType(data['type']),
            operator=ComparisonOperator(data['operator']),
            parameters=data['parameters'],
        )


@dataclass(frozen=True)
class Action:
    """
    交易动作（值对象）
    
    不变量:
    - type 必须有效
    - 如果是BUY/SELL/REBALANCE，target_weight或quantity至少有一个
    """
    type: ActionType
    target_weight: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    order_type: OrderType = OrderType.MARKET
    
    def __post_init__(self):
        if self.type in [ActionType.BUY, ActionType.SELL, ActionType.REBALANCE]:
            if self.target_weight is None and self.quantity is None:
                raise ValueError("BUY/SELL/REBALANCE动作必须指定target_weight或quantity")
        
        if self.target_weight is not None:
            if self.target_weight < 0 or self.target_weight > 1:
                raise ValueError(f"target_weight必须在0-1之间: {self.target_weight}")
        
        if self.quantity is not None and self.quantity < 0:
            raise ValueError(f"quantity不能为负: {self.quantity}")
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'type': self.type.value,
            'target_weight': float(self.target_weight) if self.target_weight else None,
            'quantity': float(self.quantity) if self.quantity else None,
            'order_type': self.order_type.value,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Action':
        """从字典创建"""
        return cls(
            type=ActionType(data['type']),
            target_weight=Decimal(str(data['target_weight'])) if data.get('target_weight') else None,
            quantity=Decimal(str(data['quantity'])) if data.get('quantity') else None,
            order_type=OrderType(data.get('order_type', 'market')),
        )


@dataclass(frozen=True)
class TradingRule:
    """
    交易规则（值对象）
    
    不变量:
    - condition 和 action 不能为空
    - priority >= 0
    """
    condition: Condition
    action: Action
    priority: int = 0
    enabled: bool = True
    rule_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    def __post_init__(self):
        if self.priority < 0:
            raise ValueError(f"priority不能为负: {self.priority}")
    
    def evaluate_and_execute(self, market_data: Dict[str, Any]) -> Optional[Action]:
        """
        评估条件并返回动作
        
        Args:
            market_data: 市场数据
            
        Returns:
            如果条件满足则返回动作，否则返回None
        """
        if not self.enabled:
            return None
        
        if self.condition.evaluate(market_data):
            return self.action
        
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'rule_id': self.rule_id,
            'condition': self.condition.to_dict(),
            'action': self.action.to_dict(),
            'priority': self.priority,
            'enabled': self.enabled,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradingRule':
        """从字典创建"""
        return cls(
            condition=Condition.from_dict(data['condition']),
            action=Action.from_dict(data['action']),
            priority=data.get('priority', 0),
            enabled=data.get('enabled', True),
            rule_id=data.get('rule_id', str(uuid.uuid4())),
        )


@dataclass
class TradingStrategy:
    """
    交易策略（聚合根）
    
    职责:
    - 管理交易规则集合
    - 生成交易信号
    - 验证策略有效性
    """
    name: str
    description: str = ""
    rules: List[TradingRule] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    status: StrategyStatus = StrategyStatus.ACTIVE
    strategy_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        # 按优先级排序规则
        self.rules.sort(key=lambda r: r.priority)
    
    def add_rule(self, rule: TradingRule) -> None:
        """添加交易规则"""
        # 检查优先级是否重复
        if any(r.priority == rule.priority and r.rule_id != rule.rule_id for r in self.rules):
            raise ValueError(f"优先级 {rule.priority} 已存在")
        
        self.rules.append(rule)
        self.rules.sort(key=lambda r: r.priority)
        self.updated_at = datetime.now()
    
    def remove_rule(self, rule_id: str) -> bool:
        """移除交易规则"""
        initial_count = len(self.rules)
        self.rules = [r for r in self.rules if r.rule_id != rule_id]
        
        if len(self.rules) < initial_count:
            self.updated_at = datetime.now()
            return True
        
        return False
    
    def enable_rule(self, rule_id: str) -> bool:
        """启用规则"""
        for rule in self.rules:
            if rule.rule_id == rule_id:
                # 由于是frozen，需要替换
                idx = self.rules.index(rule)
                self.rules[idx] = TradingRule(
                    condition=rule.condition,
                    action=rule.action,
                    priority=rule.priority,
                    enabled=True,
                    rule_id=rule.rule_id,
                )
                self.updated_at = datetime.now()
                return True
        return False
    
    def disable_rule(self, rule_id: str) -> bool:
        """禁用规则"""
        for rule in self.rules:
            if rule.rule_id == rule_id:
                idx = self.rules.index(rule)
                self.rules[idx] = TradingRule(
                    condition=rule.condition,
                    action=rule.action,
                    priority=rule.priority,
                    enabled=False,
                    rule_id=rule.rule_id,
                )
                self.updated_at = datetime.now()
                return True
        return False
    
    def evaluate(self, market_data: Dict[str, Any]) -> List[Action]:
        """
        评估策略并返回所有触发的动作
        
        Args:
            market_data: 市场数据
            
        Returns:
            触发的动作列表（按优先级排序）
        """
        if self.status != StrategyStatus.ACTIVE:
            return []
        
        actions = []
        for rule in self.rules:
            action = rule.evaluate_and_execute(market_data)
            if action:
                actions.append(action)
        
        return actions
    
    def validate(self) -> List[str]:
        """
        验证策略有效性
        
        Returns:
            错误消息列表，空列表表示验证通过
        """
        errors = []
        
        if not self.name:
            errors.append("策略名称不能为空")
        
        if not self.rules:
            errors.append("策略至少需要一条规则")
        
        # 检查是否有启用的规则
        if not any(r.enabled for r in self.rules):
            errors.append("策略至少需要一条启用的规则")
        
        # 检查优先级唯一性
        priorities = [r.priority for r in self.rules]
        if len(priorities) != len(set(priorities)):
            errors.append("规则优先级不能重复")
        
        return errors
    
    def clone(self) -> 'TradingStrategy':
        """克隆策略"""
        from copy import deepcopy
        return deepcopy(self)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'strategy_id': self.strategy_id,
            'name': self.name,
            'description': self.description,
            'rules': [r.to_dict() for r in self.rules],
            'parameters': self.parameters,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradingStrategy':
        """从字典创建"""
        return cls(
            name=data['name'],
            description=data.get('description', ''),
            rules=[TradingRule.from_dict(r) for r in data.get('rules', [])],
            parameters=data.get('parameters', {}),
            status=StrategyStatus(data.get('status', 'active')),
            strategy_id=data.get('strategy_id', str(uuid.uuid4())),
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else datetime.now(),
            updated_at=datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else datetime.now(),
        )
    
    def __repr__(self) -> str:
        return (
            f"TradingStrategy(id={self.strategy_id}, name={self.name}, "
            f"rules={len(self.rules)}, status={self.status.value})"
        )
