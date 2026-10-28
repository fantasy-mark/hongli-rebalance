"""GitHub Actions 契约 —— **唯一**知道 Actions 环境变量长什么样的地方 (D12)。

---
**为什么要有这个包**

GitHub Actions 是一套**外部系统**。它在我们代码里留下的痕迹散落在三个
地方, 每个地方都自己 `os.environ.get("GITHUB_...")`:

| 痕迹 | 原先住在 |
|---|---|
| `$GITHUB_OUTPUT` (step outputs) | `src/application/daily_snapshot.py:write_github_output` |
| `::error::` annotation | `scripts/apply_grid.py:fail` |
| `GITHUB_RUN_ID` / `GITHUB_REPOSITORY` / `GITHUB_SERVER_URL` / `GITHUB_WORKSPACE` | `src/presentation/notify_failure.py` |

应用层 (用例编排) 不该知道 workflow 的环境变量名; 呈现层也不该。
现在**机制**全部收进这个包 —— 它是 GitHub Actions 的**防腐层**:

- `set_output` / `set_outputs` / `publish_snapshot_outputs` —— 写 step outputs
- `annotate` / `error` / `warning` / `notice` —— 打 annotation
- `append_summary` —— 写 run summary 面板
- `current_context()` —— 这次 run 是谁 (仓库 / run id / 工作区)

**stdlib-only** —— `scripts/apply_grid.py` 与 `scripts/apply_holdings.py` 在一个
**不装** pandas/akshare 的 GitHub Actions job 里 import 这条链路, 所以这个
包不能有任何第三方依赖。
"""
from .annotations import LEVELS, annotate, error, notice, warning
from .context import RunContext, current_context, workflow_token
from .outputs import (
    SNAPSHOT_DATE,
    SNAPSHOT_TOTAL,
    SNAPSHOT_TRIGGER,
    publish_snapshot_outputs,
    set_output,
    set_outputs,
)
from .summary import append_summary

__all__ = [
    "LEVELS",
    "annotate",
    "error",
    "warning",
    "notice",
    "RunContext",
    "current_context",
    "workflow_token",
    "SNAPSHOT_DATE",
    "SNAPSHOT_TOTAL",
    "SNAPSHOT_TRIGGER",
    "publish_snapshot_outputs",
    "set_output",
    "set_outputs",
    "append_summary",
]
