"""账本**写路径**的用例 —— 页面「交易记录 → 交易表单」提交的那一批。

`data/trades.json` 是**唯一的事实源** (BC-A); `data/holdings.json` 只是它的
派生缓存。页面的每一次提交走这里: 校验 → 读账本 → 重放 → 写两份文件。

用例的形状跟 `daily_snapshot` 一样 —— 一串具名步骤, 规则一条都不在这里:

    validate_batch   校验整批 payload (schema / 条数 / 每一条的字段)
    assign_ids       服务端发 id = <date>-<side>-<seq>
    apply_batch      重放**合并后**的账本 → 持仓 + 两个现金池
    apply_trades     上面三步 + 读盘 + 写盘 (完整的写路径)

⚠️ **重放只有一份实现**: `src/domain/ledger/ledger.py` —— 跟每日快照
(`daily_snapshot.replay_trade_ledger`) **同一份**。以前
`scripts/apply_holdings.py` 自己抄了一份 `inline_apply` / `inline_rebuild`,
抄的时候还留了句注释说「因为 workflow 不装依赖」—— 那条理由早就过期了
(`src/domain/ledger/*` 是 stdlib-only)。两份实现只可能长歪。

⚠️ 由此带来**一处口径收紧** (Phase 5c-2): 领域层的不变量 I1 是「两个现金池
都不得为负」, 而且是**逐笔**检查; 旧脚本是**整批结束后**才查一次。
逐笔更严 —— 它挡住的是「先记一笔买入、后记一笔入金」这种会让每日快照
(走领域层重放) 直接崩掉的账本。现在写路径与读路径**同一把尺子**。

⚠️ **stdlib-only** —— 跑它的 GitHub Actions job **不装** pandas/akshare
(跟 `src/domain/grid/*` 同一条规矩)。
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from ..domain.events.domain_events import EventStream
from ..domain.ledger.projection import project_ledger
from ..domain.ledger.trade_side import AMOUNT_SIDES, SHARE_SIDES, TradeSide
from ..domain.shared.identifiers import AssetCode, TradingDate
from ..infrastructure.json_repositories import read_trades, write_json

#: payload 的 schema 版本。
SCHEMA = "trades-v1"

#: 一批最多多少条 (防手滑)。
MAX_ROWS = 20

#: 合法 side —— 词表的**本体**是 `TradeSide` (八个)。以前
#: `scripts/apply_holdings.py` 自己抄了一份 `VALID_SIDES` —— 改一处漏一处。
VALID_SIDES: frozenset[str] = frozenset(s.value for s in TradeSide)

#: 需要 code + shares + price (+ 可选 fee) 的两类。
ETF_SIDES = SHARE_SIDES

#: 只要 amount 的六类。
CASH_SIDES = AMOUNT_SIDES


class TradeError(ValueError):
    """用例层拒绝这一批 —— CLI 层负责翻译成退出码。

    规则错误**不是** `SystemExit`: 用例层不该决定进程怎么退出。
    """


@dataclass(frozen=True)
class TradeBatch:
    """一批交易落地之后的结果。

    Attributes:
        merged:        existing + new (带 id) —— 写进 trades.json 的内容
        new_trades:    只是这一批新加的 (写进 echo)
        positions:     重放出来的持仓 —— 写进 holdings.json
        cash_rebalance / cash_grid: 两个现金池
        events:        这一批发出的领域事件 (Phase 5b)
    """

    merged: list[dict] = field(default_factory=list)
    new_trades: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    cash_rebalance: float = 0.0
    cash_grid: float = 0.0
    events: EventStream = field(default_factory=EventStream)


# -----------------------------------------------------------------------------
# 校验
# -----------------------------------------------------------------------------

def _coerce_number(v: Any, name: str, allow_zero: bool = False) -> float:
    try:
        n = float(v)
    except (ValueError, TypeError):
        raise TradeError(f"{name} is not a number: {v!r}")
    if n < 0 or (not allow_zero and n <= 0):
        raise TradeError(f"{name} must be > 0, got {n}")
    return n


def validate_trade(t: Any, idx: int, today: datetime.date,
                   allowed_codes: Mapping[str, str]) -> dict:
    """校验一笔交易 —— 返回**清洗后**的行。

    清洗 = 类型收好 + **丢掉不相关的字段** (现金类不许带 code/shares/price),
    语义不变。key 的顺序也是契约 —— 它直接决定写进 trades.json 的样子。
    """
    if not isinstance(t, dict):
        raise TradeError(f"trade {idx}: not an object")
    side = str(t.get("side", "")).strip()
    if side not in VALID_SIDES:
        raise TradeError(
            f"trade {idx}: side must be one of {sorted(VALID_SIDES)}, got {side!r}")
    raw_date = str(t.get("date", "")).strip()
    try:
        trade_date = TradingDate(raw_date)
    except ValueError:
        raise TradeError(f"trade {idx}: date must be YYYY-MM-DD, got {raw_date!r}")
    if trade_date > today:
        # 容忍 today+1d (时区缓冲)
        if trade_date > today + datetime.timedelta(days=1):
            raise TradeError(
                f"trade {idx}: date {raw_date} is in the future (today={today})")
    note = str(t.get("note", "") or "").strip()

    out: dict = {"side": side, "date": trade_date.isoformat(), "note": note}
    if side in ETF_SIDES:
        raw_code = str(t.get("code", "")).strip()
        try:
            code = AssetCode(raw_code)
        except ValueError:
            raise TradeError(
                f"trade {idx}: code must be 6 digits, got {raw_code.split('.')[0]!r}")
        if code not in allowed_codes:
            raise TradeError(
                f"trade {idx}: code {code} not in whitelist; allowed: "
                f"{sorted(allowed_codes)}")
        out["code"] = code
        out["shares"] = _coerce_number(t.get("shares"), f"trade {idx}.shares")
        out["price"] = _coerce_number(t.get("price"), f"trade {idx}.price")
        fee = t.get("fee", 0.0)
        if fee is None or fee == "":
            fee = 0.0
        out["fee"] = _coerce_number(fee, f"trade {idx}.fee", allow_zero=True)
    else:  # 现金类 (存取 / 池间划转)
        if "code" in t and str(t.get("code", "")).strip():
            raise TradeError(f"trade {idx}: {side} must not have a code")
        if "shares" in t and str(t.get("shares", "")).strip():
            raise TradeError(f"trade {idx}: {side} must not have shares")
        if "price" in t and str(t.get("price", "")).strip():
            raise TradeError(f"trade {idx}: {side} must not have a price")
        out["amount"] = _coerce_number(t.get("amount"), f"trade {idx}.amount")
    return out


def validate_batch(payload: Mapping[str, Any], *, today: datetime.date,
                   allowed_codes: Mapping[str, str]) -> list[dict]:
    """校验**整批** payload —— 返回清洗后的行。

    ⚠️ 整批 all-or-nothing: 任何一条不合法 → 整批拒绝 (抛 `TradeError`)。
    """
    # 老格式 {rows, cash} 直接拒 —— 编辑器已经升级, 让用户刷新浏览器。
    if "rows" in payload and "trades" not in payload:
        raise TradeError(
            "editor 已升级为交易表单. 请刷新页面 (Ctrl+Shift+R) 重新加载 app.js.")
    schema = payload.get("schema", "")
    if schema != SCHEMA:
        raise TradeError(
            f"schema mismatch: only '{SCHEMA}' accepted, got {schema!r}")
    raw_trades = payload.get("trades", [])
    if not isinstance(raw_trades, list):
        raise TradeError(
            f"trades must be a JSON array, got {type(raw_trades).__name__}")
    if len(raw_trades) > MAX_ROWS:
        raise TradeError(f"too many trades ({len(raw_trades)} > {MAX_ROWS})")
    if not raw_trades:
        raise TradeError("no trades to apply")
    return [validate_trade(t, i, today, allowed_codes)
            for i, t in enumerate(raw_trades)]


# -----------------------------------------------------------------------------
# 发 id
# -----------------------------------------------------------------------------

def _parse_seq(id_str: str) -> int:
    """从已有 id (如 '2026-10-12-buy-002') 里抠出末尾的序号。
    id 坏了就返回 0 (这样下一个 id 至少是 001)。"""
    m = re.search(r"-(\d+)$", id_str or "")
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def assign_ids(new_trades: list[dict], existing: list[dict]) -> list[dict]:
    """服务端 id = <date>-<side>-<seq>; seq 数的是 existing + 已分配的新交易里
    同 (date, side) 的条数。

    ⚠️ 必须在**重放之前**跑 —— 序号要按 (date, side) 数, 而重放要按 id 排序。
    """
    counts: dict[tuple[str, str], int] = {}
    for t in existing:
        key = (t.get("date", ""), t.get("side", ""))
        if key[0] and key[1]:
            counts[key] = max(counts.get(key, 0), _parse_seq(t.get("id", "")))
    for t in new_trades:
        key = (t["date"], t["side"])
        counts[key] = counts.get(key, 0) + 1
        t["id"] = f"{key[0]}-{key[1]}-{counts[key]:03d}"
    return new_trades


# -----------------------------------------------------------------------------
# 应用
# -----------------------------------------------------------------------------

def apply_batch(cleaned: list[dict], existing: list[dict], *,
                name_lookup: Mapping[str, str],
                events: Optional[EventStream] = None) -> TradeBatch:
    """重放**合并后**的账本 —— 返回新的持仓 + 两个现金池。

    ⚠️ 重放用的是 `src/domain/ledger/ledger.py` —— 与每日快照**同一份**实现。
    不变量 I1 (两个池都不得为负) 与 I2 (卖出不得超过持仓) 在这里逐笔生效。

    Raises `TradeError` if the ledger rejects a trade (oversell / 现金为负)。
    """
    merged = list(existing) + list(cleaned)
    stream = events if events is not None else EventStream()
    try:
        positions, cash_rebalance, cash_grid = project_ledger(
            merged, name_lookup=name_lookup, events=stream)
    except ValueError as ex:
        raise TradeError(str(ex)) from ex
    # 整份的份额写成 int —— 存量 holdings.json 的形状 (32000 而不是 32000.0)
    records = [
        {"name": p["name"], "code": p["code"],
         "shares": int(p["shares"]) if p["shares"] == int(p["shares"])
                   else p["shares"]}
        for p in positions
    ]
    return TradeBatch(merged=merged, new_trades=list(cleaned),
                      positions=records,
                      cash_rebalance=cash_rebalance, cash_grid=cash_grid,
                      events=stream)


# -----------------------------------------------------------------------------
# 文档 (写盘的内容)
# -----------------------------------------------------------------------------

def trades_document(merged: list[dict]) -> dict:
    """`data/trades.json` 的内容 —— 账本是**只追加**的, 顺序即历史。"""
    return {
        "_schema": "trades-v1",
        "_comment": ("Append-only trade ledger. Source of truth for cash "
                     "and positions[].shares. Do not reorder; corrections "
                     "are new entries."),
        "trades": merged,
    }


def holdings_document(positions: list[dict], cash_rebalance: float,
                      cash_grid: float) -> dict:
    """`data/holdings.json` 的内容 —— 账本的**派生缓存** (双池)。"""
    return {
        "_schema": "v2",
        "_comment": ("REBUILT from data/trades.json. 双池: "
                     "cash_rebalance (真实现金, ETF买卖用) + "
                     "cash_grid (虚拟, 网格记账用). 编辑持仓请用页面"
                     "交易表单 (会同时更新 trades.json); 直接改这个文件"
                     "会被下一次 apply-holdings 覆盖."),
        "cash_rebalance": cash_rebalance,
        "cash_grid": cash_grid,
        "positions": positions,
    }


# -----------------------------------------------------------------------------
# 完整写路径
# -----------------------------------------------------------------------------

def apply_trades(payload: Mapping[str, Any], trades_path: Path,
                 holdings_path: Path, *,
                 today: datetime.date,
                 allowed_codes: Mapping[str, str],
                 name_lookup: Mapping[str, str]) -> TradeBatch:
    """完整的写路径: 校验 → 读盘 → 发 id → 重放 → 写两份文件。

    ⚠️ **整批 all-or-nothing**: 校验或重放不通过 → 两份文件都不写。

    Raises `TradeError` on any rejection — the caller (the CLI) turns it
    into an exit code.
    """
    cleaned = validate_batch(payload, today=today, allowed_codes=allowed_codes)
    try:
        existing = read_trades(trades_path)
    except ValueError as ex:
        raise TradeError(str(ex)) from ex
    cleaned = assign_ids(cleaned, existing)
    batch = apply_batch(cleaned, existing, name_lookup=name_lookup)
    write_json(trades_path, trades_document(batch.merged), trailing_newline=True)
    write_json(holdings_path,
               holdings_document(batch.positions, batch.cash_rebalance,
                                 batch.cash_grid),
               trailing_newline=True)
    return batch


__all__ = [
    "SCHEMA", "MAX_ROWS", "VALID_SIDES", "ETF_SIDES", "CASH_SIDES",
    "TradeError", "TradeBatch",
    "validate_trade", "validate_batch", "assign_ids", "apply_batch",
    "trades_document", "holdings_document", "apply_trades",
]
