"""`$GITHUB_OUTPUT` —— step outputs 的**唯一**写入口 (D12)。

workflow 里下游 step 读的是 `steps.<id>.outputs.<name>`, 而那些值是通过
`$GITHUB_OUTPUT` 这个文件传递的。以前 `src/application/daily_snapshot.py`
自己 `open(os.environ["GITHUB_OUTPUT"], "a")` —— 应用层知道了 Actions 的
环境变量名。现在写法的**机制**住在这个包里, 应用层只说「我要发布这三个
输出」。

**step output 的名字是 workflow 契约** —— `daily.yml` 的
「Commit data files」步骤读 `steps.record.outputs.date`。所以名字也放在
这里, 而不是散在调用方。
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

# --- 每日快照 step 的输出契约 (daily.yml 的下游 step 按这些名字读) ---
SNAPSHOT_DATE = "date"
SNAPSHOT_TOTAL = "total"
SNAPSHOT_TRIGGER = "trigger"


def set_output(name: str, value: str, *, env: Optional[Mapping[str, str]] = None,
               path: Optional[str] = None) -> bool:
    """写一个 step output。

    不在 GitHub Actions 里 (没有 `GITHUB_OUTPUT`) → 什么都不做, 返回
    `False` —— 本地跑 `daily_snapshot` 时不该报错。

    Args:
        name:   输出名。
        value:  输出值 (字符串)。**不要**带换行 (Actions 用行分隔)。
        env:    环境变量映射; `None` = 用 `os.environ`。
        path:   直接指定输出文件 (测试用)。

    Returns:
        写成功 = `True`; 不在 Actions 里 = `False`。
    """
    e = os.environ if env is None else env
    target = path or (e.get("GITHUB_OUTPUT") or "")
    if not target:
        return False
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"{name}={value}\n")
    return True


def set_outputs(values: Mapping[str, str], *,
                env: Optional[Mapping[str, str]] = None) -> bool:
    """写一组 step outputs —— 顺序就是 `values` 的迭代顺序。"""
    e = os.environ if env is None else env
    written = False
    for name, value in values.items():
        written = set_output(name, value, env=e) or written
    return written


def publish_snapshot_outputs(*, date: str, total: float, triggered: bool,
                              env: Optional[Mapping[str, str]] = None) -> bool:
    """发布每日快照的三个 step output。

    ⚠️ `trigger=yes` 的含义是「**需人工关注**」—— 3pp 再平衡触发了, **或**
    网格引擎有**建议**待提交 (引擎从不自己下单)。真正被下游消费的是
    `date` (提交信息里的日期)。
    """
    return set_outputs({
        SNAPSHOT_DATE: date,
        SNAPSHOT_TOTAL: f"{float(total):.2f}",
        SNAPSHOT_TRIGGER: "yes" if triggered else "no",
    }, env=env)


__all__ = [
    "SNAPSHOT_DATE",
    "SNAPSHOT_TOTAL",
    "SNAPSHOT_TRIGGER",
    "set_output",
    "set_outputs",
    "publish_snapshot_outputs",
]
