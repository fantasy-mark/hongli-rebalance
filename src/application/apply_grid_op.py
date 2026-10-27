"""复合网格**写路径**的用例 —— 页面「交易记录 → 复合网格」提交的那一批。

⚠️ 这是实盘网格的**唯一**入口: 引擎只给建议 (open / add / tp), 真正落地的
动作全部由用户在这里提交 (`scripts/apply_grid.py` 现在只是这层用例的 CLI 外壳)。

用例的形状跟 `daily_snapshot` 一样 —— 一串具名步骤, 规则一条都不在这里:

    validate_batch   校验整批 payload (schema / 条数 / 每一条的字段)
    apply_batch      顺序应用 (同一资产的多条按提交顺序连续生效)
    apply_grid_ops   上面两步 + 读盘 + 写盘 (完整的写路径)

⚠️ **整批 all-or-nothing**: 任何一条不合法 → 整批拒绝, **不写盘**。
校验在读盘**之前**做完 —— 一条都不合法时, 连文件都不用读。

⚠️ **词表只有一个来源**: 合法的 op 是 `TriggerLevel` (open / add / tp) 的
投影。以前 `scripts/apply_grid.py` 自己抄了一份 `VALID_OPS = {"open","add","tp"}`
—— 改一处漏一处 (D10)。

⚠️ **stdlib-only** —— 跑它的 GitHub Actions job **不装** pandas/akshare
(跟 `src/domain/grid/*` 同一条规矩)。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from ..domain.events.domain_events import EventStream
from ..domain.grid.grid_position import fresh_asset_state, manual_op
from ..domain.grid.trigger_policy import TriggerLevel
from ..domain.shared.identifiers import AssetCode, TradingDate
from ..infrastructure.json_repositories import read_grid_state, write_json

#: payload 的 schema 版本。
SCHEMA = "grid-ops-v1"

#: 一批最多多少条 (防手滑)。
MAX_OPS = 20

#: 合法的 op —— 词表的**本体**是 `TriggerLevel` (open / add / tp)。
#: 以前 `scripts/apply_grid.py` 自己抄了一份 `VALID_OPS = {"open","add","tp"}`
#: —— 改一处漏一处 (D10)。现在它只是 `TriggerLevel` 的投影。
VALID_OPS: frozenset[str] = frozenset(level.value for level in TriggerLevel)


class GridOpError(ValueError):
    """用例层拒绝这一批 —— CLI 层负责翻译成退出码 + `::error::` annotation。

    规则错误**不是** `SystemExit`: 用例层不该决定进程怎么退出。
    """


@dataclass(frozen=True)
class GridOpBatch:
    """一批网格操作落地之后的结果。

    Attributes:
        state:   写盘后的网格状态 (整个文件的内容)
        summary: 每条操作一行 —— 给人看的摘要
        events:  这一批发出的领域事件 (Phase 5b)
    """

    state: dict
    summary: list[dict] = field(default_factory=list)
    events: EventStream = field(default_factory=EventStream)


# -----------------------------------------------------------------------------
# 单条操作 (对 domain 的薄封装)
# -----------------------------------------------------------------------------

def inline_manual_op(state: dict, op: str, today: str,
                     price: float, shares: float,
                     note: str = "", asset: str = "", code: str = "",
                     events: Optional[EventStream] = None) -> tuple[dict, float]:
    """Apply a manual grid operation. Returns (new_state, pnl_delta).

    `src.domain.grid.grid_position.manual_op` 的薄封装 —— **单一实现**,
    不再复制规则。保留 `(state, pnl_delta)` 的返回形状, 兼容既有调用方与测试。

    For `tp` the "sell cost only" rule applies (see cost_only_exit):
    pnl_delta is 0 on a normal above-cost exit, because only the principal
    is sold and the remainder is merged into the rebalance portfolio
    (`merged_shares`). It is negative only if the user exits BELOW cost,
    where we sell out and book the loss. For `open`/`add` it is 0.

    `events` (optional, Phase 5b) — 顺带发出 `GridCycleOpened` /
    `GridLayerAdded` / `GridCycleClosed` / `MergedSharesTransferred`。

    Raises ValueError on precondition failure.
    """
    new_state = manual_op(state, op=op, today=today, price=price,
                          shares=shares, note=note, asset=asset, code=code,
                          events=events)
    pnl_delta = 0.0
    if op == "tp":
        pnl_delta = float(new_state["ops"][-1].get("pnl", 0.0))
    return new_state, pnl_delta


# -----------------------------------------------------------------------------
# 校验
# -----------------------------------------------------------------------------

def _coerce_number(v: Any, name: str, allow_zero: bool = False) -> float:
    try:
        n = float(v)
    except (ValueError, TypeError):
        raise GridOpError(f"{name} is not a number: {v!r}")
    if n < 0 or (not allow_zero and n <= 0):
        raise GridOpError(f"{name} must be > 0, got {n}")
    return n


def validate_op(op: Any, idx: int, today: datetime.date,
                allowed_codes: Mapping[str, str]) -> dict:
    """校验一条 op —— 返回**清洗后**的行。

    清洗 = 类型收好 (code / date 归一成值对象, 数字归一成 float),
    语义不变。
    """
    if not isinstance(op, dict):
        raise GridOpError(f"op {idx}: not an object")
    kind = str(op.get("op", "")).strip()
    if kind not in VALID_OPS:
        raise GridOpError(
            f"op {idx}: op must be one of {sorted(VALID_OPS)}, got {kind!r}")
    raw_date = str(op.get("date", "")).strip()
    try:
        trade_date = TradingDate(raw_date)
    except ValueError:
        raise GridOpError(f"op {idx}: date must be YYYY-MM-DD, got {raw_date!r}")
    if trade_date > today:
        # 容忍 today+1d (时区缓冲)
        if trade_date > today + datetime.timedelta(days=1):
            raise GridOpError(
                f"op {idx}: date {raw_date} is in the future (today={today})")
    raw_code = str(op.get("code", "")).strip()
    try:
        code = AssetCode(raw_code)
    except ValueError:
        raise GridOpError(
            f"op {idx}: code must be 6 digits, got {raw_code.split('.')[0]!r}")
    if code not in allowed_codes:
        raise GridOpError(
            f"op {idx}: code {code} not in whitelist; allowed: "
            f"{sorted(allowed_codes)}")
    price = _coerce_number(op.get("price"), f"op {idx}.price")
    note = str(op.get("note", "") or "").strip()
    out = {"op": kind, "date": trade_date.isoformat(), "code": code,
           "price": price, "note": note}
    if kind in ("open", "add"):
        out["shares"] = _coerce_number(op.get("shares"), f"op {idx}.shares")
    else:  # tp
        # tp 的数量被忽略 —— 卖出量由周期自己的投入额算出来
        sh_raw = op.get("shares", 0.0)
        try:
            out["shares"] = float(sh_raw) if sh_raw not in (None, "") else 0.0
        except (ValueError, TypeError):
            raise GridOpError(f"op {idx}: shares must be a number, got {sh_raw!r}")
    return out


def validate_batch(payload: Mapping[str, Any], *, today: datetime.date,
                   allowed_codes: Mapping[str, str]) -> list[dict]:
    """校验**整批** payload —— 返回清洗后的行。

    ⚠️ 整批 all-or-nothing: 任何一条不合法 → 整批拒绝 (抛 `GridOpError`)。
    """
    schema = payload.get("schema", "")
    if schema != SCHEMA:
        raise GridOpError(
            f"schema mismatch: only '{SCHEMA}' accepted, got {schema!r}")
    raw_ops = payload.get("ops", [])
    if not isinstance(raw_ops, list):
        raise GridOpError(
            f"ops must be a JSON array, got {type(raw_ops).__name__}")
    if len(raw_ops) > MAX_OPS:
        raise GridOpError(f"too many ops ({len(raw_ops)} > {MAX_OPS})")
    if not raw_ops:
        raise GridOpError("no ops to apply")
    return [validate_op(o, i, today, allowed_codes)
            for i, o in enumerate(raw_ops)]


# -----------------------------------------------------------------------------
# 应用
# -----------------------------------------------------------------------------

def apply_batch(cleaned: list[dict], state: dict, *,
                allowed_codes: Mapping[str, str],
                events: Optional[EventStream] = None) -> GridOpBatch:
    """顺序应用一批网格操作 —— 返回**新**的网格状态 (不改传入的 `state`)。

    同一资产的多条按**提交顺序**连续生效: 第二条看到的是第一条留下的状态。

    Raises `GridOpError` if a precondition fails (open on an in-cycle asset,
    add/tp without a cycle …) —— 消息里带着是第几条 op。
    """
    stream = events if events is not None else EventStream()
    new_state: dict = dict(state or {})
    summary: list[dict] = []
    for o in cleaned:
        name = allowed_codes[o["code"]]
        # 没见过的资产 → 全新状态 (6 个 key, 顺序由 domain 的 mapper 说了算)
        asset_state = new_state.get(name)
        if not isinstance(asset_state, dict):
            asset_state = fresh_asset_state()
        else:
            asset_state = dict(asset_state)
        # 兼容: 极老的 state 用单个 last_120d_min 记窗口最低价
        if (not asset_state.get("last_prices")
                and asset_state.get("last_120d_min") is not None):
            asset_state["last_prices"] = [float(asset_state["last_120d_min"])]

        before_in_cycle = bool(asset_state.get("in_cycle", False))
        before_cum = float(asset_state.get("cum_realized", 0.0))

        try:
            new_asset_state, pnl_delta = inline_manual_op(
                asset_state, op=o["op"], today=o["date"],
                price=o["price"], shares=o.get("shares", 0.0),
                note=o["note"], asset=name, code=o["code"], events=stream,
            )
        except ValueError as ex:
            raise GridOpError(f"op {len(summary)} ({o['op']} {o['code']}): {ex}")

        new_state[name] = new_asset_state
        summary.append({
            "code": o["code"], "name": name, "op": o["op"],
            "date": o["date"], "price": o["price"], "shares": o.get("shares", 0.0),
            "note": o["note"],
            "before_in_cycle": before_in_cycle,
            "after_in_cycle": bool(new_asset_state["in_cycle"]),
            "before_cum_realized": before_cum,
            "after_cum_realized": float(new_asset_state["cum_realized"]),
            "num_entries_after": len(new_asset_state["entries"]),
            "pnl_delta": pnl_delta,
        })
    return GridOpBatch(state=new_state, summary=summary, events=stream)


# -----------------------------------------------------------------------------
# 完整写路径
# -----------------------------------------------------------------------------

def apply_grid_ops(payload: Mapping[str, Any], state_path: Path, *,
                   today: datetime.date,
                   allowed_codes: Mapping[str, str]) -> GridOpBatch:
    """完整的写路径: 校验 → 读盘 → 应用 → 写盘。

    ⚠️ **整批 all-or-nothing**: 校验不通过 → 连文件都不读, 更不写。

    Raises `GridOpError` on any rejection — the caller (the CLI) turns it
    into an exit code.
    """
    cleaned = validate_batch(payload, today=today, allowed_codes=allowed_codes)
    try:
        state = read_grid_state(state_path)
    except ValueError as ex:
        raise GridOpError(str(ex)) from ex
    batch = apply_batch(cleaned, state, allowed_codes=allowed_codes)
    write_json(state_path, batch.state)
    return batch


__all__ = [
    "SCHEMA", "MAX_OPS", "VALID_OPS",
    "GridOpError", "GridOpBatch",
    "inline_manual_op", "validate_op", "validate_batch",
    "apply_batch", "apply_grid_ops",
]
