# 交易策略与再平衡策略 DDD 建模

## 1. 战略设计 - 边界上下文

### 1.1 核心域 (Core Domain)

**策略上下文 (Strategy Context)**
- 核心价值：定义和执行交易决策逻辑
- 关键能力：信号生成、订单创建、绩效评估

**再平衡上下文 (Rebalance Context)**
- 核心价值：维护目标资产配置比例
- 关键能力：权重监控、调仓计算、成本控制

**执行上下文 (Execution Context)**
- 核心价值：将策略决策转化为实际交易
- 关键能力：订单路由、成交模拟、成本计算

### 1.2 支撑域 (Supporting Domain)

**价格上下文 (Price Context)** - 已实现
**持仓上下文 (Position Context)** - 已实现

---

## 2. 战术设计 - 领域模型

### 2.1 策略上下文实体

#### `TradingStrategy` (聚合根)
```
代表一个完整的交易策略

属性:
- strategy_id: StrategyId
- name: str
- description: str
- rules: List[TradingRule]  # 交易规则集合
- parameters: Dict[str, Any]  # 策略参数
- status: StrategyStatus  # ACTIVE/PAUSED/ARCHIVED
- created_at: datetime
- updated_at: datetime

行为:
- evaluate_signal(market_data): Signal  # 评估信号
- validate(): List[str]  # 验证规则有效性
- clone(): TradingStrategy  # 克隆策略
```

#### `TradingRule` (值对象)
```
单条交易规则

属性:
- rule_id: RuleId
- condition: Condition  # 触发条件
- action: Action  # 执行动作
- priority: int  # 优先级
- enabled: bool  # 是否启用

不变量:
- condition 和 action 不能为空
- priority >= 0
```

#### `Condition` (值对象)
```
交易条件表达式

类型:
- PriceCondition: 价格条件（突破、回调等）
- IndicatorCondition: 指标条件（MA、RSI等）
- TimeCondition: 时间条件（特定时间段）
- CompositeCondition: 组合条件（AND/OR/NOT）

属性:
- type: ConditionType
- parameters: Dict[str, Any]
- operator: ComparisonOperator  # >, <, >=, <=, ==, !=
```

#### `Action` (值对象)
```
交易动作

类型:
- BuyAction: 买入
- SellAction: 卖出
- HoldAction: 持有
- RebalanceAction: 再平衡

属性:
- type: ActionType
- target_weight: Optional[Decimal]  # 目标权重（再平衡用）
- quantity: Optional[Decimal]  # 数量
- order_type: OrderType  # MARKET/LIMIT
```

### 2.2 再平衡上下文实体

#### `RebalancePolicy` (聚合根)
```
再平衡策略配置

属性:
- policy_id: PolicyId
- name: str
- trigger: RebalanceTrigger  # 触发机制
- target_allocation: Dict[AssetCode, Decimal]  # 目标配置
- constraints: RebalanceConstraints  # 约束条件
- cost_model: TransactionCostModel  # 成本模型
- status: PolicyStatus

行为:
- should_rebalance(current_state): bool  # 判断是否需要再平衡
- calculate_trades(current_state): List[TradeOrder]  # 计算调仓订单
- validate(): List[str]  # 验证配置
```

#### `RebalanceTrigger` (值对象)
```
再平衡触发机制

类型:
- ThresholdTrigger: 阈值触发（已实现）
- PeriodicTrigger: 定期触发
- EventTrigger: 事件触发
- CompositeTrigger: 组合触发

属性:
- type: TriggerType
- parameters: Dict[str, Any]
```

#### `RebalanceConstraints` (值对象)
```
再平衡约束条件

属性:
- max_turnover: Decimal  # 最大换手率
- min_trade_size: Decimal  # 最小交易金额
- max_single_asset_change: Decimal  # 单资产最大调整幅度
- allowed_trading_hours: TimeRange  # 允许交易时段
```

### 2.3 执行上下文实体

#### `TradeOrder` (实体)
```
交易订单

属性:
- order_id: OrderId
- strategy_id: StrategyId
- asset_code: AssetCode
- side: OrderSide  # BUY/SELL
- quantity: Decimal
- price: Optional[Decimal]  # 限价
- order_type: OrderType
- status: OrderStatus
- created_at: datetime
- executed_at: Optional[datetime]
- execution_price: Optional[Decimal]
- commission: Decimal  # 佣金

行为:
- execute(execution_context): ExecutionResult  # 执行订单
- cancel(): bool  # 取消订单
```

#### `ExecutionResult` (值对象)
```
执行结果

属性:
- success: bool
- filled_quantity: Decimal
- average_price: Decimal
- total_cost: Decimal
- slippage: Decimal  # 滑点
- message: str  # 执行信息
```

### 2.4 共享值对象

#### `Signal` (值对象)
```
交易信号

属性:
- timestamp: datetime
- asset_code: AssetCode
- signal_type: SignalType  # BUY/SELL/HOLD/REBALANCE
- strength: Decimal  # 信号强度 0-1
- confidence: Decimal  # 置信度 0-1
- metadata: Dict[str, Any]  # 附加信息
```

#### `MarketData` (值对象)
```
市场数据快照

属性:
- timestamp: datetime
- prices: Dict[AssetCode, Decimal]
- volumes: Dict[AssetCode, Decimal]
- indicators: Dict[str, Dict[AssetCode, Decimal]]  # 技术指标
```

#### `PortfolioState` (值对象)
```
持仓状态

属性:
- timestamp: datetime
- positions: Dict[AssetCode, Position]
- cash: Decimal
- total_value: Decimal
- weights: Dict[AssetCode, Decimal]
```

---

## 3. 领域服务

### 3.1 `SignalGenerator` (信号生成器)
```python
class SignalGenerator:
    """根据策略规则生成交易信号"""
    
    def generate(strategy: TradingStrategy, 
                 market_data: MarketData) -> List[Signal]:
        """评估策略并生成信号"""
        
    def backtest(strategy: TradingStrategy,
                 historical_data: List[MarketData]) -> BacktestResult:
        """回测策略表现"""
```

### 3.2 `RebalanceEngine` (再平衡引擎)
```python
class RebalanceEngine:
    """执行再平衡逻辑"""
    
    def check_trigger(policy: RebalancePolicy,
                      current_state: PortfolioState) -> bool:
        """检查是否触发再平衡"""
        
    def calculate_orders(policy: RebalancePolicy,
                         current_state: PortfolioState) -> List[TradeOrder]:
        """计算再平衡订单"""
        
    def optimize_orders(orders: List[TradeOrder],
                        constraints: RebalanceConstraints) -> List[TradeOrder]:
        """优化订单（考虑约束）"""
```

### 3.3 `OrderExecutor` (订单执行器)
```python
class OrderExecutor:
    """执行交易订单"""
    
    def execute(order: TradeOrder,
                execution_context: ExecutionContext) -> ExecutionResult:
        """执行单个订单"""
        
    def batch_execute(orders: List[TradeOrder]) -> List[ExecutionResult]:
        """批量执行订单"""
```

---

## 4. 仓储接口

### 4.1 `StrategyRepository`
```python
class StrategyRepository(ABC):
    @abstractmethod
    def save(self, strategy: TradingStrategy) -> None: ...
    
    @abstractmethod
    def find_by_id(self, strategy_id: StrategyId) -> Optional[TradingStrategy]: ...
    
    @abstractmethod
    def find_all_active(self) -> List[TradingStrategy]: ...
```

### 4.2 `RebalancePolicyRepository`
```python
class RebalancePolicyRepository(ABC):
    @abstractmethod
    def save(self, policy: RebalancePolicy) -> None: ...
    
    @abstractmethod
    def find_by_id(self, policy_id: PolicyId) -> Optional[RebalancePolicy]: ...
```

### 4.3 `OrderRepository`
```python
class OrderRepository(ABC):
    @abstractmethod
    def save(self, order: TradeOrder) -> None: ...
    
    @abstractmethod
    def find_by_strategy(self, strategy_id: StrategyId) -> List[TradeOrder]: ...
    
    @abstractmethod
    def update_status(self, order_id: OrderId, status: OrderStatus) -> None: ...
```

---

## 5. 应用服务

### 5.1 `StrategyManagementService`
```python
class StrategyManagementService:
    """策略管理服务"""
    
    def create_strategy(config: StrategyConfig) -> TradingStrategy: ...
    
    def update_strategy(strategy_id: StrategyId, 
                       updates: StrategyUpdates) -> TradingStrategy: ...
    
    def activate_strategy(strategy_id: StrategyId) -> None: ...
    
    def pause_strategy(strategy_id: StrategyId) -> None: ...
    
    def backtest_strategy(strategy_id: StrategyId,
                          date_range: DateRange) -> BacktestResult: ...
```

### 5.2 `RebalanceService`
```python
class RebalanceService:
    """再平衡服务"""
    
    def create_policy(config: RebalanceConfig) -> RebalancePolicy: ...
    
    def check_and_execute(policy_id: PolicyId) -> RebalanceResult: ...
    
    def simulate_rebalance(policy_id: PolicyId,
                           hypothetical_state: PortfolioState) -> SimulationResult: ...
```

### 5.3 `TradingService`
```python
class TradingService:
    """交易执行服务"""
    
    def place_order(order_request: OrderRequest) -> TradeOrder: ...
    
    def cancel_order(order_id: OrderId) -> bool: ...
    
    def get_order_status(order_id: OrderId) -> OrderStatus: ...
    
    def get_strategy_performance(strategy_id: StrategyId) -> PerformanceMetrics: ...
```

---

## 6. 领域事件

```python
@dataclass
class StrategySignalGenerated:
    """策略生成信号事件"""
    strategy_id: StrategyId
    signal: Signal
    timestamp: datetime

@dataclass
class RebalanceTriggered:
    """再平衡触发事件"""
    policy_id: PolicyId
    reason: str
    current_weights: Dict[AssetCode, Decimal]
    timestamp: datetime

@dataclass
class OrderExecuted:
    """订单执行完成事件"""
    order_id: OrderId
    result: ExecutionResult
    timestamp: datetime

@dataclass
class StrategyPerformanceUpdated:
    """策略绩效更新事件"""
    strategy_id: StrategyId
    metrics: PerformanceMetrics
    period: DateRange
```

---

## 7. 业务规则

### 7.1 策略规则验证
1. **规则完整性**: 至少有一条启用的规则
2. **条件有效性**: 所有条件参数必须合法
3. **动作一致性**: 动作类型与条件匹配
4. **优先级唯一性**: 同策略内规则优先级不重复

### 7.2 再平衡规则
1. **权重总和**: 目标权重总和必须为100%
2. **阈值合理性**: 触发阈值在合理范围内（1%-10%）
3. **约束可行性**: 约束条件不冲突
4. **成本可控**: 预估交易成本不超过阈值

### 7.3 执行规则
1. **订单有效性**: 数量>0，价格>=0
2. **资金充足**: 买入订单需有足够现金
3. **持仓充足**: 卖出订单需有足够持仓
4. **风控检查**: 单笔交易不超过限制

---

## 8. 目录结构

```
src/
├── domain/
│   ├── strategy/
│   │   ├── entities.py          # TradingStrategy, TradingRule
│   │   ├── value_objects.py     # Condition, Action, Signal
│   │   ├── services.py          # SignalGenerator
│   │   └── events.py            # 领域事件
│   ├── rebalance/
│   │   ├── entities.py          # RebalancePolicy
│   │   ├── value_objects.py     # RebalanceTrigger, Constraints
│   │   ├── services.py          # RebalanceEngine
│   │   └── events.py
│   └── execution/
│       ├── entities.py          # TradeOrder
│       ├── value_objects.py     # ExecutionResult
│       ├── services.py          # OrderExecutor
│       └── events.py
│
├── application/
│   ├── strategy/
│   │   ├── management_service.py
│   │   └── dto.py
│   ├── rebalance/
│   │   ├── service.py
│   │   └── dto.py
│   └── trading/
│       ├── service.py
│       └── dto.py
│
└── infrastructure/
    ├── strategy/
    │   └── repositories.py
    ├── rebalance/
    │   └── repositories.py
    └── execution/
        ├── repositories.py
        └── simulators.py        # 模拟执行器
```

---

## 9. 使用示例

### 9.1 创建自定义策略
```python
from src.domain.strategy.entities import TradingStrategy, TradingRule
from src.domain.strategy.value_objects import Condition, Action

# 创建均线交叉策略
strategy = TradingStrategy(
    name="双均线交叉策略",
    rules=[
        TradingRule(
            condition=Condition(
                type="indicator_cross",
                parameters={"fast_ma": 5, "slow_ma": 20},
                operator="cross_above"
            ),
            action=Action(type="buy", target_weight=0.25),
            priority=1
        ),
        TradingRule(
            condition=Condition(
                type="indicator_cross",
                parameters={"fast_ma": 5, "slow_ma": 20},
                operator="cross_below"
            ),
            action=Action(type="sell", target_weight=0),
            priority=2
        )
    ]
)
```

### 9.2 创建再平衡策略
```python
from src.domain.rebalance.entities import RebalancePolicy
from src.domain.rebalance.value_objects import RebalanceTrigger

policy = RebalancePolicy(
    name="阈值再平衡",
    trigger=RebalanceTrigger(
        type="threshold",
        parameters={"threshold_pp": 3}
    ),
    target_allocation={
        "515180": Decimal("0.25"),
        "159937": Decimal("0.25"),
        "511380": Decimal("0.25"),
        "513300": Decimal("0.25"),
    }
)
```

### 9.3 执行回测
```python
from src.application.strategy.management_service import StrategyManagementService

service = StrategyManagementService()
result = service.backtest_strategy(
    strategy_id=strategy.id,
    date_range=DateRange(start=date(2020, 1, 1), end=date(2026, 11, 6))
)
print(result.summary())
```

---

## 10. 与现有系统集成

### 10.1 复用现有组件
- **价格数据**: 使用已有的 `PriceQueryService`
- **持仓管理**: 使用已有的 `AdjustedPriceSeries`
- **回测引擎**: 扩展已有的 `RebalanceBacktester`

### 10.2 新增组件
- **策略引擎**: 独立的信号生成和执行逻辑
- **规则解析器**: 解析自定义规则配置
- **模拟器**: 模拟成交环境

---

## 11. 测试策略

### 11.1 单元测试
- `TradingRule` 验证逻辑
- `Condition` 评估准确性
- `RebalanceEngine` 计算正确性

### 11.2 集成测试
- 完整策略回测流程
- 再平衡触发和执行
- 订单执行模拟

### 11.3 回归测试
- 与历史回测结果对比
- 确保策略行为一致性
