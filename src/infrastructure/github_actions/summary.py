"""`$GITHUB_STEP_SUMMARY` —— run summary 面板 (D12)。

Actions 的 run 页面右侧有一块 summary。**邮件发没发出去**这种问题, 在
SMTP 之外留一份可读的痕迹很有用: 摘要写在 run 页面上, 不用翻日志。

不在 Actions 里 → 什么都不做 (返回 `False`)。
"""
from __future__ import annotations

import os
from typing import Mapping, Optional


def append_summary(markdown: str, *, env: Optional[Mapping[str, str]] = None,
                   path: Optional[str] = None) -> bool:
    """往 summary 面板追加一段 markdown。

    Returns:
        写成功 = `True`; 不在 Actions 里 = `False`。
    """
    e = os.environ if env is None else env
    target = path or (e.get("GITHUB_STEP_SUMMARY") or "")
    if not target:
        return False
    text = str(markdown)
    if not text.endswith("\n"):
        text += "\n"
    with open(target, "a", encoding="utf-8") as f:
        f.write(text)
    return True


__all__ = ["append_summary"]
