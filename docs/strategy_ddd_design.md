# 交易策略与再平衡策略设计（策略模块）

> 本文只描述**已经落地**的设计。
> 历史上这里写过一套"通用策略上下文"（`TradingStrategy` / `TradingRule` / `Condition` / `Action` /
> `StrategyManagementService` …）的构想，但真正被实现和使用的只有"策略规格 + 策略规则"这一层，
> 那些未被任何代码使用的抽象已删除（详见文末「取舍记录」）。

---

## 1. 模块边界

```
src/strategy/                # 策略模块
├── allocation.py            # 总资产配置（网格与再平衡共用同一份）
├── portfolio.py             # 组合与持仓（两策略各持一份，类型级隔离）
├── performance.py           # 统一绩效口径
├── spec.py                  # 策略规格：策略名 → 规则描述 → 规则实现
├── ledger/                  # 交易流水账本
├── grid/                    # 网格策略
│   ├── models.py            # 网格实体与值对象
│   ├── engine.py            # ★ 网格全部业务规则
│   ├── backtest.py          # 回测编排
│   └── spec.py              # 网格策略规格
├── rebalance/               # 再平衡策略
│   ├── rules.py             # ★ 再平衡全部业务规则
│   ├── backtest.py          # 回测编排
│   └── spec.py              # 再平衡策略规格
└── combined.py              # 组合回测（两策略持仓隔离）
```

规则只写在两处：

| 策略 | 规则实现 |
| --- | --- |
| 网格 | `src/strategy/grid/engine.py::GridEngine` |
| 再平衡 | `src/strategy/rebalance/rules.py` |

回测引擎（`grid/backtest.py`、`rebalance/backtest.py`）只负责**逐日编排 + 记账**，
不写业务判断。

---

## 2. 策略规格：策略名 → 规则描述 → 规则实现

页面需要能说清楚「这个策略叫什么、按什么规则跑、规则写在哪」。
为此定义三个值对象（`src/strategy/spec.py`）：

```python
StrategySpec          # 策略名
├── strategy_id       # 'grid' / 'rebalance'
├── name              # 复合网格 / 阈值再平衡
├── summary           # 一句话规则摘要
├── rules: tuple[StrategyRule, ...]
└── implementation    # 策略整体实现位置清单

StrategyRule          # 一条规则 = 描述 + 参数 + 公式 + 实现
├── rule_id           # 'grid.trigger' / 'grid.add' / ...
├── title             # 触发条件 / 加仓规则 / 止盈规则 / 资金隔离
├── description       # 人读描述（由参数推导）
├── formula           # 公式
├── parameters        # 规则参数（值）
└── implementation    # 'src/strategy/grid/engine.py::GridEngine._process_levels'

RuleParameter         # 一个可调旋钮
├── name / key / value / meaning
```

### 关键约束：描述由参数推导

规则描述**不允许硬编码**，必须由 `GridTrigger` / `RebalanceConfig` 的真实参数拼出来。
否则改了参数忘了改文案，就会出现「页面说 2ATR、代码跑 0.5ATR」的漂移。

以网格加仓规则为例（`src/strategy/grid/spec.py`）：

```python
description=(
    f"网格激活后，每再下跌 {step_ratio}×ATR({trigger.atr_period}) "
    f"加一档仓位；每档买入 {trigger.buy_amount:,.0f} 元，"
    f"股数向下取整到 {trigger.lot_size} 股的整数倍；"
    f"档位价格 = 锚点 − 步长 × 档位序号。"
)
```

### 规格构建

```python
from src.strategy.grid.spec import build_grid_spec
from src.strategy.rebalance.spec import build_rebalance_spec

grid_spec = build_grid_spec(grid_trigger)
reb_spec = build_rebalance_spec(rebalance_config)

grid_spec.to_dict()
# {'strategy_id': 'grid', 'name': '复合网格', 'summary': ..., 'rules': [...]}
```

---

## 3. 网格策略规则

实现见 `src/strategy/grid/engine.py::GridEngine`。

| 规则 | 说明 | 参数 |
| --- | --- | --- |
| 监控启动 | 当日价格跌破**前 N 个交易日最低价**（不含当日） | `lookback_days` |
| 网格步长 | `step = ATR(触发日, atr_period) × step_ratio`（轮内固定） | `atr_period` / `step_ratio` |
| 档位推进 | 档位 N 价格 = 锚点 − step × N，价格 ≤ 档位价即买入 | `anchor` |
| 单格买入 | `buy_amount` 元，股数向下取整到 `lot_size` 整数倍 | `buy_amount` / `lot_size` |
| 止盈 | 回升至「首次建仓价 + `take_profit_ratio`×步长」整轮卖出 | `take_profit_ratio` |
| 资金隔离 | 只动网格自己的资金池与持仓 | `initial_capital` |
| 轮次结束 | 完全止盈后必须**重新跌破前低**才开下一轮 | — |

```python
from src.strategy.grid.engine import GridEngine

decision = GridEngine.evaluate(strategy, "515180", snapshot)
if decision.has_orders:
    for order in decision.orders:
        print(order.to_dict())
```

---

## 4. 再平衡策略规则

实现见 `src/strategy/rebalance/rules.py`（纯函数，无 IO）。

| 函数 | 职责 |
| --- | --- |
| `should_rebalance(target_weights, current_weights, threshold_pp)` | 任一标的偏离目标权重超过阈值即触发 |
| `initial_build_orders(...)` | 首次建仓：按目标权重分配初始资金 |
| `plan_rebalance_orders(...)` | 调仓：先卖超配，再买低配，股数向下取整到整手 |
| `transaction_cost(amount, cost_bps)` | 交易成本 = 成交金额 × 单边费率（基点） |

```python
from src.strategy.rebalance.rules import should_rebalance

if should_rebalance(target_weights, current_weights, threshold_pp):
    orders = plan_rebalance_orders(...)
```

---

## 5. 共享内核

| 共享物 | 位置 | 为什么共享 |
| --- | --- | --- |
| 总资产配置 `AllocationPlan` | `allocation.py` | 保证网格监控的资产与再平衡配置的资产完全一致 |
| 组合与持仓 `Portfolio` | `portfolio.py` | 每个策略各持一份实例，实现持仓隔离 |
| 绩效口径 `PerformanceCalculator` | `performance.py` | 三个回测结果指标口径一致 |
| 整手取整 `LotSize` | `ledger/values.py` | 两个策略都要按 100 股取整，避免各写一份 |
| 交易账本 `TradeLedger` | `ledger/` | 追加式流水，持仓由流水重演得出 |

---

## 6. 使用示例

```python
from decimal import Decimal
from src.strategy.allocation import AllocationPlan
from src.strategy.grid.backtest import GridBacktester, GridConfig
from src.strategy.grid.models import GridTrigger

plan = AllocationPlan.equal_weight(
    [("515180", "红利ETF"), ("159937", "黄金ETF")], name="两资产等权"
)

config = GridConfig.from_allocation(
    plan=plan,
    trigger=GridTrigger(
        lookback_days=120,
        atr_period=120,
        step_ratio=Decimal("0.5"),
        buy_amount=Decimal("10000"),
        take_profit_ratio=Decimal("1"),
    ),
    initial_capital=Decimal("200000"),
)

result = GridBacktester(config).run()
print(result.summary())

# 表格数据（图表模块）
from src.chart.tables import grid_trigger_log, grid_trade_table
print(grid_trigger_log(result))
print(grid_trade_table(result))
```

---

## 7. 测试策略

| 层次 | 位置 | 特点 |
| --- | --- | --- |
| 规则单测 | `tests/test_grid_domain.py` | 直接构造行情快照驱动 `GridEngine`，离线可跑 |
| 回测单测 | `tests/test_grid_domain.py` | 注入价格 provider，不访问网络 |
| 规格单测 | `tests/test_strategy_spec.py` | 校验描述确实由参数推导 |
| 共享内核 | `tests/test_portfolio.py` / `tests/test_trading_ledger.py` | 不变量与重演一致性 |

---

## 8. 取舍记录

| 曾经的设想 | 处理 | 原因 |
| --- | --- | --- |
| `TradingStrategy` / `TradingRule` / `Condition` / `Action` 通用策略上下文 | **删除** | 从未被实现或使用；真实需求只有"规则描述 + 实现位置"，由 `StrategySpec` 覆盖 |
| `StrategyManagementService` 策略管理应用服务 | **删除** | 没有"管理策略"用例，策略是代码而非数据 |
| `RebalancePolicy` / `RebalanceEngine` 通用再平衡策略模型 | **删除** | 与回测引擎里的实现重复（两套规则、两套口径）；现在规则只在 `rebalance/rules.py` 一处 |
| `RebalanceConstraints`（换手率 / 最小成交额约束） | **删除** | 回测未使用该约束，留着只会让人误以为生效 |
| `TradeOrder` / `ExecutionResult` / `OrderExecutor` 执行上下文 | **未实现** | 实盘执行尚无需求，等有真实用例再做 |

原则：**没有真实用例的抽象不预先建**（见 `.pi/agent/AGENTS.md` 第 6 节「防过度设计自查」）。
