# 数据获取 API 指南（数据模块）

> 本模块提供**后复权（hfq）**行情数据：多源容灾 + 增量缓存。
> 设计细节见 [price_sources_design.md](price_sources_design.md) 与 [price_cache_design.md](price_cache_design.md)。

---

## 1. 快速开始

统一入口是 `PriceQueryService`（`src/data/service.py`）：

```python
from src.data.service import PriceQueryService

service = PriceQueryService()

# 获取后复权 K 线（默认全历史，命中缓存时只补增量）
df = service.get_adjusted_prices("515180", start_date="2023-01-01")

print(df.head())
print(f"数据来源: {df['source'].iloc[0]}")
```

返回的 DataFrame：

| 列 | 含义 | 单位 |
| --- | --- | --- |
| `open` / `high` / `low` / `close` | 后复权价 | 元 |
| `volume` | 成交量 | 手 |
| `amount` | 成交额 | 元 |
| `source` | 数据来源标识 | - |

索引是日期（`datetime64`）。

---

## 2. API 参考

### `PriceQueryService`

| 方法 | 用途 | 返回 |
| --- | --- | --- |
| `get_adjusted_prices(code, start_date, end_date, use_cache)` | 获取后复权 K 线 | `DataFrame` |
| `calculate_returns(code, start_date, end_date)` | 收益率指标 | `ReturnMetrics` |
| `get_latest_price(code)` | 最新价格与涨跌幅 | `Dict` |
| `compare_assets(codes, start_date, end_date, normalize)` | 多资产对比 | `DataFrame` |
| `get_technical_indicators(code, index)` | 技术指标 | `Dict` 或 `None` |
| `invalidate_cache(code)` | 清除缓存（`None` = 全部） | `None` |

### `ReturnMetrics`

```python
metrics.total_return       # 累计收益
metrics.annualized_return  # 年化收益 (CAGR)
metrics.volatility         # 年化波动率
metrics.sharpe_ratio       # 夏普比率
metrics.max_drawdown       # 最大回撤（负值）
metrics.summary()          # 可读摘要
```

### 数据源适配器（`src/data/providers.py`）

| 源 | 复权口径 | 历史深度 | 备注 |
| --- | --- | --- | --- |
| 东财 `EastmoneyPriceSource` | 后复权 hfq | 全历史 | 主源，有 IP 限流风险 |
| 腾讯 `TencentPriceSource` | 后复权 hfq | ~640 根 | 备源，与主源同口径可直接拼接 |
| 新浪 `SinaPriceSource` | 不复权 | 全历史 | 兜底，需按倍率对齐到缓存口径 |
| 实时 `RealtimeQuoteSource` | 不复权 | 仅当日 1 根 | 只用于补齐"当日"K 线 |

```python
from src.data.providers import EastmoneyPriceSource

df = EastmoneyPriceSource().fetch("515180", start_date="2023-01-01")
```

### 主备容灾（`src/data/fallback.py`）

```python
from src.data.fallback import FallbackPriceProvider

provider = FallbackPriceProvider()
fetched = provider.fetch("515180", start_date="2024-01-01")

print(fetched.source_id)     # 实际使用的源
print(fetched.adjustment)    # 该源的复权口径
print(fetched.frame)         # 统一格式 DataFrame
```

行为：

1. **使用前探测**：对主备源分别拉最近 N 个交易日，判断可用性
2. **主源优先**：按优先级选第一个健康的源（选择规则在 `src/data/sources.py`）
3. **运行期降级**：选中的源正式拉取失败时，自动顺延到下一个健康源
4. **口径对齐**：源的复权口径与缓存不一致时，先换算再合并

---

## 3. 缓存行为

- 缓存目录：`data/price_cache/<代码>.json`
- 缓存口径统一为**后复权**，并记录在 JSON 的 `adjustment` 字段
- 命中缓存时只拉缺失的尾部区间（增量），实测 44 条 vs 全量 1668 条
- **接缝校验**：后复权价会因分红除权被整体重算，合并前会检查重叠区偏差；
  偏差超阈值则改为**全量重建**，避免历史被静默截断
- 数据源不可用时沿用已有缓存，不抛错

```python
service.invalidate_cache("515180")   # 清除单个资产缓存
service.invalidate_cache()            # 清除全部缓存
```

---

## 4. 常见场景

### 绘制价格曲线

```python
import matplotlib.pyplot as plt

df = service.get_adjusted_prices("515180", start_date="2024-01-01")
plt.figure(figsize=(12, 6))
plt.plot(df.index, df['close'], label='515180')
plt.title('红利ETF后复权价格')
plt.legend()
plt.show()
```

### 计算并对比收益率

```python
for code in ["515180", "159937", "511380", "513300"]:
    m = service.calculate_returns(code, start_date="2024-01-01")
    print(f"{code}: 累计{m.total_return:.2%}, 年化{m.annualized_return:.2%}")
```

### 技术指标

```python
indicators = service.get_technical_indicators("515180")
if indicators:
    print(f"MA20: {indicators['ma_20']:.3f}")
    print(f"RSI: {indicators['rsi_14']:.2f}")
```

---

## 5. 注意事项

1. **口径**：缓存与主源都是后复权(hfq)，最新价 515180 ≈ 1.762
2. **网络**：需要访问国内金融接口（eastmoney.com / gtimg.cn / hq.sinajs.cn）
3. **首次较慢**：首次拉全历史需要几秒，之后命中缓存毫秒级
4. **日期格式**：入参用 `'YYYY-MM-DD'` 字符串
5. **ETF 为主**：当前数据源链面向 ETF；个股需另接数据源

---

## 6. 故障排查

**Q: 所有数据源都失败？**
A: 查看日志中的 `source_id` 与失败原因；网络不通或被限流时会退化为「沿用缓存」。
可用 `FallbackPriceProvider.probe(code)` 单独探测各源健康状态。

**Q: 腾讯源数据只有 ~640 条？**
A: 腾讯接口本身只返回约 640 根（约 2.5 年）。主源不可用时它的历史深度不够，
缓存会记录 `history_complete` 标记，避免反复全量重拉。

**Q: 日志提示「检测到后复权口径变化，改为全量重建」？**
A: 后复权价在分红除权时会整体重算，属于正常现象：系统会自动全量重建缓存，
并保证重建结果不比原缓存短（否则保留原缓存）。

**Q: 如何排查这批数据来自哪个源？**
A: DataFrame 的 `source` 列会标记来源（`eastmoney` / `tencent` / `sina` / `realtime`）。

---

## 7. 扩展新数据源

1. 在 `src/data/providers.py` 实现 `PriceSource` 协议：
   `source_id` / `adjustment` / `provides_full_history` / `max_bars` / `fetch()`
2. `fetch()` 返回统一格式 DataFrame（索引 date，列 open/high/low/close/volume/amount）
3. 在 `default_sources()` 中按优先级加入
4. 补充测试：`tests/test_price_sources.py`
