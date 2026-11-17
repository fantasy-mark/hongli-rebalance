# 项目结构概览

## 📁 文件清单

### 核心代码 (7个Python文件)

#### 领域层 (Domain Layer) - 业务核心
```
src/domain/price/
├── entities.py              # 实体: AssetCode, PricePoint, AdjustedPriceSeries
├── value_objects.py         # 值对象: ReturnMetrics, DateRange, TechnicalIndicators
│                             #          CachePolicy, RefreshPlan, MergeResult
└── services.py              # 领域服务: PriceCalculator (+ATR / 前N日最低价)
                             #          PriceCachePlanner, PriceSeriesMerger

src/domain/allocation/      # ★ 总资产配置（再平衡与网格共享内核）
└── entities.py              # 聚合根: AllocationPlan, 值对象: AssetAllocation

src/domain/performance/     # ★ 绩效统计（三个策略共享内核）
├── value_objects.py         # 值对象: ReturnStatistics
└── services.py              # （见 value_objects 内 PerformanceCalculator）

src/domain/trading/         # ★ 交易内核（整手取整等市场规则共享）
└── value_objects.py         # 值对象: LotSize

src/domain/portfolio/       # ★ 组合与持仓（两策略共享内核）
└── entities.py              # 聚合根: Portfolio, 值对象: Position
                             #   每策略一个实例 -> 持仓隔离

src/domain/grid/            # ★ 网格策略上下文
├── entities.py              # 聚合根: GridStrategy, GridState
├── value_objects.py         # 值对象: GridTrigger, GridCostModel, GridMarketSnapshot,
│                             #          GridDecision, GridTradeType, 枚举
└── services.py              # 领域服务: GridEngine

src/domain/rebalance/        # 再平衡上下文
├── entities.py              # RebalancePolicy 等
├── value_objects.py         # 值对象: RebalanceOrder（上下文自有，不依赖其它上下文）
└── services.py              # 领域服务: RebalanceEngine
```

#### 应用层 (Application Layer) - 用例编排
```
src/application/price/
└── query_service.py         # 应用服务: PriceQueryService

src/application/grid/        # ★ 网格策略上下文
├── grid_backtester.py       # 应用服务: GridBacktester
├── grid_chart.py            # 应用服务: GridChartRenderer（多面板图 + 触发日志）
└── combined_backtester.py   # 应用服务: CombinedBacktester（共享配置，持仓隔离）

src/application/chart/       # ★ 图表（技术关注点，由领域层迁出）
├── entities.py              # ChartConfig, ChartSeries, ChartMarkerGroup ...
└── services.py              # ChartRenderer, ChartFactory
```

#### 基础设施层 (Infrastructure Layer) - 技术实现
```
src/infrastructure/
├── price/
│   └── repositories.py      # 仓储: InMemoryPriceRepository, JsonFilePriceRepository
├── grid/                    # ★ 网格策略上下文
│   └── repositories.py      # 仓储: InMemoryGridStateRepository, JsonFileGridStateRepository
└── adjusted_price_provider.py  # 数据源适配器 (已修复为hfq)
```

### 测试 (2个文件)
```
tests/
├── test_price_domain.py     # 27个单元测试 ✅
└── test_grid_domain.py      # 36个网格策略单元测试 ✅（离线，不依赖网络）
```

### 示例 (2个文件)
```
examples/
├── ddd_price_model_demo.py  # 6个完整使用场景示例
└── grid_strategy_demo.py    # 网格策略示例（回测/参数对比/组合隔离/绘图）
```

### 文档 (6个Markdown文件)
```
docs/
├── ddd_price_model_design.md          # DDD设计文档
├── ddd_implementation_summary.md      # 实现总结
├── grid_strategy_ddd_design.md        # ★ 网格策略DDD设计文档
├── adjusted_price_provider_usage.md   # 使用指南
├── migration_summary.md               # 迁移报告
└── price_data_sources.md              # 数据源说明 (原有)

QUICKSTART.md                          # 快速参考
README.md                              # 项目说明
PROJECT_STRUCTURE.md                   # 本文件
```

---

## 🎯 核心功能

### 1. 价格数据获取
- ✅ 后复权(hfq)价格，515180最新价1.762
- ✅ 支持ETF和股票
- ✅ 自动缓存（内存+JSON文件）
- ✅ 多数据源降级（东财→腾讯）

### 1.5 价格缓存与增量同步 ★新增
- ✅ JSON 文件缓存，跨进程复用（`data/price_cache/`）
- ✅ 缓存命中只拉缺失的尾部区间（实测 44 条 vs 全量 1668 条）
- ✅ 接缝校验：后复权口径被重算时自动全量重建
- ✅ 失败降级：数据源不可用时沿用缓存
- ✅ 热启动 5.4s → 1.1s（4 资产组合回测）

### 2. 收益率计算
- ✅ 累计收益率
- ✅ 年化收益率(CAGR)
- ✅ 波动率
- ✅ 夏普比率
- ✅ 最大回撤

### 3. 技术分析
- ✅ 移动平均线(MA5/10/20/60)
- ✅ RSI指标
- ✅ 布林带
- ✅ ATR(平均真实波幅) 与 前N日最低价

### 4. 多资产对比
- ✅ 归一化价格对比
- ✅ 批量收益率计算
- ✅ 统一时间范围

### 5. 网格策略 ★新增
- ✅ 跌破前120日最低价启动监控
- ✅ 网格间距 = 触发日ATR(120) × 可配置比例
- ✅ 每格买入固定金额（默认10000元，可配置）
- ✅ **网格轮次生命周期**：跌破前低 → 完全止盈 → 重新跌破才开新轮
- ✅ **总资产配置共享**：网格只监控再平衡组合中的资产
- ✅ 交易类型标记：开仓 / 加仓 / 减仓 / 止盈
- ✅ 网格持仓与再平衡持仓完全隔离
- ✅ 组合回测可分别归因两套策略收益

---

## 🚀 快速开始

### 安装依赖
```bash
pip install pandas akshare requests
```

### 运行测试
```bash
python -m pytest tests/test_price_domain.py -v
python -m pytest tests/test_grid_domain.py -v    # 网格策略（离线，无需网络）
```

### 运行示例
```bash
python examples/ddd_price_model_demo.py
python examples/grid_strategy_demo.py           # 网格策略示例
```

### 基本用法
```python
from src.application.price.query_service import PriceQueryService

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
from src.application.grid.grid_backtester import GridBacktester, GridConfig
from src.domain.allocation.entities import AllocationPlan
from src.domain.grid.value_objects import GridTrigger

# 总资产配置（再平衡与网格共用）
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

# 触发日志（资产代码/名称/触发日期/交易类型）
from src.application.grid.grid_chart import GridChartRenderer
print(GridChartRenderer.trigger_log(result))
```

---

## 📊 验证结果

### 单元测试
- **27/27** 测试通过 ✅
- 覆盖领域层、应用层、集成测试

### 关键数据
- **515180最新价**: 1.762 ✓
- **2020-2026累计收益**: 70.90%
- **年化收益**: 8.14%
- **最大回撤**: -17.31%

### 性能
- 首次获取: ~2-5秒（网络IO）
- 缓存命中: 毫秒级
- 收益率计算: 毫秒级

---

## 📖 文档导航

| 文档 | 用途 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | ⭐ 5分钟上手 |
| [README.md](README.md) | 项目概述 |
| [docs/ddd_price_model_design.md](docs/ddd_price_model_design.md) | DDD设计详解 |
| [docs/grid_strategy_ddd_design.md](docs/grid_strategy_ddd_design.md) | ★ 网格策略DDD设计 |
| [docs/price_cache_design.md](docs/price_cache_design.md) | ★ 价格缓存与增量同步设计 |
| [docs/price_sources_design.md](docs/price_sources_design.md) | ★ 多数据源容灾与复权口径设计 |
| [docs/ddd_implementation_summary.md](docs/ddd_implementation_summary.md) | 实现总结 |
| [docs/migration_summary.md](docs/migration_summary.md) | 迁移报告 |
| [docs/adjusted_price_provider_usage.md](docs/adjusted_price_provider_usage.md) | API详细文档 |

---

## 🎨 架构特点

### DDD分层架构
```
┌─────────────────────────┐
│   Application Layer     │  ← PriceQueryService
├─────────────────────────┤
│   Domain Layer          │  ← Entities, Value Objects, Services
├─────────────────────────┤
│   Infrastructure Layer  │  ← Repositories, Data Providers
└─────────────────────────┘
```

### 核心设计原则
- ✅ 聚合根保护不变量
- ✅ 值对象不可变
- ✅ 领域服务无状态
- ✅ 仓储接口抽象
- ✅ 业务规则内聚

---

## 🔧 技术栈

- **Python 3.11+**
- **pandas** - 数据处理
- **akshare** - 金融数据源
- **requests** - HTTP客户端
- **pytest** - 测试框架
- **Decimal** - 高精度计算

---

## ✨ 下一步

### 立即可用
- ✅ 绘制价格图表
- ✅ 计算收益率指标
- ✅ 多资产对比分析
- ✅ 技术指标查询

### 待开发
- ⏳ 策略回测框架
- ⏳ 数据库仓储
- ⏳ 实时价格推送
- ⏳ 更多技术指标

---

## 📞 支持

如有问题，请查阅：
1. [快速参考](QUICKSTART.md) - 常见问题
2. [使用指南](docs/adjusted_price_provider_usage.md) - API文档
3. [示例代码](examples/ddd_price_model_demo.py) - 实际用法

---

**最后更新**: 2026-11-06  
**版本**: v1.0  
**状态**: ✅ 生产就绪
