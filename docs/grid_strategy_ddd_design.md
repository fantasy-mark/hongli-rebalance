# 网格策略 DDD 建模与实现

> 需求：当某个资产跌破**前 120 日（不含最近交易日）最低价**时开始监控，
> 之后每下跌一个**触发日 120 日 ATR 的比例**（可配置），
> **买入 10000 元**（可配置）。
> 特别的：**网格持仓与再平衡持仓相互隔离**。
>
> 补充需求：
> 1. 网格只监控**再平衡组合中的资产**——两者共用同一份**总资产配置**（AllocationPlan）
> 2. 一个资产的**网格轮次** = 从跌破前 120 日最低价开始，到**本轮完全止盈**结束
> 3. 下一轮必须**重新跌破前 120 日最低价**才会开启

---

## 1. 战略设计 — 限界上下文

```
┌──────────────────────────────────────────────┐
│  Allocation Context (总资产配置)  ★新增       │
│  共享内核: 再平衡与网格共用的资产清单+权重     │
└──────────────────────────────────────────────┘
        │ 共享内核 (Shared Kernel)
        ├──────────────────┬──────────────────┐
        ▼                  ▼                  │
┌────────────────┐  ┌────────────────┐        │
│ Rebalance 上下文│  │  Grid 上下文   │        │
│ 目标权重再平衡  │  │ 网格轮次/资金隔离│        │
└────────────────┘  └────────────────┘        │
        │ 依赖（向下）                            │
        ▼                  ▼                  │
┌──────────────────────────────────────────────┐
│  Price Context (价格上下文)  ✓扩展            │
│  新增：ATR 计算、前 N 日最低价计算            │
└──────────────────────────────────────────────┘
```

**上下文映射**：
- `Allocation Context` 是 **Shared Kernel**，被再平衡与网格两个上下文共同引用
- Grid Context 依赖 Price Context（ATR / 前低）
- Grid 与 Rebalance 之间为 **Partnership**（组合回测时协作），
  但**资金与持仓完全隔离**（各自独立的聚合根与账本）
- Chart Context 扩展了**分组标记点**能力（`ChartMarkerGroup`），供网格图使用

---

## 2. 战术设计 — 领域模型

### 2.1 共享内核：`AllocationPlan`（总资产配置）

```
AllocationPlan (聚合根)
├── name: str
└── allocations: List[AssetAllocation]

AssetAllocation (值对象)
├── code: str
├── name: str
└── target_weight: Decimal   # 再平衡用；网格只用清单
```

不变量：至少一个资产；权重之和为 1（容差 1%）；资产代码不重复。

派生方式：
- `RebalanceConfig.from_allocation(plan, ...)` → 再平衡子配置
- `GridConfig.from_allocation(plan, ...)` → 网格子配置（监控同一批资产）
- `CombinedConfig(allocation=plan, ...)` → 自动派生出上面两份配置

### 2.2 聚合根：`GridStrategy`

```
GridStrategy (聚合根)
├── name: str
├── trigger: GridTrigger            # 触发参数（值对象）
├── cost_model: GridCostModel       # 成本模型（值对象）
├── asset_codes: List[str]          # 监控资产列表
├── cash: Decimal                   # ★ 网格资金池（与再平衡资金隔离）
├── status: GridStrategyStatus      # ACTIVE / PAUSED / ARCHIVED
├── states: Dict[str, GridState]    # 各资产的网格状态
└── strategy_id / created_at / updated_at

行为:
- state_for(asset_code) -> GridState     # 懒加载资产状态
- positions_value(prices) -> Decimal     # 网格持仓市值
- total_value(prices) -> Decimal         # 网格总市值 = 现金 + 持仓
- validate() -> List[str]
- to_dict() / from_dict()                # 实盘状态持久化
```

**不变量**：`cash >= 0`；`asset_codes` 非空；激活状态必须有步长。

### 2.2 实体：`GridState`（单资产网格状态）

**网格轮次生命周期**：

```
IDLE ──开始监控──> MONITORING ──跌破前N日最低价──> ACTIVE
                                                  │
                                    全部持仓止盈卖出 │（本轮结束）
                                                  ▼
                            COMPLETED ──重新跌破前N日最低价──> ACTIVE(新一轮)
```

```
GridState
├── asset_code: str
├── status: GridStatus        # IDLE / MONITORING / ACTIVE / COMPLETED
├── anchor_price: Decimal     # 网格锚点（触发日价格或前低）
├── anchor_date / triggered_at: date
├── atr_at_trigger: Decimal   # 触发日的 ATR(120)
├── step: Decimal             # 网格间距 = atr_at_trigger * step_ratio
├── next_level: int           # 下一个待触发档位（从 1 开始）
├── filled_levels: List[int]  # 已成交档位
├── skipped_levels: List[int] # 已跳过档位（资金不足等）
├── tranches: List[GridTranche]# 持仓批次（用于按批次止盈）
├── shares / cost_basis: Decimal
├── round_no: int             # 当前轮次编号
├── rounds_completed: int     # 已完全止盈的轮次数
└── last_price / last_action

行为:
- start_monitoring(on_date)             # IDLE → MONITORING
- activate(anchor, atr, date, step)     # MONITORING → ACTIVE（开启新一轮）
- complete_round(on_date)               # ACTIVE → COMPLETED（本轮完全止盈）
- rearm()                              # COMPLETED → MONITORING（重新武装监控）
- level_price(level) -> Decimal         # 档位价 = 锚点 - 步长 * level
- record_buy(order) / record_sell(order)
- market_value(price) / unrealized_pnl(price)
```

### 2.3 值对象

| 值对象 | 关键属性 | 说明 |
|--------|---------|------|
| `GridTrigger` | lookback_days=120, atr_period=120, step_ratio=0.5, buy_amount=10000, lot_size=100, anchor, price_basis, take_profit_ratio=1, max_levels | 全部可配置 |
| `GridCostModel` | commission_rate=0.0003, slippage_rate=0.001, min_commission=5 | 交易成本 |
| `GridMarketSnapshot` | date, close, prior_low, atr, open, high, low | 领域决策的唯一输入 |
| `GridDecision` | action, asset_code, date, reason, orders, anchor_price, step, level, round_closed | 引擎决策结果 |
| `GridOrder` | asset_code, side, price, shares, amount, cost, date, level | 成交记录 |
| `GridTranche` | level, price, shares, amount, date | 持仓批次 |
| `GridLevel` | level, price | 档位 |

### 2.4 枚举

- `GridStatus`：IDLE / MONITORING / ACTIVE / COMPLETED
- `GridStrategyStatus`：ACTIVE / PAUSED / ARCHIVED
- `GridAction`：MONITOR / ACTIVATE / BUY / SELL / CLOSED / HOLD / SKIPPED
- `GridTradeType`：**开仓 OPEN / 加仓 ADD / 止盈 TAKE_PROFIT**（无减仓）
- `GridAnchor`：TRIGGER_PRICE（默认，触发日价格为锚）/ PRIOR_LOW（被跌破的前低为锚）
- `GridPriceBasis`：CLOSE（默认，收盘价口径）/ LOW（最低价口径，限价单模拟）
- `GridOrderSide`：BUY / SELL

---

## 3. 核心业务规则（`GridEngine` 领域服务）

### 规则 1：监控启动

```
当 基准价 < 前 N 个交易日最低价（不含当日）：
    状态 IDLE/MONITORING → ACTIVE
```

- 「前 N 日最低价」= 前 N 个交易日 `low` 的最小值，**严格不含当日**
- 历史数据不足（前低或 ATR 为 None）→ 保持监控，不激活
- 基准价取决于 `price_basis`：CLOSE 用收盘价，LOW 用当日最低价

### 规则 2：网格步长

```
step = ATR(触发日, atr_period) × step_ratio
```

ATR 采用 Wilder 平滑法：

```
TR_i  = max(high-low, |high-prev_close|, |low-prev_close|)
ATR_i = (ATR_{i-1} × (period-1) + TR_i) / period
```

首个 ATR 出现在第 `period` 根 K 线（需要 period 根历史）。

### 规则 3：档位推进与买入

```
档位 N 价格 = 锚点 - step × N        (N = 1, 2, 3, ...)

当 基准价 <= 档位 N 价格：
    股数 = floor(buy_amount / 成交价 / lot_size) × lot_size
    若 股数 <= 0        → 跳过该档（资金不足一手）
    若 网格现金 < 成交额 + 成本 → 跳过该档（资金隔离，绝不动用再平衡资金）
    否则 成交并推进 next_level
```

- 成交价：
  - CLOSE 口径 → 当日收盘价
  - LOW 口径 → 档位限价；若当日**跳空低开**（open < 档位价）则按开盘价成交
- 跳空下跌时**一次可触发多个档位**，每档独立下单
- 止盈（`take_profit_ratio`，默认 1.0）：
  `止盈价 = 买入价 + step × take_profit_ratio`，按批次平仓
  **T+1 规则**：当日买入的批次当日不可卖出（A 股限制）

### 规则 4：网格轮次生命周期 ★

```
IDLE ──> MONITORING ──跌破前N日最低价──> ACTIVE(第N轮)
                                          │
                        全部持仓止盈卖出    │（本轮结束）
                                          ▼
                              COMPLETED ──重新跌破前N日最低价──> ACTIVE(第N+1轮)
```

- 一轮网格 = 从跌破前 120 日最低价 → 本轮持仓**完全止盈**
- 本轮结束后**不会自动重启**，必须**再次跌破前 120 日最低价**才开启下一轮
- 每轮用**当时**的 ATR 重新计算锚点与步长（ATR 取触发日，轮内固定）
- 交易类型标记：
  - **开仓**：本轮首笔买入
  - **加仓**：本轮后续买入
  - **止盈**：触发止盈后**一次性卖出本轮全部持仓**（本轮结束，无减仓）

### 规则 5：盈亏核算

```
已实现盈亏(t) = Σ已平仓档位 [ (卖出净额) - (买入总支出，含手续费) ]
浮动盈亏(t)   = Σ持仓市值(t) - Σ持仓成本(含手续费)
总盈亏(t)     = 已实现盈亏(t) + 浮动盈亏(t)

恒等式: 网格总市值(t) = 初始资金 + 总盈亏(t)

图表展示口径（右轴百分比）:
    收益% = 累计已实现收益(元) / 回测期间网格最大持仓(堆叠图最大高度)
    例: 已实现收益 1930.28 元 / 峰值持仓 102793.60 元 = 1.88%
```

### 规则 6：资金隔离 ★

| | 再平衡子组合 | 网格子组合 |
|---|---|---|
| 现金池 | `RebalanceBacktester` 内部变量 | `GridStrategy.cash` |
| 持仓账本 | `current_shares` | `GridState.shares` |
| 交易触发 | 权重偏离阈值 | 跌破前低 + 档位推进 |

网格买入只消耗 `GridStrategy.cash`，永远不会读取或修改再平衡组合的现金与持仓；
组合层面 `总市值 = 再平衡市值 + 网格市值`，可分别归因。

---

## 4. 分层实现

```
src/
├── domain/
│   ├── allocation/                ★ 新增（共享内核）
│   │   └── entities.py             # AllocationPlan(聚合根), AssetAllocation
│   ├── grid/                       ★ 新增
│   │   ├── entities.py             # GridStrategy(聚合根), GridState, floor_to_lot
│   │   ├── value_objects.py        # GridTrigger, GridCostModel, GridMarketSnapshot,
│   │   │                           # GridDecision, GridOrder, GridTranche, GridTradeType, 枚举
│   │   └── services.py             # GridEngine(领域服务)
│   ├── chart/
│   │   ├── entities.py             ★ 扩展: ChartMarkerGroup(分组标记点)
│   │   └── services.py             ★ 扩展: ChartRenderer.render_on(多面板复用)
│   └── price/
│       └── services.py             ★ 扩展: calculate_atr_series / calculate_atr /
│                                       calculate_prior_low_series / calculate_prior_low
├── application/
│   ├── rebalance/
│   │   └── backtest_engine.py      ★ 扩展: RebalanceConfig.from_allocation
│   └── grid/                       ★ 新增
│       ├── grid_backtester.py      # GridConfig / GridBacktester / GridBacktestResult
│       ├── grid_chart.py           # GridChartRenderer（多面板图 + 触发日志）
│       └── combined_backtester.py  # CombinedBacktester（共享配置，持仓隔离）
└── infrastructure/
    └── grid/                       ★ 新增
        └── repositories.py         # GridStateRepository / InMemory / JsonFile
```

### 4.1 依赖方向

```
Application (GridBacktester)
      │ 编排
      ▼
Domain (GridEngine ← GridStrategy ← GridTrigger)
      │ 依赖
      ▼
Price Domain (PriceCalculator: ATR / 前低)
```

领域层**不依赖 pandas / 网络**；应用层负责把 DataFrame 转换为
`AdjustedPriceSeries` 与 `GridMarketSnapshot`。

### 4.2 关键扩展点

| 需求 | 扩展方式 |
|------|---------|
| 前低窗口天数 | `GridTrigger.lookback_days` |
| ATR 周期 | `GridTrigger.atr_period` |
| 网格间距比例 | `GridTrigger.step_ratio` |
| 单格买入金额 | `GridTrigger.buy_amount` |
| 最小交易单位 | `GridTrigger.lot_size` |
| 锚点来源 | `GridTrigger.anchor` |
| 价格口径 | `GridTrigger.price_basis` |
| 最大网格层数 | `GridTrigger.max_levels` |
| 止盈倍数 | `GridTrigger.take_profit_ratio` |
| 交易成本 | `GridCostModel` |
| 网格资金规模 | `GridConfig.initial_capital` |

---

## 5. 使用示例

### 5.1 网格回测

```python
from decimal import Decimal
from src.application.grid.grid_backtester import GridBacktester, GridConfig
from src.domain.allocation.entities import AllocationPlan
from src.domain.grid.value_objects import GridTrigger, GridPriceBasis

# 总资产配置（再平衡与网格共用）
plan = AllocationPlan.equal_weight(
    [("515180", "红利ETF"), ("159937", "黄金ETF")], name="两资产等权"
)

config = GridConfig.from_allocation(
    plan=plan,
    trigger=GridTrigger(
        lookback_days=120,            # 前120日最低价（不含当日）
        atr_period=120,              # 120日ATR
        step_ratio=Decimal("0.5"),   # 网格间距 = ATR * 0.5
        buy_amount=Decimal("10000"), # 每格买入 10000 元
        take_profit_ratio=Decimal("1"),  # 上涨一个步长止盈（决定本轮结束）
        price_basis=GridPriceBasis.LOW,
    ),
    initial_capital=Decimal("200000"),
)

result = GridBacktester(config).run()
print(result.summary())

# 触发日志：资产代码 / 名称 / 触发日期 / 交易类型
from src.application.grid.grid_chart import GridChartRenderer
print(GridChartRenderer.trigger_log(result))
```

### 5.2 组合回测（共享配置 + 持仓隔离）

```python
from src.application.grid.combined_backtester import CombinedBacktester, CombinedConfig

combined = CombinedBacktester(
    CombinedConfig(
        allocation=plan,                     # ★ 一份配置驱动两个策略
        rebalance_initial_capital=Decimal("1000000"),
        grid_initial_capital=Decimal("200000"),
        grid_trigger=trigger,
    )
).run()
print(combined.summary())
```


### 5.3 实盘：单日决策 + 状态持久化

```python
from src.infrastructure.grid.repositories import JsonFileGridStateRepository
from src.domain.grid.services import GridEngine

repo = JsonFileGridStateRepository("data/grid_states")
strategy = repo.find_by_id(strategy_id)      # 恢复上次运行的档位进度

decision = GridEngine.evaluate(strategy, "515180", snapshot)
if decision.has_orders:
    for order in decision.orders:
        print(order.to_dict())

repo.save(strategy)                            # 保存新的档位与持仓状态
```

---

## 5.4 图表输出（`GridChartRenderer`）

主图 `grid_strategy_result.png` 为两行布局：

```
┌──────────────────────────────────────────────────────────────┐
│ 上：各资产走势图（归一化 起点=100）                            │
│   · 每个资产一条曲线（有过网格交易的资产为实线，其余虚线淡化）   │
│   · 不绘制买卖点标记，不绘制轮次竖线                            │
│   · 仅在「开仓」日标注：交易日期 + 步长占价格比例                │
│     标注框颜色 = 资产颜色，细引线连回锚点，按资产错层            │
├──────────────────────────────────────────────────────────────┤
│ 下：各资产网格持仓市值堆叠图 + 网格交易收益曲线（右轴）         │
│   · 堆叠面积：每层 = 一个资产的网格持仓市值（份额 × 收盘价）     │
│   · 阶梯绘制，短持仓周期也能看清持仓区间                        │
│   · Y 轴（左）按堆叠高度自适应，直观反映资金在资产间的进出      │
│   · Y 轴（右）叠加网格交易收益曲线（百分比，仅已实现）：        │
│       百分比 = 累计已实现收益(元) / 回测期间网格最大持仓         │
│       - 阶梯曲线，仅在止盈平仓时跳变                            │
│       - 不绘制含浮动的总盈亏曲线                                │
│   · 不绘制网格总市值曲线，不绘制轮次辅助线                      │
│       （总市值/轮次信息只在控制台摘要与触发日志中给出）         │
└──────────────────────────────────────────────────────────────┘
```

辅助输出：

- `grid_strategy_comparison.png`：再平衡 vs 网格 **归一化净值**（起点=100，持仓隔离对比）
  —— 不绘制网格总市值等绝对金额曲线（两个子组合规模不同，绝对值对比无意义）
- `GridChartRenderer.trigger_log(result)`：控制台触发日志
  （资产代码 / 名称 / 触发日期 / 交易类型：开仓·加仓·止盈 / 轮次 / 档位 / 股数 / 价格 / 金额）
  —— **同日同轮的止盈会聚合为一行**（整轮一次性清仓时每个批次各产生一笔卖出，
  聚合后按加权均价与合计股数展示，避免刷屏）；
  `aggregate_take_profit=False` 可查看逐笔明细
- `GridChartRenderer.trigger_log(result)`：控制台触发日志
  （资产代码 / 名称 / 触发日期 / 交易类型：开仓·加仓·止盈 / 轮次 / 档位 / 价格 / 金额）

```python
from src.application.grid.grid_chart import GridChartRenderer

GridChartRenderer.render(result, output_path='grid_strategy_result.png')
GridChartRenderer.render_comparison(combined, output_path='grid_strategy_comparison.png')
print(GridChartRenderer.trigger_log(result))
```

---

## 6. 测试

```bash
python -m pytest tests/test_grid_domain.py -v
```

覆盖点：

- 值对象不变量（ATR 比例、买入金额、窗口天数、止盈倍数…）
- ATR / 前 N 日最低价计算（含"不含当日"边界）
- 网格状态机：IDLE → MONITORING → ACTIVE
- 跌破前低才激活；未跌破只监控
- 步长 = ATR × 比例；档位价 = 锚点 - 步长 × N
- 买入金额与整手取整；跳空多档触发
- 资金不足跳过档位；网格现金永不为负
- 止盈按批次卖出；**T+1** 当日买入不可当日卖出
- **本轮完全止盈后结束；需重新跌破前低才开启下一轮**
- 交易类型分类：开仓 / 加仓 / 止盈（整轮一次性清仓，无减仓）
- 两种价格口径（收盘价 / 最低价 + 限价成交、跳空成交）
- **总资产配置共享**：网格监控资产 = 再平衡配置资产
- **网格与再平衡的资金/持仓隔离**
- 策略序列化往返（实盘状态持久化）

---

## 7. 实盘注意事项

1. **默认口径说明**：`CLOSE` 口径最保守；`LOW` 口径更贴近真实挂单撮合，
   但对数据质量与滑点更敏感。
2. **网格越跌越买**：资金耗尽后自动跳过档位（记录在 `skipped_levels`），
   不会强行成交。
3. **轮次不会自动重启**：本轮完全止盈后，必须**再次跌破前 120 日最低价**
   才会开启下一轮。若希望连续接，请将 `take_profit_ratio` 设为 None
   （变为只买不卖的无限网格）。
4. **T+1**：当日买入的批次当日不会卖出，止盈最早发生在次日。
5. **后复权价格**：ATR 与前低均基于后复权价计算，避免分红除权造成的假突破。
6. **状态持久化**：实盘务必保存 `GridStrategy`，否则重启会丢失档位进度，
   可能重复在错误档位买入。
7. **共享配置**：改一次 `AllocationPlan` 即可同时影响再平衡与网格两侧，
   避免两边监控资产不一致。
