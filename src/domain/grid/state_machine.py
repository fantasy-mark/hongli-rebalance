"""网格状态机的**合法边** —— 「一个周期 N 买 1 卖」的形状 (BC-C)。

唯一一处定义「哪些动作在什么状态下合法」的地方。`GridPosition.manual_op`
只查这张表 —— 合法边不散落在 if 语句里。

    Idle ──open──► Open(N 层) ──add──► Open(N+1 层)
      ▲                                │
      └──────── exit (cost-only) ──────┘
            + 归并份额移交组合

**「N 买 1 卖」就是这张表的形状**, 不是一句约定:

  - 从 Idle 出去**只有** `open` —— 所以 `add` / `tp` 不可能凭空出现;
  - 回到 Idle 的**唯一**一条边是 `tp` —— 而 `tp` 把整个周期结束掉
    (entries 清空、`in_cycle` 归零), 所以一个周期里 `add` 可以来任意多次
    (N 买), `tp` **只能**发生一次 (1 卖)。

⚠️ 词表是 `TriggerLevel` 的投影 —— 不另抄一份 (跟 `apply_grid_op.VALID_OPS`
同一个道理, D10)。

⚠️ **stdlib-only** —— 与 `grid_position.py` / `trigger_policy.py` 同样的理由:
`scripts/apply_grid.py` 在不装 pandas/akshare 的 GitHub Actions job 里
import 这条链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .trigger_policy import TriggerLevel


@dataclass(frozen=True)
class GridTransition:
    """状态机的一条边。

    Attributes:
        op:            这条边的动作 (`TriggerLevel` 的一个)
        from_in_cycle: 走这条边之前**必须**在周期内吗
        to_in_cycle:   走这条边之后在周期内吗
        requirement:   不满足 `from_in_cycle` 时的报错文案
                       (「为什么这一步不合法」写在这里, 不写在调用处)
    """

    op: TriggerLevel
    from_in_cycle: bool
    to_in_cycle: bool
    requirement: str


#: 状态机的**全部**合法边 —— 表以外的 (状态, 动作) 组合都是非法边。
LEGAL_TRANSITIONS: tuple[GridTransition, ...] = (
    GridTransition(
        op=TriggerLevel.OPEN, from_in_cycle=False, to_in_cycle=True,
        requirement="open requires no open cycle (use tp to close first)"),
    GridTransition(
        op=TriggerLevel.ADD, from_in_cycle=True, to_in_cycle=True,
        requirement="add requires an open cycle"),
    GridTransition(
        op=TriggerLevel.TP, from_in_cycle=True, to_in_cycle=False,
        requirement="tp requires an open cycle"),
)

_BY_KEY: dict[tuple[bool, str], GridTransition] = {
    (t.from_in_cycle, t.op.value): t for t in LEGAL_TRANSITIONS
}
_BY_OP: dict[str, GridTransition] = {t.op.value: t for t in LEGAL_TRANSITIONS}


def known_op(op: Union[str, TriggerLevel]) -> str:
    """op 在不在词表里 —— 不在就抛错。返回归一后的裸字符串。

    「未知 op」这条消息**只**在这里写一遍 (别处只调它)。

    ⚠️ `TriggerLevel` 是 str 的子类, 但它的 `repr()` 不是 `'open'`
    (会打印成 `<TriggerLevel.OPEN: 'open'>`) —— 所以先把枚举摊成裸值,
    报错文案里不能出现那个。
    """
    raw = op.value if isinstance(op, TriggerLevel) else op
    key = raw if isinstance(raw, str) else str(raw)
    if key not in _BY_OP:
        raise ValueError(f"unknown op: {raw!r}")
    return key


def transition(in_cycle: bool, op: Union[str, TriggerLevel]) -> GridTransition:
    """走一步状态机 —— 非法边就抛错 (消息说清楚为什么)。

    Args:
        in_cycle: 走这一步之前的状态 (True = 周期中)
        op:       动作

    Returns:
        这条边 —— 调用方读 `edge.to_in_cycle` 决定新状态。

    Raises:
        ValueError: op 不在词表里, 或者这条 (状态, 动作) 的边不存在。
    """
    key = known_op(op)
    edge = _BY_KEY.get((bool(in_cycle), key))
    if edge is None:
        # 动作认识, 但当前状态下没有这条边 → 报该动作的「前置条件」
        raise ValueError(_BY_OP[key].requirement)
    return edge


__all__ = ["GridTransition", "LEGAL_TRANSITIONS", "known_op", "transition"]
