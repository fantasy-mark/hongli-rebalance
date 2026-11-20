"""
数据模块

职责：把「行情数据」从外部取进来、缓存起来，并对外提供查询能力。

分层（按依赖方向，自下而上）：

| 文件 | 职责 |
|---|---|
| `models.py` | 价格聚合根与值对象（AssetCode / PricePoint / AdjustedPriceSeries） |
| `value_objects.py` | 指标类值对象（ReturnMetrics / TechnicalIndicators） |
| `indicators.py` | 收益率与技术指标计算（PriceCalculator） |
| `cache.py` | 缓存策略值对象 + 刷新规划 + 增量合并（接缝校验） |
| `sources.py` | 数据源规则：复权口径、源健康、源选择、口径对齐 |
| `providers.py` | 数据源适配器（东财 / 腾讯 / 新浪 / 实时报价） |
| `fallback.py` | 多源容灾：主源优先、失败降级（FallbackPriceProvider） |
| `cache_store.py` | 价格缓存落盘（内存 / JSON 文件） |
| `service.py` | 应用服务：编排缓存 + 数据源 + 计算，对外统一入口 |

对外统一入口：`PriceQueryService`（src/data/service.py）。
"""

__all__ = ["service", "models", "cache", "sources", "providers"]
