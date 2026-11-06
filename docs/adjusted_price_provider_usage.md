# 后复权价格K线模块使用指南

## 概述

`adjusted_price_provider.py` 是一个专门用于获取ETF/股票**后复权（前复权 qfw）**历史K线数据的模块。

### 核心特性

- ✅ **统一接口**：一个函数搞定所有资产类型的后复权数据
- ✅ **自动降级**：东方财富 → 腾讯，无缝切换
- ✅ **质量审计**：个股qfw数据自动检测异常
- ✅ **标准输出**：pandas DataFrame，带日期索引和OHLCV列
- ✅ **灵活过滤**：支持按日期范围筛选

---

## 快速开始

### 1. 基本用法

```python
from src.infrastructure.adjusted_price_provider import fetch_adjusted_kline

# 获取ETF完整历史后复权数据
df = fetch_adjusted_kline("515180")  # 红利ETF

# 获取指定时间范围
df = fetch_adjusted_kline("515180", start_date="2023-01-01", end_date="2024-12-31")

# 查看数据结构
print(df.head())
print(f"数据来源: {df['source'].iloc[0]}")
```

### 2. 返回数据格式

```
              open    high     low   close    volume     amount      source
date                                                                        
2023-01-03  1.234  1.256  1.230  1.245  12345678  15345678.9  eastmoney
2023-01-04  1.245  1.260  1.240  1.255  13456789  16789012.3  eastmoney
...
```

**列说明：**
- `open/high/low/close`: 开盘/最高/最低/收盘价（后复权）
- `volume`: 成交量（手）
- `amount`: 成交额（元）
- `source`: 数据来源标识（`eastmoney` 或 `tencent`）

---

## API 参考

### `fetch_adjusted_kline(code, start_date=None, end_date=None, adjust_type='qfw')`

获取后复权K线数据的主入口函数。

**参数：**
- `code` (str): 6位证券代码，如 `'515180'` 或 `'601088'`
- `start_date` (str, optional): 起始日期，格式 `'YYYY-MM-DD'`
- `end_date` (str, optional): 结束日期，格式 `'YYYY-MM-DD'`
- `adjust_type` (str): 复权类型
  - `'qfw'`（默认）: 前复权，消除除息缺口
  - `''`: 不复权，原始价格

**返回：**
- `pd.DataFrame`: 索引为datetime的DataFrame

**异常：**
- `ValueError`: 所有数据源都失败时抛出

---

### `get_latest_adjusted_price(code)`

获取最新的后复权收盘价。

**参数：**
- `code` (str): 6位证券代码

**返回：**
- `Tuple[float, str]`: (最新收盘价, 数据来源)

**示例：**
```python
price, source = get_latest_adjusted_price("515180")
print(f"最新价格: {price:.3f}, 来源: {source}")
```

---

## 数据源说明

### 优先级策略

| 顺位 | 数据源 | 优势 | 限制 |
|------|--------|------|------|
| 1 | 东方财富 | 全历史数据，稳定 | 个股qfw可能有质量问题 |
| 2 | 腾讯 | 作为备用可靠 | 最多~641根数据（约2.5年） |

### ETF vs 个股处理差异

**ETF：**
- 直接使用东财qfw数据
- 降级到腾讯qfw

**个股：**
- 使用东财qfw数据 + **自动质量审计**
- 如果审计发现异常（非除息日收益率偏差>1%），记录警告但仍返回数据
- 降级到腾讯qfw

> ⚠️ **重要提示**：根据 `price_data_sources.md` 文档，东财的个股qfw在某些股票上存在严重问题（如601088中国神华曾出现单日+48.66%的虚假涨幅）。本模块会审计并警告，但不会静默使用垃圾数据。

---

## 常见场景

### 场景1：绘制价格曲线

```python
import matplotlib.pyplot as plt
from src.infrastructure.adjusted_price_provider import fetch_adjusted_kline

# 获取多只ETF的后复权数据
codes = ["515180", "159937", "513300"]
for code in codes:
    df = fetch_adjusted_kline(code, start_date="2020-01-01")
    plt.plot(df.index, df["close"], label=code)

plt.legend()
plt.title("ETF后复权价格对比")
plt.show()
```

### 场景2：计算收益率

```python
from src.infrastructure.adjusted_price_provider import fetch_adjusted_kline

df = fetch_adjusted_kline("515180", start_date="2020-01-01")

# 累计收益率
start_price = df["close"].iloc[0]
end_price = df["close"].iloc[-1]
total_return = (end_price / start_price - 1) * 100

print(f"累计收益率: {total_return:.2f}%")

# 年化收益率
years = (df.index[-1] - df.index[0]).days / 365.25
cagr = ((end_price / start_price) ** (1 / years) - 1) * 100
print(f"年化收益率 (CAGR): {cagr:.2f}%")
```

### 场景3：获取特定时间段数据

```python
# 只获取2024年的数据
df_2024 = fetch_adjusted_kline(
    "515180",
    start_date="2024-01-01",
    end_date="2024-12-31"
)

# 检查数据来源
if df_2024["source"].iloc[0] == "tencent":
    print("⚠️ 警告：使用了腾讯数据，只有~641根，可能不完整")
```

### 场景4：批量获取多个资产

```python
from concurrent.futures import ThreadPoolExecutor

codes = ["515180", "159937", "511380", "513300"]

def fetch_one(code):
    return fetch_adjusted_kline(code, start_date="2023-01-01")

with ThreadPoolExecutor(max_workers=4) as executor:
    results = list(executor.map(fetch_one, codes))

for code, df in zip(codes, results):
    print(f"{code}: {len(df)} 条记录, 来源: {df['source'].iloc[0]}")
```

---

## 注意事项

### 1. 依赖安装

需要安装以下库：
```bash
pip install pandas akshare requests
```

### 2. 网络要求

- 需要访问国内金融数据接口（东方财富、腾讯）
- 建议在境内服务器运行，避免限流

### 3. 性能考虑

- 首次获取全历史数据可能需要几秒
- 腾讯数据源最快但窗口短
- 如需频繁调用，建议自行缓存结果

### 4. 数据口径一致性

⚠️ **重要**：根据 `price_data_sources.md` 第2节警告：

- **东财qfw ≠ 总收益指数**：qfw是"减法去分红"，不是复利再投资
- 对于高分红标的（如红利ETF），用qfw算收益率会**虚高**
- 如需精确收益率，应使用 `fetch_etf_total_return_history()` 自算总收益序列

本模块适用于：
- ✅ 图表展示（曲线连续）
- ✅ 相对价格比较
- ❌ 精确收益率计算（需用专门的总收益模块）

### 5. 个股质量审计

对于个股，模块会自动运行 `_audit_stock_qfq_quality()`：
- 比较qfw与不复权的日收益率差异
- 标记超过1%阈值的异常天数
- 在日志中输出警告信息

如果审计失败次数过多，建议：
1. 检查日志中的具体警告
2. 考虑切换到自算总收益方法
3. 或联系数据源提供方

---

## 故障排查

### 问题1：所有数据源都失败

**症状：**
```
ValueError: 无法获取ETF 515180 的后复权数据，所有数据源均失败
```

**解决：**
- 检查网络连接（能否访问 eastmoney.com / gtimg.cn）
- 检查代码是否正确（6位数字）
- 查看日志中的具体错误信息

### 问题2：腾讯数据只有641条

**症状：**
```
WARNING: ETF 515180 使用腾讯备用源（仅~641根数据）
```

**说明：**
- 这是正常行为，腾讯接口限制最多返回~641根
- 如果需要更长历史，确保东方财富可用
- 641根约覆盖2.5年日线数据

### 问题3：个股qfw审计警告

**症状：**
```
WARNING: 个股 601088 东财qfw数据质量不佳: 243天超过1%阈值
```

**说明：**
- 该股票的东财qfw数据可能存在错误
- 数据仍会返回，但需谨慎使用
- 建议对比其他数据源或使用自算总收益

---

## 与现有系统的集成

本模块设计为独立模块，可与现有 `quote_providers.py` 并存：

```python
# 旧系统（实时报价、不复权价格）
from src.infrastructure.quote_providers import latest_for_code

# 新系统（后复权K线、图表、分析）
from src.infrastructure.adjusted_price_provider import fetch_adjusted_kline
```

**分工建议：**
- `quote_providers.py`: 实时报价、账本成本、当日成交价（不复权）
- `adjusted_price_provider.py`: 历史K线、图表展示、技术分析（后复权）

---

## 扩展开发

如需添加新的数据源：

1. 实现 `_newsource_qfq(sina_symbol)` 函数，返回标准DataFrame
2. 在 `_fetch_etf_adjusted_kline()` 和 `_fetch_stock_adjusted_kline()` 中添加降级逻辑
3. 定义新的 `SOURCE_*` 常量
4. 更新本文档

参考 `price_data_sources.md` 第6节。

---

## 版本历史

- **v1.0** (2026-11-06): 初始版本，支持东财+腾讯双源，个股质量审计
