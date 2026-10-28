"""仓库路径 —— **唯一**知道「仓库根在哪」的地方 (D13)。

⚠️ **为什么必须有这个模块**

2026-10-26 的结构重构把 `src/notify.py` 挪到 `src/presentation/digest.py`、
把 `src/chart.py` 挪到 `src/infrastructure/chart_renderer.py` —— 两处都
**下了一层**, 但里面的

    ROOT = Path(__file__).resolve().parent.parent

**没跟着改**:

    src/notify.py              → parent.parent = 仓库根    ✓
    src/presentation/digest.py → parent.parent = src/      ✗

于是 `HISTORY` 指向 `src/data/history.json` —— 不存在 →
`latest_record()` 返回 `None` → 邮件**静默不发送**, 而 workflow 那一步
**照样是绿的** (代码只是 `print("WARN ...")` 然后正常返回, 退出码 0)。

后果: 邮件从 2026-10-26 起就没再发出去过, 而 Actions UI 上每一个
「Send email digest」步骤都是 ✅ —— **绿色的步骤 + 没有邮件**。
同理 `chart_renderer._load_grid_state_safe()` 读不到 `grid_state.json`
→ 图表少画网格仓位。

⇒ **仓库根只有一个来源**: 这个模块。谁也别再自己算 `parent.parent`。

**stdlib-only** —— `scripts/apply_grid.py` 在一个**不装** pandas/akshare 的
GitHub Actions job 里 import 这条链路, 所以这里不能有任何第三方依赖。
"""
from __future__ import annotations

from pathlib import Path

#: 仓库根 —— `src/infrastructure/paths.py` → src/infrastructure → src → 根。
#:
#: ⚠️ **三层** (`parent.parent.parent`): 这个文件在 `src/infrastructure/` 下。
ROOT = Path(__file__).resolve().parent.parent.parent

#: 全部落盘数据所在目录。
DATA_DIR = ROOT / "data"

#: 每日快照的记录流水 (append-only, 用户读 diff)。
HISTORY_FILE = DATA_DIR / "history.json"

#: 复合网格的状态 (6 个 key, 见 `src/domain/grid/grid_position.py`)。
GRID_STATE_FILE = DATA_DIR / "grid_state.json"

#: 账本的派生缓存 (schema v2, 两个现金池)。
HOLDINGS_FILE = DATA_DIR / "holdings.json"

#: 交易账本 (**事实源**)。
TRADES_FILE = DATA_DIR / "trades.json"

#: 图表 PNG (Pages 首页的 `<img>` 消费它)。
GRID_CHART_FILE = DATA_DIR / "grid_chart.png"


__all__ = [
    "ROOT",
    "DATA_DIR",
    "HISTORY_FILE",
    "GRID_STATE_FILE",
    "HOLDINGS_FILE",
    "TRADES_FILE",
    "GRID_CHART_FILE",
]
