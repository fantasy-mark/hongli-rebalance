# 领域模型分析 (DDD) — hongli-rebalance

> **目的**：把当前项目里已经长出来的概念（资金池、虚拟盘回测、回测图表、实盘交易记录、
> 多资产再平衡、复合网格）用 DDD 的语言重新表述，**提取出适合后续重构的领域模型**，
> 并把边界、不变量、口径差异显式写下来。
>
> **范围**：本文是**分析 + 建模建议**，不是实施方案。所有"现状"描述都带 `文件:行号`，
> 可以直接核对。现状与建议在文中严格分开：§2 是"代码里现在是什么"，§3 起是"应该建模成什么"。
>
> **日期**：2026-10-25　**代码基线**：`9504817` (feat(ui): 网格建议口径 + 手动提交前置预检)
>
> **重构进度**（§11.2 有各阶段的详细记录）：
> Phase 0 ✅ 立柱 · Phase 1 ✅ 值对象 · Phase 2 ✅ 网格聚合显式化 ·
> Phase 3 ✅ 触发规则去重 · Phase 4 ✅ 组合聚合与来源标注 ·
> Phase 5 ✅ 事件化 + 应用层瘦身（5a 配置对象 · 5b 领域事件 · 5c 应用层瘦身
> —— 5c-1 每日快照 · 5c-2 写路径）· Phase 6 ✅ 状态机显式化 ·
> Phase 7 ✅ 账本读侧拆成投影 · D6 ✅ 披露侧（真实持仓 / 归并 分栏）·
> D11 ✅ 图表读实际持仓 · D3 ✅ 虚拟预算改 `GridBudget` ·
> D4 ✅ 资金管理与 badge 口径对齐 · D1 ✅ 120 日窗口带日期 ·
> D12 ✅ GitHub Actions 契约提取 · D13 ✅ 仓库路径单一真源。
> 各阶段的「落地情况」见 §11.1.1 ~ §11.1.15。

---

## 1. 结论先行

当前项目**已经**是一个有明确领域模型的系统，但模型是**隐式的**——它散落在
dict 的形状、函数名、注释和 README 文案里，而不是体现在类型和边界上。这带来三类成本：

1. **口径漂移**（同一条规则在 2~3 处各写一遍，改一处漏一处）
2. **边界泄漏**（模拟数据与真实数据混入同一个聚合）
3. **隐式契约**（"6 key 字面量"、"dict 里必须有 `signal` 字段"这类规则只存在于注释里）

本文提取出 **7 个限界上下文、5 个聚合根、约 20 个值对象、9 个领域服务、10 个领域事件**，
并给出一条 6 阶段的重构路线（§11）。核心判断：

> **不要重写。** 这个项目的领域知识是稀缺的、正确的、也是用户逐条决策出来的
> （见 §5 通用语言里那些带"用户决策"标记的条目）。重构的目标应该是
> **把隐式模型显式化**，而不是重新设计业务。

---

## 2. 领域全景（现状盘点）

### 2.1 上下文与代码的对应

| # | 领域概念（用户说法） | 代码位置 | 入口 | 持久化 |
|---|---|---|---|---|
| A | **实盘交易记录** | `src/record.py:145-264`, `scripts/apply_holdings.py` | 页面编辑器 → `trades-v1` payload | `data/trades.json`（事实源）<br>`data/holdings.json`（派生缓存 v2） |
| B | **多资产再平衡模型** | `src/record.py:326-463`, `src/three_pp.py` | 每日 Action | `data/history.json` 的 `plan` / `weights` |
| C | **复合网格模型** | `src/grid.py`（747 行）, `scripts/apply_grid.py` | 页面编辑器 → `grid-ops-v1` payload | `data/grid_state.json` |
| D | **行情/定价** | `src/fetch.py`（395 行） | 每日 Action（realtime→spot→history 三级降级） | 无（外部系统） |
| E | **虚拟盘回测 + 图表生成** | `src/chart.py`（534 行）, `src/three_pp.py:63`, `src/grid.py:116` | 每日 Action（`record.py:812`） | `data/grid_chart.png` |
| F | **通知/呈现** | `src/notify.py`（589 行）, `app.js`（1356 行） | 每日 Action / 页面 | `data/history.json`（只读） |
| G | **编排** | `src/record.py:640`（`main()` 848 行）, `.github/workflows/*` | GitHub Actions | — |

> **注（2026-10-28 补）**：BC-G 的**上游系统**就是 GitHub Actions。它的契约
> （`$GITHUB_OUTPUT` / `::error::` / `GITHUB_RUN_ID` …）原先散在应用层、
> 呈现层和脚本里，现已收进 `src/infrastructure/github_actions/`（防腐层）——
> 详见 §11.1.15 的 D12。

### 2.2 一次每日运行的数据流（现状）

```
                    ┌──────────── src/fetch.py (BC-D 行情) ────────────┐
                    │  latest() → close     fetch_kline_history() → low │
                    └───────────────┬──────────────────────┬────────────┘
                                    │                      │
  data/trades.json ──► record.py:229 rebuild_holdings_from_trades
                                    │                      │
                            (positions, cash_rebalance, cash_grid)
                                    │                      │
                    record.py:369 merge_grid_shares_into_positions
                                    │        (归并份额并入组合)
                                    ▼
                            record.py:431 evaluate()
                                    │  total = Σvalue + cash_rebalance + cash_grid
                                    │  target_w = 1/len(positions)
                                    │  triggered = any |w − target_w|×100 > 3
                                    ▼
  data/grid_state.json ──► grid.py:501 advisory() ──► grid.py:539 advance_one_day()
                                    │                     (只滚窗口，不下单)
                                    ▼
                            record.py:535 build_record()
                                    ▼
              data/history.json ─┬─► chart.py (BC-E) ─► data/grid_chart.png
                                   └─► notify.py (BC-F) ─► 邮件
```

**关键观察**：图中每一条箭头在代码里都是一个**裸 dict 的字段传递**。没有类型、
没有接口、没有边界。这是本项目最核心的结构性问题，也是 DDD 重构的主战场。

---

## 3. 战略设计（Strategic Design）

Evans 的三分类：

| 分类 | 上下文 | 理由 |
|---|---|---|
| **核心域**（Core） | **C 复合网格** | 项目的全部差异化所在。触发规则、成本额出场、归并份额这套语义是用户逐条决策出来的，别家的 ETF 再平衡工具都没有。**投入最多抽象精力**。 |
| **核心域**（Core） | **B 多资产再平衡** | 3pp 等权 + 动态篮子 + 快照回正，是策略主干。规则简单但**口径极多**（归并份额、网格占用、现金池），复杂度在口径而非算法。 |
| **支撑域**（Supporting） | **A 实盘交易记录** | 需要，但通用。就是一个 append-only 的复式账本 + 投影。没有独特的领域知识。 |
| **支撑域**（Supporting） | **F 通知/呈现** | 典型的下游读取方。复杂度在"别把模拟说成实盘"这种口径纪律上，不在功能。 |
| **通用域**（Generic） | **D 行情/定价** | 纯粹的外部系统适配。sina/eastmoney/tencent/akshare 的降级链、QDII 溢价、复权处理。**买现成方案或隔离成 ACL，不要自己建模**。 |
| **通用域**（Generic） | **G 编排** | 应该是薄的应用服务层，不该有领域模型。 |
| **（独立）** | **E 虚拟回测** | 它**不是**生产上下文的子集，而是一个**独立的、共享核心概念的模拟器**。需要特别小心 —— 见 §9 口径鸿沟。 |

> **E 是最容易被误判的一格。** 现在的 `src/grid.py` 把回测（`run_compound_grid`）
> 和生产（`advance_one_day` / `manual_op`）放在同一个模块里，共用 `split_take_profit`
> 但**各写各的触发逻辑**。DDD 上必须把 E 拆出去，用防腐层隔开。

---

## 4. 限界上下文（Bounded Contexts）

### BC-A「账本」Ledger

> **统一语言**：交易记录、成交、投出、取出、划转。

| 项 | 内容 |
|---|---|
| **聚合根** | `Ledger` — 事实源。append-only。 |
| **实体** | `LedgerEntry`（身份 = `id`，格式 `<date>-<side>-<seq>`） |
| **值对象** | `TradeSide`、`Money`、`ShareCount`、`AssetCode`、`TradingDate` |
| **领域服务** | `LedgerProjection`（把账本投影成 `PortfolioState`） |
| **仓储** | `TradeLedgerRepository` → `data/trades.json`（append）<br>`HoldingsCacheRepository` → `data/holdings.json`（**派生缓存**，可重建） |
| **不变量** | 现金池不得为负；卖不得超持；同资产同时买同价可并存（成本法） |

**现状实现**：`record.py:145 apply_trade`（幂等校验后提交）、`record.py:229
rebuild_holdings_from_trades`（按 `(date, 原始下标)` 排序重放）。

**必须显式化的边界**：`holdings.json` **不是事实源**，是投影缓存。当前
`record.py:645` 又把它当"资产宇宙"用（取 code 列表），`record.py:658` 还拿它的
shares 做兜底 —— 同一个文件扮演了 3 种角色。模型上应拆成：

- `Ledger`（事实）→ `data/trades.json`
- `PortfolioProjection`（派生）→ 内存/缓存
- `AssetUniverse`（**独立的**资产注册表：`code → (name, sina, em, is_qdii)`）→ 应从
  `record.py:38 ETF_DEFS` + 一个显式的 `data/universe.json` 派生，而不是从持仓反推。

### BC-B「组合」Portfolio / Rebalance

> **统一语言**：持仓、市值、权重、偏离、目标权重、阈值、调仓计划。

| 项 | 内容 |
|---|---|
| **聚合根** | `Portfolio` |
| **实体** | `Position`（身份 = `AssetCode`） |
| **值对象** | `Price`、`Weight`、`TargetWeight`、`DeviationPP`、`Valuation`、`CashAmount` |
| **领域服务** | `ValuationService`（定价 + 估值）<br>`RebalancePolicy`（偏离判定 + 调仓计划） |
| **事件** | `RebalanceTriggered`、`PositionValued` |

**核心规则**（`record.py:431 evaluate`）：
- `total = Σ position.value + cash_rebalance + cash_grid`
- `target_w = 1 / len(positions)` —— **动态篮子**（3/4/5 资产通用）
- `triggered = ∃ asset : |w − target_w| × 100 > 3pp`
- 触发时**全额快照回正**（不是增量再平衡）

**`RebalancePolicy` 是纯函数**（输入 positions + cash，输出 total/weights/triggered/plan），
天然是领域服务。当前它塞在 `record.py` 的模块级函数里，依赖模块级 `THRESHOLD_PP`
环境变量 —— 重构时应改为**显式参数**，让阈值成为策略实例的一部分。

**⚠ 边界泄漏（重要）**：`record.py:369 merge_grid_shares_into_positions` 把
**模拟口径**的归并份额直接加进**真实口径**的 `Position`，产生
`merged_shares` / `total_shares` / `merged_value` / `merged_only` 四个字段
（`record.py:396-427`）。这些模拟份额随后参与 `evaluate()`，**能触发真实的再平衡交易**。

领域上正确的建模是让 `Position` 持有一组**带来源的批次**：

```python
@dataclass(frozen=True)
class PositionLot:
    shares: ShareCount
    cost: Money            # 归并份额 cost = 0
    provenance: Provenance  # REAL | SIMULATED_GRID
```

`Position.total_shares` = 所有 lot 之和（对外口径不变），
但 `Position.real_shares` / `Position.simulated_shares` 可分别取，
让"模拟份额推着真实组合调仓"这件事在代码里**显形**，而不是靠 `merged_only` 布尔位。

### BC-C「复合网格」Compound Grid —— 核心域

> **统一语言**：周期、层、开仓、加仓、回本出场、归并份额、资金占用、建议、操作记录。

| 项 | 内容 |
|---|---|
| **聚合根** | `GridPosition`（身份 = 资产；一个资产最多一个进行中的周期） |
| **实体** | `GridCycle`（进行中的周期）<br>`GridLayer`（一层买入 = date + price + shares）<br>`GridOp`（操作记录行） |
| **值对象** | `TriggerLevel`（`OPEN`/`ADD`/`EXIT_AT_COST` 三种）<br>`TriggerDistance`（带符号百分比）<br>`PriceWindow`（滚动 120 日）<br>`LayerSize`（层额）<br>`MergedShares` |
| **领域服务** | `TriggerPolicy`（什么算触发）<br>`GridAdvisor`（给建议）<br>`LayerSizingPolicy`（层额）<br>`CostOnlyExitPolicy`（只卖成本额） |
| **仓储** | `GridStateRepository` → `data/grid_state.json` |
| **事件** | `GridCycleOpened`、`GridLayerAdded`、`GridCycleClosed`、`MergedSharesTransferred` |

**规则**（全部来自用户决策，不是推导出来的）：

| 规则 | 值 | 位置 |
|---|---|---|
| 开仓触发 | 价格跌破过去 120 日窗口最低 | `grid.py:395 _triggers` |
| 加仓触发 | 价格 ≤ 最近一笔买入价 × 97% | 同上 |
| 回本触发 | 价格 ≥ **第一笔**买入价 × 103% | 同上（`GRID_TP_BASIS="first_buy"`） |
| 层额 | `¥10,000 + max(池值 − 757日最小池值, 0) × 5%` | `grid.py:54 layer_yuan` |
| 出场 | 只卖 `本金 / 价格` 份（恰好回本），余量归并 | `grid/exit_policy.py cost_only_exit` |
| 周期结构 | N 笔买入 + 1 笔回本出场 | 用户决策 2026-10-25 |

**⚠ 术语债（必须处理）✅ 规则本体已处理（2026-10-27）**：代码里原来叫
`take_profit` / `split_take_profit`，但业务语义是**"只卖成本额、恰好回本"**——
浮盈一分都不兑现。这跟"止盈"是**相反**的。
现已改名为 `ExitAtCost` / `cost_only_exit`（见 §11.1.3），否则新人和未来的自己
会一直误读。`app.js` / `notify.py` 里已经用的是「建议回本」。

> `GRID_TP_PCT` **故意没改名**：它是「回本**触发**阈值」（高于首笔买入价 3%），
> 属于**触发规则**而不是出场规则 —— Phase 3 的 `TriggerPolicy` 已接管它
> （生产路径与回测路径现在共用同一个类），届时再随该上下文一起命名。

**⚠ 建议 ≠ 成交**：2026-10-25 起引擎降级为**只建议不下单**（`grid.py:539
advance_one_day` 只滚窗口 + 算建议；`grid.py:668 manual_op` 是唯一实盘入口）。
这在模型上是一条**必须写进类型系统的边界**：`GridSuggestion` 和 `GridOp` 是
两个不同的概念，不能共用一个 `signal` 字符串。详见 §6.3。

### BC-D「行情」Market Data —— 通用域

> **统一语言**：实时价、收盘价、盘中最低价、复权、溢价率、IOPV。

外部系统适配层。三级降级链（`fetch.py:318 latest`）：实时 → 现货 → 历史。
另有两个专用能力：`fetch.py:339 fetch_kline_history`（取当日 **low**，实盘触发价
用它）、`fetch.py:240 fetch_qdii_premium`（QDII 溢价率告警）。

**领域模型建议**：只保留 3 个值对象 `Price` / `IntradayLow` / `PremiumRate`，
以及一个 `QuoteProvider` 接口。**不要**在这里建模"ETF"、"基金"、"份额类别"这些
概念 —— 那是上游供应商的事。

### BC-E「回测」Simulation —— 独立上下文

> **统一语言**：重放、模拟、权益曲线、资金占用曲线、归并市值。

| 项 | 内容 |
|---|---|
| **服务** | `GridSimulator`（`grid.py:116 run_compound_grid`）<br>`EqualWeightBacktester`（`three_pp.py:63`）<br>`ChartRenderer`（`chart.py`） |
| **输入** | `PriceSeries`（时间索引的收盘价序列） |
| **输出** | `EquityCurve` / `CapitalUsageSeries` / `CycleReport` |
| **仓储** | `ChartArtifactRepository` → `data/grid_chart.png` |
| **落库** | `ops_from_cycles`（`grid.py:251`）把模拟周期转成 `source="backtest"` 的 op 行 |

**它和生产共享领域概念，但不共享实现** —— 这正是要设防腐层的地方（§9）。

### BC-F「呈现」Presentation

> **统一语言**：快照、摘要、徽章、建议徽章、邮件主题。

只读消费 BC-B / BC-C 产出的 `DailySnapshot`。两个实现：
`notify.py:122 build_text` / `notify.py:335 build_html` / `notify.py:520 build_subject`
（邮件），`app.js` 的 `renderStatus` / `renderLatest` / `renderGridOpsLog`（页面）。

**呈现层的领域规则只有一条，但它极其重要**：
> **不得把模拟说成实盘。**

页面与邮件必须同时标注：网格资金占用是虚拟的、归并份额是模拟口径、ops 里
`backtest` 是历史重放。`app.js:283` 的 PAT 存在 localStorage（未加密）—— 这是
另一个必须由呈现层负责披露的事实。

### BC-G「编排」Application

`record.py:640 main()` 848 行做了 9 件事（读账本→取价→合并归并→求值→算层额→
推进网格→再求值→写盘→画图→发通知）。它**应该**是薄的应用服务：
一串 `use case` 的编排，每步调一个领域服务，不含业务规则。

---

## 5. 通用语言（Ubiquitous Language）

> 这一节是重构的**验收标准**：重构后代码里的标识符应该长成右列。
> 标注「⚠」的是当前与业务含义**不一致**、必须改名的。

| 代码里的词 | 领域词 | 说明 |
|---|---|---|
| `cash_rebalance` | `RebalancePool` | **真实**现金，用于 ETF 买卖。`CashPool(kind=REAL)` |
| `cash_grid` | `GridBudget` | **虚拟**预算槽。⚠ 它不是真钱，名字像现金池会误导 |
| `positions` | `Position` + `PositionLot[]` | ⚠ 现在把真实和模拟份额混在一个 dict 里 |
| `weights` | `Weight` | 值对象，不是裸 float 列表 |
| `target_w` | `TargetWeight` | 动态 = 1/n |
| `3pp` | `RebalanceThreshold` | `DeviationPP` 值对象 |
| `plan` | `RebalancePlan` | 目标市值 / 差额 |
| `in_cycle` | `GridPosition.IsOpen` | 聚合根的状态属性 |
| `entries` | `GridLayer[]` | 周期内的层 |
| `last_prices` | `PriceWindow` | ⚠ 现在是裸 float 列表；应带日期（见 §10 已知问题） |
| `ops` | `GridOp`（**两张账**：执行日志 / 模拟日志） | ⚠ 现在用 `source` 字符串在同一数组里区分 |
| `merged_shares` | `MergedShares` | 已移交组合的零成本份额，**模拟口径** |
| `current_value` | `CapitalTiedUp` | 网格**在周期内**占用的钱。⚠ 不是"市值" |
| `cum_realized` | `RealizedPnl` | ⚠ 正常路径恒为 0 |
| `signal` | `GridSuggestion`（open/add/tp/None） | ⚠ 是建议不是成交，类型上必须分开 |
| `trigger_pct` / `dist_*_pct` | `TriggerDistance` | 约定：**正数 = 未触发，≤0 = 已越过** |
| `take_profit` / `tp` | `ExitAtCost` | ⚠ 名字与语义相反 |
| `layer_yuan` | `LayerSize` | 浮动层额策略 |
| `source: manual` | `ExecutedOp` | 页面提交的真实操作 |
| `source: backtest` | `SimulatedOp` | 历史重放 |
| `source: auto` | （已废弃） | 2026-10-25 迁移删除，保留兜底映射 |
| `total` | `PortfolioValue` | = ETF 市值 + 两个池 |
| `history.json` 的一条 | `DailySnapshot` | 每天一份的不可变快照 |
| `qfq` / 前复权 | `AdjustedPrice` | 所有 ETF 收盘价用前复权 |

### 用户决策溯源（重构时不能推翻的语义）

这些是 AskUserQuestion 里用户逐条拍板的，重构时**只允许改实现，不许改语义**：

1. 出场只卖成本额（恰好回本），浮盈永久化 → `grid-cost-only-exit`
2. 余量归并进再平衡组合的份额，参与权重与市值，但**不写** `trades.json` → `grid-merge-to-rebalance`
3. 网格引擎**只给建议，不下单**；实盘开/加/回本全部由用户提交 → `grid-engine-advisory-only`
4. 一个周期 = N 笔买入 + 1 笔止盈；买入份数 − 止盈卖出份数 = 归并份额
5. 存量自动开仓周期清空，从零开始（2026-10-25 迁移）
6. 复合网格的 h3 资金占用列 = 唯一权威口径

---

## 6. 聚合、实体与值对象

### 6.1 聚合根一览

| 聚合根 | 边界 | 内部实体 | 持久化 |
|---|---|---|---|
| `Ledger` | 一次账本文件 | `LedgerEntry` | `data/trades.json` |
| `Portfolio` | 一次快照 | `Position`（含 `PositionLot`） | 投影 → `holdings.json` / `history.json` |
| `GridPosition` | 单个资产的网格账 | `GridCycle` → `GridLayer`；`GridOp` | `data/grid_state.json` |
| `DailySnapshot` | 一天 | `Position` / `GridSignal` 快照 | `data/history.json`（一条） |
| `ChartArtifact` | 一张图 | — | `data/grid_chart.png` |

> ✅ **Phase 4 已落地**（2026-10-27）。`Portfolio` 现在是**真的聚合根**：
> `Position` 持有 `PositionLot[]`，每批份额带自己的 `Provenance`；`Portfolio`
> 是**唯一**计算市值 / 权重 / `total` 的地方，`data/history.json` 里的持仓
> 行是它的**输出**（`Portfolio.to_records`），不是反过来。
>
> 另有一个**注册表**（不是聚合根，是值）：`AssetUniverse`
> （`domain/portfolio/asset_universe.py`）——「本项目跟踪哪些资产」的定义，
> 从 `holdings.json` 里独立出来。详见 §11.1.6。

**`GridPosition` 是唯一一个有真正状态机的聚合**，也是唯一值得花力气做行为建模的：

```
        ┌──────┐  open   ┌────────────────────────┐
   待机 │ Idle │────────►│  Open (N layers)        │
        └──────┘         │  layers: 1..n           │
           ▲             │  add → layers+1         │
           │             │  exit → 只卖成本额       │
           │             └───────────┬────────────┘
           │                         │
           └─────────────────────────┘
                  exit (cost-only)
                  + MergedShares 移交组合
```

`Idle` 与 `Open` 的**唯一合法出口**是 `ExitAtCost`；进入 `Idle` 时必须同时产出
`MergedShares` 事件。这条不变量就是"一个周期 = N 买 1 卖"的形式化。

### 6.2 值对象清单

| 值对象 | 语义 | 建议实现 | 现状 |
|---|---|---|---|
| `Money` | 带 ¥ 语义的金额 | `Decimal` 或 `int(分)` | 裸 float + `round(x, 2)`（`record.py:208`） |
| `ShareCount` | 份额 | `Decimal` 或 `int(1000份)` | 裸 float（`round(x, 2)`） |
| `Price` | 价格 | `Decimal` | 裸 float（`round(x, 4)`） |
| `Weight` | 权重 0~1 | `Decimal` | 裸 float 列表 |
| `DeviationPP` | 百分点偏离 | `Decimal` | `abs(w - target_w) * 100` |
| `TradingDate` | 交易日 | `datetime.date` | ISO 字符串 + 正则校验（`apply_grid.py:70`） |
| `AssetCode` | 6 位代码 | 新类型 | 裸 str + `re` 校验（`apply_grid.py:69`） |
| `TradeSide` | 8 种交易方向 | 枚举 | 裸 str（`record.py:172-187`） |
| `TriggerLevel` | open/add/exit_at_cost | 枚举 | 裸 str（`signal` 字段） |
| `TriggerDistance` | 带符号距离 | 值对象 | 裸 float，`round(x, 2)`（`grid.py:518`） |
| `PriceWindow` | 滚动 120 日窗口 | 不可变序列 | 裸 `list[float]` |
| `LayerSize` | 层额 | 值对象 | 裸 float（`grid.py:54`） |
| `Provenance` | REAL / SIMULATED_GRID | 枚举 | ✅ Phase 4 —— `PositionLot.provenance`，`merged_shares` 不再是旁挂字段 |

**收益点**：把 `Money` / `ShareCount` / `Price` 换成 `Decimal` 可以一次性消灭
"份额和市值不自洽"这一类 bug（`grid.py:632` 注释里记的那个 3.885 份 × 103 =
400.16 vs 400.67 的问题）。

> **落地说明（2026-10-26）**：这批值对象已实现，但**后端是 `float` 子类而不是
> `Decimal`** —— `data/*.json` 由 `app.js` 读，`Decimal` 序列化不了；而
> `Decimal` 的算术会改变舍入时机，把账上的数字全改掉。取值规则（构造时取整、
> 算术不重写）与理由见 **§11.1.2**。

### 6.3 建议与成交必须分型

当前 `signal` 是一个字符串，值为 `"open" | "add" | "tp" | None`（外加遗留的
`"buy" | "sell"`）。它在 `record.py:611` 被注释为"建议提交哪种 op, 不是'已成交'"，
但类型上完全分不出来。

建议：

```python
@dataclass(frozen=True)
class GridSuggestion:
    level: TriggerLevel | None      # None = 无建议
    distance: TriggerDistance
    suggested_price: Price
    suggested_size: Money | None    # 只对 open/add 有值
```

而 `GridOp` 是**已经发生**的操作，带 `source: manual`。这样：
"引擎永远不下单"这条不变量由**类型**保证，而不是靠注释和 code review。

> ✅ **Phase 2b 已落地**（2026-10-27）。实际落地形状与上面的草稿略有不同：
> `GridSuggestion` 的三个距离分成了 `open_distance` / `add_distance` /
> `tp_distance` 三个独立字段（不是一个 `distance`）—— 页面要分别显示
> 「距开仓 / 距加仓 / 距回本」；成交侧分成了 `GridOp`（基类）/ `ExecutedOp`
> （`source="manual"`）/ `SimulatedOp`（`source="backtest"`）三个类型。
> 详见 §11.1.4。

---

## 7. 领域服务

| 领域服务 | 现状函数 | 性质 | 备注 |
|---|---|---|---|
| `LedgerProjection` | `record.py:229 rebuild_holdings_from_trades` | 纯 | 排序规则 `(date, 原下标)` 是隐含约定 |
| `TradeApplier` | `record.py:145 apply_trade` | 纯 | 幂等校验 + 提交，好设计 |
| `ValuationService` | `record.py:295 fetch_positions` | 有 I/O | 属 BC-D，但结果组装在 BC-B |
| `RebalancePolicy` | `domain/portfolio/rebalance_policy.py:RebalancePolicy.evaluate` | 纯 | ✅ Phase 4 —— 阈值是**实例状态**（`threshold_pp`），模块级 `evaluate()` 只是兼容包装 |
| `TargetWeightPolicy` | `domain/portfolio/rebalance_policy.py:TargetWeightPolicy` | 纯 | ✅ Phase 4 —— 等权 `1/n` 独立成类，「动态篮子」由它定义 |
| `RebalancePlanner` | `domain/portfolio/rebalance_policy.py`（plan 构造） | 纯 | ✅ Phase 4 —— 从 `evaluate` 里搬进 `RebalanceOutcome.plan` |
| `TriggerPolicy` | `grid.py:395 _triggers` | 纯 | ✅ Phase 2b 提成类 `domain/grid/trigger_policy.py`；**Phase 3 生产 + 回测两条路径都接上** |
| `GridAdvisor` | `grid.py:501 advisory` | 纯 | ✅ 纯函数，不改 state → Phase 2b 提成类 `domain/grid/advisor.py` |
| `LayerSizingPolicy` | `grid.py:54 layer_yuan` | 纯 | ✅ Phase 3 提成类 `domain/grid/layer_sizing.py`（参数显式化） |
| `CostOnlyExitPolicy` | `grid/exit_policy.py cost_only_exit` | 纯 | ✅ 三处复用，唯一实现 |
| `GridSimulator` | `grid.py:116 run_compound_grid` | 纯（有 I/O 取价） | ✅ Phase 3 —— 触发判定改为调用 `TriggerPolicy`，不再自带一套 |
| `EqualWeightBacktester` | `three_pp.py:63 run_backtest` | 纯 | 通用，可直接被 DDD 包一层 |
| `DailyDigestBuilder` | `notify.py:122/335/520` | 纯 | 呈现层 |

### ✅ 最严重的一处重复：网格规则有两套实现 —— Phase 3 已消除

| 规则 | 生产（`TriggerPolicy.levels`） | 回测（`TriggerPolicy.levels`，**同一个类**） |
|---|---|---|
| 开仓 | `price < min(last_prices)` | `price < past.min()`（同） |
| 加仓 | `price ≤ entries[-1].price × 97%` | `price ≤ last_price × 97%`（同） |
| 回本 | `price ≥ entries[0].price × 103%` | `price ≥ tp_target`（同） |
| 出场 | `cost_only_exit` | `cost_only_exit`（✅ 已复用） |
| **驱动价** | **K 线最低价**（`record.py:768`） | **收盘价** |
| **层额** | `layer_yuan()` 浮动 | 固定 `GRID_BASE_YUAN` ¥10,000 |
| **起点** | 引擎上线以来 | 2020-11-05 全历史 |
| **买入价来源** | 用户提交 | 模拟器自己算 |

**重构目标**：把"什么算触发"抽成**一个** `TriggerPolicy`，由三个 driver 复用：

```
TriggerPolicy
  ├── LiveAdvisor      (price = K线最低价, layer = 浮动层额)  → 只出建议
  ├── ManualApplier   (price = 用户提交,  layer = 用户提交)  → 改状态
  └── GridSimulator   (price = 收盘价,    layer = 固定 ¥10k)  → 出模拟周期
```

三个 driver 的**驱动价/层额**不同是**合理的**（它们本来就是三种场景），
但**判定规则**必须只有一份。

> **进度** ✅ **已消除**（Phase 3，2026-10-27）。`TriggerPolicy` 类在 Phase 2b
> 落地（`src/domain/grid/trigger_policy.py`），Phase 3 把**回测路径也接上** ——
> `run_compound_grid` 里的内联判断换成调用同一个 `TriggerPolicy.levels(...)`。
> 剩下的驱动价/层额差异是**场景差异**（收盘价 vs K 线最低价、浮动层额 vs 固定
> ¥10k），**不是规则的两套实现**。行为**逐位不变**已验证 —— 详见 §11.1.5。

---

## 8. 领域事件

事件是重构里**最先能兑现收益**的部分：它们把"谁在什么时候因为什么改变了状态"
变成显式的一等公民，直接解决 §10 的可观测性问题。

| 事件 | 载荷 | 生产者 | 消费者 |
|---|---|---|---|
| `TradeRecorded` | side, code, shares/amount, price, date | BC-A | BC-B（投影） |
| `CashTransferred` | from_pool, to_pool, amount | BC-A | BC-B（total 变化） |
| `PositionValued` | code, price, value | BC-B | BC-F |
| `RebalanceTriggered` | deviating asset, deviation_pp, plan | BC-B | BC-F（徽章/邮件） |
| `GridCycleOpened` | asset, price, shares | BC-C | BC-F |
| `GridLayerAdded` | asset, layer_no, price, shares | BC-C | BC-F |
| `GridCycleClosed` | asset, exit_price, sold_shares, realized_pnl | BC-C | BC-F |
| `MergedSharesTransferred` | asset, shares, provenance=SIMULATED | BC-C | **BC-B**（并入组合） |
| `DailySnapshotProduced` | date, total, weights, signals | BC-G | BC-F |

`MergedSharesTransferred` 是**唯一跨上下文的领域事件**，也是 §4-BC-B 那个
"边界泄漏"最自然的收口点：网格只管宣告"我移交了 N 份零成本模拟份额"，
组合自己决定怎么吸收（并记 `Provenance.SIMULATED_GRID`）。

> **注意**：目前 `record.py` 的事件都是"事后记录"式的 —— 状态先改，
> 然后才把结果写进 dict。事件模型要求反过来：**先发事件，再由事件驱动状态变更**。
> 这是重构中最能提升可测试性的一步。

> ✅ **Phase 5b 已落地**（2026-10-27）—— 事件在 `src/domain/events/domain_events.py`。
> 九个事件全部有类型，每个会改状态的领域函数都接受一个**可选**的
> `events: EventStream | None = None`：
>
> | 事件 | 谁发 | 什么时候发 |
> |---|---|---|
> | `TradeRecorded` / `CashTransferred` | `ledger.apply_trade` | 每一笔真实成交 / 每一笔现金划转 |
> | `PositionValued` | `portfolio.valuation.value_positions` | 每个成功估值的持仓 |
> | `RebalanceTriggered` | `RebalancePolicy.evaluate` | 3pp 触发时（**只**最终那次评估发） |
> | `GridCycleOpened` / `GridLayerAdded` / `GridCycleClosed` | `grid.manual_op` | 手动 open / add / tp |
> | `MergedSharesTransferred` | `grid.manual_op` + `grid.merged_share_transfers` | 出场有归并时 / 每日从状态宣告 |
> | `DailySnapshotProduced` | `daily_snapshot.main` | 快照产出后 |
>
> ⚠️ **不是事件溯源**（§11.3 明确不做）—— 事实源仍然是 JSON 文件，
> Git 提交历史就是最好的事件溯源。事件是**运行时的通知**。
>
> 唯一**承重**的事件通路是 `MergedSharesTransferred`：组合现在**只**从事件
> 吸收归并份额（`Portfolio.merged_by_code_from_transfers`），不再直接读网格
> 状态。这是 I9 的形状。

---

## 9. 上下文映射与口径鸿沟

### 9.1 上下文地图

```
        ┌──────────────┐
        │  BC-D 行情   │  (外部: sina/eastmoney/tencent/akshare)
        └──────┬───────┘
     upstream   │  Price / IntradayLow
        ┌──────┴──────────────┐
        ▼                     ▼
  ┌───────────┐         ┌───────────┐
  │ BC-C 网格 │         │ BC-B 组合 │
  └─────┬─────┘         └─────┬─────┘
        │ MergedSharesTransferred │ RebalanceTriggered
        └──────────────┬───────────┘
                       ▼
                 ┌───────────┐
                 │ BC-F 呈现 │
                 └───────────┘
   ┌───────────┐
   │ BC-A 账本 │──TradeRecorded──► BC-B 组合 (Conformist: 组合接受账本的投影)
   └───────────┘
   ┌───────────────────┐
   │ BC-E 回测 (独立)  │══ACL══► BC-C 网格  (回测有自己的口径, 绝不回写 grid_state)
   └───────────────────┘
```

### 9.2 三个必须设防腐层的口径鸿沟

#### ① 回测 vs 实盘（最严重）

`memory/grid-ops-backtest` 记的表：

| | 图表回放 | 实盘（手动提交） |
|---|---|---|
| 起点 | 2020-11-05 全历史 | 用户自己提交的 op |
| 层额 | 固定 ¥10,000 | 用户填的份额 / 浮动层额 |
| 价格 | **收盘价** | **K 线最低价** |
| 成交 | 模拟器自己算 | 真实成交 |

**结论：两者永远不可逐笔对账。** 这不是 bug，是两个场景。
DDD 上必须让这条鸿沟**在类型上不可混淆**：

- `SimulatedOp` 与 `ExecutedOp` 是两个类型，不共用一个 `source: str` 字段
- 回测上下文**不得**持有 `GridStateRepository` 的写权限
- `ops_from_cycles`（`grid.py:251`）产出的行必须带不可伪造的来源标记

#### ② 真实成交 vs 模拟成交

`data/trades.json` 只记真实成交（`apply_holdings.py` 的 `trades-v1` schema）。
归并份额是**模拟**的，但进了组合、参与真实权重的判定。

→ 用 `Provenance` 值对象在 `PositionLot` 上标注；
→ 页面/邮件必须分别显示"真实持仓"与"含归并"。

> ✅ **标注已落地**（Phase 4）：`Position` 现在持有 `PositionLot[]`，
> 每批份额带自己的 `Provenance`（`REAL` / `SIMULATED_GRID`）。`merged_shares`
> 不再是旁挂在持仓上的字段，而是 `position.simulated_shares` 这个**推出来的
> 属性** —— 「模拟份额推着真实组合调仓」是模型里读得到的事实，不是四个兄弟
> 字段拼出来的巧合。**披露**那一半还没做（呈现层仍只写「含归并」），留到
> Phase 5 的事件化一起做。

#### ③ 引擎建议 vs 用户操作

见 §6.3。`GridSuggestion` 与 `GridOp` 分型。

### 9.3 一处隐藏的近似（已在代码注释里，但值得单列）

`record.py:738-795` 的**两段式求值**：

```
total_pre  (归并前)  → 决定本次网格层额
total      (归并后)  → 写进快照 + 3pp 判阈
```

引擎不再自动交易后，这两个值**必然相等**，两段式只剩结构冗余。
但如果将来恢复任何自动行为，它就是**一天量级的近似**，不是同环精确解。
重构时应把这一层显式命名为 `LayerSizingBasis` / `SnapshotBasis`，
而不是靠两个变量名 `total_pre` / `total` 暗示。

---

## 10. 不变量清单（Invariants）

每条都标出**当前在哪里被强制**、**重构后应该在哪被强制**。

| # | 不变量 | 现状 | 重构后 |
|---|---|---|---|
| I1 | 现金池不得为负 | `record.py:211` | `Ledger.apply` 内部 |
| I2 | 卖不得超持 | `record.py:201` | `Ledger.apply` 内部 |
| I3 | 交易账本只能追加，不能改写 | `apply_holdings.py` 追加 | `Ledger.append` |
| I4 | 持仓/现金 = 账本重放的投影 | `record.py:229` | `LedgerProjection` |
| I5 | 一个资产同时最多一个进行中的网格周期 | `grid.py:697-700` | `GridPosition` 状态机 |
| I6 | 一个周期 = N 买 + 1 卖 | `grid.py:668 manual_op` | `GridPosition` 状态机 |
| I7 | 正常回本出场的已实现盈亏 ≡ 0 | `grid.py:108` | `CostOnlyExitPolicy` |
| I8 | 网格资金占用 = 仅周期内的层市值 | `grid.py:614 asset_snapshot` | `GridPosition.capital_tied_up` |
| I9 | 归并份额**不写** `trades.json` | 靠"没人写"这个事实 | ✅ `MergedSharesTransferred` 事件 + 账本侧**无写入口**（Phase 5b）—— 组合只从事件吸收归并份额；八个 `TradeSide` 里没有任何一个是「归并」，`apply_trade` 遇到未知 side 直接抛错 |
| I10 | 引擎**永不**改仓位/层数/盈亏/归并份额 | `grid.py:539` + 单测 | ✅ `GridAdvisor` 返回**不可变** `GridSuggestion`（Phase 2b） |
| I11 | 网格状态只由固定 6 个 key 构成 | ⚠ 隐含在 `grid.py:599` / `grid.py:702` 的字面量 | ✅ 显式 dataclass + 显式序列化（Phase 2a） |
| I12 | 目标权重 = 1/持仓数（动态篮子） | `record.py:449` | ✅ `TargetWeightPolicy`（Phase 4） |
| I13 | 偏离 > 3pp 才触发，全额快照回正 | `record.py:451` | ✅ `RebalancePolicy`（Phase 4）—— 阈值是**实例状态**，不是模块级环境变量 |
| I14 | 一批网格操作 all-or-nothing | `apply_grid.py` 校验失败即退出 | `GridOpBatch` 的原子性 |
| I15 | 页面/邮件不得把模拟说成实盘 | ⚠ 靠文案自觉 | ✅ `ExecutedOp` / `SimulatedOp` 分型（Phase 2b）+ `Provenance` 落在 `PositionLot` 上（Phase 4）+ 呈现层披露 |

### I11 的具体危害（已发生过）

`advance_one_day` / `manual_op` 各自用**固定 6 key 字面量**重建 state
（`grid.py:599`、`grid.py:702` 的 `new_state`）。任何新增的 per-asset 字段
**会在下一次每日运行时被静默丢弃**。

2026-10-23~24 期间就是因为这个坑：`free_shares` 改名成 `merged_shares` 时，
旧文件里的 `free_shares` 被静默忽略，4 个资产都要手工迁移。

→ 重构后 `GridPosition` 应该是 dataclass，序列化由专门的 mapper 负责，
新增字段时**忘记迁移会立刻报错**而不是静默丢数据。

---

## 11. 重构路线图

### 11.1 目标目录结构

```
src/
  domain/                      # 纯领域层 —— 零 I/O、零 pandas、零 akshare
    shared/
      money.py                 # Money / ShareCount / Price / Weight / DeviationPP
      identifiers.py           # AssetCode / TradingDate
      provenance.py            # REAL | SIMULATED_GRID
    ledger/
      ledger.py                # Ledger(根) / LedgerEntry
      trade_side.py            # 8 种 side
      projection.py            # LedgerProjection
    portfolio/
      portfolio.py             # Portfolio(根) / Position / PositionLot
      rebalance_policy.py      # RebalancePolicy / RebalancePlan
      valuation.py             # Valuation
      asset_universe.py        # AssetUniverse —— 「跟踪哪些资产」的注册表
      cash_pools.py            # CashPoolBreakdown —— 「合计」口径 (D4)
    grid/
      grid_position.py         # GridPosition(根) / GridCycle / GridLayer
      grid_op.py               # GridOp / ExecutedOp / SimulatedOp
      trigger_policy.py        # TriggerPolicy / TriggerLevel / TriggerDistance
      exit_policy.py           # CostOnlyExitPolicy  (原 split_take_profit)
      advisor.py               # GridAdvisor → GridSuggestion
      layer_sizing.py          # LayerSizingPolicy
      price_window.py          # PriceWindow / WindowBar —— 带日期的 120 日窗口 (D1)
      state_machine.py         # LEGAL_TRANSITIONS —— 「N 买 1 卖」的那张表
      config.py                # GridConfig —— 唯一的 env 读取点
    events/
      domain_events.py         # 9 个事件
  application/                 # 用例编排 —— 薄
    daily_snapshot.py          # ← record.py:main()
    apply_trade.py             # ← apply_holdings.py 的领域部分
    apply_grid_op.py           # ← apply_grid.py 的领域部分
  infrastructure/              # 适配器
    paths.py                   # 仓库路径 —— **唯一真源** (D13)
    json_repositories.py       # 4 个 JSON 文件的读写（原子写）
    quote_providers.py         # sina/eastmoney/tencent/akshare 防腐层
    chart_renderer.py          # ← chart.py
    github_actions/            # GitHub Actions 防腐层 (D12)
      context.py               # RunContext —— 这次 run 是谁
      outputs.py               # $GITHUB_OUTPUT —— step outputs 的唯一写入口
      annotations.py           # ::error:: / ::warning:: / ::notice::
      summary.py               # $GITHUB_STEP_SUMMARY —— run summary 面板
  simulation/                  # BC-E 独立上下文
    grid_simulator.py          # ← run_compound_grid
    equal_weight_backtest.py   # ← three_pp.py
  presentation/
    digest.py                  # ← notify.py
    web/                       # app.js / index.html / style.css
```

**依赖方向铁律**：`domain` 不 import 任何其他层；`application` 只调 `domain` +
`infrastructure` 的接口；`simulation` 依赖 `domain` 但**不依赖**
`infrastructure` 的写仓储。

#### 11.1.1 Phase 0 落地情况（2026-10-26）

上面这棵树是**目标**。Phase 0 立柱之后实际长这样（✅ = 已建，⬜ = 后续阶段）：

```
src/
  domain/                      ✅ 纯领域层 —— 零 I/O、零 pandas、零 akshare
    shared/
      provenance.py             ✅ REAL | SIMULATED_GRID
      money.py                  ✅ Phase 1 —— Money / ShareCount / Price /
                                Weight / DeviationPP / LayerSize
      identifiers.py            ✅ Phase 1 —— AssetCode / TradingDate
    ledger/
      ledger.py                 ✅ 写侧 —— apply_trade（追加一笔 + 校验不变量）
      trade_side.py             ✅ 8 种 side
      projection.py             ✅ Phase 7 —— 读侧 —— LedgerProjection /
                                project_ledger（重放账本 → 持仓 + 现金池）
    portfolio/
      portfolio.py              ✅ Phase 4 —— Portfolio(根) / Position / PositionLot
                                （Provenance 落地，市值/权重由聚合推出来）
      rebalance_policy.py       ✅ Phase 4 —— RebalancePolicy / RebalanceOutcome /
                                TargetWeightPolicy（阈值是实例状态）
      valuation.py              ✅ value_positions / value_extra_codes（价格来源注入）
      asset_universe.py         ✅ Phase 4 —— AssetUniverse（注册表）/ UNIVERSE
                                + ETF_DEFS / ASSET_NAMES / NAME_TO_CODE / QDII_CODES
      cash_pools.py             ✅ D4 —— CashPoolBreakdown：资金管理表的「合计」
                                口径写在这里（合计市值 + 合计余额 = badge 总市值）
    grid/
      grid_position.py          ✅ 网格聚合的领域半（stdlib-only！）
      grid_op.py                ✅ Phase 2b —— GridOp / ExecutedOp / SimulatedOp
      trigger_policy.py         ✅ Phase 2b —— TriggerPolicy / TriggerLevel /
                                TriggerDistance（Phase 3 生产+回测都已接上）
      exit_policy.py            ✅ Phase 2 —— ExitAtCost / cost_only_exit
      advisor.py                ✅ Phase 2b —— GridAdvisor → GridSuggestion
      layer_sizing.py           ✅ Phase 3 —— LayerSizingPolicy
      state_machine.py          ✅ Phase 6 —— 合法边表 (LEGAL_TRANSITIONS)：
                                「一个周期 N 买 1 卖」的形状
      config.py                 ✅ Phase 5a —— GridConfig（七个网格参数 + from_env，
                                领域层读环境变量的**唯一**一处）
      price_window.py           ✅ D1 —— PriceWindow / WindowBar：120 日滚动窗口
                                **带日期**（同一天重复运行是覆盖，不是追加）
    events/
      domain_events.py          ✅ Phase 5b —— 九个领域事件 + EventStream
  application/                 ✅ 用例编排
    config.py                   ✅ Phase 5a —— AppConfig（threshold_pp + grid），
                                环境的知识属于**边缘**
    daily_snapshot.py           ✅ ← record.py:main()（Phase 5c-1: 拆成 12 个具名
                                用例 + SnapshotRun 上下文）
    apply_trade.py              ✅ Phase 5c-2 —— 账本写路径的用例
                                （校验 → 发 id → 重放 → 写盘）
    apply_grid_op.py            ✅ Phase 5c-2 —— 复合网格写路径的用例
                                （校验 → 应用 → 写盘）
  infrastructure/              ✅ 适配器
    paths.py                    ✅ D13 —— 仓库路径的**唯一真源**（Phase 0 搬家时
                                忘了改 `parent.parent` ⇒ 邮件静默不发，见 §11.1.15）
    json_repositories.py        ✅ 4 个 JSON 文件的读写（原子写）+ 写路径的
                                严格读 (read_trades / read_grid_state)
    quote_providers.py          ✅ sina/eastmoney/tencent/akshare 防腐层
    chart_renderer.py           ✅ ← chart.py
    github_actions/             ✅ D12 —— GitHub Actions 防腐层
      context.py                ✅ RunContext —— 这次 run 是谁
      outputs.py                ✅ $GITHUB_OUTPUT —— step outputs
      annotations.py            ✅ ::error:: / ::warning:: / ::notice::
      summary.py                ✅ $GITHUB_STEP_SUMMARY —— run summary 面板
  simulation/                  ✅ BC-E 独立上下文
    grid_simulator.py           ✅ ← run_compound_grid（依赖 domain，不碰写仓储）
    equal_weight_backtest.py    ✅ ← three_pp.py
  presentation/
    digest.py                   ✅ ← notify.py（Phase 5d: 真实持仓 / 归并 分栏披露）
    notify_failure.py           ✅
```

**与目标树的三处有意偏差**：

1. **网格拆成两半**。`grid_position.py`（领域：状态、触发、成本出场、建议）
   与 `grid_simulator.py`（回测：历史重放）**跨限界上下文**，所以不放在同一个
   目录。⚠️ `grid_position.py` **必须保持 stdlib-only** ——
   `scripts/apply_grid.py` 在一个**不装 pandas/akshare** 的 GitHub Actions
   job 里 import 它；`import pandas` 留在 `run_compound_grid` 内部。
2. **`AssetUniverse` 落在 `domain/portfolio/`** 而不是单列一个文件 ——
   它就是「组合里有哪些资产」的定义，Phase 4 已顺手完成。
3. **`valuation.py` 的价格来源是注入的**（`PriceProvider` 协议），领域层
   因此不需要知道 sina/eastmoney 的存在。原来的 `record.py` 在估值循环里
   直接调 `latest()`，领域逻辑和 I/O 焊死了。

**已知的口径保留**：Phase 0 **只搬不改行为**。`chart_renderer.py` 里
`holdings, _ = read_holdings()` 的解包 bug（见 D11）被**原样保留** ——
修它会改变图表输出，属于另一个提交的事。
（→ D11 已于 2026-10-28 修掉，见 §11.1.11。）

#### 11.1.2 Phase 1 落地情况（2026-10-26）

Phase 1 分两次提交落地：

| 提交 | 内容 |
|---|---|
| `687b828` | `src/domain/shared/identifiers.py` —— `AssetCode` / `TradingDate`；`apply_grid.py` 与 `apply_holdings.py` 各自那份 `CODE_RE` / `DATE_RE` 删除 |
| 本次 | `src/domain/shared/money.py` —— `Money` / `ShareCount` / `Price` / `Weight` / `DeviationPP` / `LayerSize` |

**⚠️ 一处有意偏差：值对象用 `float` 子类，不用 `Decimal`。**
§6.2 建议用 `Decimal`，落地时改成了 `float` 子类，理由不是偏好，是两条硬约束：

1. **`data/*.json` 由 `app.js` 读**。`Decimal` 不是 JSON 原生类型 ——
   `json.dumps` 要么抛错，要么靠 `default=str` 变成字符串 `"1.5"`，
   页面拿到字符串就没法算数了。
2. **改算术会改数字**。`Decimal` 的加减乘除自带精度规则；一旦值对象参与
   运算，舍入的**时机**就变了 —— 今天的 `round(x, 2)` 是**算完之后**做的，
   换成 `Decimal` 会变成"每一步都舍入"，账上的数字就全变了。

`float` 子类两头都躲开：**构造时按语义取整**（"份额就是 2 位小数"这条规则
只写一遍，不再散落成裸 `round(x, 2)`），但**不重写算术**。于是
`isinstance(x, float)` 依然成立、`json.dumps` 出来还是数字、
`x * y` 与改动前**逐位相同**。

**舍入点纪律**（这是本次最重要的约束）：**值对象的构造 = 原来那一次
`round()`**。在没有 `round()` 的地方构造值对象，就是在偷偷改数字。
据此有三个地方**刻意不包**：

| 地方 | 为什么不包 |
|---|---|
| `rebalance_policy.evaluate` 内部算触发判定的权重 | 取整到 4 位会**移动触发边界** —— 让「差 0.0001pp 不触发」变成「触发」。`Weight` 只在写进 `history.json` 时构造 |
| `cost_only_exit` 的中间量（卖出份额 / 归并份额 / 盈亏） | 它们是**比例**。四舍五入会破坏「买入 − 卖出 == 归并」这个恒等式，也会让 `cum_realized` 的数字变 |
| `layer_yuan()` 返回的层额 | 层额要除以价格算份额，提前取整会改掉份额。`LayerSize` 只在**输出边界**构造 |

**逐位验证**：改动前后跑同一段**确定性**（不联网，价格写死）流程 ——
账本重放 → 估值 → 归并 → 3pp → 网格推进 → `build_record` → `advisory` →
`asset_snapshot` → `cost_only_exit` —— 输出的 JSON **逐字节相同**。
107 个测试全绿。

**⚠️ 尚未替换的裸 float**（有意为之，Phase 2+ 再处理）：
`asset_snapshot` 的 `return_pct`（收益率 %，不是百分点偏离）、
`quote_providers` 的 `premium_pct`、`build_record` 的 `target_w`
（`1/n` 不取整，页面按它算目标市值）、`rebalance_policy.target_weight`。

#### 11.1.3 Phase 2a 落地情况（2026-10-27）

Phase 2 分两次提交落地，**这一节记的是 2a（聚合 + mapper + 改名）**：

| 提交 | 内容 |
|---|---|
| 2a | `src/domain/grid/exit_policy.py` + `GridPosition` 聚合根 + 显式 mapper |
| 2b | `src/domain/grid/grid_op.py`（`GridOp` / `ExecutedOp` / `SimulatedOp`）、`src/domain/grid/advisor.py`（`GridSuggestion`）、`src/domain/grid/trigger_policy.py`（`TriggerPolicy`）—— 详见 §11.1.4 |

##### (a) 出场规则独立成模块 + 改名

`src/domain/grid/exit_policy.py`（stdlib-only）：

```python
@dataclass(frozen=True)
class ExitAtCost:          # 一次「只卖成本额」出场的**结果**
    shares_sold: float
    proceeds: float
    merged_shares: float
    pnl: float

def cost_only_exit(total_shares, total_invested, price) -> ExitAtCost: ...
```

- 术语债（§4-BC-C 那条）**结清**：规则本体现在叫 `cost_only_exit`，
  返回值叫 `ExitAtCost`。
- `split_take_profit` 保留为**兼容别名**（返回旧版的 dict 形状），
  `grid_position.py` / `scripts/apply_grid.py` 的既有 import 不断。
- ⚠️ **中间量保持全精度**，不套 `ShareCount` / `Money` —— 理由同 §11.1.2：
  它们是**比例**，取整会破坏「买入 − 卖出 == 归并」这个恒等式。
  值对象用在**输出边界**，不是用在这条链路上。

##### (b) `GridPosition` 聚合根 + 严格 mapper

`src/domain/grid/grid_position.py` 里原来的 6 key 字面量：

```python
new_state = {
    "in_cycle": in_cycle, "entries": entries, "last_prices": last_prices,
    "cum_realized": cum_realized, "ops": ops, "merged_shares": merged_shares,
}
```

现在换成 dataclass + 显式 mapper：

```python
@dataclass
class GridPosition:
    in_cycle: bool = False
    entries: list[dict] = field(default_factory=list)
    last_prices: list[float] = field(default_factory=list)
    cum_realized: float = 0.0
    ops: list[dict] = field(default_factory=list)
    merged_shares: float = 0.0

    def to_state(self) -> dict: ...      # 写盘
    @classmethod
    def from_state(cls, state) -> "GridPosition": ...   # 读盘
```

**这解决的是 I11（§10）**。此前的危害是**具体的、已经发生过**的：
`advance_one_day` 和 `manual_op` 各自用**自己的** 6 key 字面量重建 state，
新增一个字段时谁都没改到 → 那个字段**被静默丢弃**。2026-10-23
`free_shares` 改名为 `merged_shares` 就踩过一次。

现在两头都堵死：

| 方向 | 守卫 |
|---|---|
| **写**（`to_state`） | 产出的 key 集合 vs dataclass 声明的字段集合，**不等就抛错** —— 少写一个 key 立刻炸，不会等到线上 |
| **读**（`from_state`） | 出现**未知 key** 就抛错（`last_120d_min` 是唯一豁免的 legacy 键）—— 旧 state 读不进来立刻炸 |

**`data/grid_state.json` 的 JSON 格式完全不变**：每个资产还是那 6 个 key，
值形状也没动。mapper 负责读写，**外面看不见它**。

##### (c) 验证

| 检查 | 结果 |
|---|---|
| 逐字节回归（确定性流程 dump 前后对比） | **逐字节相同** |
| `python -X utf8 -m tests.test_record` | **115/115 通过**（新增 8 个 mapper / 出场策略测试） |
| `scripts/apply_grid.py` 真实 payload 冒烟 | rc=0，state 正确 |
| `import src.domain.grid.grid_position` 拉起 pandas / akshare？ | **没有**（stdlib-only 约束仍成立） |

##### (d) 一处**有意**的行为差异

`from_state` 会用 legacy 的 `last_120d_min` 兜底给**所有** mutator 填
`last_prices`；改动前只有 `advance_one_day` 这么做，`manual_op` 不做。

实际是 no-op：线上 `data/grid_state.json` 里**没有** `last_120d_min`，
而 `scripts/apply_grid.py` 在调 `manual_op` **之前**本来就自己补了这一步。
两条路径的最终 state 一致。

##### (e) Phase 2b 待做

- ~~`grid_op.py`：`GridOp`（基类）/ `ExecutedOp`（`source="manual"`）/
  `SimulatedOp`（`source="backtest"`）—— 把「建议 ≠ 成交」写成类型（§6.3）。~~
  ✅ 见 §11.1.4
- ~~`advisor.py`：`GridSuggestion` —— 把 `advisory()` 现在返回的裸 dict 变成
  具名值对象，`signal: str` 变成 `level: TriggerLevel | None`。~~ ✅ 见 §11.1.4

> ⚠️ 本文档 §4 / §7 里的 `grid.py:NNN` 行号引用指的是 **Phase 0 之前**的文件布局；
> 该文件现在是 `src/domain/grid/grid_position.py`。

#### 11.1.4 Phase 2b 落地情况（2026-10-27）

Phase 2b 把「**建议**」和「**成交**」从两个裸 dict 变成**两个不同的类型**，
并顺手把触发规则提成独立的 `TriggerPolicy`（§7 点名的最严重那处重复的第一步）。

新增三个模块，全部 **stdlib-only**（`scripts/apply_grid.py` 在不装
pandas/akshare 的 Actions job 里 import 这条链路）：

```
domain/grid/trigger_policy.py   TriggerLevel / TriggerDistance /
                                 TriggerLevels / TriggerPolicy
domain/grid/grid_op.py           OpKind / OpSource / GridOp /
                                 ExecutedOp / SimulatedOp
domain/grid/advisor.py           GridSuggestion / GridAdvisor
```

依赖方向是单向的：`trigger_policy`（叶子）→ `grid_op` → `advisor` →
`grid_position`。`advisor` 收的是**裸参数**（`in_cycle` / `entries` /
`window` / `price`），所以它不需要 import `grid_position`，也就没有环。

##### (a) `TriggerPolicy` —— 触发规则的**唯一**定义

`trigger_policy.py` 的 `TriggerPolicy.levels(...)` 是老 `_triggers` 的**逐字移植**：
同样的分支顺序、同样的 `min(cands, key=abs)` 平手取 `add`、同样的 `or 0.0` 兜底。

生产路径的 `_triggers` 现在只是个**薄适配器**（返回 `TriggerLevels.to_dict()`，
形状与老版本一字不差），`advance_one_day` 直接调 `policy.levels(...)`。

⚠️ **`TriggerDistance.pct` / `.price` 保持原始 float**，不套 `DeviationPP` /
`Price` —— 取整发生在**输出边界**（`GridSuggestion` 构造时）。这跟 §11.1.2 的
舍入点纪律是同一条：在没有 `round()` 的地方构造值对象，就是在偷偷改数字。

##### (b) `GridOp` —— 「已经发生」的那一条

| 类型 | `source` | 含义 |
|---|---|---|
| `ExecutedOp` | `"manual"` | **实盘成交** —— 用户在页面提交的 op。**唯一的实盘来源** |
| `SimulatedOp` | `"backtest"` | **模拟成交** —— 历史重放产物。**不是**成交 |
| `GridOp` | 其它 | 遗留的 `"auto"`（自动交易时代引擎自己下的单）落在这里，**不猜** |

`ExecutedOp` / `SimulatedOp` 在 `__post_init__` 里**校验** `source`：
`ExecutedOp(source="backtest")` 直接抛 `ValueError`。这条不变量
（**没有任何实盘 op 来自引擎**）因此由类型保证。

⚠️ **`data/grid_state.json` 的 JSON 格式不变**：`GridOp.to_dict()` 的 key 顺序
与老版本一字不差（`date/op/source/price/shares/note`，tp 额外四个在后面），
值也**不取整** —— 这些数字原样进出文件，套值对象会改掉账。

##### (c) `GridSuggestion` —— 「应该做什么」

`advisory()` 现在返回 `GridSuggestion`（具名值对象）而不是裸 dict：

| 字段 | 老 dict 里的对应 |
|---|---|
| `level: TriggerLevel \| None` | `signal` |
| `nearest: TriggerDistance` | `trigger_kind` + `trigger_pct` + `trigger_price` |
| `open_distance` / `add_distance` / `tp_distance` | `dist_open_pct` / `dist_add_pct` / `dist_tp_pct` |
| `suggest_price` / `suggest_shares` / `suggest_amount` | 同名 |

`to_dict()` 输出的形状与老 `advisory()` **一字不差**，`history.json` 里
`grid_signals` 的形状、`app.js` 读的字段名都不用动。

##### (d) 两个过渡期入口

`GridOp` 和 `GridSuggestion` 都实现了 `__getitem__` / `__contains__` ——
既有的调用方（`daily_snapshot.py`、`app.js` 的数据来源、`scripts/` 的对账脚本、
测试）都按 `o["op"]` 读 dict，保持这个入口它们就不必跟着改。

##### (e) 一个必须避开的坑：`str, Enum` 的格式化

`TriggerLevel` 是 `str` 的子类，所以 `TriggerLevel.OPEN == "open"` 成立、
`json.dumps` 直接输出 `"open"`。**但** `Enum` 覆盖了 `str.__str__` ——
`f"{TriggerLevel.OPEN}"` 会显示成 `TriggerLevel.OPEN` 而不是 `open`。

`daily_snapshot.py` 里有一处 `距{trigger_kind}` 的 f-string。踩中就是
**页面显示「距TriggerLevel.OPEN」**。三道防线：

1. 两个 enum 都显式定义了 `__str__` 返回 `self.value`；
2. 所有 JSON / 格式化边界一律用 `.value`；
3. `GridSuggestion.__getitem__` 返回 `to_dict()[key]`（**裸字符串**），
   所以 `daily_snapshot.py` 一行都不用改。

##### (f) 验证

| 检查 | 结果 |
|---|---|
| 逐字节回归（确定性流程 dump 前后对比：state 往返 / `advance_one_day` × 20 天 × 4 资产 / `advisory` / `manual_op` open→add→tp / `ops_from_cycles` / `merge_backtest_ops`） | **逐字节相同** |
| `python -X utf8 -m tests.test_record` | **131/131 通过**（新增 16 个 TriggerPolicy / GridOp / GridSuggestion 测试） |
| `data/grid_state.json` 往返（4 个资产） | 每个都 `to_state() == 原 dict`，6 个 key 不变 |
| `scripts/apply_grid.py` 真实 payload 冒烟 | import 链正常；非法 op 正确报错并退出 rc=1（`::error::` annotation 在） |
| `import src.domain.grid.{trigger_policy,grid_op,advisor,grid_position}` 拉起 pandas / akshare？ | **没有**（stdlib-only 约束仍成立） |

##### (g) Phase 3 的后续

`TriggerPolicy` 已就位，**回测路径也接上了**（见 §11.1.5）。

#### 11.1.5 Phase 3 落地情况（2026-10-27）

Phase 3 = **触发规则去重**。两件事：

| # | 做了什么 | 文件 |
|---|---|---|
| (a) | 回测路径接上 `TriggerPolicy` —— `run_compound_grid` 不再有内联的开仓/加仓/回本判断 | `src/simulation/grid_simulator.py` |
| (b) | 层额规则提成显式策略类 —— 依赖模块级常量的三行变成可注入的纯函数 | `src/domain/grid/layer_sizing.py`（新） |

##### (a) 回测接上 `TriggerPolicy`

```python
# src/simulation/grid_simulator.py —— 以前是内联的 30 行 if/elif
policy = TriggerPolicy(step_pct=step_pct, tp_pct=tp_pct, tp_basis=tp_basis)

for i, (date, price) in enumerate(prices_sorted.items()):
    past = prices_sorted.iloc[:i] if trigger_mode == "global" \
        else prices_sorted.iloc[max(0, i - lookback):i]
    window = past.dropna().tolist()          # ← 为了跟 pandas 的 nanmin 逐位一致
    levels = policy.levels(in_cycle=in_cycle, entries=entries,
                            window=window, price=float(price))
    signal = levels.signal
    if signal is TriggerLevel.OPEN:  ...
    elif signal is TriggerLevel.ADD:  ...
    elif signal is TriggerLevel.TP:   ...
```

⚠️ **三个必须原样保留的细节**，否则不是「逐位不变」而是「差一点点」：

1. **`.dropna()`** —— pandas 的 `.min()` 是 `nanmin`（跳过 NaN），而 Python 的
   `min(list)` 遇到 NaN 会给出不一样的结果。回测路径必须用 `dropna()`。
2. **触发位看的是「今天之前」的收盘价** —— `past = prices_sorted.iloc[:i]`
   （`global` 模式）或 `iloc[max(0, i-lookback):i]`（`window` 模式）。
   今天的价不能自己当自己的门槛。
3. **tp 在 add 之前判定** —— `TriggerPolicy.levels` 里 tp 的分支在 add 之前。
   金字塔的买价严格递减（`entries[-1].price ≤ entries[0].price`，
   `avg_cost ≥ entries[-1].price`），所以「`price ≤ entries[-1].price × 0.97`」
   与「`price ≥ tp_target`」**不可能同时成立** —— tp 优先与旧的「先 add 后 tp」
   等价，不会改变任何一根 K 线的结果。

**驱动价/层额仍然不同，这是对的**：回测用**收盘价** + 固定 ¥10k/层，
生产用 **K 线最低价** + 浮动层额 —— 这是**场景差异**，不是规则的两套实现。
§11.3 明确「统一回测与实盘口径」是**不做**的，正确做法是隔离。

##### (b) `LayerSizingPolicy` —— 层额的唯一实现

```python
# src/domain/grid/layer_sizing.py
@dataclass(frozen=True)
class LayerSizingPolicy:
    base_yuan: float = GRID_BASE_YUAN              # 默认 ¥10,000
    floating_fraction: float = GRID_FLOATING_FRACTION   # 默认 0.05

    def layer_yuan(self, pool_value: float, recent_min: float) -> float:
        floating = max(pool_value - recent_min, 0.0)
        return self.base_yuan + floating * self.floating_fraction
```

以前这段逻辑是 `grid_position.layer_yuan()` 里的三行，依赖模块级常量 ——
没法做多策略并存，测试也只能改环境变量。现在参数是显式的。

⚠️ **返回未取整的 float** —— 层额要除以价格算份额（`amount / price`），
提前取整会改掉份额。它只在**输出边界**上被包成 `LayerSize`
（`GridSuggestion.suggest_amount` 与快照里的 `layer_yuan`）。这跟 §11.1.2 的
舍入点纪律是同一条。

⚠️ **回测口径不同**：`grid_simulator` 每层**固定** ¥10,000（或由调用方给回调）
—— 那是**另一种场景**的口径，不是本规则的重复实现（§9.2 ① 列的就是这条
防腐层）。

⚠️ **常量在调用时读取**：`layer_yuan()` 便捷入口每次调用都重新读模块级常量
（不是在 import 时冻结），改了 `GRID_BASE_YUAN` 立即生效 —— 与以前模块级函数
的行为一致。

##### (c) 验证

| 检查 | 结果 |
|---|---|
| 逐字节回归（确定性流程 dump 前后对比：state 往返 / `advance_one_day` × 20 天 × 4 资产 / `advisory` / `manual_op` open→add→tp / `ops_from_cycles` / `merge_backtest_ops` / `layer_yuan`） | **逐字节相同**（sha256 一致） |
| 60 行 `backtest` op 重算（4 个资产 × 全历史） | **完全一致** —— 可转债 13 / 红利 20 / 纳指 18 / 黄金 9，全部 `same=True` |
| 模拟器**完整输出**重算（cycles + history，4 组场景 = trigger_mode {window, global} × tp_basis {first_buy, avg_cost}） | **逐字节相同** |
| `scripts/compare_grid_exit_mode.py --selftest`（独立重实现 vs 生产逻辑） | ✓ 重实现与生产逻辑一致（4 个资产 5 项指标全 ✓） |
| `python -X utf8 -m tests.test_record` | **136/136 通过**（新增 5 个 Phase 3 测试） |
| `import src.domain.grid.{trigger_policy,grid_op,advisor,layer_sizing,grid_position}` 拉起 pandas / akshare？ | **没有**（stdlib-only 约束仍成立） |

##### (d) 新增测试（5 个）

| 测试 | 断言 |
|---|---|
| `test_simulator_follows_the_shared_trigger_policy` | 模拟器的信号与 `TriggerPolicy.levels` **逐个 bar** 一致 |
| `test_simulator_add_and_tp_never_fire_on_the_same_bar` | add 与 tp 互斥（等价性前提） |
| `test_simulator_trigger_sequence_matches_policy_level_by_level` | 整段序列与 policy 驱动的期望逐条对齐 |
| `test_simulator_global_trigger_mode_uses_all_history` | `trigger_mode="global"` 用**全历史**最低价，而非 120 日窗口 |
| `test_simulator_has_no_inline_trigger_arithmetic` | **静态**断言 `grid_simulator.py` 源码里没有 `1 - step_pct / 100` / `1 + tp_pct / 100` 这类内联算术，且必须出现 `TriggerPolicy(` —— 防止有人日后把判定逻辑又写回回测路径 |

最后那个测试是这一阶段的**防回归钉子**：光比对行为不够，行为一致但逻辑
重新分叉（比如有人为了「优化」把内联判断抄回去）也该被拦住。

#### 11.1.6 Phase 4 落地情况（2026-10-27）

Phase 4 有**两个半**目标：组合聚合显式化、来源标注、UI 口径债。前两个在
领域层，第三个在 `app.js`。**没有拆成两次提交** —— 因为 UI 的修法取决于
领域层定下来的「引擎的清单从哪来」，拆开反而要做两遍。

##### (a) `Portfolio` 聚合根 —— 市值不再是被抄的字段

Phase 4 之前，`Portfolio` / `Position` / `PositionLot` 三个 dataclass
**定义了但从没人用** —— 应用层一路传的是 position **dict**，市值 (`value`)
是**照抄**记录里的数，份额和市值可以自相矛盾而不报任何错。

现在方向反过来了：**dict 是聚合的输出**。

```python
@dataclass
class Position:
    code: str
    name: str
    price: float = 0.0
    lots: list[PositionLot] = field(default_factory=list)   # ← 份额在这里
    date: str = ""
    premium: Optional[dict] = None

    @property
    def real_shares(self) -> float: ...      # 账本的数，**不取整**
    @property
    def simulated_shares(self) -> float: ... # 网格归并的数
    @property
    def total_shares(self) -> float: ...     # ShareCount(real + simulated)
    @property
    def value(self) -> float: ...            # Money(total_shares × price)
    @property
    def merged_only(self) -> bool: ...
```

三条要点：

1. **市值是推出来的**。`value = total_shares × price`，不再照抄。
   记录里如果出现「份额 1 股、价 100、市值 200」这种自相矛盾的行，
   聚合**拒绝**它（用的是 100），而不是把 200 传下去。
   ⚠️ 这条改动了 `tests/test_record.py` 里一个 fixture ——
   `test_evaluate_with_cash_affects_weights` 原来只改 `value` 不改 `price`，
   现在必须**一起改**，否则测试会红。**测试的意图没变，变的只是夹具要自洽。**
2. **`real_shares` 不取整**。账本已经取整过了，再取一次会改记录。
   `simulated_shares` 取整到 2 位（份额就是 2 位小数）。这正是 §11.1.2 的
   舍入点纪律：**值对象的构造 = 原来那一次 `round()`**。
3. **`total` 的加法顺序保留** `etf + (cr + cg)`。浮点加法不满足结合律，
   旧代码算的就是这个顺序 —— 括号不能动。

##### (b) `Provenance` —— 从「旁挂字段」变成「模型里读得到的事实」

旧代码里「这个持仓有多少是模拟的」是四个**兄弟字段**拼出来的：
`merged_shares` / `total_shares` / `merged_value` / `merged_only`。
它们各自散落在记录里，谁也不知道它们是**同一个事实**的四个投影。

现在：

```python
@dataclass(frozen=True)
class PositionLot:
    shares: float
    cost: float = 0.0
    provenance: Provenance = Provenance.REAL
```

`Position.simulated_shares` 是**求和出来的**，不是抄来的。
「模拟份额推着真实组合调仓」现在是模型里读得到的事实，而不是四个字段的巧合。

⚠️ `PositionLot.cost` 目前**恒为 0 = 未记录**（账本投影不带成本基准），
**不是**「白捡的」。这个字段留到账本投影长出成本基准时才有意义。

##### (c) `RebalancePolicy` / `TargetWeightPolicy` —— 阈值从环境变量变成实例状态

```python
@dataclass(frozen=True)
class RebalancePolicy:
    threshold_pp: float = 3.0
    target: TargetWeightPolicy = field(default_factory=TargetWeightPolicy)

    def evaluate(self, portfolio: Portfolio) -> RebalanceOutcome: ...
```

「我们现在用的是哪个阈值」现在**看一个对象**就知道，不用去翻模块级环境变量。
`TargetWeightPolicy` 单独成类，是因为「每个资产该占多少权重」是**一条独立的
规则** —— 它正是「动态篮子」（3 / 4 / 5 个资产都不用改代码）的定义。

模块级 `evaluate(positions, cash_rebalance, cash_grid, threshold_pp=3.0)`
保留为**兼容包装**（半壁代码和测试都吃这个形状），它**委托**给类 ——
实现只有一份。

应用层那边是 `daily_snapshot.rebalance_policy()` —— 一个**函数**，不是模块级
常量实例。原因很具体：旧的 `evaluate(..., threshold_pp=THRESHOLD_PP)` 是在
**调用时**读模块全局 `THRESHOLD_PP` 的，绑定时机必须保持一致（有人在
import 之后改这个全局，行为也得跟着变）。阈值仍然是**实例状态** ——
想用别的阈值就 `RebalancePolicy(threshold_pp=1.0)`，一个进程里可以并存多套。

##### (d) `AssetUniverse` —— 资产清单从 `holdings.json` 里独立出来

`data/holdings.json` 曾经同时扮演三个角色：交易派生的持仓缓存、持仓的
兜底来源，以及**事实上的「本项目跟踪哪些资产」清单**。第三个角色既不是持仓
也不是缓存 —— 它是**注册表**。

```python
@dataclass(frozen=True)
class AssetUniverse:
    defs: Mapping[str, tuple[str, str, str, bool]]   # code → (sina, eastmoney, name, is_qdii)

    @property
    def names(self) -> list[str]: ...
    @property
    def name_to_code(self) -> dict[str, str]: ...
    @property
    def qdii_codes(self) -> set[str]: ...
```

模块级的 `ETF_DEFS` / `ASSET_NAMES` / `NAME_TO_CODE` / `QDII_CODES`
保留为**同一个单例的别名** —— 既有调用方一行不用改，但它们再也不会各自漂移。

##### (e) UI 口径债 —— `app.js` 里抄的那份清单删掉了

§11.2 给 Phase 4 的收益写着：「引擎换篮子时 UI 不会再跟着说谎（这个 bug
已经发生过一次 —— 引擎 4 资产 vs `app.js` 3 资产）」。

`app.js` 里原来有一份手抄的：

```js
const GRID_CODES_LIST = ["515180", "159937", "511380", "513300"];
```

现在删掉了，两个用它的地方都改成**从记录/状态里推**：

| 位置 | 改前 | 改后 |
|---|---|---|
| 资金管理「复合网格」资产列 | `GRID_CODES_LIST.filter(被持有的名字)` | `latestRec.grid_signals` 里 `current_value > 0` 的行 |
| 网格预览「当前复合网格」 | `Object.entries(ETF_CATALOG)` | `Object.entries(currentGridState)` —— `grid_state.json` 的 key |
| 代码 → 名字的反查 | 无 | 新增 `codeForName(name, latestRec)` |

`ETF_CATALOG` **还留着** —— 它是交易表单下拉框的**候选项**，不是网格的清单。
这两个东西过去混为一谈了。

> ⚠️ `grid_state.json` 与 `history.json` 的 key 顺序都跟 `ETF_CATALOG` 一致
> （红利 / 黄金 / 可转债 / 纳指），所以**渲染结果逐字不变** —— 这次改的是
> 「清单从哪来」，不是「清单长什么样」。

##### (f) 验证

| 检查 | 结果 |
|---|---|
| 逐字节回归（确定性流程 dump 前后对比：归并 → 3pp → 网格推进 20 天 × 4 资产 + `advisory` → `build_record`） | **逐字节相同** —— 改动前后 sha256 均为 `75ede44e6…`（`git stash` 前后各跑一次） |
| `python -X utf8 -m tests.test_record` | **144/144 通过**（新增 8 个 Phase 4 测试） |
| `node --check app.js` | ✓ 语法通过 |
| 页面（chrome-devtools，硬刷新） | 无 JS 报错；网格预览输出与改动前逐字相同：`515180 待开仓 \| 159937 待开仓 \| 511380 待开仓 \| 513300 待开仓`；资金管理「复合网格」资产列 = `— 无持仓 —`（4 个资产 `current_value` 全 0，与改动前一致） |

##### (g) 新增测试（8 个）

| 测试 | 断言 |
|---|---|
| `test_asset_universe_is_a_domain_object` | `AssetUniverse` 是可传的值；`names` / `name_to_code` / `qdii_codes` 都从 `defs` 推出来 |
| `test_position_splits_real_and_simulated_lots` | `PositionLot.provenance` 分得开真实 / 模拟份额 |
| `test_portfolio_merges_grid_shares_into_lots` | 网格归并的份额进的是 `SIMULATED_GRID` 那一批 |
| `test_portfolio_record_roundtrip_is_lossless` | `to_records()` → `from_records()` 往返不丢东西 |
| `test_position_value_is_derived_from_lots_not_copied` | **市值是推出来的**，不是照抄记录里的 `value` |
| `test_rebalance_policy_threshold_is_instance_state` | 阈值是实例状态 —— 两个 policy 可以并存不同阈值 |
| `test_target_weight_policy_is_dynamic` | `1/n` 随篮子大小变 |
| `test_simulated_shares_still_drive_the_weights` | 模拟份额**仍然**参与权重（用户 2026-10-24 的明确决定，语义不变） |

#### 11.1.7 Phase 5 落地情况（2026-10-27）

Phase 5 有三件事：显式配置对象、领域事件、应用层瘦身。分四次提交 ——
5a 配置对象 / 5b 领域事件 / 5c-1 每日快照瘦身 / 5c-2 写路径瘦身。

##### (a) 5a —— 显式配置对象（D8 收口）

**改前**：参数散在三个地方，各读各的环境变量 ——
`src/application/daily_snapshot.py:THRESHOLD_PP`、
`src/domain/grid/trigger_policy.py:GRID_STEP_PCT` 等、
`src/domain/grid/layer_sizing.py:GRID_BASE_YUAN` 等、
`src/simulation/equal_weight_backtest.py:THRESHOLD_PP`。
`grid_position.py` 里还有一份 `GRID_LOOKBACK = 120` 的**硬编码副本**。

**改后**：值**只在一个地方**被写下来。

```
src/domain/grid/config.py          GridConfig (frozen) — 七个网格参数 + from_env
src/application/config.py          AppConfig (frozen) — threshold_pp + grid
```

依赖方向：`config`（叶子）→ `trigger_policy` / `layer_sizing` → `grid_position`。
`layer_sizing` 是**独立叶子**（不依赖 `trigger_policy`），两条线互不牵扯。

```python
@dataclass(frozen=True)
class GridConfig:
    lookback: int = 120
    step_pct: float = 3.0
    tp_pct: float = 3.0
    tp_basis: str = "first_buy"
    base_yuan: float = 10000.0
    floating_fraction: float = 0.05
    floating_window: int = 756
```

三条要点：

1. **领域层不读应用的环境变量**。`domain/grid/config.py` 是纯数据 + 一个
   `from_env` 工厂（读 `os.environ` 的地方**只有这一处**）；转换发生在
   `application/config.py` —— 环境的知识属于**边缘**。
2. **旧名字全部保留**，但它们现在是**配置对象的投影**而不是各自读环境变量：
   `grid_pos.GRID_LOOKBACK`、`grid_trigger_policy.GRID_STEP_PCT`、
   `layer_sizing.GRID_BASE_YUAN` …… 既有 import 一行不用改。
3. **策略可以并存**。`TriggerPolicy.from_config(cfg)` /
   `LayerSizingPolicy.from_config(cfg)` / `advance_all_grids(..., config=cfg)`
   —— 一个进程里「生产用 3%」与「回测用 2%」各走各的。
   回测路径同理：`run_backtest(..., threshold_pp=...)`。

`daily_snapshot.rebalance_policy()` **仍然是函数**（不是模块级常量实例）——
绑定时机必须跟旧代码一致（旧的 `evaluate(..., threshold_pp=THRESHOLD_PP)`
是在**调用时**读模块全局的）。见 §11.1.6 (c)。

**逐字节回归**：确定性流程（固定价格 → 归并 → 3pp → 网格推进 → `build_record`）
在 `git stash` 前后各跑一次，记录 sha256 **完全相同**
（`8cac6576ee367cc4…`，7260 字节）。

> ⚠️ `src/domain/grid/*` 必须保持 **stdlib-only** —— `scripts/apply_grid.py`
> 在**不装** pandas/akshare 的 GitHub Actions job 里 import 这条链路。
> `test_domain_grid_stays_stdlib_only` 静态钉住这条。

##### (b) 5b —— 领域事件

九个事件落在 `src/domain/events/domain_events.py`（**stdlib-only**），
外加一个 `EventStream` 收集器。

**三个设计决定：**

1. **事件是可选的。** 每个会改状态的领域函数都多一个
   `events: EventStream | None = None` 参数 —— 传 `None` 就什么都不发。
   老调用方**一行不用改**，行为逐位不变（已用 sha256 回归证明）。
2. **事件不可变，载荷是具名字段**（不是 dict）。「这个事件带什么」由
   **类型**回答 —— 不用翻文档。`type` 属性就是类名，与 §8 的表一一对应。
3. **唯一承重的事件通路是 `MergedSharesTransferred`。** 组合现在**只**从
   事件吸收归并份额：

   ```python
   transfers = grid_mod.merged_share_transfers(state)   # 网格宣告
   events.extend(transfers)
   merged_before = merged_by_code_from_transfers(transfers)   # 组合吸收
   ```

   载荷里的 `shares` 是**累计**量（不是增量）—— 组合每次重建时吸收的是网格
   **当前**持有的归并份额，所以同一资产多条声明时**最后一条**说了算。
   `test_merged_share_transfers_match_the_grid_state` 钉住「新通路 == 旧读法」。

   这就是 I9（归并份额不写 `trades.json`）的形状：模拟份额进组合的**唯一**
   通路就是这条事件；而账本那边，八个 `TradeSide` 里没有任何一个是「归并」，
   `apply_trade` 遇到未知 side 直接抛错 ——
   `test_ledger_cannot_record_simulated_shares` 钉住这条。

**验证**

| 检查 | 结果 |
|---|---|
| 逐字节回归 | **逐字节相同** —— 改动前后记录 sha256 均为 `8cac6576e…`（7260 字节） |
| `python -X utf8 -m tests.test_record` | **168/168 通过**（新增 11 个 Phase 5b 测试） |
| `src/domain/grid/*` + `src/domain/events/*` 仍 **stdlib-only** | ✓ —— `scripts/apply_grid.py` 在不装 pandas/akshare 的 job 里 import 这条链路 |

##### (c) 5c-1 —— 应用层瘦身（每日快照）

**改前**：`daily_snapshot.main()` 是一条 ~180 行的直线流程 —— 12 个步骤串在
一起，中间穿插着十几个互相传递的局部变量（`positions` / `positions_eff` /
`portfolio` / `merged_before` / `merged_after` / `total_pre` / `total` …）。
读代码的人要数行数才知道「现在这个 `total` 是记录里那个吗」。

**改后**：主流程是一串**具名用例**，共享一个 `SnapshotRun` 上下文：

```
load_universe            读 holdings.json (只取代码清单) + grid_state.json
replay_trade_ledger      重放 trades.json → 真实持仓 + 两个现金池
value_holdings           给每个持仓定价 (价格来源注入)
attach_qdii_premiums     挂上 QDII 溢价 (best-effort)
absorb_merged_shares     网格宣告的归并份额 → 组合吸收
size_grid_layer          算网格层额
advance_grids            推进网格状态 + 生成建议 (**不下单**)
judge_rebalance          评估 3pp —— 记录里那个 total 的来源
persist_snapshot         写 data/history.json
render_snapshot_chart    画 data/grid_chart.png (best-effort)
report_snapshot          打印人看的摘要
emit_step_outputs        写 $GITHUB_OUTPUT
```

`absorb_merged_shares` 出现**两次** —— 推进网格前一次（给层额定尺）、
推进网格后一次（记录里那个组合）。这是旧代码里的两段式求值，现在**有名字**了。

**D9 收口** —— 两个「基准」各有名字：

| 字段 | 是什么 | 谁用它 |
|---|---|---|
| `layer_sizing_basis` | 给网格层额定尺的那次评估 | `size_grid_layer` / `advance_grids` |
| `snapshot_basis` | 记录里 `total` / `weights` / `triggered` 的来源 | `persist_snapshot` / `report_snapshot` |

以前这两段是 `main()` 里相隔几十行的两行代码；现在「哪个 total 是记录里
那个」由**字段名**回答。`test_size_grid_layer_uses_the_layer_sizing_basis_total`
钉住「层额用的是层额定尺那个数」。

**应用层只剩接线**：`main()` 里没有一条业务算术 ——
`test_main_is_pure_wiring` 静态钉住（`abs(` / `round(` / `float(` / `min(` /
`max(` 一个都不许出现）。规则一条都不许回到应用层。

**验证**

| 检查 | 结果 |
|---|---|
| 逐字节回归 | **四项全部逐字节相同** —— 把 `daily_snapshot.main()` 的全部 I/O mock 掉（固定价格 / 固定账本 / 固定网格状态），基线（`git stash`）与重构后各跑一次：stdout `6c5da940…`、stderr `ecc7cb6d…`、写出的 history `1e97beb7…`、写出的 grid_state `a83c6596…` —— sha256 全部一致 |
| `python -X utf8 -m tests.test_record` | **176/176 通过**（新增 8 个 Phase 5c 测试） |
| 应用层只剩接线 | ✓ —— `test_main_is_pure_wiring` 静态钉住 `main()` 里没有业务算术 |

##### (d) 5c-2 —— 写路径的用例层

5c-1 把**每日快照**拆成了具名用例。5c-2 处理另外两条写路径 ——
页面提交的两张表单:

| 路径 | 改前 | 改后 |
|---|---|---|
| 网格操作 | `scripts/apply_grid.py`（303 行，自带校验 + 复制品 + 写盘） | `src/application/apply_grid_op.py`（273 行用例）+ 155 行的 CLI 外壳 |
| 交易账本 | `scripts/apply_holdings.py`（366 行，自带校验 + **重放复制品** + 写盘） | `src/application/apply_trade.py`（315 行用例）+ 144 行的 CLI 外壳 |

**用例的形状**（跟 `daily_snapshot` 一致 —— 一串具名步骤，规则一条都不在脚本里）：

```
apply_grid_op.py                        apply_trade.py
  validate_batch   校验整批 payload        validate_batch   校验整批 payload
  apply_batch      顺序应用这一批          assign_ids       服务端发 id
  apply_grid_ops   校验→读盘→应用→写盘      apply_batch      重放合并后的账本
                                         apply_trades      校验→读盘→重放→写两份
```

**三个真正的去重**：

1. **op 词表**（D10）—— `VALID_OPS` 现在是 `TriggerLevel` 的投影，
   不再是脚本自己抄的 `{"open","add","tp"}`。
2. **side 词表** —— `VALID_SIDES` 现在是 `TradeSide`（八个）的投影。
3. **账本重放** —— `scripts/apply_holdings.py` 里那份 `inline_apply` /
   `inline_rebuild` **删掉了**。它当年抄的注释说「因为 workflow 不装依赖」——
   那条理由早就过期了（`src/domain/ledger/*` 是 stdlib-only）。
   现在写路径与每日快照跑**同一份** `src/domain/ledger/projection.py`。

**一处口径收紧（有意为之）**：不变量 I1「两个现金池都不得为负」在领域层是
**逐笔**检查，旧脚本是**整批结束后**才查一次。逐笔更严，挡住的是这种账本：

```
入金 20000 (10-12)  +  买入 1000×5.0 (10-11)   ← 买入日期早于入金
```

旧脚本放行（整批末尾现金 +13500 ≥ 0），但**每日快照会崩** —— 它走领域层
重放，重放到那笔买入时现金是 −5000，直接抛错。现在写路径在提交时就拒掉。

**验证**

| 检查 | 结果 |
|---|---|
| 逐字节回归 | **17 个场景里 15 个逐字节相同** —— 基线脚本（`git show HEAD:` 抽出来）与重构后各跑一次，比对**退出码 / stdout / stderr / 写出的每一个文件**的 sha256：网格 9 个场景（happy + 8 种拒绝）全部相同；账本 8 个场景（happy / 卖超 / 坏代码 / 老格式 / 六个现金 side / 坏 side）里 6 个相同 |
| 两处**有意**的差异 | ① 现金为负的报错文案：旧 `cash_rebalance would go negative after this batch: current=-81500.00` → 新 `cash would go negative: rebalance=-81500.00, grid=0.00 (after buy)`（领域层的原话，多了「哪一笔」）；② 「买入早于入金」的场景：旧**放行**（exit 0）→ 新**拒绝**（exit 1，不写盘）—— 见上面的「口径收紧」 |
| `python -X utf8 -m tests.test_record` | **183/183 通过**（新增 7 个 Phase 5c-2 测试） |
| 词表只有一份 | ✓ —— `test_write_path_vocabularies_come_from_the_domain` 钉住 `VALID_OPS == TriggerLevel`、`VALID_SIDES == TradeSide` |
| 脚本是薄壳 | ✓ —— `test_the_write_path_scripts_are_thin_shims` 钉住脚本里**没有** `inline_apply` / `inline_rebuild` / `_validate_op` / `_validate_trade` / `_assign_ids` |
| 写路径仍 **stdlib-only** | ✓ —— `test_write_path_modules_stay_stdlib_only`；两个脚本在**不装** pandas/akshare 的 job 里跑 |
| 写路径与读路径同一把尺子 | ✓ —— `test_write_path_and_daily_path_replay_the_ledger_identically` 钉住「写路径重放出的持仓/现金 == 每日快照重放的」 |

**为什么读盘要「严格」**：`json_repositories.load_trades()` 是**宽松**的 ——
文件坏了只 WARN，然后当空账本继续跑。每日快照可以这样（跑砸了只是今天的
记录没出来），**写路径不行**：一个坏掉的 `trades.json` 被当成空账本，重放的
结果就是「持仓全没了」，而用户只是提交了一笔新交易。所以写路径走新的
`read_trades()` / `read_grid_state()` —— 坏了就抛错，绝不静默清空。

#### 11.1.8 Phase 6 落地情况（2026-10-27）

Phase 6 是被 §13 的**最后一条**验收项逼出来的：

> 「一个周期 N 买 1 卖」在哪里保证？→ 答不出「`GridPosition` 状态机的合法边」
> —— 因为合法边当时是 `manual_op` 里两行内联的 `if`。

##### (a) 合法边表 —— `src/domain/grid/state_machine.py`

```
LEGAL_TRANSITIONS = (
    GridTransition(op=OPEN, from_in_cycle=False, to_in_cycle=True,
                   requirement="open requires no open cycle (use tp to close first)"),
    GridTransition(op=ADD,  from_in_cycle=True,  to_in_cycle=True,
                   requirement="add requires an open cycle"),
    GridTransition(op=TP,   from_in_cycle=True,  to_in_cycle=False,
                   requirement="tp requires an open cycle"),
)
```

`manual_op` 现在只做一件事：**查表**。

```python
edge = transition(position.in_cycle, op)   # 非法边 → 抛错 (文案来自表)
...
position.in_cycle = edge.to_in_cycle
```

词表是 `TriggerLevel` 的投影（不另抄一份），报错文案也**跟着边走** ——
「为什么这一步不合法」写在表里，不写在调用处。顺带把「未知 op」的消息收进
`known_op` —— 那句话以前写在 `manual_op` 里，现在只有一处出处。

##### (b) 「N 买 1 卖」现在是**表的形状**，不是一句约定

| 事实 | 由哪条边保证 |
|---|---|
| `add` / `tp` 不可能凭空出现 | 两条边都要求 `from_in_cycle=True`；而 `in_cycle` 只有 `open` 会置真 |
| 一个周期可以买任意多次 | `add` 的边是 Open→Open（自环） |
| 一个周期**只能**卖一次 | 回到 Idle 的边**只有** `tp` 一条，而 `tp` 把 `entries` 清空、`in_cycle` 归零 |

##### (c) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **188/188 通过**（新增 5 个） |
| 网格写路径逐字节回归 | ✓ —— 5 个场景（happy / 已在周期内 open / 无周期 add / 无周期 tp / 非法 op）与 HEAD **逐字节相同**（退出码 / stdout / stderr / 写出的 state） |
| 非法边的报错文案 | ✓ —— 与改前**一字不差**（文案只是从调用处搬到了表里） |
| 合法边只有一份 | ✓ —— `test_the_legal_edges_are_written_only_in_the_state_machine_table` 静态钉住 `manual_op` 里没有内联的状态判断 |
| 状态机仍 stdlib-only | ✓ —— `test_the_state_machine_stays_stdlib_only` |

#### 11.1.15 D12 + D13 落地情况（2026-10-28）

这一节记的是**两件事**，都是 2026-10-28 用户报「今天的邮件没发过来」时挖出来的：

- **D13** —— 仓库根算错一层 ⇒ 邮件**静默**不发（绿的步骤，零封邮件）
- **D12** —— GitHub Actions 的契约散落在三个地方 ⇒ 收进一个适配器

##### (a) D13: 原来错在哪

2026-10-26 的结构重构（Phase 0）把 `src/notify.py` 挪到
`src/presentation/digest.py`、把 `src/chart.py` 挪到
`src/infrastructure/chart_renderer.py` —— 两处都**下了一层**。但文件里的

```python
ROOT = Path(__file__).resolve().parent.parent
```

**没跟着改**：

| 文件 | `parent.parent` 指向 | 对不对 |
|---|---|---|
| `src/notify.py`（重构前） | 仓库根 | ✓ |
| `src/presentation/digest.py`（重构后） | `src/` | ✗ |
| `src/infrastructure/chart_renderer.py` | `src/` | ✗ |

于是 `HISTORY` 指向 `<repo>/src/data/history.json` —— **不存在**。

##### (b) 为什么「邮件没发」是**绿的**

`digest.main()` 原来是：

```python
r = latest_record()
if not r:
    print("WARN no history.json; skipping email", file=sys.stderr)
    return          # ← 退出码 0
```

`latest_record()` 返回 `None` → 打一行 WARN → **正常返回** → 退出码 0 →
workflow 那一格是 ✅ → Actions 界面上「Send email digest」全是绿的，
**一封邮件都没发出去**。

从 2026-10-26 到 2026-10-28，**三天，一封邮件都没有**。

`chart_renderer` 是同一个错的另一半：`_load_grid_state_safe()` 读不到
`grid_state.json` → 图表**少画了网格仓位**。

##### (c) 改法：路径只有一个家

新增 `src/infrastructure/paths.py`（stdlib-only）：

```python
ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
HISTORY_FILE = DATA_DIR / "history.json"
GRID_STATE_FILE = DATA_DIR / "grid_state.json"
HOLDINGS_FILE = DATA_DIR / "holdings.json"
TRADES_FILE = DATA_DIR / "trades.json"
GRID_CHART_FILE = DATA_DIR / "grid_chart.png"
```

`json_repositories` / `chart_renderer` / `digest` 全部从这里取。

**并且把「不发邮件」改成「发不出就报错」** —— `digest.main()` 现在在没有
可用记录时**抛错**（退出码 1 ⇒ 那一步红）。同样，`send()` 现在检查
`sendmail()` 的返回值：**部分**收件人被拒时 Python **不抛异常**，只是把
被拒的人放进返回值里 —— 以前那个返回值被丢掉了。

再叠两层：SMTP 瞬时故障重试 3 次（5s / 10s 退避），以及 run summary 面板
留痕（不用翻日志就知道邮件发没发、是哪天的记录）。

##### (d) D12: GitHub Actions 契约散在三个地方

| 痕迹 | 原先住在 |
|---|---|
| `$GITHUB_OUTPUT`（step outputs） | `src/application/daily_snapshot.py:write_github_output` |
| `::error::` annotation | `scripts/apply_grid.py:fail` |
| `GITHUB_RUN_ID` / `GITHUB_REPOSITORY` / `GITHUB_SERVER_URL` / `GITHUB_WORKSPACE` | `src/presentation/notify_failure.py` |

应用层（用例编排）不该知道 workflow 的环境变量名；呈现层也不该。

新增 `src/infrastructure/github_actions/` —— GitHub Actions 的**防腐层**：

```
src/infrastructure/github_actions/
    __init__.py     # 对外的全部词汇
    context.py      # RunContext —— 这次 run 是谁（仓库 / run id / 工作区 / API 根）
    outputs.py      # $GITHUB_OUTPUT —— step outputs 的唯一写入口
    annotations.py  # ::error:: / ::warning:: / ::notice::（带转义）
    summary.py      # $GITHUB_STEP_SUMMARY —— run summary 面板
```

`daily_snapshot` 只说「我要发布这三个输出」，`notify_failure` 只问
「这次 run 的链接是什么」，脚本只调 `annotate_error(msg)`。
step output 的**名字**（`date` / `total` / `trigger`）也住在这里 —— 它是
workflow 的契约，不该散在调用方。

顺带：两个 `apply-*.yml` 里**各抄了一份**同样的 `curl .../dispatches`
（而且 `curl -fsS` 在 401 上是**静默**的 —— 令牌没注入时看到的是
「没反应」而不是「401」）。现在收进 `scripts/trigger_daily.py`，
一处实现，失败时抛错 + 打 `::error::` annotation。

##### (e) 顺带修的 workflow 顺序

`daily.yml` 的「Commit data files」原来是 `if: success()`，而邮件步骤
排在它**前面** —— 于是**发邮件失败会顺带把当天的快照扣住**。

现在：

```yaml
- name: Commit data files
  if: steps.record.outcome == 'success'     # 只看快照那一步
...
- name: Send email digest                    # 邮件在提交**之后**
  if: always()
```

邮件出问题不再丢数据；而邮件发出去时，快照已经推上去了（页脚的
「在线查看」链接才不是空的 —— `PAGES_URL` 之前**根本没传**）。

##### (f) 验证

- `test_the_repo_root_has_one_home` —— `src/` 下只有 `paths.py` 算仓库根
- `test_the_repo_root_points_at_the_real_data_directory` / `test_the_data_repositories_use_the_same_paths`
- `test_the_digest_reads_the_real_history_file`
- `test_the_digest_never_silently_skips_a_missing_record`
- `test_the_digest_reports_refused_recipients` / `test_the_digest_refuses_to_send_without_a_recipient`
- `test_the_actions_adapter_owns_the_github_contract` —— `GITHUB_*` 的字面量只在适配器里
- `test_the_actions_adapter_is_stdlib_only`
- `test_the_step_outputs_are_published_to_the_output_file`
- `test_the_run_context_knows_where_it_is` / `test_the_annotations_are_escaped`
- `test_the_daily_workflow_commits_the_snapshot_even_if_the_email_fails`
- `test_the_email_step_knows_where_the_page_lives`
- `test_the_apply_workflows_share_one_dispatch_script` / `test_the_dispatch_script_refuses_to_run_without_a_token`

### 11.2 分阶段（每阶段都可独立交付、独立验证）

#### Phase 0 — 立柱（不改行为，1 次提交）

把 `src/` 下的现有模块**原样搬进**上面的目录结构，只改 import 路径。
目标：让 IDE 能一眼看出"哪段是领域、哪段是编排、哪段是适配器"。

- 验收：107 个测试全绿；`python -X utf8 -m tests.test_record` 输出不变
- 收益：后面每一步都有清晰的落点

#### Phase 1 — 值对象（2~3 次提交）✅ 已落地

在 `domain/shared/` 引入 `Money` / `ShareCount` / `Price` / `Weight` /
`TradingDate` / `AssetCode`，逐个替换裸 float / str。

- 优先做 `AssetCode` + `TradingDate`（消除 `apply_grid.py:69-70` 的正则散落）
- 再做 `Money` / `ShareCount`（用 `Decimal`，消灭份额/市值不自洽的 bug）
- 风险：影响面最大的一步。**每替换一个值对象就跑一次全量测试**
- 收益：一旦完成，§10 里所有"round 到 2 位小数"的手工代码都可以删掉

#### Phase 2 — 网格聚合显式化（2 次提交）✅ 已落地

- 把 6 key 字面量换成 `GridPosition` dataclass + 显式 mapper ✅ **2a**
- `take_profit` 系列改名 `ExitAtCost` / `cost_only_exit` ✅ **2a**
- `GridSuggestion` 与 `GridOp` 分型 ✅ **2b**
- 保留 `data/grid_state.json` 的 **JSON 格式不变**（mapper 负责读写） ✅ **2a / 2b**

- 收益：I11 的静默丢数据风险消失；"引擎不下单"由类型保证

#### Phase 3 — 触发规则去重（1~2 次提交）✅ 已落地

`TriggerPolicy` 已在 **2b** 提成独立的类（`domain/grid/trigger_policy.py`），
生产路径 `_triggers` 已改成它的薄适配器。剩下的是把 **`run_compound_grid`
内联的触发判断换成调用同一个 `TriggerPolicy`**。

**行为必须逐位不变**（回测历史和 ops 的 60 行会重算，需比对）。

- 注意：`ops` 里已有的 60 行 `backtest` op 是用旧逻辑算的。若新逻辑结果不同，
  要么保持旧逻辑（加测试锁定），要么重跑 `backfill_grid_ops.py` 并在文档里记一笔

- ✅ **已完成**（2026-10-27）：60 行 `backtest` op 重算**完全一致**（无需重跑
  `backfill_grid_ops.py`）；模拟器完整输出（cycles + history，4 组场景）
  **逐字节相同**。详见 §11.1.5。

#### Phase 4 — 组合聚合与来源标注（2 次提交）✅ 已落地

- `Position` 持有 `PositionLot[]`，带 `Provenance`
- `RebalancePolicy` 提为独立服务，阈值改为显式参数
- `AssetUniverse` 从 `holdings.json` 里独立出来

- 收益：§4-BC-B 的边界泄漏显形；引擎换篮子时 UI 不会再跟着说谎
  （这个 bug 已经发生过一次 —— 引擎 4 资产 vs `app.js` 3 资产）

- ✅ **已完成**（2026-10-27）：`Portfolio` 聚合根接上生产路径（市值/权重由它
  算，记录是它的**输出**）；`Provenance` 落到 `PositionLot` 上；
  `RebalancePolicy` / `TargetWeightPolicy` 成类；`AssetUniverse` 独立成注册表；
  `app.js` 里手抄的 `GRID_CODES_LIST` 删除，两个用到的地方改成从
  `grid_signals` / `currentGridState` 推。详见 §11.1.6。

#### Phase 5 — 事件化 + 应用层瘦身（2~3 次提交）

- 引入 9 个领域事件，状态变更改为"事件驱动"
- `record.py:main()`（848 行）拆成 `application/daily_snapshot.py` 的一串用例
- `record.py` 的模块级 `THRESHOLD_PP` / `GRID_*` 环境变量改为显式配置对象

- 收益：可测试性大幅提升（可以用事件序列断言，而不是断言 dict 的形状）

- ✅ **5a 已落地**（2026-10-27）：配置对象收口 —— `GridConfig` / `AppConfig`，
  模块级别名改为投影，`from_config` / `config=` / `threshold_pp=` 让多套策略
  并存。详见 §11.1.7 (a)。
- ✅ **5b 已落地**（2026-10-27）：九个领域事件 + `EventStream`；
  `MergedSharesTransferred` 成为组合吸收归并份额的**唯一**通路（I9 落地）。
  详见 §11.1.7 (b)。
- ✅ **5c-1 已落地**（2026-10-27）：`daily_snapshot.main()` 拆成 12 个具名用例 +
  `SnapshotRun` 上下文；两个评估基准各有名字（`layer_sizing_basis` /
  `snapshot_basis`，D9 收口）；应用层只剩接线。详见 §11.1.7 (c)。
- ✅ **5c-2 已落地**（2026-10-27）：两条**写路径**也搬进应用层 ——
  `apply_grid_op.py` / `apply_trade.py`；两个脚本瘦身成 CLI 外壳；
  op / side 词表改为领域枚举的投影（D10 收口）；账本重放**只剩一份实现**
  （写路径与每日快照共用 `src/domain/ledger/projection.py`）。详见 §11.1.7 (d)。

#### Phase 5d — D6 披露侧（1 次提交）✅ 已落地

§12 的 D6 只剩最后一半：**披露侧** —— 页面/邮件把「真实持仓」与「归并」
分开显示。**规则本身不动**（归并仍计入权重 —— 那是用户选的），只改呈现。

- 页面「最新持仓」表：`数量` / `含归并` → **`真实持仓`** / **`归并`**
- 邮件 3pp 再平衡表：同上
- 每日快照 stdout：`真实 N 份` + `归并 N 份`

- ✅ **已落地**（2026-10-27）：详见 §11.1.10。

#### Phase 6 — 状态机显式化（1 次提交）✅ 已落地

- 把 `manual_op` 里内联的「哪个状态能做哪个动作」收成一张**合法边表**
- 「一个周期 N 买 1 卖」从此是**表的形状**，不是注释里的约定

- ✅ **已落地**（2026-10-27）：`src/domain/grid/state_machine.py` ——
  `LEGAL_TRANSITIONS` + `transition()`；`manual_op` 只查表。
  详见 §11.1.8。

#### 11.1.9 Phase 7 落地情况（2026-10-27）

§11.1 的**目标树**里 `domain/ledger/` 有三个文件，实现上还差一个 ——
`projection.py`（`LedgerProjection`）。Phase 7 把它补上。

##### (a) 写侧与读侧分开

```
src/domain/ledger/
  ledger.py        写侧 —— apply_trade: 追加一笔, 校验不变量 (I1 / I2)
  trade_side.py    八个 side 的词表
  projection.py    读侧 —— project_ledger: 重放账本 → LedgerProjection
```

账本是唯一的事实源；`data/holdings.json` 只是它的**缓存**。这条「事实 vs
投影」的分界以前只写在注释里，现在**落在模块上** —— 读侧不必碰写侧，
写侧也不必知道「投影」这个概念。依赖是单向的：`projection` → `ledger`
（重放要用 `apply_trade`），反过来没有。

##### (b) `LedgerProjection` —— 投影是个**有名字**的值

```python
class LedgerProjection(NamedTuple):
    positions: list[dict]      # [{name, code, shares}]
    cash_rebalance: float
    cash_grid: float
```

⚠️ 选 `NamedTuple` 而不是普通 dataclass：既有调用方都是
`positions, cash_rebalance, cash_grid = project_ledger(...)` 那样解包的。
把投影**命名**不该逼着调用方改形状（那只是一次改名，不是一次建模）；
想用具名属性的人写 `proj.positions` 就行。

##### (c) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **190/190 通过**（新增 2 个） |
| 行为不变 | ✓ —— 重放的**实现**逐字搬过去（排序规则、只含份额 > 0 的持仓、事件收集都一样） |
| 投影是有名字的值 | ✓ —— `test_the_ledger_projection_is_a_named_value` 钉住「具名属性」与「照样能解包」 |
| 依赖单向 | ✓ —— `test_the_write_side_and_the_read_side_live_in_different_modules` 钉住 `ledger.py` 不反向 import `projection` |

#### Phase 7 — 账本读侧拆成投影（1 次提交）✅ 已落地

- `domain/ledger/projection.py` —— `LedgerProjection` + `project_ledger`
- 写侧（`apply_trade`）与读侧（重放）分开，依赖单向

- ✅ **已落地**（2026-10-27）：`src/domain/ledger/projection.py`；
  `ledger.py` 只剩写侧。目标树（§11.1）对齐。详见 §11.1.9。

#### 11.1.10 D6 披露侧落地情况（2026-10-27）

§12 的 D6 剩最后一半：**披露侧** —— 读者要能一眼看出「这个仓位里有多少是
我真金白银买的、多少是网格这个虚拟策略带进来的」。规则本身是用户选的，
**语义一个字不改**，只改呈现。

##### (a) 记录里本来就有这两个数

`Position.to_record()` 写出去的键一直是分开的：

| 键 | 含义 |
|---|---|
| `shares` | 账本里的**真实**成交份额 |
| `merged_shares` | 网格归并进来的**模拟**份额 |
| `total_shares` | 两者之和 —— **权重与市值用它** |

以前页面只显示「数量」（= `total_shares`）和「含归并」（= `merged_shares`），
读者要自己减一遍才知道自己真金白银拿了多少。

##### (b) 三个披露面，一套词

| 面 | 原来 | 现在 |
|---|---|---|
| 页面「最新持仓」(`app.js:renderLatest`) | 数量 / 含归并 | **真实持仓** / **归并** |
| 邮件 3pp 再平衡表(`src/presentation/digest.py`) | 数量 / 含归并 | **真实持仓** / **归并** |
| 每日快照 stdout(`src/application/daily_snapshot.py`) | (含归并 N 份) | **真实 N 份 + 归并 N 份** |

每一面自己的脚注都写清口径：

- **真实持仓** = 账本里的真实成交份额
- **归并** = 网格归并进来的份额（模拟口径，虚拟策略）
- **市值 / 权重 / 偏离** = **(真实持仓 + 归并)** —— 不是「真实持仓 × 价格」

⚠️ 网格**操作记录**表里的「数量」是**另一回事** —— 那是那一笔操作的份数，
语义不同，不改。

##### (c) 为什么「真实持仓」直接取 `shares`，而不是 `total_shares − merged_shares`

`shares` 在**每一条**历史记录里都是真实份额 —— `Position.to_record()` 从
一开始就是这么写的；`data/history.json` 里 0~9 号记录没有
`total_shares` / `merged_shares` 两列，但那时归并恒为 0，`shares` 就是全部。
所以三个面都直接读 `shares`，**不做减法** —— 少一处可能算错的算术。

##### (d) 一个「归并-only」的仓位现在看得很清楚

网格把某个资产的余量归并进组合、而组合自己一笔没买过时，那一行现在是
「真实持仓 **0** / 归并 N」—— 以前显示的是「数量 N」，看不出来这份持仓
是虚拟的。

##### (e) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **192/192 通过**（新增 2 个） |
| `node --check app.js` | ✓ |
| 页面（chrome-devtools，硬刷新） | ✓ —— 表头 `真实持仓 / 归并`；灌一份带归并的假记录，行显示 `5,200 / 1,232`，归并那栏是红的 |
| 邮件（真实记录渲染） | ✓ —— 文本表 `真实持仓 32,000 / 归并 —`，脚注两行口径说明 |

#### 11.1.11 D11 落地情况（2026-10-28）

§12 里最小的一条 —— 一行代码，但它让**图表一直在说谎**。

##### (a) 错在哪

`src/infrastructure/chart_renderer.py:_load_chart_assets()` 写的是：

```python
holdings, _ = read_holdings()      # ❌ read_holdings() 返回**三个**值
```

`read_holdings()` 返回 `(positions, cash_rebalance, cash_grid)` —— 三个。
解包成两个抛 `ValueError`；而紧接着那行 `except Exception` 把异常**吞了**，
直接走 fallback 分支：**图表永远画完整的 `ETF_DEFS`**。

所以「换仓 / 减仓后图表仍画全部 4 个品种」不是数据错了，是**异常被吞了**。

##### (b) 修法

```python
holdings, _cash_rebalance, _grid_budget = read_holdings()
```

fallback 逻辑**不动** —— 文件读不到时仍然退回全量品种（测试环境 / 第一次跑
`record.py` 之前）。兜底**只在读不到文件时**才走，不再是「读得到也走」。

⚠️ 今天的持仓恰好就是 `ETF_DEFS` 那 4 个代码，所以**图看起来没变** ——
但它现在读的是**用户实际持仓**，换仓之后会跟着变。

##### (c) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **194/194 通过**（新增 2 个） |
| `test_the_chart_draws_the_holdings_the_user_actually_has` | ✓ —— 只画用户持有的那一个品种 |
| `test_the_chart_falls_back_to_the_full_registry_when_holdings_are_unreadable` | ✓ —— 兜底路径仍在 |

#### 11.1.12 D3 落地情况（2026-10-28）

§12 里最像「改名」的一条 —— 但它要解决的是**类型说不清**的问题。

##### (a) 原来错在哪

`cash_grid` 是**虚拟预算**（网格那一池的钱，不是用户的真金白银），但：

- 名字里看不出虚拟 —— `cash_grid` 和 `cash_rebalance` 长得一模一样；
- 于是它被当成现金混进 `Portfolio.total` —— 而 `total` 是 **3pp 判阈**与
  **层额定尺**的基数。

规则本身**一个字没改**：虚拟预算仍然计入 `total`（`total` 是**策略的资本
基数** —— 两个池子的钱在同一个池子里算）。改的是「它是什么」这件事**由谁
来说**。

##### (b) 改法：给它一个自己的类型

`src/domain/shared/money.py:GridBudget`（float 子类，DP=2 —— 替掉账本写侧
那次 `Money`，**取整点没动，数字逐位相同**）。

| 地方 | 改前 | 改后 |
|---|---|---|
| `Portfolio` | `cash_grid: float` | `grid_budget: GridBudget` |
| `LedgerProjection` | `cash_grid: float` | `grid_budget: float` |
| `rebalance_policy.evaluate(...)` | `cash_grid=` | `grid_budget=` |
| 账本写侧的状态键 | `"cash_grid"` | `"grid_budget"` |

`Portfolio.__post_init__` 把它收成 `GridBudget` —— **类型不变量由代码保证**，
不靠调用方自觉。

##### (c) ⚠️ JSON 键**不改**

`data/holdings.json` 与 `data/history.json` 里的键**仍然叫 `cash_grid`** ——
那两个文件是用户直接看 diff 的 committed artifacts，改键名等于改历史。

于是**领域叫 `grid_budget`、JSON 叫 `cash_grid`**；映射只在
`json_repositories`（读）与 `apply_trade.holdings_document`（写）两处。

##### (d) 一道保险：旧键直接报错

`ledger.apply_trade` 遇到状态里还有 `cash_grid` 就**抛错** —— 静默忽略它
等于「网格那一池的钱凭空消失」，而那正是这个键存在的全部意义。

##### (e) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **198/198 通过**（新增 4 个） |
| 数字有没有变 | 没有 —— `GridBudget` 的 DP=2 与原来的 `Money` 一样，取整点也没动 |
| `test_the_virtual_budget_still_counts_towards_the_total` | ✓ —— 规则没改：1600 = 1000 + 100 + 500 |
| `test_the_ledger_rejects_the_legacy_grid_cash_key` | ✓ —— 旧键报错，钱不会凭空消失 |

#### 11.1.13 D4 落地情况（2026-10-28）

##### (a) 原来错在哪

页面上有两个「市值」，说的却不是一件事：

| 位置 | 算的是什么 |
|---|---|
| 顶部 badge「总市值」 | `etf_value + cash_rebalance + cash_grid` —— **不含**网格的虚拟占用 |
| 资金管理表「合计市值」 | ETF 持仓市值 + **网格的虚拟资金占用** |

网格那一块是**虚拟**的（网格是虚拟策略，那不是用户的钱），badge 不算它 ——
对。但表把它算进了「合计」，于是两张表自相矛盾，用户只能靠猜那个差额去哪了。

##### (b) 改法：口径写进**代码**

`src/domain/portfolio/cash_pools.py:CashPoolBreakdown`：

```
合计市值 = 只含 3pp再平衡 的 ETF 持仓市值（网格的虚拟占用**不计入**）
合计余额 = 真实现金 + 虚拟现金
⇒ 合计市值 + 合计余额 == 顶部 badge 的「总市值」
```

为什么「不计入」是对的那一个？badge 的总市值**本来就不含**网格的虚拟占用
（`total = etf_value + cash_rebalance + grid_budget`，D3）—— 要两张表对上，
合计市值就也不能含。

⚠️ **虚拟占用没有被藏起来** —— 它还在「复合网格」那一行里，标着「虚拟」，
只是**不参与合计**。D3 的原则在这里继续生效：虚拟不等于该被藏起来，只等于
**必须被标出来**。

⚠️ 虚拟的**余额**（`cash_grid`）**计入**合计余额 —— badge 就是这么算的
（`grid_budget` 计入 `total`，D3）。要是对称地把它也踢出去，那张表反而跟
badge 对不上 —— 那才是真的自相矛盾。

##### (c) 呈现层

| 位置 | 改法 |
|---|---|
| `index.html` 网格那一行 | 行名下加一行「虚拟」；两个格子挂「虚拟」标记 |
| `index.html` 合计那一行 | 行名下加一行「不含网格虚拟占用」 |
| `index.html` 页脚 | 写死三条公式（合计市值 / 合计余额 / 两者相加 = 总市值） |
| `app.js` 合计市值 | `money(rebalanceMarket)` —— **不再** `rebalanceMarket + gridMarket` |
| `app.js` badge | 网格那一笔标「复合网格(虚拟)」 |
| `src/presentation/digest.py` | 邮件头「网格(虚拟)」；网格段加一句「资金占用 = 虚拟，不计入上方总市值」 |

`daily_snapshot.build_record` 的 `cash_total` 现在从 `CashPoolBreakdown`
出 —— **数字逐位不变**（加法顺序一致）。

##### (d) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **203/203 通过**（新增 5 个） |
| 页面实测 | 合计市值 ¥168,884.40 + 合计余额 ¥165.00 = badge 总市值 ¥169,049 ✓ |
| 数字有没有变 | 没有 —— 记录的 `cash_total` 加法顺序不变 |

#### 11.1.14 D1 落地情况（2026-10-28）

##### (a) 原来错在哪

`apply-grid.yml` 提交后会 re-trigger `daily.yml` —— **同一天** `record.py`
可能跑两次。而窗口原来是一串**裸数字**：

```python
last_prices.append(float(price))          # ← 同一天跑两次 = 追加两次
if len(last_prices) > lookback:
    last_prices = last_prices[-lookback:]
```

于是同一天的 K 线在窗口里出现**两次**。窗口深度少一天。

看着无所谓 —— 但窗口是「**建议开仓**」的**唯一**依据（条件 =
`price < min(window)`），少一天就少一天的历史，**而且看不出来**：队列尾巴上
两根一模一样的 K 线，谁知道那是两天，还是同一天跑了两遍。

窗口里存的又是**前复权**之后的 K 线低点 —— 同样的「历史低点」在不同日子
算出来是不同的数（分红除权会重新基准化）。所以老窗口的**日期不可恢复**。

##### (b) 改法：给它一个自己的类型

新模块 `src/domain/grid/price_window.py`：

```
WindowBar(day, price)   一根 K 线 —— 日期 + 最低价
PriceWindow(bars)       120 日滚动窗口 (不可变)
```

`PriceWindow.record(day, price, lookback=)` 是**唯一**的写入口：

| 情况 | 行为 |
|---|---|
| 同一天 | **覆盖** —— 留**更低**的那个（那才是那天的最低价） |
| 新的一天 | 按日期插到正确的位置（无日期的旧 bar 排在最前） |
| 超过 `lookback` | 砍掉**最老**的几根 |

⚠️ **留低不留高**：窗口最低价是「今天是否创新低」的门槛。把它抬高会让已经
该触发的建议**不再触发** —— 那是假的否定信号，比深度少一天严重得多。

⚠️ **老数据的日期不可恢复** → 迁成**无日期**的 bar（`day = ""`）。日期靠之后
的每日运行**自己长出来**（约 120 个交易日后，窗口全是带日期的）。迁移动作
在 `scripts/migrate_grid_window.py`（一次性、幂等）。

##### (c) ⚠️ JSON 键**不改**

`data/grid_state.json` 的 key **仍然是 `last_prices`** —— 那是 committed
artifact，用户读它的 diff。而且 `GridPosition.to_state()` 有自检：

```python
produced = frozenset(state)
declared = frozenset(f.name for f in fields(self))
if produced != declared:
    raise ValueError("grid state mapper is out of sync: ...")
```

⇒ **字段名 == JSON 键**是强制的（不然新加字段忘了写 mapper 会静默丢数据，
不变量 I11）。改的只是**值的格式**：

```
[8.35, 8.29, ...]   →   [["2026-10-27", 8.35], ["2026-10-28", 8.29], ...]
```

两种格式都**读得进来**（`PriceWindow.from_state`）—— 旧格式迁成无日期的 bar。

##### (d) 顺带瘦身：`history.json` 里不再抄窗口

`build_record` 的 `slim_grid_state` 现在同时剥掉 `ops` **和** `last_prices`
—— 那是 4 个资产 × 120 根**带日期**的 K 线，每条历史记录抄一份的话
`history.json` 每天胖几千行。窗口的**唯一**真相在 `data/grid_state.json`。

##### (e) 验证

| 检查 | 结果 |
|---|---|
| `python -X utf8 -m tests.test_record` | **211/211 通过**（新增 7 个 D1 用例） |
| 同一天跑两遍 | 窗口**不变长**（`test_the_same_day_does_not_grow_the_window`） |
| 同一天、更低的价格 | 覆盖成更低的（`test_the_same_day_keeps_the_lower_price`） |
| 老数据读得进来 | 迁成 `["", 价格]`（`test_the_window_reads_both_the_old_and_the_new_format`） |
| 真实 `data/grid_state.json` | 4 个资产 × 120 根迁移完毕，**价格一个没改** |

### 11.3 明确**不要**做的

| 不做 | 理由 |
|---|---|
| 改成事件溯源 / CQRS / 数据库 | 数据量是 4 个资产 × 几千天。JSON 文件 + Git 提交历史就是最好的"事件溯源"。引入 DB 是净损失。 |
| 把网格改成真自动交易 | 用户 2026-10-25 明确决定：引擎只给建议。**这是产品决策，不是技术债。** |
| 统一回测与实盘口径 | 二者的**场景本来就不同**（历史重放 vs 手动提交）。强行统一会让两边都失真。正确做法是**隔离**，不是统一。 |
| 重写 `app.js` | 1356 行但职责单一（读 history.json + grid_state.json 渲染）。当前没有逻辑债，只有口径债（Phase 4 修）。 |
| 引入 Web 框架 / ORM | 收益为负。 |

---

## 12. 已知问题清单（DDD 视角下的问题登记）

| # | 问题 | 证据 | 影响 | 建议 |
|---|---|---|---|---|
| D1 | 同一天重复运行会让 120 日窗口少一天深度 | `apply-grid.yml` 提交后 re-trigger `daily.yml`；窗口尾部有重复值（黄金 `[8.35, 8.29, 8.12, 8.12]`） | 窗口是**唯一**的距开仓依据 | ✅ **已解决** —— `PriceWindow`（`src/domain/grid/price_window.py`）把窗口改成一等公民：`WindowBar(day, price)` 带日期，同一天是**覆盖**（留更低的那个）而不是追加；老数据迁成无日期的 bar（日期不可恢复 —— 前复权会重新基准化）。**JSON 键仍是 `last_prices`**（committed artifact），只改值的格式（详见 §11.1.14）|
| D2 | `ops` 混装执行日志与模拟日志 | `src/domain/grid/grid_position.py:manual_op` 写 `source:"manual"`；`src/simulation/grid_simulator.py:ops_from_cycles` 写 `source:"backtest"` | 容易被误读成"真实成交记录" | ✅ **已解决**（Phase 2b，详见 §11.1.4）—— `GridOp` 基类 + `ExecutedOp`（`source="manual"`）/ `SimulatedOp`（`source="backtest"`）两个子类；`GridOp.from_dict` 按 `source` **分型**，`SimulatedOp` 拒绝 `source!="backtest"` |
| D3 | `cash_grid` 是虚拟预算，却被 `evaluate()` 当现金计入 `total` | `src/domain/portfolio/rebalance_policy.py:evaluate` | `total` 同时是 3pp 判阈和 `layer_yuan` 的输入 | ✅ **已解决**（领域侧，详见 §11.1.12）—— 改名 `GridBudget` / `grid_budget`（`src/domain/shared/money.py`），`Portfolio.__post_init__` 保证类型；**规则不变**，虚拟预算仍然计入 `total` |
| D4 | 资金管理「合计市值」与 badge「总市值」口径不一致 | 网格虚拟占用被计入合计 | 页面自相矛盾 | ✅ **已解决** —— `CashPoolBreakdown`（`src/domain/portfolio/cash_pools.py`）把「合计」口径写成代码：合计市值**不含**网格虚拟占用、合计余额含虚拟余额 ⇒ 合计市值 + 合计余额 = badge 总市值；网格那一行标「虚拟」（详见 §11.1.13）|
| D5 | `holdings.json` 同时扮演事实源 / 派生缓存 / 资产宇宙 | ✅ **已解决**（Phase 4）—— 资产清单独立成 `AssetUniverse` 注册表（`src/domain/portfolio/asset_universe.py`），`holdings.json` 只剩持仓 | 职责混乱 | ✅ 已解决（§11.1.6） |
| D6 | 归并份额（模拟）推着真实组合触发调仓 | `src/domain/portfolio/portfolio.py:Portfolio.from_holdings` + `RebalancePolicy.evaluate` | 模拟影响真实 | ✅ **已解决**（Phase 4 标注 + Phase 5d 披露）—— 标注侧：`Provenance` 落在 `PositionLot` 上，`Position.simulated_shares` 是**求和出来的**；披露侧：页面/邮件/日志都把「**真实持仓**」与「**归并**」分成两栏显示，口径写在各自脚注里（详见 §11.1.10）。**规则本身不改** —— 归并仍计入权重，那是用户 2026-10-25 拍板的 |
| D7 | 网格规则两套实现 | ✅ **已解决**（Phase 3）—— `src/domain/grid/trigger_policy.py:TriggerPolicy` 现在是**生产 + 回测**两条路径**唯一**的判定来源 | 改参数会静默分叉 | ✅ 已解决（§11.1.5）—— `run_compound_grid` 调用 `policy.levels(...)`，并有静态测试防止内联算术回流 |
| D8 | `THRESHOLD_PP` / `GRID_*` 是模块级环境变量 | ✅ **已解决**（Phase 5a）—— 参数收进 `src/domain/grid/config.py:GridConfig` 与 `src/application/config.py:AppConfig`；模块级别名保留为**配置对象的投影**；`TriggerPolicy.from_config` / `LayerSizingPolicy.from_config` / `advance_all_grids(config=)` / `run_backtest(threshold_pp=)` 让多套策略在一个进程里并存 | 无法做多策略并存、测试需改环境 | ✅ 已解决（§11.1.7 a）—— `test_env_reads_are_confined_to_the_config_objects` 静态钉住「只有配置对象读环境变量」 |
| D9 | `evaluate()` 的两段式求值是隐式近似 | ✅ **已解决**（Phase 5c）—— `SnapshotRun.layer_sizing_basis` / `SnapshotRun.snapshot_basis` 各有名字：层额定尺用前者，记录用后者 | 恢复自动行为时会有偏差 | ✅ 已解决（§11.1.7 c）—— `test_size_grid_layer_uses_the_layer_sizing_basis_total` 钉住「层额用的是层额定尺那个数」 |
| D10 | 规则参数在 `src/domain/grid/grid_position.py` 与 `scripts/apply_grid.py` 各有一份 | ✅ **已解决**（Phase 5c-2）—— 写路径的 `VALID_OPS` 现在是 `TriggerLevel` 的**投影**，`VALID_SIDES` 是 `TradeSide` 的投影；两份脚本里手抄的词表删掉了 | 改一处漏一处 | ✅ 已解决（§11.1.7 d）—— `test_write_path_vocabularies_come_from_the_domain` 静态钉住 |
| D11 | 图表永远用完整 `ETF_DEFS`，读不到用户实际持仓 | `src/infrastructure/chart_renderer.py:_load_chart_assets()` 里 `holdings, _ = read_holdings()` —— `read_holdings()` 返回 **3** 个值，解包抛 `ValueError`，被 `except Exception` 吞掉后走 fallback 分支 | 换仓/减仓后图表仍画全部 4 个品种 | ✅ **已解决** —— `holdings, _cash_rebalance, _grid_budget = read_holdings()`；兜底分支**只在文件读不到时**才走（详见 §11.1.11）|
| D12 | GitHub Actions 的契约散落在三个地方 | `$GITHUB_OUTPUT` 在 `src/application/daily_snapshot.py`; `::error::` 在 `scripts/apply_grid.py`; `GITHUB_RUN_ID` 在 `src/presentation/notify_failure.py`; 两份 `apply-*.yml` 各抄了一份 `/dispatches` 的 curl | 应用层/呈现层知道了 workflow 的内部; 改一处漏一处; 401 是**静默**的 | ✅ **已解决** —— `src/infrastructure/github_actions/`（防腐层：context / outputs / annotations / summary）+ `scripts/trigger_daily.py` 收编派发（详见 §11.1.15）|
| D13 | 仓库根在结构重构后算错一层 ⇒ 邮件**静默**不发送 | `src/presentation/digest.py` / `src/infrastructure/chart_renderer.py` 的 `parent.parent` 指向 `src/`（下了一层但没跟着改） | 2026-10-26 起**三天一封邮件都没发**，而 workflow 那一步**是绿的**（`print(WARN)` + `return`，退出码 0）; 图表少画网格仓位 | ✅ **已解决** —— `src/infrastructure/paths.py` 单一真源; 「没有记录」改成**抛错**; `sendmail()` 的拒收返回值不再被丢（详见 §11.1.15）|

---

## 13. 验收清单

重构后，下面每一条都应该能被**代码结构**回答，而不是靠注释或文档：

- [x] 「网格引擎不交易」在哪里保证？→ `GridAdvisor` 返回不可变 `GridSuggestion`，类型上无法写 state（Phase 2b ✅）
- [x] 「一个周期 N 买 1 卖」在哪里保证？→ `src/domain/grid/state_machine.py:LEGAL_TRANSITIONS`（Phase 6 ✅）—— `manual_op` 只查那张表；回到 Idle 的**唯一**一条边是 `tp`（它把周期整个结束掉），而 `in_cycle` 只有 `open` 会置真 —— 所以一个周期里 `add` 可以任意多次（N 买）、`tp` 只能一次（1 卖）
- [x] 「出场只卖成本额」在哪里保证？→ `cost_only_exit` 唯一实现（Phase 2a ✅）
- [x] 「什么算触发只有一份规则」在哪里保证？→ `TriggerPolicy.levels` 是生产路径与回测路径**唯一**的判定来源（Phase 3 ✅）；`test_simulator_has_no_inline_trigger_arithmetic` 静态挡住内联算术回流
- [x] 「归并份额不写 trades.json」在哪里保证？→ `MergedSharesTransferred` 事件，账本上下文无写权限（Phase 5b ✅）—— 组合**只**从事件吸收归并份额；八个 `TradeSide` 里没有「归并」这一档，`apply_trade` 遇到未知 side 直接抛错
- [x] 「模拟不是实盘」在哪里保证？→ `ExecutedOp` / `SimulatedOp` 分型（Phase 2b ✅）；`Provenance` 落在 `PositionLot` 上、`Position.simulated_shares` 是求和出来的（Phase 4 ✅）；**呈现层的披露**：页面/邮件/日志都把「真实持仓」与「归并」**分成两栏**（Phase 5d ✅，§11.1.10）
- [x] 「目标权重 = 1/n」在哪里保证？→ `TargetWeightPolicy`（Phase 4 ✅）—— 类里只有这一处实现；UI 从 `Portfolio` / 记录推导而非硬编码（Phase 4 ✅，`app.js` 里手抄的清单已删）
- [x] 「持仓市值不会被抄错」在哪里保证？→ `Position.value` 是 `total_shares × price` **推出来的**，`Position.to_record()` 是它的输出（Phase 4 ✅）；`test_position_value_is_derived_from_lots_not_copied` 钉住这条
- [x] 「状态形状固定」在哪里保证？→ `GridPosition` dataclass，mapper 显式（Phase 2a ✅）
- [x] 「回测与实盘不可对账」在哪里保证？→ 两者是不同上下文、不同类型（`SimulatedOp` vs `ExecutedOp`）、不同仓储权限（Phase 2b + Phase 3 ✅）；`GridSimulator` 只能产出 `SimulatedOp`，`merge_backtest_ops` 只能写 `source="backtest"` 的行
- [x] 「规则参数只有一份」在哪里保证？→ `GridConfig` / `AppConfig`（Phase 5a ✅）—— 环境变量**只**在配置对象里读一次；模块级别名（`GRID_STEP_PCT` 等）是配置对象的**投影**；`TriggerPolicy.from_config` / `LayerSizingPolicy.from_config` / `advance_all_grids(config=)` / `run_backtest(threshold_pp=)` 让多套策略并存；`test_env_reads_are_confined_to_the_config_objects` 静态钉住「领域层与回测层不再自己读环境变量」
- [x] 「记录里那个 total 是哪一个」在哪里保证？→ `SnapshotRun.snapshot_basis`（Phase 5c ✅）—— 层额定尺那次评估是**另一个**字段 `layer_sizing_basis`；两个名字各答一个问题，`test_size_grid_layer_uses_the_layer_sizing_basis_total` 钉住「层额用的是层额定尺那个数」
- [x] 「应用层没有业务规则」在哪里保证？→ `daily_snapshot.main()` 只调 12 个具名用例（Phase 5c-1 ✅）—— `test_main_is_pure_wiring` 静态钉住（`abs(` / `round(` / `float(` / `min(` / `max(` 一个都不许出现）；`test_main_runs_the_use_cases_in_order` 钉住用例清单与顺序
- [x] 「写路径的规则只有一份」在哪里保证？→ `src/application/apply_grid_op.py` / `apply_trade.py`（Phase 5c-2 ✅）—— op 词表 = `TriggerLevel` 的投影、side 词表 = `TradeSide` 的投影；两个脚本是**薄壳**（`test_the_write_path_scripts_are_thin_shims` 钉住脚本里没有校验/重放/发 id 的实现）
- [x] 「写路径与读路径同一把尺子」在哪里保证？→ 写路径重放调用**领域层**的 `src/domain/ledger/projection.py:project_ledger`（Phase 5c-2 ✅，Phase 7 搬家）—— 与每日快照 `daily_snapshot.replay_trade_ledger` 同一份实现；`test_write_path_and_daily_path_replay_the_ledger_identically` 钉住「两条路重放出的持仓与现金相同」
- [x] 「图表画的是不是用户实际持仓」在哪里保证？→ `_load_chart_assets()` 解包 `read_holdings()` 的**三个**返回值（D11 ✅）—— 以前解包成两个抛 `ValueError`，被 `except Exception` 吞掉后静默退回全量 `ETF_DEFS`；`test_the_chart_draws_the_holdings_the_user_actually_has` 钉住
- [x] 「哪一池是虚拟的」在哪里保证？→ `GridBudget` 类型（D3 ✅）—— 领域里已经没有 `cash_grid` 这个名字了（`Portfolio.grid_budget` / `LedgerProjection.grid_budget`）；类型由 `Portfolio.__post_init__` 保证；**JSON 键仍是 `cash_grid`**（committed artifact 不改键名），映射只在读/写两处；账本写侧遇到旧键直接报错
- [x] 「资金管理表的『合计』和顶部 badge 的『总市值』是同一个数吗？」→ `CashPoolBreakdown.total == Portfolio.total`（D4 ✅）—— 规则在 `src/domain/portfolio/cash_pools.py`，不是注释；记录的 `cash_total` 从同一个对象出；页面那一行的公式写在页脚
- [x] 「窗口里的每一根 K 线是哪一天？」在哪里保证？→ `PriceWindow` / `WindowBar`（D1 ✅）—— 窗口的每一根都带日期；`PriceWindow.record()` 是**唯一**的写入口，同一天是**覆盖**（留更低的那个）而不是追加 ⇒ 窗口深度只由**交易日**决定；老数据读得进来（迁成无日期的 bar，因为前复权让历史日期不可恢复）
- [x] 「坏掉的账本不会被当成空账本」在哪里保证？→ 写路径走 `json_repositories.read_trades()` / `read_grid_state()`（Phase 5c-2 ✅）—— **严格读**：文件坏了就抛错（整批不写盘）；每日快照仍走宽松的 `load_trades()`（跑砸了只是今天的记录没出来，宁可少一天也别静默清空持仓）
- [x] 「GitHub Actions 的环境变量名在哪里？」→ `src/infrastructure/github_actions/`（D12 ✅）—— `$GITHUB_OUTPUT` / `$GITHUB_STEP_SUMMARY` / `GITHUB_RUN_ID` / `::error::` 的**字面量**只出现在这个包里（`test_the_actions_adapter_owns_the_github_contract` 钉住）; step output 的名字（`date` / `total` / `trigger`）也在那儿 —— 它是 workflow 的**契约**, 不该散在调用方
- [x] 「仓库根在哪？」→ `src/infrastructure/paths.py`（D13 ✅）—— `src/` 下只有它算 `parent.parent`（`test_the_repo_root_has_one_home` 钉住）; 「读不到记录」不再被当成「没有记录」而是**抛错**（`test_the_digest_never_silently_skips_a_missing_record`）; 「邮件发不出去」也不再是**绿的**

---

## 附：与本项目已有文档的关系

| 文档 | 关系 |
|---|---|
| `docs/correlation_analysis.md` | 资产选择的**决策依据**，属于策略研究，不属于领域模型 |
| `README.md` §复合网格 | 面向用户的**操作说明**；本文 §5 通用语言是它的**术语基准** |
| `memory/grid-*.md` | 领域规则的**决策溯源**（用户拍板记录），本文引用但不重复 |
| 本文 | 领域**结构**（边界、聚合、不变量），供后续重构使用 |
