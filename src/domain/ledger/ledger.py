"""The trade ledger — the **single source of truth** (BC-A).

`data/trades.json` records **real fills only**. Everything else in the
project (positions, cash balances) is a *projection* of this ledger —
`data/holdings.json` is just a cached projection of it, never a fact.

Invariants enforced here (see docs/domain_model.md §10):
  - I1  neither cash pool may go negative
  - I2  a sell may never exceed the shares held
  - I3  the ledger is append-only — entries are never rewritten

`apply_trade` is *transactional*: it computes the new state in a scratch
dict, validates, and only then commits. A rejected trade leaves the
caller's state untouched.
"""
from __future__ import annotations

from typing import Optional

from ..events.domain_events import (
    POOL_EXTERNAL,
    POOL_GRID,
    POOL_REBALANCE,
    CashTransferred,
    EventStream,
    TradeRecorded,
)
from ..shared.money import Money, ShareCount
from .trade_side import TradeSide

#: 现金类 side → (from_pool, to_pool)。成交类 (buy/sell) 不在这里 ——
#: 它们发的是 `TradeRecorded`, 不是现金划转。
_CASH_ROUTING: dict[str, tuple[str, str]] = {
    "deposit_rebalance": (POOL_EXTERNAL, POOL_REBALANCE),
    "deposit_grid": (POOL_EXTERNAL, POOL_GRID),
    "withdraw_rebalance": (POOL_REBALANCE, POOL_EXTERNAL),
    "withdraw_grid": (POOL_GRID, POOL_EXTERNAL),
    "transfer_to_grid": (POOL_REBALANCE, POOL_GRID),
    "transfer_to_rebalance": (POOL_GRID, POOL_REBALANCE),
}


def apply_trade(state: dict, trade: dict,
                events: Optional[EventStream] = None) -> None:
    """Mutate `state` in place: state = {"cash_rebalance": float,
    "cash_grid": float, "positions": {code: shares}}.

    Side routing:
        deposit_rebalance  → +amount to cash_rebalance
        deposit_grid       → +amount to cash_grid
        withdraw_rebalance → -amount from cash_rebalance
        withdraw_grid      → -amount from cash_grid
        transfer_to_grid   → -amount from cash_rebalance, +amount to cash_grid
        transfer_to_rebalance → +amount to cash_rebalance, -amount from cash_grid
        buy                → -amount from cash_rebalance, +shares to positions
        sell               → +amount to cash_rebalance, -shares from positions

    Atomic: computes the new state in a scratch dict, validates (no
    negative pool, no oversell), then commits. On failure the original
    `state` is left untouched and ValueError is raised.

    Rounds cash to 2 dp after each trade (`Money` — 现金永远是 2 位小数,
    这条规则只写一遍, 不再散落成裸 `round(x, 2)`)。

    Args:
        state:  就地修改的状态
        trade:  一笔交易
        events: 可选的事件收集器 —— 成交发 `TradeRecorded`, 现金划转发
                `CashTransferred`。`None` = 不发事件 (老调用方不受影响)。
    """
    side = trade["side"]
    # Compute new state in a scratch copy; commit only if validation passes.
    new_state: dict = {
        "cash_rebalance": state.get("cash_rebalance", 0.0),
        "cash_grid":      state.get("cash_grid", 0.0),
        "positions": dict(state.get("positions", {})),
    }
    if side == "deposit_rebalance":
        new_state["cash_rebalance"] += float(trade["amount"])
    elif side == "deposit_grid":
        new_state["cash_grid"] += float(trade["amount"])
    elif side == "withdraw_rebalance":
        new_state["cash_rebalance"] -= float(trade["amount"])
    elif side == "withdraw_grid":
        new_state["cash_grid"] -= float(trade["amount"])
    elif side == "transfer_to_grid":
        amt = float(trade["amount"])
        new_state["cash_rebalance"] -= amt
        new_state["cash_grid"] += amt
    elif side == "transfer_to_rebalance":
        amt = float(trade["amount"])
        new_state["cash_grid"] -= amt
        new_state["cash_rebalance"] += amt
    elif side == "buy":
        code = str(trade["code"])
        sh = float(trade["shares"])
        px = float(trade["price"])
        fee = float(trade.get("fee", 0.0))
        new_state["positions"][code] = new_state["positions"].get(code, 0.0) + sh
        new_state["cash_rebalance"] -= sh * px + fee
    elif side == "sell":
        code = str(trade["code"])
        sh = float(trade["shares"])
        px = float(trade["price"])
        fee = float(trade.get("fee", 0.0))
        held = new_state["positions"].get(code, 0.0)
        if held < sh - 1e-9:
            raise ValueError(
                f"oversell {code}: held={held:.4f}, sell={sh:.4f}")
        new_state["positions"][code] = held - sh
        new_state["cash_rebalance"] += sh * px - fee
    else:
        raise ValueError(f"unknown side: {side!r}")
    new_state["cash_rebalance"] = Money(new_state["cash_rebalance"])
    new_state["cash_grid"] = Money(new_state["cash_grid"])
    # Defense: any pool going negative is a programming error
    if new_state["cash_rebalance"] < 0 or new_state["cash_grid"] < 0:
        raise ValueError(
            f"cash would go negative: rebalance={new_state['cash_rebalance']:.2f}, "
            f"grid={new_state['cash_grid']:.2f} (after {side})")
    # All validations passed — commit
    state["cash_rebalance"] = new_state["cash_rebalance"]
    state["cash_grid"] = new_state["cash_grid"]
    for c in list(new_state["positions"].keys()):
        v = ShareCount(new_state["positions"][c])
        if v <= 0:
            # Drop zero/negative entries; if this was the only position for
            # a code, remove the key entirely.
            if c in state["positions"]:
                del state["positions"][c]
        else:
            state["positions"][c] = v
    if events is not None:
        _emit_trade_event(events, trade)


def _emit_trade_event(events: EventStream, trade: dict) -> None:
    """把一笔交易翻译成领域事件 (Phase 5)。

    - `buy` / `sell` → `TradeRecorded` (成交)
    - 其余六种现金 side → `CashTransferred` (划转)

    ⚠️ 只有**真实成交**才有 `TradeRecorded` —— 网格归并的模拟份额**永远**
    到不了这里 (它走 `MergedSharesTransferred` 事件)。这就是不变量 I9
    在代码里的形状: 账本上下文里没有能写模拟份额的入口。
    """
    side = str(trade.get("side", ""))
    date = str(trade.get("date", "") or "")
    if side in ("buy", "sell"):
        shares = float(trade.get("shares", 0.0) or 0.0)
        price = float(trade.get("price", 0.0) or 0.0)
        fee = float(trade.get("fee", 0.0) or 0.0)
        # 买入花掉 shares×price + fee; 卖出收回 shares×price − fee
        amount = (shares * price + fee) if side == "buy" else (shares * price - fee)
        events.record(TradeRecorded(date=date, side=side,
                                    code=str(trade.get("code", "")),
                                    shares=shares, price=price, amount=amount))
        return
    routing = _CASH_ROUTING.get(side)
    if routing is not None:
        from_pool, to_pool = routing
        events.record(CashTransferred(date=date, from_pool=from_pool,
                                      to_pool=to_pool,
                                      amount=float(trade.get("amount", 0.0) or 0.0)))


def rebuild_holdings_from_trades(
    trades: list[dict],
    as_of_date: str | None = None,
    name_lookup: Optional[dict[str, str]] = None,
    events: Optional[EventStream] = None,
) -> tuple[list[dict], float, float]:
    """Replay `trades` chronologically; return (positions, cash_rebalance, cash_grid).

    Only includes codes with shares > 0 in the returned positions list.
    `as_of_date` (ISO yyyy-mm-dd) is inclusive — trades dated after it are
    skipped. `name_lookup` defaults to the asset universe's names.

    `events` (optional) collects a `TradeRecorded` / `CashTransferred`
    event per applied trade — 账本重放**顺手**把「发生过什么」讲出来。
    """
    if name_lookup is None:
        from ..portfolio.asset_universe import ETF_DEFS
        name_lookup = {code: v[2] for code, v in ETF_DEFS.items()}
    state: dict = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {}}
    seen_codes: list[str] = []

    # Sort by (date, original_index) so same-day trades play in append order.
    indexed = list(enumerate(trades))
    indexed.sort(key=lambda kv: (kv[1].get("date", ""), kv[0]))

    for _, t in indexed:
        if as_of_date and t.get("date", "") > as_of_date:
            continue
        side = t.get("side")
        code = t.get("code")
        if side in ("buy", "sell") and code not in seen_codes:
            seen_codes.append(code)
        apply_trade(state, t, events)

    positions = [
        {"name": name_lookup.get(c, c), "code": c,
         "shares": state["positions"][c]}
        for c in seen_codes
        if state["positions"].get(c, 0.0) > 0
    ]
    return positions, state["cash_rebalance"], state["cash_grid"]


__all__ = [
    "apply_trade",
    "rebuild_holdings_from_trades",
    "TradeSide",
]
