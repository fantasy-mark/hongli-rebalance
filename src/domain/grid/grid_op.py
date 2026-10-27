"""网格操作 (op) 的类型 —— 「**已经发生**」的那一条 (BC-C)。

`advisor.GridSuggestion` 说「**应该**做什么」, 本模块的 `GridOp` 说
「**已经**做了什么」。两者在 `docs/domain_model.md` §6.3 里必须是**不同的
类型** —— 否则「引擎永远不下单」这条不变量就只能靠注释和 code review 守着。

三类 op:

| 类型 | `source` | 含义 |
|---|---|---|
| `ExecutedOp` | `"manual"` | **实盘成交** —— 用户在「交易记录 → 复合网格」提交的 op (scripts/apply_grid.py 落地)。**唯一的实盘来源** |
| `SimulatedOp` | `"backtest"` | **模拟成交** —— 历史价格重放的产物 (`simulation/grid_simulator.py:ops_from_cycles`)。**不是**成交 |
| `GridOp` | 其它 | 未分型的通用形状 —— 遗留的 `"auto"` (自动交易时代引擎自己下的单) 落在这里, **不猜** |

⚠️ **JSON 格式不变**: `data/grid_state.json` 里每一行 op 的 key 与顺序
一字不改 (`to_dict` 负责), 值也**不取整** —— 这些数字原样进出文件, 套
值对象会改掉账 (见 `shared/money.py` 的舍入点纪律)。

**stdlib-only** —— 与 `grid_position.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这条链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .trigger_policy import TriggerLevel

#: op 的种类 —— 与触发级别**同一套词表** (open / add / tp)。
#: 「建议提交哪种 op」和「已经执行了哪种 op」用的是同一组词, 只是类型不同。
OpKind = TriggerLevel


class OpSource(str, Enum):
    """op 的来源 —— 实盘成交 vs 历史重放。"""

    MANUAL = "manual"
    BACKTEST = "backtest"
    #: ⚠️ 遗留值: 自动交易时代引擎自己下的单。迁移脚本
    #: `scripts/reset_grid_positions.py` 已把那些行删净, 这里只为读老文件留着。
    AUTO = "auto"

    def __str__(self) -> str:
        return self.value


def _opt_float(v) -> float | None:
    """Optional float —— `None` 与 `0.0` 必须分得清 (pnl 可以合法地是 0)。"""
    if v is None:
        return None
    return float(v)


def _opt_int(v) -> int | None:
    if v is None:
        return None
    return int(v)


@dataclass(frozen=True)
class GridOp:
    """一次网格操作 —— 「已经发生」的那一条。

    字段 = `data/grid_state.json` 里一行 op 的 key:

        date / op / source / price / shares / note

    tp 的 op 额外带 4 个字段 (`pnl` / `num_buys_exited` /
    `total_shares_exited` / `shares_to_merge`), 非 tp 的 op 这 4 个是
    `None` —— `to_dict` 会**不输出**它们, 与老格式一字不差。

    ⚠️ **值一律是普通 float**, 不套 `Price` / `ShareCount`: 这些数字原样
    存进 `grid_state.json`, 提前取整会改掉账。
    """

    date: str
    op: OpKind
    price: float
    shares: float
    source: OpSource
    note: str = ""
    # --- 只有 tp 才有 ---
    pnl: float | None = None
    num_buys_exited: int | None = None
    total_shares_exited: float | None = None
    shares_to_merge: float | None = None

    def __post_init__(self) -> None:
        # frozen dataclass 要用 object.__setattr__ 写字段
        object.__setattr__(self, "op", OpKind(self.op))
        object.__setattr__(self, "source", OpSource(self.source))
        object.__setattr__(self, "price", float(self.price))
        object.__setattr__(self, "shares", float(self.shares))
        if self.num_buys_exited is not None:
            object.__setattr__(self, "num_buys_exited", int(self.num_buys_exited))

    # -- JSON 边界 ----------------------------------------------------------
    def to_dict(self) -> dict:
        """The **exact** JSON shape `data/grid_state.json` uses.

        key 的顺序与老版本一字不差 (date/op/source/price/shares/note,
        tp 额外四个在后面) —— 否则 `git diff data/grid_state.json` 会
        出现「只有顺序不同」的假 diff。
        """
        out: dict = {
            "date": self.date,
            "op": self.op.value,
            "source": self.source.value,
            "price": self.price,
            "shares": self.shares,
            "note": self.note,
        }
        if self.pnl is not None:
            out["pnl"] = self.pnl
        if self.num_buys_exited is not None:
            out["num_buys_exited"] = self.num_buys_exited
        if self.total_shares_exited is not None:
            out["total_shares_exited"] = self.total_shares_exited
        if self.shares_to_merge is not None:
            out["shares_to_merge"] = self.shares_to_merge
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "GridOp":
        """Parse a persisted op row back into a typed op.

        `source` 决定类型: manual → `ExecutedOp`, backtest → `SimulatedOp`,
        其它 (遗留的 `"auto"`) → 通用 `GridOp` —— **不猜**。
        """
        source = OpSource(str(d.get("source", "manual")))
        kwargs = dict(
            date=str(d.get("date", "")),
            op=OpKind(str(d.get("op", ""))),
            price=float(d.get("price", 0.0) or 0.0),
            shares=float(d.get("shares", 0.0) or 0.0),
            source=source,
            note=str(d.get("note", "") or ""),
            pnl=_opt_float(d.get("pnl")),
            num_buys_exited=_opt_int(d.get("num_buys_exited")),
            total_shares_exited=_opt_float(d.get("total_shares_exited")),
            shares_to_merge=_opt_float(d.get("shares_to_merge")),
        )
        if source is OpSource.BACKTEST:
            return SimulatedOp(**kwargs)
        if source is OpSource.MANUAL:
            return ExecutedOp(**kwargs)
        return GridOp(**kwargs)

    # -- 过渡期: 允许按 dict 的方式读 ---------------------------------------
    # op 在 JSON 里本来就是 dict 形状, 既有调用方 (app.js 的数据来源、
    # scripts/ 的对账脚本、测试) 都按 `o["op"]` 读。保留这个入口, 它们
    # 就不必跟着改。
    def __getitem__(self, key: str):
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


@dataclass(frozen=True)
class ExecutedOp(GridOp):
    """**实盘成交** —— 用户在「交易记录 → 复合网格」提交的 op。

    `source` 固定为 `"manual"` —— 传别的会抛错。这条不变量:
    **没有任何实盘 op 来自引擎** —— 引擎只给建议。
    """

    source: OpSource = OpSource.MANUAL

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.source is not OpSource.MANUAL:
            raise ValueError(
                f"ExecutedOp requires source='manual', got {self.source.value!r}")


@dataclass(frozen=True)
class SimulatedOp(GridOp):
    """**模拟成交** —— 历史价格重放的产物 (`source="backtest"`)。

    ⚠️ 这些行**不是**成交, 只是「如果按回测口径跑, 会发生什么」。页面上
    单独标成「回测」, 不进实盘账。
    """

    source: OpSource = OpSource.BACKTEST

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.source is not OpSource.BACKTEST:
            raise ValueError(
                f"SimulatedOp requires source='backtest', got {self.source.value!r}")


__all__ = [
    "ExecutedOp", "GridOp", "OpKind", "OpSource", "SimulatedOp",
]
