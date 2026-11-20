# 项目结构概览

本项目按**业务模块**划分，每个模块内部再按职责分层（规则 / 编排 / 适配）。

```
src/
├── data/       # 数据模块：数据获取源 + 缓存
├── strategy/   # 策略模块：交易策略 + 规则
└── chart/      # 图表模块：图片生成器 + 表格数据 + 网页生成器
```

依赖方向单向：**chart → strategy → data**（图表只消费策略与数据的结果）。

---

## 📁 模块清单

### 1️⃣ 数据模块 `src/data/`

> 负责：把行情数据取进来、缓存起来，并对外提供统一查询入口。

| 文件 | 层次 | 职责 |
|---|---|---|
| `models.py` | 规则 | 价格聚合根 `AdjustedPriceSeries`、值对象 `AssetCode` / `PricePoint` |
| `value_objects.py` | 规则 | 指标类值对象 `ReturnMetrics` / `TechnicalIndicators` |
| `indicators.py` | 规则 | `PriceCalculator`：收益率 / MA / RSI / 布林带 / ATR / 前N日最低价 |
| `cache.py` | 规则 | 缓存策略值对象 + `PriceCachePlanner`（刷新计划）+ `PriceSeriesMerger`（接缝校验） |
| `sources.py` | 规则 | 复权口径、源健康、`PriceSourceSelector`（选谁）、`PriceBasisAligner`（口径对齐） |
| `providers.py` | 适配 | 数据源适配器：东财 / 腾讯 / 新浪 / 实时报价 |
| `fallback.py` | 适配 | `FallbackPriceProvider`：使用前探测 + 主源优先 + 运行期降级 |
| `cache_store.py` | 适配 | 价格缓存落盘：内存 / JSON 文件（`data/price_cache/`） |
| `service.py` | 编排 | **`PriceQueryService`**：对外统一入口 |

### 2️⃣ 策略模块 `src/strategy/`

> 负责：定义「怎么交易」——交易策略、规则、资金与持仓记账。

| 文件 | 层次 | 职责 |
|---|---|---|
| `allocation.py` | 规则 | `AllocationPlan` 总资产配置（网格与再平衡**共用同一份**） |
| `portfolio.py` | 规则 | `Portfolio` / `Position`：现金、持仓、成本、盈亏（两策略各持一份，类型级隔离） |
| `performance.py` | 规则 | `PerformanceCalculator` / `ReturnStatistics`：统一绩效口径 |
| `spec.py` | 规则 | `StrategySpec` / `StrategyRule`：策略名 → 规则描述 → 规则实现 |
| `ledger/trade.py` | 规则 | `Trade` / `TradeSide` / `CashFlowType`：追加式成交流水 |
| `ledger/book.py` | 规则 | `TradeLedger`：账本聚合根（流水重演持仓） |
| `ledger/values.py` | 规则 | `LotSize`：最小交易单位（整手取整，共享内核） |
| `ledger/store.py` | 适配 | 账本落盘（`trades.json` 真相源 + `holdings.json` 派生视图，**尚未接入回测流程**） |
| `grid/models.py` | 规则 | 网格实体与值对象：`GridStrategy` / `GridState` / `GridTrigger` / `GridOrder` … |
| `grid/engine.py` | 规则 | **`GridEngine`**：网格全部业务规则（触发 / 加仓 / 止盈） |
| `grid/backtest.py` | 编排 | `GridBacktester`：逐日编排 + 记账，输出回测结果 |
| `grid/spec.py` | 规则 | `build_grid_spec`：网格策略规格（描述由参数推导） |
| `rebalance/rules.py` | 规则 | **`should_rebalance` / `initial_build_orders` / `plan_rebalance_orders`**：再平衡全部业务规则 |
| `rebalance/backtest.py` | 编排 | `RebalanceBacktester`：逐日编排 + 记账 |
| `rebalance/spec.py` | 规则 | `build_rebalance_spec`：再平衡策略规格 |
| `combined.py` | 编排 | `CombinedBacktester`：再平衡 + 网格组合回测（持仓隔离） |

### 3️⃣ 图表模块 `src/chart/`

> 负责：把策略结果呈现出来——图片、表格、网页。

| 文件 | 层次 | 职责 |
|---|---|---|
| `models.py` | 规则 | 图表模型：`ChartConfig` / `ChartSeries` / 样式 / 标注 |
| `renderer.py` | 适配 | **图片生成器**：`ChartRenderer` / `ChartFactory`（通用收益曲线图） |
| `overview.py` | 规则 | 策略全景图模型 `StrategyOverview`（收益率曲线 + 资金占用） |
| `overview_renderer.py` | 适配 | **图片生成器**：策略全景图 PNG |
| `grid_chart.py` | 适配 | **图片生成器**：网格多面板图（价格 / 持仓堆叠 / 组合对比） |
| `tables.py` | 规则 | **表格数据**：触发日志、成交明细、档位格式化（纯文本，不依赖 matplotlib） |
| `site.py` | 编排 | **网页生成器**：`BuildPagesSite` + 视图模型（summary / trades / holdings） |
| `site_store.py` | 适配 | 站点产物落盘（`data/site/*.json` + `grid_chart.png`） |

---

## 🧪 测试 `tests/`

| 文件 | 覆盖 |
|---|---|
| `test_price_domain.py` | 价格模型与查询服务 |
| `test_price_cache.py` | 缓存规划 / 合并 / 接缝校验 |
| `test_price_sources.py` | 数据源选择 / 口径对齐 / 降级 |
| `test_grid_domain.py` | 网格规则与回测（离线） |
| `test_strategy_spec.py` | 策略规格（描述由参数推导） |
| `test_trading_ledger.py` | 交易账本（追加式 + 双资金池） |
| `test_portfolio.py` | 组合与持仓不变量 |
| `test_rebalance_rules.py` | 再平衡规则（触发判定 / 建仓 / 调仓） |
| `test_pages_site.py` | 站点视图模型与全景图渲染 |

---

## 📚 示例 `examples/`

| 文件 | 内容 |
|---|---|
| `ddd_price_model_demo.py` | 价格模型与指标用法 |
| `grid_strategy_demo.py` | 网格回测 / 参数对比 / 组合隔离 / 绘图 |
| `rebalance_backtest_demo.py` | 再平衡回测 |
| `chart_ddd_demo.py` | 通用图表生成器 |

---

## 🛠 脚本 `scripts/`

| 文件 | 用途 |
|---|---|
| `build_pages.py` | 跑组合回测并生成站点产物（`data/site/`） |
| `verify_site.py` | 用无头浏览器验收站点（需 playwright） |

---

## 🎯 核心功能

### 1. 价格数据获取
- ✅ 后复权(hfq)价格，515180 最新价 1.762
- ✅ 支持 ETF 和股票
- ✅ 自动缓存（内存 + JSON 文件，跨进程复用）
- ✅ 多数据源降级（东财 → 腾讯 → 新浪 → 实时报价）

### 2. 价格缓存与增量同步
- ✅ JSON 文件缓存（`data/price_cache/`）
- ✅ 缓存命中只拉缺失的尾部区间（实测 44 条 vs 全量 1668 条）
- ✅ 接缝校验：后复权口径被重算时自动全量重建
- ✅ 失败降级：数据源不可用时沿用缓存
- ✅ 热启动 5.4s → 1.1s（4 资产组合回测）

### 3. 收益率与技术指标
- ✅ 累计 / 年化收益率、波动率、夏普比率、最大回撤
- ✅ 移动平均线(MA5/10/20/60)、RSI、布林带、ATR、前N日最低价

### 4. 网格策略
- ✅ 跌破前 120 日最低价启动监控
- ✅ 网格间距 = 触发日 ATR(120) × 可配置比例
- ✅ 每格买入固定金额（默认 10000 元）
- ✅ 网格轮次生命周期：跌破前低 → 完全止盈 → 重新跌破才开新轮
- ✅ 总资产配置共享：网格只监控再平衡组合中的资产
- ✅ 交易类型标记：开仓 / 加仓 / 减仓 / 止盈
- ✅ 网格持仓与再平衡持仓完全隔离

### 5. 站点产出
- ✅ 策略全景图 PNG + summary.json + trades.json
- ✅ 策略规格（规则描述由参数推导，不会与实现漂移）

---

## 🚀 快速开始

### 安装依赖
```bash
pip install pandas akshare requests matplotlib
```

### 运行测试
```bash
python -m pytest tests/ -v
```

### 基本用法
```python
from src.data.service import PriceQueryService

service = PriceQueryService()

# 获取价格
df = service.get_adjusted_prices("515180")

# 计算收益率
metrics = service.calculate_returns("515180", start_date="2020-01-01")
print(metrics.summary())

# 多资产对比
df = service.compare_assets(["515180", "159937", "511380", "513300"])
```

### 网格策略用法
```python
from decimal import Decimal
from src.strategy.grid.backtest import GridBacktester, GridConfig
from src.strategy.allocation import AllocationPlan
from src.strategy.grid.models import GridTrigger

plan = AllocationPlan.equal_weight(
    [("515180", "红利ETF"), ("159937", "黄金ETF")], name="两资产等权"
)

config = GridConfig.from_allocation(
    plan=plan,
    trigger=GridTrigger(
        lookback_days=120,            # 前120日最低价（不含当日）
        atr_period=120,              # 120日ATR
        step_ratio=Decimal("0.5"),   # 网格间距 = ATR × 比例
        buy_amount=Decimal("10000"), # 每格买入金额
        take_profit_ratio=Decimal("1"),  # 上涨一个步长止盈（决定本轮结束）
    ),
    initial_capital=Decimal("200000"),
)
result = GridBacktester(config).run()
print(result.summary())

# 表格数据：触发日志 / 成交明细
from src.chart.tables import grid_trigger_log, grid_trade_table
print(grid_trigger_log(result))
print(grid_trade_table(result))
```

### 生成站点
```bash
python scripts/build_pages.py
```

---

## 🔧 技术栈

- **Python 3.11+**
- **pandas** - 数据处理
- **akshare** - 金融数据源
- **matplotlib** - 图表渲染
- **pytest** - 测试框架
- **Decimal** - 高精度计算

---

## 📖 文档导航

| 文档 | 用途 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | ⭐ 5分钟上手 |
| [README.md](README.md) | 项目概述 |
| [docs/price_cache_design.md](docs/price_cache_design.md) | 价格缓存与增量同步设计 |
| [docs/price_sources_design.md](docs/price_sources_design.md) | 多数据源容灾与复权口径设计 |
| [docs/grid_strategy_ddd_design.md](docs/grid_strategy_ddd_design.md) | 网格策略设计 |
| [docs/trading_ledger_design.md](docs/trading_ledger_design.md) | 交易流水与持仓设计 |
| [docs/strategy_ddd_design.md](docs/strategy_ddd_design.md) | 策略规格设计 |
| [docs/pages_site_design.md](docs/pages_site_design.md) | 站点设计 |
| [docs/data_api_guide.md](docs/data_api_guide.md) | 数据获取 API 指南 |

---

**最后更新**: 2026-11-20
**版本**: v2.0（模块化重构）
