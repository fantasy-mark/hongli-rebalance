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
│   ├── fetch.py                  # akshare Sina 价格拉取
│   ├── grid.py                   # 复合网格引擎 (120 日新低 + 3% 金字塔 + 3% TP)
│   ├── three_pp.py               # 3pp 再平衡回测引擎
│   ├── record.py                 # 读 holdings.xlsx + 双策略 record + 持久化
│   ├── chart.py                  # matplotlib 渲染 grid_chart.png
│   └── notify.py                 # SMTP 邮件摘要 (双策略段)
├── data/
│   ├── holdings.xlsx             # ← 用户维护 (持仓/现金)
│   ├── history.json              # ← Action 自动追加 (每日总市值 + 网格信号)
│   ├── grid_state.json           # ← Action 自动维护 (网格 per-asset 状态)
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

在本地编辑 `data/holdings.xlsx`（修改数量 / 现金），
然后：

```bash
git add data/holdings.xlsx
git commit -m "update holdings"
git push
```

下一个外部 cron 触发周期 Action 会自动跑。

---

## 持仓 xlsx 格式

| 列 | A | B | C |
|---|---|---|---|
| 第 1 行 (header) | *空* | | |
| 第 2 行 | **现金** *(或历史用 "资产")* | `<金额>` | (空) |
| 第 3 行 | *空* | | |
| 第 4 行 | 名称 | 代码 | 数量 |
| 第 5 行起 | 黄金ETF博时 | 159937 | 20600 |
| | 红利ETF易方达 | 515180 | 122800 |
| | 可转债ETF博时 | 511380 | 13700 |
| | 纳斯达克ETF华夏 | 513300 | (持有数量) |

> **"现金"** 是账户可用现金余额。
> **总资金 = ETF 总市值 + 现金**（自动计算）。
> "资产" 字段名已弃用，新文件请用 "现金"。

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
- 网页可视化编辑持仓
- 用户鉴权 (Pages 默认 public)

---

## 参考

- 母项目：[hongli](../hongli)
- 网格引擎母本：`hongli/backend/scripts/grid_dip_compound.py`
- 3pp 引擎母本：`hongli/backend/scripts/verify_3pp_strategy.py`
- 图表母本：`hongli/backend/scripts/plot_strategy_with_grid.py`
- ETF 代码核对：`hongli/memory/etf-code-mapping.md`