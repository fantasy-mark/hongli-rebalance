# 价格缓存与增量同步 DDD 设计

## 1. 问题背景

回测每次运行都要重新拉取全历史行情（单资产 1400~3000 条），
4 个资产的首跑成本高达数秒，且数据源不稳定时会重复失败。

目标：
- **无缓存** → 全量拉取一次并持久化
- **有缓存** → 只拉取缺失的**尾部**区间（最近交易日）
- 数据源不可用 → 降级用缓存，不影响回测
- 后复权价格**被重算**（分红除权）→ 自动识别并全量重建

## 2. 分层职责（均在数据模块 `src/data/` 内）

```
┌────────────────────────────────────────────────────────────┐
│ 编排层: PriceQueryService (service.py)                      │
│   编排：读缓存 → 问规划器 → 按计划拉取 → 合并 → 回写        │
└────────────────────────────────────────────────────────────┘
        │ 调用（只做编排，不做判断）
        ▼
┌────────────────────────────────────────────────────────────┐
│ 规则层（纯业务规则，无 IO）                                 │
│  · AdjustedPriceSeries.covers / merged_with                 │
│      / overlap_close_deviation        （聚合根行为）        │
│  · PriceCachePlanner.plan(...)        → RefreshPlan         │
│  · PriceSeriesMerger.merge(...)       → MergeResult         │
│  · CachePolicy / RefreshMode / RefreshPlan / MergeResult    │
└────────────────────────────────────────────────────────────┘
        │ 依赖抽象
        ▼
┌────────────────────────────────────────────────────────────┐
│ 适配层                                                       │
│  · PriceSeriesRepository（端口，含"完整历史"标记能力）      │
│    - InMemoryPriceRepository / JsonFilePriceRepository      │
│  · providers: 区间下推到数据源（东财 / 腾讯 / 新浪）        │
└────────────────────────────────────────────────────────────┘
```

## 3. 领域模型

### 3.1 `CachePolicy`（值对象）

| 属性 | 默认 | 含义 |
|---|---|---|
| `overlap_days` | 5 | 增量拉取时向回多取的日历天数，用于接缝校验 |
| `seam_tolerance` | 0.001 | 重叠区间收盘价相对偏差阈值，超过判定为口径变化 |
| `min_refresh_interval_hours` | 0 | 最小刷新间隔，避免同日重复访问数据源 |

### 3.2 `RefreshPlan`（值对象）

由 `PriceCachePlanner` 推导，应用层只执行：

| 模式 | 触发条件 | 拉取区间 |
|---|---|---|
| `FULL` | 无缓存 / 需向前扩展且缓存非完整历史 | 全历史（`start_date=None`） |
| `INCREMENTAL` | 缓存尾部过期 | `缓存末日 - overlap_days` ~ 目标结束日 |
| `NONE` | 缓存已覆盖 / 完整历史 / 最小刷新间隔内 | 不访问数据源 |

不变量：`INCREMENTAL` 必须带 `fetch_start`；`NONE` 不得带 `fetch_start`。

### 3.3 聚合根行为

```python
AdjustedPriceSeries.covers(start, end) -> bool
AdjustedPriceSeries.merged_with(update) -> AdjustedPriceSeries   # 按日期去重合并，不改自身
AdjustedPriceSeries.overlap_close_deviation(other) -> (点数, 最大偏差)
```

### 3.4 接缝校验（关键）

**后复权（hfq）价格会在分红除权时整体重算**，若直接把新拉取的尾部拼到旧缓存上，
序列会在接缝处出现价格断层，导致 ATR、前低、收益率全部失真。

因此增量拉取时故意**多取 `overlap_days` 天**，用重叠区间比对：

```
max_dev = max(|新收盘 - 旧收盘| / 旧收盘)
max_dev > seam_tolerance  →  口径已变  →  全量重建
max_dev <= seam_tolerance →  正常增量  →  合并
```

## 4. 关键实现细节

### 4.0 增量必须包含缓存最后交易日

缓存的最后一条 bar 可能是**盘中未收盘**的数据（历史接口盘中会写入当天的
不完整 bar）。因此增量拉取的起点必须**包含**该日，让它被重新拉取并覆盖：

```
fetch_start = 缓存末日 - overlap_days      （overlap_days 只为接缝校验提供重叠点）
```

同时，若缓存末条恰好就是"今天"且今天可能是交易日，
即使请求区间已被覆盖也会触发一次刷新（`CachePolicy.refresh_today`，默认开启）。

### 4.0.1 当日行情用实时接口补齐

历史 K 线接口（东财/腾讯/新浪）盘中往往**不包含当天**这条记录，
导致缓存永远落后一个交易日。因此新增实时报价源补齐：

| 源 | 接口 | 字段 |
|---|---|---|
| 新浪实时 | `hq.sinajs.cn/list=sh515180` | 开/昨收/现/高/低/量/额/日期/时间 |
| 腾讯实时 | `qt.gtimg.cn/q=sh515180` | 备用 |

实时报价是**不复权**口径，补齐时按

```
倍率 = 缓存最后交易日收盘价 / 报价昨收
当日价 = 实时价 × 倍率
```

换算到缓存口径。该倍率仅在"缓存末日 = 今日的前一交易日"时成立
（跨除权日会变化），因此换算前提不满足时**放弃补齐**并记录日志，
避免写入错误价格。

### 4.1 全量拉取不受请求区间影响

用户可能只请求 `start_date="2026-11-01"`。若此时按该区间拉取并存入缓存，
缓存就只剩 7 条，**后续其他调用会因数据不足而算错**（真实踩过这个坑：
一次窄请求让组合回测收益从 74% 变成 1.9%）。

规则：`FULL` 模式一律拉全历史，窄区间只影响**返回时切片**。

### 4.2 「完整历史」标记

资产上市较晚（如 511380 自 2020-04-07 起），而回测预热起点是 2020-03-10，
`requested_start < cached.start_date` 会一直成立，导致每次运行都全量重拉。

解决：仓储记录 `history_complete` 标记（随 JSON 持久化）。
全量拉取（无起始限制）成功后打标；规划器见到该标记即判定 `NONE`
——更早的数据源本就不存在。

### 4.3 失败降级

| 场景 | 行为 |
|---|---|
| 增量拉取抛异常 | 沿用缓存，打印 warning |
| 数据源返回空 | 沿用缓存 |
| 全量重建失败（口径冲突） | **保留原缓存，不用残缺窗口覆盖** |

### 4.4 数据源区间下推

`EastmoneyPriceSource.fetch()` 底层把 `start_date`/`end_date` 转为 `YYYYMMDD`
传给 `ak.fund_etf_hist_em`，使增量请求真正只下载尾部区间
（实测：增量 44 条 vs 全量 1668 条）。

### 4.5 反序列化性能

JSON 仓储改为**紧凑数组 + Decimal 存字符串**，并直接用 `date.fromisoformat`
构造 `PricePoint`，不再经过 `pd.to_datetime` 逐行与 DataFrame 中转。
热缓存读取 4 个资产由 ~3s 降到 **0.06s**。

## 5. 使用

```python
from src.data.value_objects import CachePolicy
from src.data.service import PriceQueryService

# 默认：JSON 文件缓存（data/price_cache），跨进程复用
service = PriceQueryService()

# 自定义策略：向回多取 10 天做校验，1 小时内不重复访问数据源
service = PriceQueryService(
    cache_policy=CachePolicy(overlap_days=10, min_refresh_interval_hours=1)
)

# 纯内存缓存（不落盘，适合测试）
service = PriceQueryService(persist_cache=False)

df = service.get_adjusted_prices("515180")            # 命中缓存 → 0 网络
df = service.get_adjusted_prices("515180", start_date="2024-01-01")  # 切片返回
```

回测引擎共享同一实例即可复用缓存：

```python
service = PriceQueryService()
RebalanceBacktester(cfg, price_service=service)
GridBacktester(grid_cfg, price_provider=service)
CombinedBacktester(combined_cfg, price_service=service)  # 内部已共享
```

## 6. 实测效果

4 资产组合回测（2020-11-05 ~ 2026-11-10）：

| 场景 | 耗时 | 说明 |
|---|---|---|
| 冷启动（无缓存） | 2.86s | 4 资产全量拉取 + 回测 |
| 热启动（命中缓存） | 1.13s | 零网络，仅回测计算 |
| 增量（缓存缺失 40 天） | +0.5s | 每资产仅拉 44 条 |

结果完全一致：组合收益 74.3238%，再平衡 88.8295%，网格 1.8314%。

## 7. 测试

```bash
python -m pytest tests/test_price_cache.py -v
```

27 个离线用例，覆盖：

- `covers` / `merged_with` / `overlap_close_deviation` 的边界
- 规划器五种分支（无缓存 / 已覆盖 / 增量 / 向前扩展 / 完整历史 / 最小间隔）
- 值对象不变量（INCREMENTAL 必须有 fetch_start 等）
- 合并器：口径变化判定、容差内不判定冲突
- 应用编排：缓存命中零拉取、增量只拉尾部、失败降级、冲突触发全量重建

## 8. 注意事项

1. **首次运行较慢**：需要建立缓存，之后显著加快。
2. **缓存目录**：`data/price_cache/*.json`（已在 `.gitignore` 中忽略）。
3. **删除缓存**：直接删除该目录即可，下次会自动重建。
4. **多进程并发**：当前实现没有文件锁，不要同时用多个进程写同一缓存。
5. **数据源口径变化**：接缝校验会在 1 个交易日内自动发现；若阈值设得过松
   （如 0.05）可能漏判，建议保持 0.001 量级。
