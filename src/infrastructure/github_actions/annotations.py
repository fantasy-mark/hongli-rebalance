"""Actions annotations —— `::error::` / `::warning::` / `::notice::` (D12)。

annotation 会直接显示在 Actions UI 上 (还有订阅者的邮件里), 所以它是
「失败原因**看得见**」的唯一渠道 —— `scripts/apply_grid.py` 的 `fail()`
就是靠它把「批次被拒」摆在用户面前, 而不是只写一行 stderr。

⚠️ **转义**: workflow command 的格式是

    ::error key=value,key2=value2::消息

属性值里的 `:` → `%3A`、`,` → `%2C`、`%` → `%25`; 消息里的换行 → `%0A`。
不转义的话, 消息里一个逗号就能把属性切断, annotation 直接被 Actions
忽略。
"""
from __future__ import annotations

import sys
from typing import Optional

#: 合法的 annotation 级别。
LEVELS = ("error", "warning", "notice")


def _escape_property(value: str) -> str:
    """转义**属性值** —— 逗号/冒号会破坏 `key=value` 的解析。"""
    return (str(value)
            .replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
            .replace(":", "%3A")
            .replace(",", "%2C"))


def _escape_data(value: str) -> str:
    """转义**消息** —— 逗号在消息里是安全的, 但 `%` 和换行不是。"""
    return (str(value)
            .replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A"))


def annotate(level: str, message: str, *, file: Optional[str] = None,
             line: Optional[int] = None, column: Optional[int] = None,
             title: Optional[str] = None,
             stream=None) -> str:
    """打一条 annotation, 返回那行文本 (便于测试断言)。

    Args:
        level:  `error` / `warning` / `notice`。
        message: 人类可读的原因。
        file / line / column / title: 可选的定位信息。
        stream: 写到哪里 (默认 stdout —— Actions 读的就是 stdout)。

    Raises:
        ValueError: `level` 不在 `LEVELS` 里。
    """
    if level not in LEVELS:
        raise ValueError(f"未知的 annotation 级别: {level!r} (合法: {LEVELS})")

    props = {}
    if file:
        props["file"] = file
    if line is not None:
        props["line"] = str(int(line))
    if column is not None:
        props["column"] = str(int(column))
    if title:
        props["title"] = title

    head = f"::{level}"
    if props:
        head += " " + ",".join(f"{k}={_escape_property(v)}"
                                for k, v in props.items())
    head += "::"
    out = head + _escape_data(message)
    print(out, file=sys.stdout if stream is None else stream)
    return out


def error(message: str, **kwargs) -> str:
    """`::error::` —— 失败原因。"""
    return annotate("error", message, **kwargs)


def warning(message: str, **kwargs) -> str:
    """`::warning::` —— 提醒。"""
    return annotate("warning", message, **kwargs)


def notice(message: str, **kwargs) -> str:
    """`::notice::` —— 中性提示。"""
    return annotate("notice", message, **kwargs)


__all__ = ["LEVELS", "annotate", "error", "warning", "notice"]
