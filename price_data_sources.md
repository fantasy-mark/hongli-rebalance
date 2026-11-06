# K 线 / 价格数据源清单

> 代码审计日: 2026-11-06 ｜ 唯一出口: `src/infrastructure/quote_providers.py`
> 本文件回答一个问题: **这个项目从哪儿拿价格, 以及每一份价格是什么口径。**

---

## 0. 一句话

所有价格都走**三家免费公开源**: 东方财富 (主)、腾讯 (备)、新浪 (兜底)。
`src/infrastructure/quote_providers.py` 是唯一 import `akshare` 的生产模块
(例外: `chart_renderer.py` 只为画沪深 300 基准线直接调了一次 akshare)。
**没有任何本地行情库/付费源**, 也没有把历史 K 线落盘成文件 ——
`data/history.json` 存的是**每日组合快照**, 不是行情。

不装 `pandas`/`akshare` 的那两个 GitHub Actions job
(`scripts/apply_trade.py` / `scripts/apply_grid.py`) **完全不碰价格** ——
重量级取价全部留在装了 pandas 的 job 里 (`src/infrastructure/paths.py` 的 D13 约束)。

---

## 1. 数据源总表

### 1.1 历史日线 (K 线) —— 真正的「价格序列」

| 函数 | 底层接口 | 实际端点 | 口径 | 长度 | 位次 |
|---|---|---|---|---|---|
| `_eastmoney(code, adjust="qfq")` | `ak.fund_etf_hist_em` | `push2his.eastmoney.com/api/qt/stock/kline/get` | **前复权** | 2010 起全历史 | ETF **首选** |
| `_eastmoney(code, adjust="")` | 同上 | 同上 | 不复权 | 同上 | 成本基准用 |
| `fetch_stock_qfq_history(code)` | `ak.stock_zh_a_hist(adjust="qfq")` | `push2his.eastmoney.com/api/qt/stock/kline/get` (33./63. 镜像) | **前复权** | 1990 起全历史 | 个股首选 |
| `_tencent_qfq(sina)` | 自写 `requests` | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | 前复权 | **只有 ~641 根** | ETF 第二 |
| `_sina(sina)` | `ak.fund_etf_hist_sina` | `vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php` | ⚠️ **不复权** | 全历史 | ETF 最后兜底 |
| `fetch_kline_history(code)` | 自写 `requests` | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | 腾讯 qfq | ~641 根 | 网格触发 (要 low) |

> `fetch_qfq_history` / `fetch_qfq_history_ex` 是 **ETF 日线** 的门面:
> `_fetch_qfq()` 内部按 东财 qfq → 腾讯 qfq → 新浪不复权 的顺序降级,
> 并把「这次到底用了哪个源」作为 `SOURCE_*` 常量一起返回, 让图表能喊出来。

### 1.2 实时报价 —— 今天这一刻的成交价

| 函数 | 底层 | 端点 | 口径 | 备注 |
|---|---|---|---|---|
| `_eastmoney_universal_quote(code)` | 自写 `requests` | `push2.eastmoney.com/api/qt/stock/get` | 不复权 | **主源**, 覆盖个股/ETF/债券 ETF (511380) |
| `_eastmoney_etf_spot(code)` | `ak.fund_etf_spot_em` | `88.push2.eastmoney.com/api/qt/clist/get` | 不复权 | **不含 511380** 这类债券 ETF |
| `_tencent_quote(code)` | 自写 `requests` | `qt.gtimg.cn/q=sh######` | 不复权 | push2 被限流时顶上来 |

`latest()` = push2 全市场 → ETF 实时表 → 腾讯 → **历史末根** (`latest_history`)。
`latest_for_code(code)` 是领域层 `PriceProvider` 的适配器, 每日快照走这条。

### 1.3 分红 / 净值 —— 不产价格, 但决定收益率口径

| 函数 | 底层 | 端点 | 用途 |
|---|---|---|---|
| `_stock_dividend_events(code)` | `ak.stock_history_dividend_detail` | `vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/###.phtml` | 个股除权除息日 + 每股派息/送转 |
| `fund_dividends(code)` | `ak.fund_fh_em` | `fund.eastmoney.com/Data/funddataIndex_Interface.aspx` | 全市场基金分红表 (~74 页, 进程内缓存) |
| `fetch_qdii_premium(code)` | 自写 `requests` | `api.fund.eastmoney.com/f10/lsjz` + `qt.gtimg.cn` | QDII T-1 净值 vs 实时价 → 溢价率 |
| `_fetch_csi300()` (chart_renderer) | `ak.stock_zh_index_daily("sh000300")` | 新浪指数日线 | 图表基准线 |

### 1.4 自算总收益序列 —— 唯一能拿来算收益率的口径

| 函数 | 价格来源 | 分红来源 | 股息税 |
|---|---|---|---|
| `fetch_stock_total_return_history(code, dividend_tax_bps=...)` | 东财**不复权**日线 | 新浪分红送转表 | 个人: ≤1 月 20% / 1 年 10% / >1 年 0% |
| `fetch_etf_total_return_history(code)` | 新浪**不复权**日线 | 东财基金分红表 | **0** (基金层面已扣) |

算法: `份额 ×(1+送转) + 份额 × 现金分红 ×(1−税) / 除息价`, 再按 qfq 惯例
把末值归一到真实收盘价 —— 所以能跟其它 qfq 序列**直接拼接**。

---

## 2. 三个口径, 不能混用

| 口径 | 谁在用 | 为什么 |
|---|---|---|
| **前复权 qfq** | 图表曲线、多年回测、`latest_history` | 消除除息缺口, 曲线连续 |
| **不复权** | `fetch_traded_close`、实时报价、账本成本 | 用户当天**付的就是这个价** |
| **自算总收益** | 算收益率 / CAGR / 夏普 | 东财 qfq ≠ 总收益 (见下) |

### ⚠️ 坑 1: 东财的「前复权」是**加法减分红**, 不是总收益指数

`qfq_t = 不复权价_t − (t 之后还没发的那几笔分红)`。拿它的**比值**当收益率会虚高。
实测 515180 红利 ETF, 2020-11 → 2026-11:

| 序列 | 累计收益 |
|---|---|
| 东财 qfq | +77.63% |
| **自算总收益** | **+59.40%** |
| 官方累计净值 | +56.97% ← 权威 |
| 不复权 (分红全丢) | +23.39% |

⇒ **分红标的用东财 qfq 算收益, 六年虚高 20.7pp。**
篮子里黄金 / 可转债 / 纳指期间不分红, 所以这个坑只被红利 ETF 暴露出来。

### ⚠️ 坑 2: 账本成本基准必须不复权

红利 ETF 2026-10-16 除息, 10-12 的 qfq 价比当天真实成交价低 ¥0.061/份 (4.4%)。
`fetch_traded_close(code, date)` 专门负责给「用户当天付的钱」,
降级链: 东财不复权 → 新浪不复权 → 抛错。

### ⚠️ 坑 3: 东财的个股 qfq 可能是坏的

实测 601088 中国神华: 非除息日里 qfq 日收益与不复权日收益差 >1pp 的有**数百天**
(代码里两处注释分别记「538 天 / 最大 38.67pp」和「243 天 / 最大 21.63pp」,
实测次数尚未统一), 甚至出现过 **+48.66%** 的单日「涨幅」(主板涨停才 10%)。
600900 / 000887 / 601398 同一项检查是 0.10~0.21pp 均值, 干净。
⇒ **用之前先跑 `audit_stock_qfq(code)`**, 审计不过就换自算总收益, 别静默用垃圾数据。

---

## 3. 生产调用链

```
GitHub Actions (每日 9:00 北京)
└─ src/application/daily_snapshot.py
   ├─ value positions ──→ latest_for_code() ──→ latest()
   │                       push2 全市场 → ETF 实时表 → 腾讯 → 历史末根
   ├─ _intraday_lows() ──→ fetch_kline_history(today, today)   # 网格触发要「今日最低价」
   └─ _enrich_qdii_premiums() ──→ fetch_qdii_premium()          # 513300 溢价告警

图表 job
└─ src/infrastructure/chart_renderer.py
   ├─ _load_chart_assets() ──→ holdings.xlsx / ETF_DEFS         # 这次画哪几只
   ├─ fetch_qfq_history_ex() ──→ _fetch_qfq()                   # 东财 qfq → 腾讯 qfq → 新浪(⚠️不复权)
   └─ _fetch_csi300() ──→ ak.stock_zh_index_daily("sh000300")   # 基准线
```

图表那侧**必须**在日志里喊出来: 走了腾讯 (窗口短) 或新浪 (不复权) 都会 `WARN`,
因为「口径不一致的图比没有图更糟」(与 `data/history.json` 的实时市值对不上)。

---

## 4. 资产清单 (改这里就能加资产)

`src/domain/portfolio/asset_universe.py`:

| 代码 | 新浪符号 | 东财代码 | 名称 | QDII |
|---|---|---|---|---|
| 515180 | sh515180 | 515180 | 红利ETF易方达 | |
| 159937 | sz159937 | 159937 | 黄金ETF博时 | |
| 511380 | sh511380 | 511380 | 可转债ETF博时 | |
| 513300 | sh513300 | 513300 | 纳斯达克ETF华夏 | ✅ |

符号推导规则 (无注册表时): 6 位码首位 `5/6/9` → 上交所 (`sh` / secid `1.`),
其余 → 深交所 (`sz` / secid `0.`)。

---

## 5. 已知限制与可疑点

1. **腾讯 `fqkline` 最多 ~641 根** —— `count` 填 1000/1600/2000 都只给 641,
   带日期区间直接 `param error`。所以它只能当第二顺位 (够画近两年, 不够画六年)。
2. **新浪 ETF 日线没有复权参数** —— 只能兜底, 且会污染收益率 (CAGR 11.95% → 10.03%)。
3. **`ak.fund_etf_spot_em` 不含债券 ETF** (511380) —— 所以实时价第一顺位必须是
   push2 全市场接口, 不能只靠 ETF 实时表。
4. **push2his 在国内 ISP 常被限流** —— 腾讯 K 线正是为此存在的备胎;
   `fetch_kline_history` 带 3 次重试 + 递增退避。
5. **`fetch_kline_history` 的 docstring 与代码不一致** —— 注释说「返回不复权 OHLC」,
   实际请求参数是 `,day,,,1000,qfq` (前复权)。对「相对低点」比较影响不大
   (同一窗口内口径自洽), 但注释该改。
6. **无本地行情缓存** —— 每次跑都打网络; 只有 `fund_dividends` 有进程内缓存
   (`_FUND_FH_CACHE`), 因为 `fund_fh_em` 要翻 ~74 页。
7. **`quote_providers.py` 内部有两套 601088 审计数字** —— 模块 docstring 说
   「538 天 / 38.67pp」, `fetch_stock_total_return_history` docstring 说
   「243 天 / 21.63pp」。结论 (东财个股 qfq 不可无条件信) 不变, 但数字该跑一遍统一。
8. **`fetch_qdii_premium` 的 IOPV 是 T-1 净值** —— QDII 的 IOPV 要到北京 22:30
   才公布, 盘中口径只能拿上一日净值当参考, 溢价率是「相对昨日净值」而非真 IOPV。

---

## 6. 想加一个新的数据源?

改这三处, 别的地方不用动:

1. `quote_providers.py`: 新增 `_xxx()` 取数函数, 返回带 `date` 索引的 `close` 列;
2. `_fetch_qfq()` 的降级链里插位次 (记得加 `SOURCE_*` 常量, 图表面板要显示);
3. 若是**个股**, 先跑 `audit_stock_qfq()` 确认东财 qfq 可用, 否则改走
   `fetch_stock_total_return_history()`。

测试: `tests/test_record.py` 里有数据源口径的固定用例
(`test_the_stock_price_source_uses_the_forward_adjusted_convention` 等),
新增源要补一条同样性质的断言。
