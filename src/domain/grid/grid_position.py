"""Compound (pyramid) grid — **the domain model** (BC-C).

⚠️ The live engine no longer opens/adds/exits positions. `advance_one_day()`
only rolls the 120-day window and *reports* what the rules would suggest.
Every real open/add/tp comes from the user submitting an op in the Pages
editor ("交易记录 → 复合网格"), which lands in `data/grid_state.json` via
`scripts/apply_grid.py` → `manual_op`.

Rules (per asset, independent) — the **suggestion** the engine reports:
  - open: today's price < min(price of past `lookback` days) → "建议开仓".
    The "price" passed in by record.py is the day's K-line LOW (intraday)
    so the suggestion can fire on an intraday dip even if the close is
    above yesterday's close; the trigger logic is price-agnostic — any
    monotonic series works.
  - add:  while in a cycle, a further `step_pct` % drop from the LAST buy
    price → "建议加仓" (another `layer_yuan` layer).
  - tp:   when price >= first_buy_price * (1 + tp_pct/100) → "建议回本出场".
    The exit rule is "sell only the cost amount" (`total_invested / price`
    shares, recovering exactly the principal) and the leftover
    `total_shares - total_invested/price` shares are **merged into the
    rebalance portfolio** (`merged_shares`) — they leave the grid's book
    and become ordinary portfolio holdings — see `cost_only_exit`.
    So realized P&L per cycle is 0 by construction, and all return comes
    from the merged shares' appreciation.

One cycle = N buys + 1 exit.  The N buys are the open plus the pyramid
adds; the exit is a single tp whose leftover (bought − sold) is merged
into the rebalance portfolio.

State persistence: `advance_one_day()` returns a JSON-serializable dict so
the Action can commit `data/grid_state.json` after each run.

Layer sizing:
  layer_yuan(d) = GRID_BASE_YUAN + max(pool_value - recent_min, 0) * GRID_FLOATING_FRACTION
  where recent_min = min(pool_value over the past GRID_FLOATING_WINDOW trading days).

---

**Layering note (2026-10-26)**: this module is the *domain* half of the old
monolithic `src/grid.py`. The *simulation* half (`run_compound_grid`,
`ops_from_cycles`, `merge_backtest_ops`) moved to
`src/simulation/grid_simulator.py` — it is a **separate bounded context**
(BC-E) that replays historical closes and must never write grid_state.

**This module must stay stdlib-only** (no pandas / no akshare at module
level) — `scripts/apply_grid.py` imports it inside a GitHub Actions job
that does not install those dependencies. pandas is a *local* import inside
`run_compound_grid`, which now lives in the simulation layer.

---

**Phase 2 note (2026-10-27)**: 状态形状从「散落的 6 key 字面量」收成
`GridPosition` 聚合根 + 显式 mapper (见 `to_state` / `from_state`)。
这消掉了不变量 **I11** 的静默丢数据风险: 以前 `advance_one_day` 与
`manual_op` 各自用一份 6 key 字面量重建状态, 任何新增字段都会在**下一次
每日运行时被静默丢弃** (2026-10-23 的 `free_shares` → `merged_shares`
改名就是这么丢过一次)。现在:
  - 输出侧: `to_state` 校验产出的 key 集合 == dataclass 的字段集合,
    **新增字段忘了写 mapper 会立刻抛错**;
  - 输入侧: `from_state` 拒绝未知 key, 不再默默忽略。

`data/grid_state.json` 的 **JSON 格式不变** —— mapper 负责读写。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Optional

from ..shared.money import Money, Price, ShareCount
from .advisor import GridAdvisor, GridSuggestion
from .exit_policy import ExitAtCost, cost_only_exit
from .grid_op import ExecutedOp, GridOp, OpKind, OpSource
from .trigger_policy import (
    GRID_STEP_PCT,
    GRID_TP_BASIS,
    GRID_TP_PCT,
    TriggerLevel,
    TriggerPolicy,
)

# 网格参数 (env-overridable for parameter sweeps)
GRID_BASE_YUAN: float = float(os.environ.get("GRID_BASE_YUAN", "10000"))
GRID_FLOATING_FRACTION: float = float(os.environ.get("GRID_FLOATING_FRACTION", "0.05"))
GRID_FLOATING_WINDOW: int = int(os.environ.get("GRID_FLOATING_WINDOW", "756"))
GRID_LOOKBACK: int = 120
# ⚠️ 触发参数 (`GRID_STEP_PCT` / `GRID_TP_PCT` / `GRID_TP_BASIS`) 的**本体**
# 在 `trigger_policy.py` —— 这里只是 re-export, 既有 import 不断。
# "avg_cost"   — exit when price >= avg_cost * (1 + tp_pct/100)             [v2]
# "first_buy"  — exit when price >= first_buy_price * (1 + tp_pct/100)       [v3]


def layer_yuan(pool_value: float, recent_min: float) -> float:
    """Compute today's per-layer buy amount in ¥.

    ⚠️ 返回**未取整**的 float —— 层额要除以价格算份额 (`amount / price`),
    提前取整会改掉份额。它只在**输出边界**上被包成 `LayerSize`
    (见 `advisory` 的 `suggest_amount` 与快照里的 `layer_yuan`)。

    Args:
        pool_value: current 3pp portfolio total (¥).
        recent_min: min pool value over the past GRID_FLOATING_WINDOW days.
    """
    floating = max(pool_value - recent_min, 0.0)
    return GRID_BASE_YUAN + floating * GRID_FLOATING_FRACTION


# -----------------------------------------------------------------------------
# 止盈规则 (single source of truth)
# -----------------------------------------------------------------------------
# 网格止盈时**只卖出成本金额**: 卖出 `total_invested / price` 份, 恰好把本金
# 取回来, 于是每个周期的已实现盈亏恒为 0; 剩下的份额**归并到再平衡的持仓**
# (merged shares) —— 它们不再是网格的资产, 而是组合的一部分, 此后按再平衡
# 的规则参与权重计算。
#
# 这实质上把网格变成了买入持有: 止盈不再兑现浮盈, 只是把它永久化并交给组合。
# 网格仍然决定加仓的层数和时点, 但不再有退出机制。
#
# 口径后果 (所有消费方都必须同步, 否则指标会说谎):
#   - `cum_realized` 恒为 0 —— 已实现盈亏不再是有效的收益指标;
#   - 收益体现在**再平衡那一栏的持仓**里 (归并份额 × 市价), 不在网格账上;
#   - 网格的**资金占用只算在周期内的层** —— 出场后本金回来了, 资金占用回落;
#   - 再平衡的权重/总市值要**把归并份额算进去**, 否则组合的真实敞口被低估。
#
# 归并份额是**模拟口径**: 网格本身是虚拟策略, 所以归并进组合的份额也是模拟
# 的, 不写进 data/trades.json (那里只记录真实成交)。
#
# ⚠️ 规则本体在 `exit_policy.py` (`CostOnlyExitPolicy` / `cost_only_exit`)。
# 这里保留 `split_take_profit` 作为**兼容别名** —— 旧的 import 路径还能用,
# 但它返回的是旧版的 dict 形状; 新代码请用 `cost_only_exit` (返回
# `ExitAtCost` 数据类)。

def split_take_profit(total_shares: float, total_invested: float,
                      price: float) -> dict:
    """⚠️ 兼容别名 —— 规则本体见 `exit_policy.cost_only_exit`。

    返回旧版的 dict 形状 {shares_sold, proceeds, merged_shares, pnl}。
    新代码请直接用 `cost_only_exit(...)` → `ExitAtCost`。
    """
    return cost_only_exit(total_shares, total_invested, price).to_dict()


# -----------------------------------------------------------------------------
# 聚合根: GridPosition —— 一个资产一份网格状态
# -----------------------------------------------------------------------------
# ⚠️ **JSON 格式不变**: `data/grid_state.json` 里每个资产仍然是**同样那 6 个
# key**, 一字不差。变化的是「谁定义这 6 个 key」—— 从散落在两个 mutator
# 里的字面量, 收成这一个 dataclass + 一对显式 mapper。

#: 状态里允许出现的 key —— **唯一**的事实源。
#: `to_state` 会校验产出的 key 集合与它一致, `from_state` 会拒绝它之外的
#: key (唯一的例外是下面那个历史别名)。
_STATE_KEYS: frozenset[str] = frozenset({
    "in_cycle", "entries", "last_prices", "cum_realized", "ops", "merged_shares",
})

#: 历史别名: 极老的 state 用单个 `last_120d_min` 记窗口最低价, 没有
#: `last_prices` 队列。读的时候折进 `last_prices`, 写的时候不再输出 ——
#: 与 Phase 0 之前的行为一致 (以前也是被 6 key 字面量丢掉的)。
_LEGACY_KEYS: frozenset[str] = frozenset({"last_120d_min"})


@dataclass
class GridPosition:
    """网格聚合根 —— 一个资产的状态。

    字段就是 `data/grid_state.json` 里那 6 个 key, 一一对应:

      in_cycle      是否处于一个进行中的周期
      entries       周期内的买入层 [{date, price, shares}]
      last_prices   滚动窗口 (最多 GRID_LOOKBACK 根), 「建议开仓」的唯一依据
      cum_realized  累计已实现盈亏 (正常回本出场恒为 0)
      ops           只增不改的操作日志 (manual = 实盘, backtest = 模拟)
      merged_shares 已归并到再平衡组合的份额 (模拟口径)

    **JSON 契约** (改格式 = 破坏线上数据, 改之前先想清楚):

      - `entries` 每行: {date, price, shares}
      - `ops` 每行: {date, op, source, price, shares, note}, tp 额外带
        {pnl, num_buys_exited, total_shares_exited, shares_to_merge}
      - `ops[].source` 只有两个值:
          "manual"   —— 页面编辑器通过 apply_grid.py 提交的**实盘**操作
                       (**唯一的实盘来源**)
          "backtest" —— ops_from_cycles() 对历史价格的**重放**,
                       即**模拟** (见 scripts/backfill_grid_ops.py)
        ("auto" 是自动交易时代的遗留值, 迁移脚本
         scripts/reset_grid_positions.py 已把那些行删掉)

    ⚠️ `last_prices` **不能被迁移清空** —— 它是「建议开仓」的**唯一**依据
    (条件 = `price < min(window)`, 即 120 日新低)。删了它, 窗口要重新灌满
    120 天才会恢复。

    状态机 (不变量 I5 / I6) 由 `manual_op` 保证:
      - 一个资产同时最多一个进行中的周期 (I5);
      - 一个周期 = N 买 + 1 卖 (I6)。

    ⚠️ `entries` 里存的仍是 **dict** —— 每一层的形状会变 (将来还要带
    「这一层是第几层」), 而 `data/grid_state.json` 的格式不能变。它的
    显式化留给后续阶段。`ops` 已经是**带类型**的 (`GridOp`)。
    """

    in_cycle: bool = False
    entries: list[dict] = field(default_factory=list)
    last_prices: list[float] = field(default_factory=list)
    cum_realized: float = 0.0
    ops: list[GridOp] = field(default_factory=list)
    merged_shares: float = 0.0

    # -- mapper: 领域对象 ↔ JSON -------------------------------------------
    def to_state(self) -> dict:
        """Serialize to the **exact** JSON shape `data/grid_state.json` uses.

        ⚠️ 这是**唯一**写状态形状的地方。守卫: 产出的 key 集合必须与
        dataclass 的字段集合**完全一致** —— 给 dataclass 加了字段却忘了
        在这里写, 会在**第一次写状态时立刻抛错**, 而不是像以前那样
        在下一次每日运行时被静默丢弃 (不变量 I11)。
        """
        state = {
            "in_cycle": self.in_cycle,
            "entries": self.entries,
            "last_prices": self.last_prices,
            "cum_realized": self.cum_realized,
            "ops": [op.to_dict() for op in self.ops],
            "merged_shares": self.merged_shares,
        }
        produced = frozenset(state)
        declared = frozenset(f.name for f in fields(self))
        if produced != declared:
            raise ValueError(
                f"grid state mapper is out of sync: produced {sorted(produced)}, "
                f"dataclass declares {sorted(declared)} — a field was added to "
                f"GridPosition without updating to_state()"
            )
        return state

    @classmethod
    def from_state(cls, state: dict) -> "GridPosition":
        """Parse a persisted state dict into the aggregate.

        缺 key 用默认值 (向后兼容老文件); **多** key 直接报错 —— 以前
        多出来的 key 会被 6 key 字面量静默丢弃, 丢掉之后没人知道 (I11)。
        """
        unknown = frozenset(state) - _STATE_KEYS - _LEGACY_KEYS
        if unknown:
            raise ValueError(
                f"grid state has unknown keys {sorted(unknown)}; "
                f"known keys are {sorted(_STATE_KEYS)}"
            )
        last_prices = list(state.get("last_prices") or [])
        if not last_prices and state.get("last_120d_min") is not None:
            # 历史别名: 单值窗口 → 折成一条队列
            last_prices = [float(state["last_120d_min"])]
        return cls(
            in_cycle=bool(state.get("in_cycle", False)),
            entries=list(state.get("entries") or []),
            last_prices=last_prices,
            cum_realized=float(state.get("cum_realized", 0.0) or 0.0),
            ops=[GridOp.from_dict(o) for o in (state.get("ops") or [])],
            merged_shares=float(state.get("merged_shares", 0.0) or 0.0),
        )


def fresh_asset_state() -> dict:
    """Empty state for one asset."""
    return GridPosition().to_state()



# -----------------------------------------------------------------------------
# 建议 (advisory) —— 引擎**只回答"今天该做什么", 绝不自己下单**
# -----------------------------------------------------------------------------
# 三个触发位 (都用 record.py 传进来的当日 K 线最低价比较):
#   open — 今日价 < 过去 120 日窗口最低             → 建议开仓
#   add  — 今日价 ≤ 最近一笔买入价 × (1 - 3%)       → 建议加仓
#   tp   — 今日价 ≥ 第一笔买入价 × (1 + 3%)          → 建议回本出场
#
# 距离口径 (正数 = 还没触发, ≤0 = 已越过触发线):
#   open:     (今日价 - 窗口最低) / 窗口最低 × 100
#   add / tp: (触发位 - 今日价) / 今日价 × 100
#
# ⚠️ 窗口是**追加今天之前**的 —— 今天的低价不能自己当自己的门槛, 否则
#    "今天是否创新低"永远等于"没创新低"。

def _triggers(state: dict, price: float, step_pct: float, tp_pct: float,
              tp_basis: str) -> dict:
    """算出三个触发位与今日价的关系。**纯函数, 不改 state。**

    ⚠️ 规则本体在 `trigger_policy.TriggerPolicy` —— 这里只是**适配器**
    (从 state 取出字段 + 转回老的 dict 形状)。

    Returns:
      signal  "open"|"add"|"tp"|None  —— 已触发的建议 (至多一个)
      kind    "open"|"add"|"tp"        —— **最近**的那个触发位
      pct     float                    —— 距 kind 的百分比 (≤0 = 已触发)
      price   float                    —— kind 的绝对价格
      dist_open_pct / dist_add_pct / dist_tp_pct: float|None
      open_price / add_price / tp_price: float|None
    """
    position = GridPosition.from_state(state)
    policy = TriggerPolicy(step_pct=step_pct, tp_pct=tp_pct, tp_basis=tp_basis)
    return policy.levels(in_cycle=position.in_cycle, entries=position.entries,
                          window=position.last_prices,
                          price=float(price)).to_dict()


def advisory(state: dict, price: float, layer_yuan_value: float,
             step_pct: float = GRID_STEP_PCT,
             tp_pct: float = GRID_TP_PCT,
             tp_basis: str = GRID_TP_BASIS) -> GridSuggestion:
    """给页面/邮件用的完整建议。纯函数 (不改 state)。

    返回一个 `GridSuggestion` —— **建议**, 不是成交 (成交是 `GridOp`)。
    老代码按 dict 读它 (`adv["suggest_shares"]`) 仍然可用, 那是
    `GridSuggestion.__getitem__` 提供的过渡期入口。

    ⚠️ `signal` 是**建议提交哪种 op**, 不是"已成交" —— 引擎不再下单。
    """
    position = GridPosition.from_state(state)
    advisor = GridAdvisor(step_pct=step_pct, tp_pct=tp_pct, tp_basis=tp_basis)
    return advisor.suggest(in_cycle=position.in_cycle, entries=position.entries,
                            window=position.last_prices, price=price,
                            layer_yuan_value=layer_yuan_value)


# -----------------------------------------------------------------------------
# Daily advancement (called once per asset per Action run)
# -----------------------------------------------------------------------------

def advance_one_day(
    state: dict,
    date_str: str,
    price: float,
    layer_yuan_value: float,
    lookback: int = GRID_LOOKBACK,
    step_pct: float = GRID_STEP_PCT,
    tp_pct: float = GRID_TP_PCT,
    tp_basis: str = GRID_TP_BASIS,
) -> tuple[dict, Optional[str]]:
    """Advance one asset's grid state by one trading day — **只给建议, 不下单**。

    ⚠️ 引擎**不再自动交易**。它只做两件事:
      1) 滚动 120 日窗口 (`last_prices`) —— 距开仓建议要靠它;
      2) 算出今天"该做什么" (open / add / tp / None)。

    它**绝不**修改 `entries` / `in_cycle` / `cum_realized` / `merged_shares`,
    也**绝不**追加 `ops`。真正的开仓/加仓/回本只能来自用户在
    「交易记录 → 复合网格」提交的 ops (scripts/apply_grid.py → manual_op)。

    Returns:
        (new_state, suggestion), suggestion ∈ {"open", "add", "tp", None}。
        `suggestion` 是**建议提交哪种 op**, 不是"已成交"。
    """
    position = GridPosition.from_state(state)

    # 建议基于**追加今天之前**的窗口算 (规则本体在 trigger_policy.TriggerPolicy)
    policy = TriggerPolicy(step_pct=step_pct, tp_pct=tp_pct, tp_basis=tp_basis)
    levels = policy.levels(in_cycle=position.in_cycle, entries=position.entries,
                            window=position.last_prices, price=float(price))
    suggestion = levels.signal.value if levels.signal is not None else None

    # 唯一的副作用: 滚动窗口
    position.last_prices.append(float(price))
    if len(position.last_prices) > lookback:
        position.last_prices = position.last_prices[-lookback:]

    return position.to_state(), suggestion


# -----------------------------------------------------------------------------
# Snapshot helpers (for chart + notify)
# -----------------------------------------------------------------------------

def asset_snapshot(state: dict, current_price: float) -> dict:
    """Return a read-only summary for a single asset's state.

    `current_value` is the **grid's own capital still tied up** — the layers
    of the currently-open cycle, and nothing else. It drops back to 0 after
    a take-profit, because the principal came back.

    `merged_shares` / `merged_value` are the shares that were handed over to
    the **rebalance portfolio** at earlier take-profits. They are NOT the
    grid's holdings: the grid no longer has a say in them, and the
    rebalance side owns them (they must be counted in the portfolio's
    weights). They are reported here only so the caller can fold them into
    the portfolio — `src/domain/portfolio/rebalance_policy.py:evaluate` does exactly that.
    """
    position = GridPosition.from_state(state)
    entries = position.entries
    in_cycle = position.in_cycle
    # 份额先定到 2 位小数, 再由它算市值 —— 这样页面/邮件上"多少份"和
    # "多少钱"是自洽的 (用户拿 3.89 份 × 103 验算能对上 400.67)。
    merged_shares = ShareCount(position.merged_shares)
    merged_value = Money(merged_shares * float(current_price))
    if in_cycle and entries:
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        avg_cost = total_invested / total_shares if total_shares else 0.0
        current_value = total_shares * current_price
        unrealized = current_value - total_invested
        return {
            "in_cycle": True,
            "num_buys": len(entries),
            "avg_cost": Price(avg_cost),
            "total_invested": Money(total_invested),
            "current_value": Money(current_value),
            "unrealized_pnl": Money(unrealized),
            "return_pct": round(unrealized / total_invested * 100, 2) if total_invested else 0.0,
            "merged_shares": ShareCount(merged_shares),
            "merged_value": Money(merged_value),
        }
    return {
        "in_cycle": False,
        "num_buys": 0,
        "avg_cost": 0.0,
        "total_invested": 0.0,
        "current_value": 0.0,
        "unrealized_pnl": 0.0,
        "return_pct": 0.0,
        "merged_shares": ShareCount(merged_shares),
        "merged_value": Money(merged_value),
    }


# -----------------------------------------------------------------------------
# Manual operations (driven by the Pages editor, not by daily advancement)
# -----------------------------------------------------------------------------

def manual_op(state: dict, op: str, today: str,
              price: float, shares: float, note: str = "") -> dict:
    """Apply a manual grid operation. Returns a NEW state dict (does not mutate).

    ⚠️ 这是**唯一的实盘网格入口** —— 引擎不再自动交易, 所有 open/add/tp
    都由用户在「交易记录 → 复合网格」提交后走这里。

    op ∈ {"open", "add", "tp"}:
      - open: in_cycle must be False. Append an entry, set in_cycle=True,
        and push `price` to the rolling `last_prices` window (so the manual
        buy is also seen as a known-low for the next daily run).
      - add:  in_cycle must be True. Append an entry. `last_prices` is NOT
        touched. `entries[-1].price` becomes the new "加仓参考价" for the
        next 建议加仓.
      - tp:   in_cycle must be True. Applies the "sell cost only" rule
        (cost_only_exit): sell back exactly the principal, merge the
        remainder into the rebalance portfolio, set in_cycle=False,
        clear entries. `last_prices` is NOT touched.
    """
    if op not in ("open", "add", "tp"):
        raise ValueError(f"unknown op: {op!r}")
    if not (price > 0):
        raise ValueError(f"price must be > 0, got {price}")
    # For "open" and "add", `shares` is the layer share count → must be > 0.
    # For "tp", `shares` is ignored (the sell quantity is derived from the
    # cycle's own invested amount), so any value (including 0) is accepted.
    if op in ("open", "add") and not (shares > 0):
        raise ValueError(f"{op} requires shares > 0, got {shares}")
    position = GridPosition.from_state(state)
    if op == "open" and position.in_cycle:
        raise ValueError("open requires no open cycle (use tp to close first)")
    if op in ("add", "tp") and not position.in_cycle:
        raise ValueError(f"{op} requires an open cycle")

    if op in ("open", "add"):
        position.entries.append({
            "date": today, "price": float(price), "shares": float(shares),
        })
        position.in_cycle = True
        if op == "open":
            position.last_prices.append(float(price))
            if len(position.last_prices) > GRID_LOOKBACK:
                position.last_prices = position.last_prices[-GRID_LOOKBACK:]
        position.ops.append(ExecutedOp(
            date=today, op=OpKind(op), source=OpSource.MANUAL,
            price=float(price), shares=float(shares), note=note,
        ))
    else:  # tp
        entries = position.entries
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        split = cost_only_exit(total_shares, total_invested, float(price))
        position.merged_shares += split.merged_shares
        # Capture per-event exit stats BEFORE clearing `entries`.
        position.ops.append(ExecutedOp(
            date=today, op=OpKind.TP, source=OpSource.MANUAL,
            price=float(price), shares=float(split.shares_sold),
            note=note,
            pnl=float(split.pnl),
            num_buys_exited=len(entries),
            # "出场"的全部份额 = 卖出的 + 归并出去的 (== 原持仓)
            total_shares_exited=float(split.shares_sold
                                      + split.merged_shares),
            shares_to_merge=float(split.merged_shares),
        ))
        position.cum_realized += split.pnl
        position.entries = []
        position.in_cycle = False

    return position.to_state()
