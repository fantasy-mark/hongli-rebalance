# GitHub Pages 站点设计（ddd 分支）

## 目标

把「再平衡 + 网格」组合回测的结果发布成一个静态站点，页面顺序固定：

1. **策略全景图** —— 网格资金占用，样式对齐 main 分支的 `data/grid_chart.png`
2. **交易记录** —— 网格成交明细（开仓 / 加仓 / 止盈）
3. **持仓** —— 再平衡组合与网格组合**分栏**展示，各自独立

## 分层

| 层 | 模块 | 职责 |
| --- | --- | --- |
| domain | `src/domain/chart/overview.py` | 值对象 `ReturnCurve` / `CapitalOccupation`，聚合根 `StrategyOverview`。只表达「画什么」，含不变量校验，不依赖 matplotlib |
| application | `src/application/pages/site_builder.py` | 用例 `BuildPagesSite`：跑一次 `CombinedBacktester`，把结果翻译成 `StrategyOverview` + 视图模型（`SummaryView` / `TradeView` / `PositionView` / `StrategyView`） |
| infrastructure | `src/infrastructure/chart/strategy_overview_renderer.py` | `StrategyOverviewRenderer.render()`：matplotlib 绘制 PNG，配色与版式对齐 main 分支 |
| infrastructure | `src/infrastructure/pages/repositories.py` | `PagesSiteRepository.save_all()`：把视图模型落盘成 `pages/data/*.json`，并调用渲染器 |
| CLI | `scripts/build_pages.py` | 薄入口，解析参数 → 调用用例 → 调用仓储 |
| 静态资源 | `pages/index.html` / `app.js` / `style.css` / `pages.css` | 纯前端渲染，`fetch` 读 `data/*.json` |

依赖方向：`domain ← application ← infrastructure ← scripts`。domain 层无 IO、无第三方绘图库。

## 领域模型

```python
StrategyOverview
├── window_start / window_end
├── return_curves: Tuple[ReturnCurve, ...]
│     role = ASSET | REBALANCE | GRID_ROI | GRID_PROFIT
├── occupations: Tuple[CapitalOccupation, ...]
├── peak_occupation           # 各资产占用合计的历史峰值（网格 ROI 分母）
└── grid_rounds_completed / grid_rounds_total
```

关键不变量：

- `ReturnCurve.dates` 与 `values` 等长且非空
- `CapitalOccupation.values` 非负
- `StrategyOverview.return_curves` 非空，`peak_occupation >= 0`

派生量：

- `total_occupation_series` —— 各资产占用逐日合计
- `current_occupation` —— 当前占用合计
- `occupation_by_code` —— 按代码索引，供渲染器取配色

## 全景图口径

上 panel（对数刻度，收益 %，起点 = 0%）：

| 曲线 | 数据来源 | 样式 |
| --- | --- | --- |
| 各资产买入持有 | 网格侧 `daily_values['{code}_close']` 归一化 | 虚线，`#C0392B` / `#C9A961` / `#2C5F8D` / `#48C9B0` |
| `3pp 再平衡` | 再平衡侧 `daily_values['total_value']` 归一化 | 黑色 `#1A1A1A` 粗实线 |
| `网格 ROI` | `grid.total_pnl / peak_occupation * 100` | 绿色 `#27AE60` 实线 |

右端标签采用贪心避让（`min_gap = y 轴范围 × 0.06`），与 main 分支一致。

下 panel（¥）：

| 元素 | 数据来源 | 样式 |
| --- | --- | --- |
| 各资产资金占用堆叠 | `{code}_shares × {code}_close` | `stackplot`，资产配色 |
| 网格累计收益 | `grid.daily_values['total_pnl']` | 红色 `#B03A2E` 虚线 |

> **与 main 分支的差异**：main 用 `归并到再平衡 (¥)` 表示「只卖成本金额」规则下留在组合里的份额市值；
> ddd 分支采用「整轮一次性止盈」，止盈时整批卖出，没有归并份额，因此下 panel 改为
> `网格累计收益`（已实现 + 浮盈），语义等价但口径不同。

## 数据产物

`python scripts/build_pages.py` 生成：

```
pages/data/grid_chart.png   策略全景图（约 2000×1400）
pages/data/summary.json     组合 / 再平衡 / 网格 三段视图（含持仓与绩效）
pages/data/trades.json      网格成交明细（129 笔）
```

`summary.json` 结构：

```jsonc
{
  "generated_at": "YYYY-MM-DD",
  "window": { "start": "...", "end": "..." },
  "combined":  { "key": "combined",  "positions": [...], "total_return": 0.69, ... },
  "rebalance": { "key": "rebalance", "cash": ..., "positions": [...] },
  "grid":      { "key": "grid", "buy_count": 65, "sell_count": 64,
                 "completed_rounds": 18, "total_rounds": 22, ... }
}
```

持仓隔离在 JSON 层面即可验证：`rebalance.cash` 与 `grid.cash` 是两个独立数字，
`combined` 只是展示用的合并视图（同标的多策略持仓按代码合并股数）。

## 部署

`.github/workflows/pages.yml`：

1. `schedule`（工作日 UTC 00:10）+ `workflow_dispatch` + `push`（`pages/**`、`src/**` 等路径）
2. 装 `fonts-noto-cjk`（matplotlib 中文渲染必需）
3. `python scripts/build_pages.py` 跑回测 + 渲染
4. 若有变化则 commit 回 `ddd` 分支的 `pages/data`
5. `actions/upload-pages-artifact` + `actions/deploy-pages` 发布

仓库 Settings → Pages → Source 需选 **GitHub Actions**。

## 本地预览

```bash
python scripts/build_pages.py
cd pages && python -m http.server 8000
# 打开 http://127.0.0.1:8000/
```

注意用 HTTP 服务器打开，直接 `file://` 打开会被 CORS 拦住 `fetch`。

## 注意点

- `.gitignore` 里原有 `data/` 与 `*.png` 规则会吞掉站点产物，已追加
  `!pages/data/` / `!pages/data/**` / `!pages/data/*.png` 反向豁免。
- 回测默认走价格缓存；东财 push2 有限流（`Push2RateLimitError`），
  触发后会有「数据源口径…本次不更新缓存」提示，属正常降级。
- 组合口径的 `total_return` 以共同区间首日合计市值为基准，
  与「初始资金 120 万」不完全等价，页面上两者都展示以便核对。
