# GitHub Pages 站点设计（ddd 分支）

## 目标

把「再平衡 + 网格」组合回测的结果发布成一个静态站点，页面顺序固定：

1. **策略全景图** —— 网格资金占用，样式对齐 main 分支的 `data/grid_chart.png`
2. **回测交易记录** —— 再平衡调仓 + 网格建仓/加仓/止盈，含策略与步长列
3. **持仓** —— 再平衡组合与网格组合**分栏**展示，各自独立
4. **策略说明** —— 策略名 → 规则描述 → 规则实现 的对应关系

## 分层

| 层 | 模块 | 职责 |
| --- | --- | --- |
| domain | `src/domain/chart/overview.py` | 值对象 `ReturnCurve` / `CapitalOccupation`，聚合根 `StrategyOverview`。只表达「画什么」，含不变量校验，不依赖 matplotlib |
| domain | `src/domain/strategy/spec.py` | 策略规格通用值对象：`StrategySpec`（策略名）/ `StrategyRule`（规则描述 + 实现位置）/ `RuleParameter`（参数） |
| domain | `src/domain/grid/spec.py` | 由 `GridTrigger` 真实参数**推导**网格规则描述与实现位置 |
| domain | `src/domain/rebalance/spec.py` | 由 `RebalanceConfig` 真实参数推导再平衡规则描述与实现位置 |
| application | `src/application/pages/site_builder.py` | 用例 `BuildPagesSite`：跑一次 `CombinedBacktester`，把结果翻译成 `StrategyOverview` + 视图模型（`SummaryView` / `TradeView` / `PositionView` / `StrategyView`） |
| infrastructure | `src/infrastructure/chart/strategy_overview_renderer.py` | `StrategyOverviewRenderer.render()`：matplotlib 绘制 PNG，配色与版式对齐 main 分支 |
| infrastructure | `src/infrastructure/pages/repositories.py` | `PagesSiteRepository.save_all()`：把视图模型落盘成 `data/site/*.json`，并调用渲染器 |
| CLI | `scripts/build_pages.py` | 薄入口，解析参数 → 调用用例 → 调用仓储 |
| 静态资源 | `index.html` / `app.js` / `style.css` / `pages.css` | 纯前端渲染，`fetch` 读 `data/site/*.json` |

依赖方向：`domain ← application ← infrastructure ← scripts`。domain 层无 IO、无第三方绘图库。

## 策略规格：策略名 / 规则描述 / 规则实现

交易策略的业务规则复杂，页面需要能说清楚「这个策略叫什么、按什么规则跑、规则写在哪」。

### 领域模型

```python
StrategySpec          # 策略名
├── strategy_id       # 'grid' / 'rebalance'
├── name              # 复合网格 / 阈值再平衡
├── summary           # 一句话规则摘要
├── rules: tuple[StrategyRule, ...]
└── implementation    # 策略整体实现位置清单

StrategyRule          # 一条规则 = 描述 + 参数 + 公式 + 实现
├── rule_id           # 'grid.trigger' / 'grid.add' / ...
├── title             # 触发条件 / 加仓规则 / 止盈规则 / 资金隔离
├── description       # 人读描述（由参数推导）
├── formula           # 公式
├── parameters        # 规则参数（值）
└── implementation    # 'src/domain/grid/services.py::GridEngine._process_levels'
```

### 关键约束：描述由参数推导

规则描述**不允许硬编码**，必须由 `GridTrigger` / `RebalanceConfig` 的真实参数拼出来。
否则改了参数忘了改文案，就会出现「页面说 2ATR、代码跑 0.5ATR」的漂移。

以网格加仓规则为例：

```python
description=(
    f"网格激活后，每再下跌 {step_ratio}×ATR({trigger.atr_period}) "
    f"加一档仓位；每档买入 {trigger.buy_amount:,.0f} 元，"
    f"股数向下取整到 {trigger.lot_size} 股的整数倍；..."
)
```

改 `step_ratio` 后描述、公式、摘要、`sum()` 全部自动跟着变，
`tests/test_strategy_spec.py` 里的 `test_description_follows_step_ratio` 守住这条不变式。

### 规则清单

| 策略 | rule_id | 规则 | 实现 |
| --- | --- | --- | --- |
| 网格 | `grid.trigger` | 触发条件 | `GridEngine.evaluate` |
| 网格 | `grid.add` | 加仓规则 | `GridEngine._process_levels` |
| 网格 | `grid.take_profit` | 止盈规则 | `GridEngine._process_take_profit` |
| 网格 | `grid.isolation` | 资金隔离 | `Portfolio` |
| 再平衡 | `rebalance.allocation` | 目标配置 | `AllocationPlan` |
| 再平衡 | `rebalance.trigger` | 触发条件 | `RebalanceBacktester._check_rebalance` |
| 再平衡 | `rebalance.cost` | 交易成本 | `RebalanceBacktester._rebalance` |
| 再平衡 | `rebalance.isolation` | 资金隔离 | `Portfolio` |

## 领域模型（全景图）

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
data/site/grid_chart.png   策略全景图（约 2000×1400）
data/site/summary.json     组合 / 再平衡 / 网格 三段视图（含持仓与绩效）+ 策略规格
data/site/trades.json      回测交易记录（网格 83 笔 + 再平衡 66 笔 = 149 笔）
```

`summary.json` 结构：

```jsonc
{
  "generated_at": "YYYY-MM-DD",
  "window": { "start": "...", "end": "..." },
  "combined":  { "key": "combined",  "positions": [...], "total_return": 0.69, ... },
  "rebalance": { "key": "rebalance", "cash": ..., "positions": [...] },
  "grid":      { "key": "grid", "buy_count": 65, "sell_count": 64,
                 "completed_rounds": 18, "total_rounds": 22, ... },
  "strategies": {
    "grid":      { "strategy_id": "grid", "name": "复合网格", "summary": "...",
                   "rules": [ { "rule_id": "grid.trigger", "description": "...",
                                "implementation": "src/domain/grid/services.py::GridEngine.evaluate" } ] },
    "rebalance": { "strategy_id": "rebalance", "name": "阈值再平衡", "rules": [...] }
  }
}
```

持仓隔离在 JSON 层面即可验证：`rebalance.cash` 与 `grid.cash` 是两个独立数字，
`combined` 只是展示用的合并视图（同标的多策略持仓按代码合并股数）。

## 部署

`.github/workflows/pages.yml`：

1. `schedule`（工作日 UTC 00:10）+ `workflow_dispatch` + `push`（`pages/**`、`src/**` 等路径）
2. 装 `fonts-noto-cjk`（matplotlib 中文渲染必需）
3. `python scripts/build_pages.py` 跑回测 + 渲染
4. 若有变化则 commit 回 `ddd` 分支的 `data/site`
5. GitHub 自动重建 Pages（分支部署，无需 deploy-pages action）

仓库 Settings → Pages → Source 需选 **Deploy from a branch**（分支 `ddd` / 目录 `/`）。

## 本地预览

```bash
python scripts/build_pages.py
python -m http.server 8000        # 在仓库根目录
# 打开 http://127.0.0.1:8000/
```

验收（无头浏览器 37 项检查）：

```bash
python scripts/verify_site.py                                  # 本地
python scripts/verify_site.py --url https://fantasy-mark.github.io/hongli-rebalance/
```

注意用 HTTP 服务器打开，直接 `file://` 打开会被 CORS 拦住 `fetch`。

## 注意点

- `.gitignore` 里原有 `data/` 与 `*.png` 规则会吞掉站点产物，已追加
  `!data/site/` / `!data/site/**` / `!data/site/*.png` 反向豁免。
- 回测默认走价格缓存；东财 push2 有限流（`Push2RateLimitError`），
  触发后会有「数据源口径…本次不更新缓存」提示，属正常降级。
- 组合口径的 `total_return` 以共同区间首日合计市值为基准，
  与「初始资金 120 万」不完全等价，页面上两者都展示以便核对。
- **网格步长口径**：当前 `step_ratio = 0.5`，即步长 = 0.5×ATR(120)。
  页面规则描述由该参数推导，改 `step_ratio` 后描述/步长列/回测结果同步变化。
  若要改成 2×ATR，只需改 `src/application/pages/site_builder.py::default_config`
  里的 `step_ratio`（实测：0.5→网格 +1.12% / 83 笔 / 18-22 轮；
  2→网格 +0.40% / 25 笔 / 9-13 轮）。
