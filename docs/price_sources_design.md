# 多数据源容灾与复权口径设计

## 1. 背景

单一数据源（东方财富）存在**IP 限流**风险：

```
Push2RateLimitError: 东方财富 push2 行情服务器断开了连接，
通常是短时间内请求过多，当前 IP 被临时限流。
首次限流一般约 5 分钟后恢复，反复触发后可能需要 40 分钟以上。
```

限流期间所有回测都无法取数。因此引入**主备多数据源**。

## 2. 数据源对比（实测）

| 维度 | 东方财富（主） | 腾讯（备） | 新浪（第三） |
|---|---|---|---|
| 复权口径 | 后复权 hfq | **后复权 hfq** | 不复权 |
| 字段顺序 | 日期/开/高/低/收/量/额 | 日期/开/**收/高/低**/量 | 日期/开/高/低/收/量/额 |
| 成交量单位 | 手 | 手 | **股** |
| 成交额 | 有 | **无** | 有 |
| 历史深度 | 全历史 | **~640 根（约 2.5 年）** | 全历史 |
| 限流 | 有 | 无（实测） | 无（实测） |

实测校验（2026-11-12，515180）：

```
腾讯 volume = 5,049,234        （手）
新浪 volume = 504,923,400      （股）  = 腾讯 × 100
新浪 amount = 686,070,545 元   / volume(股) = 1.363 = 当日均价 ✓
```

## 3. 统一格式

所有源适配为同一形态，下游无感知：

```
索引:  date
列:    open / high / low / close / volume / amount / source
volume 单位: 手（与主源一致）
amount 单位: 元（缺失记 0）
```

## 4. 分层设计

```
┌──────────────────────────────────────────────────────────────┐
│ Application: PriceQueryService                               │
│   编排：读缓存 → 规划器 → provider 取数 → 口径对齐 → 合并    │
└──────────────────────────────────────────────────────────────┘
        │
        ├─────────────────────────┬───────────────────────────────
        ▼                         ▼
┌───────────────────────┐  ┌──────────────────────────────────┐
│ Domain（纯规则，无 IO）│  │ Infrastructure（IO）             │
│ · PriceAdjustment     │  │ · EastmoneyPriceSource           │
│ · SourceProfile       │  │ · TencentPriceSource             │
│ · SourceHealth        │  │ · SinaPriceSource                │
│ · PriceSourceSelector │  │   （统一格式 + 能力声明）         │
│ · PriceBasisAligner   │  │ · FallbackPriceProvider          │
│                       │  │   （探测 / 主备切换 / 降级）      │
└───────────────────────┘  └──────────────────────────────────┘
```

## 5. 使用前探测（用户要求）

每次取数前，**对主源与备用源分别拉取最近 2 个交易日**做健康检查：

| 检查项 | 规则 |
|---|---|
| 非空 | `frame` 不为空 |
| 行数 | `>= probe_trading_days`（默认 2） |
| 数值有效 | 收盘价均 > 0 |

结果按 `health_ttl_seconds`（默认 300s）缓存，避免每次取数都探测。

## 6. 主源优先 + 运行期降级

```
探测 → PriceSourceSelector（领域规则）→ 选第一个健康源
                │
                └─ 选中源正式取数失败 → 自动顺延到下一个健康源
```

选择规则（`PriceSourceSelector.select`）：
1. 按调用方的优先级顺序
2. 跳过不健康的源，记录 `skipped` 原因
3. **全历史请求时，跳过"历史有上限"的源**（如腾讯 640 根）
   —— 否则会把 640 根写入缓存，**静默截断历史**

## 7. 复权口径：一个关键的正确性约束

三种口径的数学关系：

```
后复权 hfq 与 前复权 qfq  =  同一条总收益序列的常数倍缩放
    hfq_t = k × qfq_t          （k 为常数，可用重叠交易日估算）

不复权 none 与复权价       ≠  常数倍率关系
    每逢除权除息会发生阶梯跳变，跨除权日用常数倍率对齐会引入系统性偏差
```

实测量化（640 个交易日）：

```
腾讯 hfq 末值 1.756 / 新浪 none 末值 1.363
常数倍率对齐后最大偏差 = 7.03%   ← 不可接受
```

因此领域层明确区分：

```python
PriceAdjustment.can_align_by_factor(other)
    HFQ ↔ QFQ   -> True
    复权 ↔ NONE -> False   # 本质不可换算
```

## 8. 缓存口径管理

缓存序列**必须口径同质**，因此：

| 场景 | 处理 |
|---|---|
| 空缓存 | 采用当前源口径并记录（非规范口径时**显式告警**） |
| 同口径 | 直接按接缝校验合并 |
| 不同口径 + 来源是**主源** | 直接全量重建（主源口径为规范口径） |
| 不同口径 + 来源是**备用源** | 用重叠区间估算倍率对齐后合并 |
| 不同口径 + **本质不可换算**（复权↔不复权） | **拒绝更新**，沿用旧缓存，等待主源恢复 |

口径记录在缓存 JSON 的 `adjustment` 字段（`hfq` / `qfq` / `none`）。

## 9. 实测效果

主源限流、缓存清空的情况下，4 资产组合回测仍可完成：

```
源选择：eastmoney 不可用 → 选 sina（全历史，不复权）
缓存口径：none（日志告警：分红收益可能被低估）
组合收益 64.9207%  再平衡 77.7062%  网格 1.0250%
```

对照主源（后复权）正常时的 74.32%，差异来自**不复权口径低估分红收益**——
这正是口径必须显式记录、并在主源恢复后重建的原因。

## 10. 配置与扩展

```python
from src.infrastructure.price.fallback_provider import FallbackPriceProvider
from src.infrastructure.price.sources import (
    EastmoneyPriceSource, TencentPriceSource, SinaPriceSource)
from src.application.price.query_service import PriceQueryService

provider = FallbackPriceProvider(
    sources=[EastmoneyPriceSource(),   # 主
             TencentPriceSource(),     # 备（同口径）
             SinaPriceSource()],       # 兜底（需口径转换）
    probe_trading_days=2,              # 使用前探测最近 2 个交易日
    probe_lookback_days=15,
    health_ttl_seconds=300,
)

service = PriceQueryService(price_provider=provider)
```

新增数据源只需：
1. 实现 `PriceSource` 协议（`source_id` / `adjustment` /
   `provides_full_history` / `max_bars` / `fetch`）
2. 在 `fetch` 内部把原始响应整理成统一格式
3. 加入 `sources` 列表（按优先级）

领域层规则（选择、对齐、合并）无需改动。

## 11. 注意事项

1. **腾讯只有 ~640 根**：只适合增量补数，不适合建立全历史缓存；
   全历史请求会自动跳过它。
2. **新浪是不复权**：口径不同，不能与复权缓存拼接；
   仅在无其他源时用于建立缓存，且会告警。
3. **探测有成本**：每次探测会请求所有源，因此有 TTL 缓存（默认 5 分钟）。
4. **腾讯不返回成交额**：`amount` 记 0，目前无指标依赖该字段。
5. **成交量单位已统一为「手」**：新浪原始为股，适配时除以 100。
