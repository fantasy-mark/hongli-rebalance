# 稳健再平衡 · 自动监控

等权 4 资产 (黄金ETF博时 + 红利ETF易方达 + 可转债ETF博时 + 纳斯达克ETF华夏) 双策略监控：

1. **4pp 再平衡** —— 任何 ETF 权重偏离 1/4 超过 3pp → 触发全调仓 (snap-back)
2. **复合网格 (pyramid grid)** —— 120 日新低 + 3% 加仓金字塔 + 3% 止盈；
   加仓金额 = ¥10,000 底仓 + 4pp 池子近 3 年浮盈 × 5%

每日 9:00 (北京时间) 由外部 cron 服务
(推荐 [console.cron-job.org](https://console.cron-job.org/)) 通过
`repository_dispatch` API 触发 GitHub Action，运行双策略信号检测、
渲染 matplotlib 策略图、提交每日快照到 `data/history.json` +
`data/grid_state.json` + `data/grid_chart.png`、推送邮件摘要，
GitHub Pages 实时展示曲线和策略全景图。

> 这个项目是从 `hongli` 母项目剥离出来的独立仓库，
> 用于云端无人值守运行。

---

## 架构

```
hongli-rebalance/
├── .github/workflows/daily.yml   # 由 repository_dispatch 触发 (外部 cron 驱动)
├── src/
│   ├── fetch.py                  # akshare Sina 价格拉取 (含腾讯 K 线 fallback)
│   ├── grid.py                   # 复合网格引擎 (120 日新低 + 3% 金字塔 + 3% TP, 全虚拟)
│   ├── three_pp.py               # 3pp 再平衡回测引擎
│   ├── record.py                 # 读 trades.json + 双策略 record + 持久化 (双池 cash_rebalance/cash_grid)
│   ├── chart.py                  # matplotlib 渲染 grid_chart.png
│   └── notify.py                 # SMTP 邮件摘要 (双策略段, 显示 3 个 cash 字段)
├── data/
│   ├── trades.json               # ← 用户维护 (真实成交账本, 唯一真相, 8 种 side)
│   ├── holdings.json             # ← 由 trades.json 重建 (v2 schema, 双池派生缓存)
│   ├── history.json              # ← Action 自动追加 (每日总市值 + 网格信号)
│   ├── grid_state.json           # ← Action 自动维护 (网格 per-asset 虚拟状态)
│   └── grid_chart.png            # ← Action 自动渲染 (策略全景图, 嵌入 Pages)
├── index.html / app.js / style.css  # GitHub Pages (Pages Source: / (root))
├── tests/test_record.py          # 纯函数单测 (record + grid + 3pp)
└── requirements.txt              # akshare, pandas, openpyxl, requests, matplotlib
```

> **Pages 布局说明** — GitHub Pages UI 只支持 `/ (root)` 和 `/docs/`，没有 `/site` 选项。
> 所以站点文件 (index.html / app.js / style.css) 直接放在仓库根目录，
> 访问 `https://<user>.github.io/hongli-rebalance/` 就是首页。
> 源代码 (src/、tests/) 也会被 Pages 暴露，这是项目仓库的常见做法 (无安全风险)。

每个交易日的流程：

```
console.cron-job.org (北京 9:00) → POST repository_dispatch
  ↓
record.py: fetch 4 ETF 价格 + 读 holdings.xlsx
  ↓
4pp: 计算 total / weights / triggered (snapshot)
  ↓
grid: 读 grid_state.json → 推进 4 个资产的状态 → 写回
  ↓
chart.py: 渲染 data/grid_chart.png (matplotlib, 中文字体)
  ↓
append 到 history.json (覆盖重仓版本)
  ↓
notify.py: 读 history.json → 邮件 (双策略段)
  ↓
git-auto-commit: 提交 history.json + grid_state.json + grid_chart.png
  ↓
GitHub Pages 自动重渲染 (PNG src 加 ?t=Date.now() 缓存破坏)
```

---

## 策略细节

### 4pp 再平衡

- **目标权重**: 1/4 each (黄金 / 红利 / 可转债 / 纳指 各 25%)
- **触发**: 任一 ETF 权重偏离 1/4 > 3pp → 全部 snap-back 到 1/4
- **信号**: `record.triggered = True` 时, `plan` 字段给出每个 ETF 的
  target_value / current_value / delta_value (供券商调仓参考)
- **3pp → 4pp 升级原因**: 4 资产下 `target_w = 1/n` 动态化, 加纳指后 CAGR
  +3.28pp / MaxDD -4.32pp (回测 2020-11-05 → 2026-10-13)

### 复合网格 (per-asset, independent)

- **触发 (开仓)**: 今日收盘 < 过去 120 日 (含今日) 最低价 → 买 1 层
- **加仓金字塔**: 在 cycle 中, 今日收盘 ≤ 上一笔买入价 × 97% → 再买 1 层
- **止盈 (first_buy 基准)**: 今日收盘 ≥ **第一笔买入价** × 103% → 全部卖出,
  锁住 realized P&L, 进入等待下一次 120 日新低
  - **为什么不用均价**：均价随加仓下移, 容易被小幅反弹提前平掉。
    固定以第一笔为基准, 必须真正回到起点 +3% 才卖, 拿住更多反弹空间。
  - 回测 (2020-04 → 2026-10, 3 ETF): first_buy 比 avg_cost 多赚 +33.9% 网格 PnL,
    CAGR 多 +0.27pp, MaxDD 不变。
- **加仓金额**: `layer_yuan = ¥10,000 + max(pool - recent_min, 0) × 5%`
  - `pool` = 当前 4pp 总市值
  - `recent_min` = 过去 756 交易日 (3 年) pool 最低值
  - 浮盈 0 时买底仓 ¥10k; 浮盈 ¥10 万时买 ¥15k; 浮盈 ¥50 万时买 ¥35k
- **状态持久化**: `data/grid_state.json` —— 每天 commit 后跨 Action run 保留
- **环境变量覆盖** (供参数扫描):
  - `GRID_BASE_YUAN` (默认 10000)
  - `GRID_FLOATING_FRACTION` (默认 0.05)
  - `GRID_FLOATING_WINDOW` (默认 756)
  - `GRID_TP_BASIS` (默认 `first_buy`; 可改回 `avg_cost` 切回 v2 行为)
  - `THRESHOLD_PP` (默认 3.0) — 同时影响 3pp 和网格的 ±3% 步长

### 图表 (`data/grid_chart.png`)

- 每日 Action 跑完后用 matplotlib 服务端渲染 (150 dpi)
- 包含两部分:
  - **上子图**: 6 条线对数 Y 轴 — 4 ETF 持有不动、4pp 净值、网格 ROI
  - **下子图**: 堆叠面积 — 每个 ETF 当前网格在用资金
- GitHub Pages 通过 `<img id="grid-chart" src="data/grid_chart.png?t=...">`
  嵌入；`?t=Date.now()` 强制每次访问都重新拉图
- Linux runner 安装 `fonts-noto-cjk` 解决中文字体问题

---

## 快速开始

### 1. 创建 GitHub 仓库

```bash
# 在 hongli-rebalance/ 目录下
cd C:\Users\Administrator\project\hongli-rebalance

git init
git add .
git commit -m "init: 稳健再平衡 双策略自动监控"
# 在 GitHub 上创建空仓库 (例如 hongli-rebalance), 然后:
git remote add origin git@github.com:<your-username>/hongli-rebalance.git
git branch -M main
git push -u origin main
```

或者用 `gh` CLI：
```bash
gh repo create hongli-rebalance --public --source=. --remote=origin --push
```

### 2. 配置 SMTP Secrets

仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，逐个添加：

| Secret 名 | 示例值 | 说明 |
|----------|------|------|
| `SMTP_HOST` | `smtp.qq.com` | QQ 邮箱；网易用 `smtp.163.com`，Gmail 用 `smtp.gmail.com` |
| `SMTP_PORT` | `465` | SSL 端口 |
| `SMTP_USER` | `xxx@qq.com` | 发件邮箱完整地址 |
| `SMTP_PASS` | `abcd1234efgh` | **授权码** (不是登录密码，QQ 邮箱在 设置→账户→SMTP服务 里生成) |
| `MAIL_TO` | `you@example.com` | 收件人；多个用英文逗号 `a@x.com,b@y.com` |

### 3. 启用 GitHub Pages

仓库 → **Settings** → **Pages** → **Source**: `Deploy from a branch` →
Branch: `main`、Folder: **`/ (root)`** → Save。

几分钟后访问 `https://<your-username>.github.io/hongli-rebalance/`。

### 4. 配置外部 cron 触发 (console.cron-job.org)

Action 不再内置 schedule, 由外部 cron 服务驱动。
推荐 [console.cron-job.org](https://console.cron-job.org/) (免费、无需注册即可用)：

1. 进入 https://console.cron-job.org/ → **Create cron job**
2. 填入:

   | 字段 | 值 |
   |---|---|
   | Title | `hongli-rebalance daily` |
   | URL | `https://api.github.com/repos/<your-username>/hongli-rebalance/dispatches` |
   | Method | **POST** |
   | Headers | `Accept: application/vnd.github+json`<br>`Authorization: Bearer <GitHub PAT>`<br>`User-Agent: cron-job` |
   | Body (raw JSON) | `{"event_type": "trigger"}` |
   | Schedule | `0 17 * * 1-5` (UTC 17:00 = 北京时间次日 1:00)<br>*A 股开市 9:30 前的安全时点, 早高峰 Action 不会卡* |

3. **GitHub PAT**: Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   生成, 勾选 `Contents: Read and write` (只对本仓库) 或 `Actions: Read and write`。
4. 测试: 在 cron-job.org 页面点 **Run now**, 检查 GitHub 仓库 Actions 页面是否出现新 run。

> **调时间小贴士**: GitHub Actions 高峰期 (UTC 0-3 = 北京 8-11) 排队可达 5-30 分钟。
> 如果想 9:30 开盘前看到信号, 建议 UTC 17:00 (北京 1:00) 触发, 北京 9:30 前一定完成。
> 或者 UTC 0:30 / 1:00 等避开 0:00 高峰。

### 5. 手动触发一次测试

仓库 → **Actions** → **Daily Rebalance Check** → **Run workflow**。
*(workflow_dispatch 仍保留, 仅供本地测试。日常走 cron-job.org → repository_dispatch。)*

检查：
- ✅ Action 是否绿色通过 (chart 步骤应无错)
- 📧 邮箱是否收到摘要 (双策略段都在)
- 🌐 Pages 上是否看到 ECharts 曲线 + 策略全景图 + 表格
- 📁 仓库里 `data/history.json` + `data/grid_state.json` + `data/grid_chart.png` 是否都更新了

### 6. 持仓变更流程

**通过 Pages 编辑器（推荐）：** 在 `https://<user>.github.io/hongli-rebalance/`
有 3 个独立表单：
- **交易记录**（buy / sell） —— 每行一笔 ETF 买卖。表头：日期 / 方向 / 代码 / 数量 / 价格 / 金额 / 备注
  金额列是只读 (自动 = 数量 × 价格)。表单实时显示「本批次交易净流出 ¥X | 预计剩余再平衡 ¥Y」；
  预计再平衡 < 0 时「💾 保存」按钮自动禁用。
- **资金管理**（6 种 cash 操作） —— 每行一笔资金操作。表头：日期 / 方向 / 金额 / 备注
  顶部显示「当前可用: 再平衡 ¥X / 网格 ¥Y / 总 ¥Z」。
  6 种方向（见下方说明）。提现/转换不足时前端拦截 (服务端也会二次拒绝)。
- **复合网格手动操作**（open / add / tp） —— 网格是**虚拟**的，不与真实交易挂钩，
  但开仓/加仓/止盈会修改 `data/grid_state.json`。表头：日期 / 资产 / 操作 / 单价 / 数量 / 金额 / 备注
  顶部显示「当前网格: 515180 待开仓 | 159937 待开仓 | ...」。操作后 30 秒内
  history + chart + 邮件同步重跑。
  - `open` = 强制开仓 (要求 `in_cycle=false`；新单价会 push 到 120 日滚动窗口)
  - `add`  = 加仓 (要求 `in_cycle=true`；新单价成为 `entries[-1].price`，即"加仓参考价")
  - `tp`   = 止盈 (要求 `in_cycle=true`；按 manual 价算 realized P&L 累加到 `cum_realized`)

**双池 cash 模型 (v2 schema)**：`data/holdings.json` 里 `cash` 字段被拆成两个：
- `cash_rebalance` —— 真实现金, buy/sell/deposit_rebalance/withdraw_rebalance 路由到这里
- `cash_grid` —— 虚拟现金, deposit_grid/withdraw_grid/transfer_* 涉及; 用于追踪"如果网格
  资金也要"花掉, 应该有多少

**6 种 cash 操作（资金管理表单的方向下拉）**:
| 方向 | 效果 |
|---|---|
| `deposit_rebalance` | +amount → cash_rebalance (入金到再平衡) |
| `deposit_grid` | +amount → cash_grid (入金到网格) |
| `withdraw_rebalance` | -amount ← cash_rebalance (从再平衡提现) |
| `withdraw_grid` | -amount ← cash_grid (从网格提现) |
| `transfer_to_grid` | cash_rebalance -amount, cash_grid +amount (转换: 再平衡→网格) |
| `transfer_to_rebalance` | cash_grid -amount, cash_rebalance +amount (转换: 网格→再平衡) |

旧版单池 `deposit / withdraw` 已被废弃, 服务端会拒绝并提示使用 `deposit_rebalance` 等。

三个表单各自独立保存。每个表单会一次提交一批 — 同批次内多笔累计后才做现金检查 (前端拦截 + 服务端二次拒绝)。

每次保存会：
1. 追加到 `data/trades.json`；
2. 重算 `data/holdings.json`（schema v2 派生缓存, 双池）；
3. 重跑 `daily.yml`，约 30 秒后 commit + 发邮件。

**通过直接编辑 `data/trades.json`（不推荐，需要理解账本语义）：**

- `trades.json` 是 append-only 账本，每条 trade 有 `id` / `date` / `side` /
  `code` / `shares` / `price` / `amount` 等字段（按 side 不同）。
- 直接改 trades.json 不会自动重建 holdings.json；需要本地跑
  `python scripts/init_trades_ledger.py` 或 `apply_holdings.py` 来重建。
- 想"清仓" → 录入一笔 `sell`，数量 = 持仓数。
- 想"入金" → 录入一笔 `deposit_rebalance`（双池模型下资金入金路由到再平衡池）。
- 想"转换" → 录入一笔 `transfer_to_grid`（再平衡→网格虚拟池）。
- 想"撤销" → 录入一笔反向 trade (e.g. 错的 buy 反向 sell，note 写"撤销")。

---

## trades.json 格式 (trades-v1)

`data/trades.json` 是真实成交账本，唯一真相 (single source of truth)。
`data/holdings.json` 是 schema v2 派生缓存，由 `apply_holdings.py`
每次写入时自动重建。

```json
{
  "_schema": "trades-v1",
  "trades": [
    {
      "id": "2026-10-12-deposit_rebalance-001",
      "date": "2026-10-12",
      "side": "deposit_rebalance",
      "amount": 175900.60,
      "note": "初始入金 (建仓日, 路由到 rebalance 池)"
    },
    {
      "id": "2026-10-12-buy-002",
      "date": "2026-10-12",
      "side": "buy",
      "code": "515180",
      "shares": 40600,
      "price": 1.446,
      "fee": 0.0,
      "note": "红利 ETF 易方达 建仓 (从 cash_rebalance 扣)"
    },
    {
      "id": "2026-10-17-transfer_to_grid-001",
      "date": "2026-10-17",
      "side": "transfer_to_grid",
      "amount": 5000.0,
      "note": "从再平衡转 ¥5000 到网格虚拟池"
    }
  ]
}
```

### 字段表

| field | buy | sell | deposit_rebalance | deposit_grid | withdraw_rebalance | withdraw_grid | transfer_to_rebalance | transfer_to_grid |
|---|---|---|---|---|---|---|---|---|
| `id` | auto | auto | auto | auto | auto | auto | auto | auto |
| `date` (ISO) | required | required | required | required | required | required | required | required |
| `side` | `"buy"` | `"sell"` | `"deposit_rebalance"` | `"deposit_grid"` | `"withdraw_rebalance"` | `"withdraw_grid"` | `"transfer_to_rebalance"` | `"transfer_to_grid"` |
| `code` (6 digits) | required | required | — | — | — | — | — | — |
| `shares` (> 0) | required | required | — | — | — | — | — | — |
| `price` (> 0) | required | required | — | — | — | — | — | — |
| `amount` (> 0) | — | — | required | required | required | required | required | required |
| `fee` (≥ 0, optional) | optional | optional | — | — | — | — | — | — |
| `note` (optional) | optional | optional | optional | optional | optional | optional | optional | optional |
| **Pool 路由** | rebalance − | rebalance + | rebalance + | grid + | rebalance − | grid − | rebalance +, grid − | rebalance −, grid + |

- `id` 由 server 自动生成 (`<date>-<side>-<seq>`, 三位零填充)
- `date` 必须是未来日期 ≤ today + 1 day (容忍 UTC/北京时差)
- 不允许 oversell (卖出数量 > 当前持仓)
- **不允许任意池跌至 0 以下** (rebalance 和 grid 各自必须 ≥ 0, per 用户决策)
- 所有 ETF 代码必须在白名单 (515180 / 159937 / 511380 / 513300)
- 旧版 `deposit / withdraw` 单池 side 已被废弃, 服务端拒绝并提示用 `deposit_rebalance` 等

### 首次 bootstrap

`data/trades.json` 不存在时，跑：

```bash
python -X utf8 scripts/init_trades_ledger.py
```

会读取 `data/holdings.json` 当前状态 + 2026-10-12 收盘价，
生成 1 条 `deposit` + N 条 `buy` (每只持仓 1 条)，写回两份文件。

---

## 节假日 & 触发延迟

- 外部 cron (cron-job.org) 按周一-周五固定时间触发。**A 股节假日 (春节、国庆等) 也会跑**；
  akshare 会返回上一个交易日的数据，`positions[].date` 是交易日日期 (不是当日)。
  这是已知限制，v1 不做节假日白名单。
- GitHub Actions 本身有 5-30 分钟排队延迟 (早上高峰期可能更长)。
  信号本身是确定的 (基于当日开盘前价格)，晚几分钟不影响决策。

---

## 本地开发

```bash
cd C:\Users\Administrator\project\hongli-rebalance
pip install -r requirements.txt        # akshare pandas openpyxl requests matplotlib

# 跑单测 (无网络, 17 个测试)
python -m tests.test_record

# 跑一次 record (会拉真实价格, 需要网络; 写 history.json + grid_state.json + grid_chart.png)
python -m src.record

# 仅重渲染图表 (基于现有 history.json)
python -m src.chart

# 邮件 (需要 SMTP env)
SMTP_HOST=smtp.qq.com SMTP_PORT=465 \
SMTP_USER=xxx@qq.com SMTP_PASS=xxxx MAIL_TO=you@x.com \
python -m src.notify
```

---

## 范围外 (v2 不做)

- A 股节假日白名单
- 网格止损 (已评估无效, 丢弃 — 见 `hongli/memory/rebalance-stop-loss-evaluated.md`)
- 多组合对比 / 沪深300 基准叠加
- 微信/钉钉 推送 (已选邮件)
- 用户鉴权 (Pages 默认 public)
- Trade 编辑/删除 UI — 账本是 append-only，纠错通过反向 trade 实现

---

## 参考

- 母项目：[hongli](../hongli)
- 网格引擎母本：`hongli/backend/scripts/grid_dip_compound.py`
- 3pp 引擎母本：`hongli/backend/scripts/verify_3pp_strategy.py`
- 图表母本：`hongli/backend/scripts/plot_strategy_with_grid.py`
- ETF 代码核对：`hongli/memory/etf-code-mapping.md`