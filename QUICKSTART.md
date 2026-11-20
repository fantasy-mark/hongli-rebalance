# 价格模型快速参考

## 🚀 5分钟上手

### 安装依赖
```bash
pip install pandas akshare requests
```

### 基本用法

```python
from src.data.service import PriceQueryService

# 创建服务
service = PriceQueryService()

# 1. 获取价格数据
df = service.get_adjusted_prices("515180", start_date="2024-01-01")

# 2. 计算收益率
metrics = service.calculate_returns("515180", start_date="2020-01-01")
print(metrics.summary())

# 3. 多资产对比
df_compare = service.compare_assets(
    ["515180", "159937", "511380", "513300"],
    start_date="2024-01-01"
)

# 4. 最新价格
latest = service.get_latest_price("515180")
print(f"最新价: {latest['close']}")
```

---

## 📚 API速查

### PriceQueryService

| 方法 | 用途 | 返回 |
|------|------|------|
| `get_adjusted_prices(code, start, end)` | 获取价格数据 | DataFrame |
| `calculate_returns(code, start, end)` | 计算收益率指标 | ReturnMetrics |
| `get_latest_price(code)` | 获取最新价格 | Dict |
| `compare_assets(codes, normalize)` | 多资产对比 | DataFrame |
| `get_technical_indicators(code)` | 技术指标 | Dict |
| `invalidate_cache(code)` | 清除缓存 | None |

### ReturnMetrics

```python
metrics.total_return       # 累计收益
metrics.annualized_return  # 年化收益
metrics.volatility         # 波动率
metrics.sharpe_ratio       # 夏普比率
metrics.max_drawdown       # 最大回撤
metrics.summary()          # 可读摘要
```

---

## 🎯 常见场景

### 绘制图表
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
codes = ["515180", "159937", "511380", "513300"]
for code in codes:
    m = service.calculate_returns(code, start_date="2024-01-01")
    print(f"{code}: 累计{m.total_return:.2%}, 年化{m.annualized_return:.2%}")
```

### 使用技术指标
```python
indicators = service.get_technical_indicators("515180")
if indicators:
    print(f"MA20: {indicators['ma_20']:.3f}")
    print(f"RSI: {indicators['rsi_14']:.2f}")
```

---

## ⚠️ 注意事项

1. **价格口径**: 所有数据均为**后复权(hfq)**，最新价515180=1.762 ✓
2. **数据来源**: 默认东方财富，自动降级腾讯
3. **缓存**: 首次获取较慢，后续从缓存读取
4. **日期格式**: 使用 'YYYY-MM-DD' 字符串

---

## 📖 更多文档

- [项目结构](PROJECT_STRUCTURE.md)
- [价格模型设计](docs/ddd_price_model_design.md)
- [价格缓存设计](docs/price_cache_design.md)
- [使用示例](examples/ddd_price_model_demo.py)

---

## 🆘 常见问题

**Q: 为什么价格是1.762而不是1.369？**  
A: 1.762是正确的后复权(hfq)价格，1.369是不复权价格。东财的qfw参数对ETF无效。

**Q: 如何清除缓存？**  
A: `service.invalidate_cache("515180")` 或 `service.invalidate_cache()` 清除所有

**Q: 支持哪些资产？**  
A: ETF（5/1开头）、股票（6/0/3开头），自动识别类型

**Q: 数据更新频率？**  
A: 缓存24小时，可手动调用 `invalidate_cache()` 强制更新
