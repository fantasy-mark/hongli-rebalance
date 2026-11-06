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
from src.infrastructure.adjusted_price_provider import fetch_adjusted_kline

# 获取ETF后复权数据
df = fetch_adjusted_kline("515180", start_date="2023-01-01")

# 查看数据
print(df.head())
print(f"数据来源: {df['source'].iloc[0]}")

# 获取最新价格
price, source = get_latest_adjusted_price("515180")
print(f"最新收盘价: {price:.3f}")
```

---

## 项目结构

```
rebalance/
├── src/
│   └── infrastructure/
│       ├── adjusted_price_provider.py    # ⭐ 核心模块：后复权价格获取
│       └── quote_providers.py            # 现有模块：实时报价（不复权）
├── tests/
│   └── test_adjusted_price_provider.py   # 测试脚本
├── docs/
│   └── adjusted_price_provider_usage.md  # 详细使用文档
├── price_data_sources.md                 # 数据源清单与口径说明
└── README.md                             # 本文件
```

---

## 模块对比

| 特性 | `adjusted_price_provider.py` | `quote_providers.py` |
|------|------------------------------|---------------------|
| **用途** | 历史K线、图表、技术分析 | 实时报价、账本成本 |
| **价格口径** | 后复权（qfw） | 不复权 |
| **数据长度** | 全历史（东财）或~641根（腾讯） | 当日实时 |
| **适用场景** | 回测、收益率计算、曲线绘制 | 交易执行、持仓估值 |
| **资产类型** | ETF + 个股 | ETF + 个股 + 债券 |

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

### 主要函数

#### `fetch_adjusted_kline(code, start_date=None, end_date=None, adjust_type='qfw')`

获取后复权K线数据。

**参数：**
- `code` (str): 6位证券代码，如 `'515180'`
- `start_date` (str, optional): 起始日期 `'YYYY-MM-DD'`
- `end_date` (str, optional): 结束日期 `'YYYY-MM-DD'`
- `adjust_type` (str): `'qfw'`（前复权）或 `''`（不复权）

**返回：**
- `pd.DataFrame`: 索引为datetime，列包括 open/high/low/close/volume/amount/source

#### `get_latest_adjusted_price(code)`

获取最新后复权收盘价。

**返回：**
- `Tuple[float, str]`: (价格, 数据来源)

---

## 运行测试

```bash
python tests/test_adjusted_price_provider.py
```

测试内容包括：
- ✅ ETF后复权数据获取
- ✅ 个股后复权数据（含质量审计）
- ✅ 日期范围过滤
- ✅ 获取最新价格
- ✅ 批量获取多个资产
- ✅ 错误处理

---

## 文档

- 📖 [详细使用指南](docs/adjusted_price_provider_usage.md)
- 📋 [数据源清单](price_data_sources.md)

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
