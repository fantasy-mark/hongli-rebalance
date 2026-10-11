# 稳健再平衡 · 自动监控

等权 3 资产 (黄金ETF博时 + 红利ETF易方达 + 可转债ETF博时) + 3pp 对称阈值
的实盘组合监控。GitHub Actions 每日 9:00 (北京时间) 自动检测信号，
并把每日快照提交回仓库，渲染到 GitHub Pages，同时推送邮件摘要。

> 这个项目是从 `hongli` 母项目剥离出来的独立仓库，
> 用于云端无人值守运行。

---

## 架构

```
hongli-rebalance/
├── .github/workflows/daily.yml   # cron 9:00 北京时间 (1:00 UTC)
├── src/
│   ├── fetch.py                  # akshare Sina 价格拉取
│   ├── record.py                 # 读 holdings.xlsx + 计算权重 + 写 history.json
│   └── notify.py                 # SMTP 邮件摘要
├── data/
│   ├── holdings.xlsx             # ← 用户维护 (持仓/现金)
│   └── history.json              # ← Action 自动追加
├── site/                         # GitHub Pages 根
│   ├── index.html
│   ├── app.js                    # ECharts 曲线 + 表格
│   └── style.css
├── tests/test_record.py          # 纯函数单测
└── requirements.txt
```

每个交易日的流程：

```
9:00 (北京时间) cron 触发
  ↓
record.py: fetch 3 ETF 价格 + 读 holdings.xlsx
  ↓
计算 total / weights / triggered
  ↓
append 到 history.json (覆盖重仓版本)
  ↓
notify.py: 读 history.json → 邮件
  ↓
git-auto-commit: 提交 history.json 回仓库
  ↓
GitHub Pages 自动重渲染 (无需 Action)
```

---

## 快速开始

### 1. 创建 GitHub 仓库

```bash
# 在 hongli-rebalance/ 目录下
cd C:\Users\Administrator\project\hongli-rebalance

git init
git add .
git commit -m "init: 稳健再平衡 自动监控"
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
Branch: `main`、Folder: `/site` → Save。

几分钟后访问 `https://<your-username>.github.io/hongli-rebalance/`。

### 4. 手动触发一次测试

仓库 → **Actions** → **Daily Rebalance Check** → **Run workflow**。

检查：
- ✅ Action 是否绿色通过
- 📧 邮箱是否收到摘要
- 🌐 Pages 上是否看到曲线 + 表格
- 📁 仓库里 `data/history.json` 是否新增了一条

### 5. 持仓变更流程

在本地编辑 `data/holdings.xlsx`（修改数量 / 现金），
然后：

```bash
git add data/holdings.xlsx
git commit -m "update holdings"
git push
```

下一个交易日 9:00 Action 会自动跑。

---

## 持仓 xlsx 格式

| 列 | A | B | C |
|---|---|---|---|
| 第 1 行 (header) | *空* | | |
| 第 2 行 | **现金** *(或历史用 "资产")* | `<金额>` | (空) |
| 第 3 行 | *空* | | |
| 第 4 行 | 名称 | 代码 | 数量 |
| 第 5 行起 | 红利ETF易方达 | 515180 | 122800 |
| | 黄金ETF博时 | 159937 | 20600 |
| | 可转债ETF博时 | 511380 | 13700 |

> **"现金"** 是账户可用现金余额。
> **总资金 = ETF 总市值 + 现金**（自动计算）。
> "资产" 字段名已弃用，新文件请用 "现金"。

---

## 节假日 & Cron 延迟

- **cron** `0 1 * * 1-5` 仅按周一-周五触发。**A 股节假日 (春节、国庆等) Action 也会跑**；
  akshare 会返回上一个交易日的数据，`positions[].date` 是交易日日期 (不是当日)。
  这是已知限制，v1 不做节假日白名单。
- GitHub Actions cron **有 5-30 分钟延迟** (早上高峰期可能更长)。
  信号本身是确定的 (基于昨日收盘价)，晚几分钟不影响决策。

---

## 本地开发

```bash
cd C:\Users\Administrator\project\hongli-rebalance
pip install -r requirements.txt

# 跑单测 (无网络)
python -m tests.test_record

# 跑一次 record (会拉真实价格, 需要网络)
python -m src.record

# 邮件 (需要 SMTP env)
SMTP_HOST=smtp.qq.com SMTP_PORT=465 \
SMTP_USER=xxx@qq.com SMTP_PASS=xxxx MAIL_TO=you@x.com \
python -m src.notify
```

---

## 范围外 (v1 不做)

- A 股节假日白名单
- 多组合对比 / 沪深300 基准叠加
- 微信/钉钉 推送 (已选邮件)
- 网页可视化编辑持仓
- 用户鉴权 (Pages 默认 public)

---

## 参考

- 母项目：[hongli](../hongli)
- 回测脚本：`hongli/backend/scripts/rebalance_check.py`
- ETF 代码核对：`hongli/memory/etf-code-mapping.md`