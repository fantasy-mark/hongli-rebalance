# Rebalance - ETF/股票后复权价格数据模块

## 项目概述

本项目提供一个专门用于获取ETF和股票**后复权（前复权 qfw）**历史K线数据的独立模块。

### 核心功能

- 📊 **统一接口**：一个函数获取任意ETF/个股的后复权K线
- 🔄 **自动降级**：东方财富（主）→ 腾讯（备），无缝切换
- 🔍 **质量审计**：个股qfw数据自动检测异常并警告
- 📦 **标准输出**：pandas DataFrame，包含OHLCV完整数据
- 🎯 **灵活过滤**：支持按日期范围筛选数据

---

## 快速开始

### 安装依赖

```bash
pip install pandas akshare requests
```

### 基本使用

```python
from src.data.service import PriceQueryService

service = PriceQueryService()

# 获取后复权 K 线（命中缓存时只补增量）
df = service.get_adjusted_prices("515180", start_date="2023-01-01")

# 查看数据
print(df.head())
print(f"数据来源: {df['source'].iloc[0]}")

# 最新价格
latest = service.get_latest_price("515180")
print(f"最新收盘价: {latest['close']}")
```

---

## 项目结构

按**业务模块**划分，每个模块内部再按职责分层（规则 / 编排 / 适配），
依赖方向单向：`chart → strategy → data`。

```
rebalance/
├── src/
│   ├── data/                 # 数据模块：数据获取源 + 缓存
│   │   ├── models.py         # 价格聚合根与值对象
│   │   ├── indicators.py     # 收益率 / 技术指标计算
│   │   ├── cache.py          # 缓存策略 + 刷新规划 + 增量合并
│   │   ├── sources.py        # 数据源规则（复权口径 / 选源 / 对齐）
│   │   ├── providers.py      # 数据源适配器（东财 / 腾讯 / 新浪 / 实时）
│   │   ├── fallback.py       # 主备容灾：探测 + 降级
│   │   ├── adjusted.py       # 后复权计算（分红再投资）
│   │   ├── cache_store.py    # 价格缓存落盘（内存 / JSON）
│   │   └── service.py        # PriceQueryService（对外统一入口）
│   ├── strategy/             # 策略模块：交易策略 + 规则
│   │   ├── allocation.py     # 总资产配置（两策略共用）
│   │   ├── portfolio.py      # 组合与持仓（两策略隔离）
│   │   ├── performance.py    # 统一绩效口径
│   │   ├── spec.py           # 策略规格（名 → 规则 → 实现）
│   │   ├── ledger/           # 交易流水账本
│   │   ├── grid/             # 网格策略（models / engine / backtest / spec）
│   │   ├── rebalance/        # 再平衡策略（rules / backtest / spec）
│   │   └── combined.py       # 组合回测（持仓隔离）
│   └── chart/                # 图表模块：图片 + 表格 + 网页
│       ├── models.py         # 图表模型
│       ├── renderer.py       # 图片生成器（通用曲线图）
│       ├── overview.py       # 策略全景图模型
│       ├── overview_renderer.py  # 图片生成器（全景图）
│       ├── grid_chart.py     # 图片生成器（网格多面板）
│       ├── tables.py         # 表格数据（触发日志 / 成交明细）
│       ├── site.py           # 网页生成器（视图模型 + 用例）
│       └── site_store.py     # 站点产物落盘
├── tests/                    # 单元测试（9 个文件，245 个用例）
├── examples/                 # 使用示例
├── scripts/                  # build_pages.py / verify_site.py
└── docs/                     # 设计文档
```

详见 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)。

---

## 🎯 网格策略（DDD）

在价格模型与再平衡策略之上，新增了独立的**网格策略上下文**：

| 规则 | 说明 | 配置项 |
|------|------|--------|
| 监控启动 | 资产价格跌破**前 120 日最低价**（不含最近交易日） | `lookback_days` |
| 网格间距 | **触发日 120 日 ATR × 比例**（轮内固定） | `atr_period` / `step_ratio` |
| 单格买入 | 每次下跌一个间距买入固定金额（默认 10000 元） | `buy_amount` |
| 网格轮次 | 跌破前低 → 本轮**完全止盈**结束；**重新跌破前低**才开下一轮 | `take_profit_ratio` |
| 持仓隔离 | 网格资金池与网格持仓独立于再平衡组合 | `initial_capital` |
| 共享配置 | 两个策略共用同一份**总资产配置**（网格只监控再平衡的资产） | `AllocationPlan` |

```python
from decimal import Decimal
from src.strategy.grid.backtest import GridBacktester, GridConfig
from src.strategy.allocation import AllocationPlan
from src.strategy.grid.models import GridTrigger

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
        take_profit_ratio=Decimal("1"),  # 上涨一个步长止盈
    ),
    initial_capital=Decimal("200000"),
)

result = GridBacktester(config).run()
print(result.summary())

# 表格数据：触发日志（资产代码 / 名称 / 触发日期 / 交易类型）
from src.chart.tables import grid_trigger_log, grid_trade_table
print(grid_trigger_log(result))
print(grid_trade_table(result))
```

组合回测（共享总资产配置 + 持仓隔离）：

```python
from src.strategy.combined import CombinedBacktester, CombinedConfig

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

📖 详见 [docs/grid_strategy_ddd_design.md](docs/grid_strategy_ddd_design.md)

---

## 数据源对比

| 特性 | 东财（主） | 腾讯（备） | 新浪（兜底） | 实时报价 |
|------|-----------|-----------|-------------|---------|
| **用途** | 历史K线 | 历史K线 | 历史K线 | 补齐当日 |
| **价格口径** | 后复权 | 后复权 | 不复权（需对齐） | 不复权（需换算） |
| **数据长度** | 全历史 | ~640 根 | 全历史 | 当日 1 根 |
| **适用场景** | 回测、收益率计算、曲线绘制 | 主源降级 | 兜底 | 当日 bar 补齐 |

---

## 数据源说明

### 优先级策略

1. **东方财富**（首选）
   - ETF: `ak.fund_etf_hist_em(adjust="qfw")`
   - 个股: `ak.stock_zh_a_hist(adjust="qfw")`
   - 优势：全历史数据，稳定可靠
   - 限制：部分个股qfw数据可能有质量问题

2. **腾讯**（备用）
   - 接口：`web.ifzq.gtimg.cn/appstock/app/fqkline/get`
   - 优势：作为备用源可靠
   - 限制：最多返回~641根数据（约2.5年）

### 重要提示

⚠️ **根据 `price_data_sources.md` 的警告：**

- 东财的**个股qfw**在某些股票上存在严重问题（如601088曾出现单日+48.66%的虚假涨幅）
- 本模块会对个股进行**自动质量审计**，发现异常会记录警告
- 对于高分红标的，qfw ≠ 总收益指数，精确收益率计算需用专门的总收益模块

---

## API 参考

统一入口是 `PriceQueryService`：

#### `get_adjusted_prices(code, start_date=None, end_date=None, use_cache=True)`

获取后复权 K 线数据。

**参数：**
- `code` (str): 6 位证券代码，如 `'515180'`
- `start_date` (str, optional): 起始日期 `'YYYY-MM-DD'`
- `end_date` (str, optional): 结束日期 `'YYYY-MM-DD'`
- `use_cache` (bool): 是否走缓存（默认 True，命中时只补增量）

**返回：**
- `pd.DataFrame`: 索引为日期，列包括 open/high/low/close/volume/amount/source

#### `calculate_returns(code, start_date=None, end_date=None)`

计算收益率指标，返回 `ReturnMetrics`（累计 / 年化 / 波动率 / 夏普 / 最大回撤）。

#### `get_latest_price(code)`

获取最新价格与涨跌幅，返回 `Dict`。

---

## 本地启动

本项目是**纯静态前端 + 一个轻量 Python 服务**（无 Node、无构建步骤）。

### 1. 生成站点数据（首次或数据更新后）

```bash
python scripts/build_pages.py
```

产出（均在 `data/site/`）：

| 文件 | 内容 |
|---|---|
| `summary.json` | 策略汇总（组合 / 再平衡 / 网格）+ 最新价 + 资产名称 |
| `backtest_trades.json` | 回测交易记录 |
| `prices.json` | 各资产最新价及其日期（`price_dates`） |
| `grid_chart.png` | 策略全景图 |

> ⚠️ 不会改写 `data/trades.json`（真实交易账本）。

### 2. 启动服务

```bash
python scripts/local_serve_with_api.py
```

浏览器打开 **http://localhost:8000**

- 前端：纯静态页面，由同一个服务托管
- 后端：`POST /api/trades`，把交易表单的记录追加到 `data/trades.json`
- 超额建仓（现金不足）等校验不通过时返回 HTTP 400，**账本不落盘**

只预览静态页面（不需要录入交易）可用：

```bash
python scripts/local_serve.py
```

### 3. 停止服务

前台运行时按 `Ctrl+C`；后台运行时：

```bash
# Windows
powershell -Command "Stop-Process -Name python -Force"
# Linux / macOS
pkill -f local_serve_with_api.py
```

### 端口被占用

`OSError: [WinError 10048]` 表示 8000 端口被占（常见于旧进程没退干净）：

```bash
powershell -Command "Stop-Process -Name python -Force"   # 清掉旧进程后重启
```

### 端口可配吗？

目前固定 `PORT = 8000`（`scripts/local_serve_with_api.py` 顶部），需要改端口时改这一行。

---

## 运行测试

```bash
python -m pytest tests/ -v
```

测试内容包括：
- ✅ 价格模型、收益率与技术指标
- ✅ 缓存规划 / 增量合并 / 接缝校验
- ✅ 数据源选择 / 复权口径对齐 / 主备降级
- ✅ **最新价必须是最近交易日**（冲突换源 / 口径对齐 / 实时报价补齐）
- ✅ 网格规则与回测（离线）
- ✅ 交易账本与持仓不变量
- ✅ **账本保护**（构建流程不得改写 `data/trades.json`）
- ✅ 站点视图模型与全景图渲染

校验脚本（不依赖 pytest）：

```bash
python scripts/verify_ledger_stats.py   # Python 与前端统计口径一致
node scripts/verify_page_render.js      # 页面渲染结果与账本一致
```

---

## 文档

- 📖 [数据获取 API 指南](docs/data_api_guide.md)
- 📖 [价格缓存设计](docs/price_cache_design.md)
- 📖 [数据源容灾设计](docs/price_sources_design.md)
- 📖 [项目结构](PROJECT_STRUCTURE.md)

---

## 注意事项

### 1. 网络要求
- 需要访问国内金融数据接口（eastmoney.com, gtimg.cn）
- 建议在境内服务器运行

### 2. 性能考虑
- 首次获取全历史数据可能需要几秒
- 如需频繁调用，建议自行缓存结果

### 3. 数据口径
- **后复权（qfw）**适用于：图表展示、相对价格比较
- **不适用于**：精确收益率计算（需用总收益模块）
- 原因：东财qfw是"减法去分红"，不是复利再投资的总收益指数

### 4. 依赖库
```
pandas >= 1.0
akshare >= 1.0
requests >= 2.0
```

---

## 扩展开发

如需添加新数据源，参考 `price_data_sources.md` 第6节：

1. 实现 `_newsource_qfq(sina_symbol)` 函数
2. 在降级链中添加新源
3. 定义新的 `SOURCE_*` 常量
4. 补充测试用例

---

## 版本历史

- **v1.0** (2026-11-06): 初始版本
  - 支持ETF和个股后复权数据获取
  - 东财+腾讯双源自动降级
  - 个股质量审计功能
  - 完整的测试和文档

---

## 许可证

本项目遵循与原项目相同的许可证。

---

## 联系方式

如有问题或建议，请参考原有项目的沟通渠道。
