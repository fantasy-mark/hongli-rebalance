"""Compound (pyramid) grid engine + daily-state advancement.

Rules (per asset, independent):
  - Trigger: today's price < min(price of past `lookback` days) → start a
    new cycle, buying `layer_yuan` worth. The "price" passed in by
    record.py is the day's K-line LOW (intraday) so the trigger can fire
    on an intraday dip even if the close is above yesterday's close;
    `advance_one_day` itself is price-agnostic — any monotonic series
    works.
  - Pyramid: while in a cycle, every additional `step_pct` % drop from
    the LAST buy price adds another `layer_yuan` layer.
  - Take-profit: when price >= first_buy_price * (1 + tp_pct/100), sell
    **only the cost amount** (`total_invested / price` shares, recovering
    exactly the principal), then wait for the next trigger. The leftover
    `total_shares - total_invested/price` shares are **merged into the
    rebalance portfolio** (`merged_shares`) — they leave the grid's book
    and become ordinary portfolio holdings — see `split_take_profit`.
    So realized P&L per cycle is 0 by construction, and all return comes
    from the merged shares' appreciation.

State persistence: `advance_one_day()` mutates a JSON-serializable dict so
the Action can commit `data/grid_state.json` after each run.

Layer sizing:
  layer_yuan(d) = GRID_BASE_YUAN + max(pool_value - recent_min, 0) * GRID_FLOATING_FRACTION
  where recent_min = min(pool_value over the past GRID_FLOATING_WINDOW trading days).
"""
from __future__ import annotations

import os
from typing import Callable, Optional

# 网格参数 (env-overridable for parameter sweeps)
GRID_BASE_YUAN: float = float(os.environ.get("GRID_BASE_YUAN", "10000"))
GRID_FLOATING_FRACTION: float = float(os.environ.get("GRID_FLOATING_FRACTION", "0.05"))
GRID_FLOATING_WINDOW: int = int(os.environ.get("GRID_FLOATING_WINDOW", "756"))
GRID_LOOKBACK: int = 120
GRID_STEP_PCT: float = 3.0
GRID_TP_PCT: float = 3.0
# "avg_cost"   — exit when price >= avg_cost * (1 + tp_pct/100)             [v2]
# "first_buy"  — exit when price >= first_buy_price * (1 + tp_pct/100)       [v3]
GRID_TP_BASIS: str = os.environ.get("GRID_TP_BASIS", "first_buy")


def layer_yuan(pool_value: float, recent_min: float) -> float:
    """Compute today's per-layer buy amount in ¥.

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

def split_take_profit(total_shares: float, total_invested: float,
                      price: float) -> dict:
    """按"只卖成本金额"规则拆分一次止盈。

    Returns {shares_sold, proceeds, merged_shares, pnl}:
      shares_sold  实际卖出的份额 (= 本金 / 价格, 取回全部本金)
      merged_shares 归并到再平衡持仓的份额 (网格不再持有)
      pnl          已实现盈亏; 正常路径恒为 0, 亏本出场时为负

    当 `price` 低于持仓均价时要卖的份额会超过持仓, 归并出"负的份额"没有
    意义, 此时退化为全部卖出并认亏。这只可能由**手动**止盈触发 —— 自动止盈的
    触发价 (`first_buy * 1.03` 或 `avg_cost * 1.03`) 必然高于均价, 因为金字塔
    只在更低处加仓, 所以 `first_buy >= avg_cost`。
    """
    if not price > 0:
        raise ValueError(f"price must be > 0, got {price}")
    if total_shares <= 0:
        return {"shares_sold": 0.0, "proceeds": 0.0, "merged_shares": 0.0, "pnl": 0.0}
    want = total_invested / price
    if want >= total_shares:
        # 亏本出场 (只有手动路径可能): 卖光并认亏
        proceeds = total_shares * price
        return {"shares_sold": total_shares, "proceeds": proceeds,
                "merged_shares": 0.0, "pnl": proceeds - total_invested}
    return {"shares_sold": want, "proceeds": total_invested,
            "merged_shares": total_shares - want, "pnl": 0.0}


# -----------------------------------------------------------------------------
# Full backtest (used by chart rendering and tests)
# -----------------------------------------------------------------------------

def run_compound_grid(
    prices,
    lookback: int = GRID_LOOKBACK,
    step_pct: float = GRID_STEP_PCT,
    tp_pct: float = GRID_TP_PCT,
    tp_basis: str = GRID_TP_BASIS,
    pos_yuan: float = GRID_BASE_YUAN,
    pos_yuan_fn: Optional[Callable[[object], float]] = None,
    trigger_mode: str = "window",
) -> dict:
    """Run the compound grid on a full price series (used for chart + tests).

    trigger_mode:
        "window" — open cycle when price < min of past `lookback` days  (default)
        "global" — open cycle when price < min of all prior history (rolling global low)

    Returns dict with: cycles (closed), history (daily DataFrame), cum_realized,
    open_cycle, open_avg_cost, open_invested, open_shares, merged_shares.
    """
    import pandas as pd  # local import keeps the module lightweight

    in_cycle = False
    entries: list[dict] = []
    cycles: list[dict] = []
    cum_realized = 0.0
    # 已归并到再平衡持仓的份额 (止盈时从网格账上转出, 此后归组合所有)
    merged_shares = 0.0
    history: list[dict] = []
    prices_sorted = prices.sort_index()

    def _layer(d, p) -> float:
        if pos_yuan_fn is not None:
            return float(pos_yuan_fn(d))
        return float(pos_yuan)

    for i, (date, price) in enumerate(prices_sorted.items()):
        if not in_cycle:
            if i > 0:
                if trigger_mode == "global":
                    past = prices_sorted.iloc[:i]
                else:
                    past = prices_sorted.iloc[max(0, i - lookback):i]
                if len(past) >= 1 and price < past.min():
                    sh = _layer(date, price) / price
                    entries.append({"date": date, "price": float(price), "shares": sh})
                    in_cycle = True
        else:
            last_price = entries[-1]["price"]
            if price <= last_price * (1 - step_pct / 100):
                sh = _layer(date, price) / price
                entries.append({"date": date, "price": float(price), "shares": sh})
            total_shares = sum(e["shares"] for e in entries)
            total_invested = sum(e["shares"] * e["price"] for e in entries)
            avg_cost = total_invested / total_shares if total_shares else 0.0
            if tp_basis == "first_buy" and entries:
                tp_target = entries[0]["price"] * (1 + tp_pct / 100)
            else:
                tp_target = avg_cost * (1 + tp_pct / 100)
            if price >= tp_target:
                split = split_take_profit(total_shares, total_invested, float(price))
                merged_shares += split["merged_shares"]
                cum_realized += split["pnl"]
                cycles.append({
                    "entries": entries,
                    "exit_date": date,
                    "exit_price": float(price),
                    "num_buys": len(entries),
                    "avg_cost": avg_cost,
                    # 卖出的是"回本所需"那部分, 不是全部持仓 —— 见 split_take_profit
                    "total_shares": split["shares_sold"],
                    "shares_transferred": split["merged_shares"],
                    "total_invested": total_invested,
                    "exit_value": split["proceeds"],
                    "pnl": split["pnl"],
                    "return_pct": 0.0,
                })
                in_cycle = False
                entries = []

        if in_cycle:
            total_shares = sum(e["shares"] for e in entries)
            total_invested = sum(e["shares"] * e["price"] for e in entries)
            avg_cost = total_invested / total_shares if total_shares else 0.0
            current_value = total_shares * price
            unrealized = current_value - total_invested
        else:
            total_shares = 0.0
            total_invested = 0.0
            avg_cost = 0.0
            current_value = 0.0
            unrealized = 0.0

        # 归并份额的市值 = 网格累计赚到的钱 (本金早已收回后的净多头)。
        # 它归**再平衡**所有, 不算网格的资金占用。
        merged_value = merged_shares * price

        history.append({
            "date": date,
            "in_cycle": in_cycle,
            "num_buys": len(entries),
            "avg_cost": avg_cost,
            "total_invested": total_invested,
            # 网格自己的资金占用 = 只算在周期内的层 (出场后本金回来, 占用回落)
            "current_value": current_value,
            "unrealized_pnl": unrealized,
            "cum_realized": cum_realized,
            "merged_shares": merged_shares,
            "merged_value": merged_value,
        })

    df = pd.DataFrame(history).set_index("date")
    return {
        "cycles": cycles,
        "history": df,
        "cum_realized": cum_realized,
        "open_cycle": entries if in_cycle else None,
        "open_avg_cost": avg_cost if in_cycle else None,
        "open_invested": total_invested if in_cycle else None,
        "open_shares": total_shares if in_cycle else None,
        "merged_shares": merged_shares,
    }


def _iso_date(d) -> str:
    """Normalize a date-ish to 'YYYY-MM-DD'.

    `run_compound_grid` yields pandas Timestamps; live ops already carry
    ISO strings. The page sorts ops with `localeCompare`, so both paths
    must produce the same shape (a Timestamp would sort wrong).
    """
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def ops_from_cycles(cycles: list[dict], open_cycle=None) -> list[dict]:
    """Convert a `run_compound_grid` result into per-event ops rows.

    Pure function: no I/O, no pandas import (so tests can call it without
    akshare/matplotlib). Mirrors the schema `advance_one_day` /
    `manual_op` append, but tagged source="backtest" because the inputs
    come from a historical replay rather than the live engine — the page
    renders that as a separate 回测 chip.

    Mapping:
      cycle.entries[0]     -> op="open", note "回测·窗口突破开仓"
      cycle.entries[i > 0] -> op="add",  note "回测·金字塔第i+1层"
      cycle exit           -> op="tp",   note "回测·回本出场", plus pnl /
                              num_buys_exited / total_shares_exited /
                              shares_to_merge (归并到再平衡持仓的部分)
      open_cycle entries   -> open/add only (no tp — that cycle is open)

    Sorted date ASC, with open/add before tp on a tie: in
    `run_compound_grid` a pyramid add and the take-profit can land on the
    same bar, so the order has to be explicit rather than incidental.
    """
    ops: list[dict] = []

    def _entries(entries) -> None:
        for i, e in enumerate(entries):
            ops.append({
                "date": _iso_date(e["date"]),
                "op": "open" if i == 0 else "add",
                "source": "backtest",
                # Rounded so grid_state.json stays auditable and its git diffs
                # readable — raw floats carry 16 significant digits
                # (969.4619486185168 shares). Price matches the `last_prices`
                # precision convention in grid_state.json.
                "price": round(float(e["price"]), 4),
                "shares": round(float(e["shares"]), 2),
                "note": "回测·窗口突破开仓" if i == 0 else f"回测·金字塔第{i + 1}层",
            })

    for c in cycles or []:
        _entries(c["entries"])
        # 出场现在是两条腿: 卖"回本所需"那部分, 余量归并到再平衡持仓。
        # `total_shares` 已经是"卖出的份额", 归并出去的那部分另计。
        sold = float(c.get("total_shares", 0.0))
        to_merge = float(c.get("shares_transferred", 0.0) or 0.0)
        ops.append({
            "date": _iso_date(c["exit_date"]),
            "op": "tp",
            "source": "backtest",
            "price": round(float(c["exit_price"]), 4),
            "shares": round(sold, 2),
            "note": "回测·回本出场",
            "pnl": round(float(c["pnl"]), 2),
            "num_buys_exited": int(c["num_buys"]),
            "total_shares_exited": round(sold + to_merge, 2),
            "shares_to_merge": round(to_merge, 2),
        })

    # The still-open cycle has no exit yet — entries only, no tp.
    if open_cycle:
        _entries(open_cycle)

    _order = {"open": 0, "add": 1, "tp": 2}
    ops.sort(key=lambda o: (o["date"], _order[o["op"]]))
    return ops


def merge_backtest_ops(state: dict, name: str, ops: list[dict]) -> int:
    """Replace `name`'s backtest ops in `state`, keeping any live ops.

    The idempotency rule for scripts/backfill_grid_ops.py: re-running drops
    every source=="backtest" op and re-appends the freshly generated ones,
    so repeated runs never accumulate duplicates, while live ops
    (source "auto" from record.py / "manual" from the page editor) survive
    untouched. No-op with a warning if the asset isn't in the state.

    Returns the number of live ops preserved.
    """
    asset = state.get(name)
    if not isinstance(asset, dict):
        print(f"[{name}] 不在 grid_state 里, 跳过")
        return 0
    live = [o for o in asset.get("ops", []) if o.get("source") != "backtest"]
    asset["ops"] = live + list(ops)
    return len(live)


# -----------------------------------------------------------------------------
# Per-asset state container (JSON-serializable, used by record.py)
# -----------------------------------------------------------------------------

def fresh_asset_state() -> dict:
    """Empty state for one asset."""
    return {
        "in_cycle": False,
        "entries": [],                # [{date: ISO, price: float, shares: float}]
        # Rolling window of past closes (length up to GRID_LOOKBACK). The
        # trigger condition is `price < min(window)` — i.e. a new 120-day low.
        # We persist a queue so the rule survives Action restarts and JSON
        # round-trips.
        "last_prices": [],
        "cum_realized": 0.0,
        # Append-only log of every executed open/add/tp event (auto or manual).
        # Schema per op: {date, op, source, price, shares, note, (tp-only:
        # pnl, num_buys_exited, total_shares_exited)}. The page renders this
        # as the "复合网格 操作记录" table under the strategy chart. Cleared on
        # tp is `entries`, NOT `ops` — the op log is the permanent record.
        #
        # `source` has three values:
        #   "auto"     — record.py daily trigger (live)
        #   "manual"   — page editor via apply_grid.py (live)
        #   "backtest" — ops_from_cycles() replay of historical prices, i.e.
        #                SIMULATED (see scripts/backfill_grid_ops.py)
        "ops": [],
        # 归并份额 (float, shares). 每次止盈时 `total_shares -
        # total_invested/price` 归并到这里 —— 这批份额**离开网格的账, 变成
        # 再平衡持仓的一部分** (模拟口径, 见 split_take_profit 的说明)。
        # 旧版 state 没有这个 key, 读取一律走 .get("merged_shares", 0.0)
        # 以保持向后兼容。
        #
        # ⚠️ state 是用**字面量 dict** 重建的 (见 advance_one_day /
        # inline_manual_op), 少写一个 key 就会被静默丢弃而不报错。
        "merged_shares": 0.0,
    }


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
    """Advance one asset's grid state by one trading day.

    Args:
        state:        asset state dict (mutated and returned).
        date_str:     ISO date string for today.
        price:        today's price. The daily caller (record.py) passes
                      the K-line LOW so an intraday dip can fire the
                      trigger even if the close is above yesterday's
                      close; this function is price-agnostic.
        layer_yuan_value: pre-computed buy amount for today (¥).
        lookback/step_pct/tp_pct: strategy parameters.

    Returns:
        (new_state, signal) where signal ∈ {"buy", "sell", None}.
    """
    in_cycle = bool(state.get("in_cycle", False))
    entries: list[dict] = list(state.get("entries", []))
    # Compat: if state was saved under the old "last_120d_min" key, start
    # the window with that single value (treat it as a known low).
    legacy_min = state.get("last_120d_min")
    last_prices: list[float] = list(state.get("last_prices", []))
    if not last_prices and legacy_min is not None:
        last_prices = [float(legacy_min)]
    cum_realized = float(state.get("cum_realized", 0.0))
    # 归并份额: 已交给再平衡持仓的份额 (不再是网格的资产)
    merged_shares = float(state.get("merged_shares", 0.0))
    ops: list[dict] = list(state.get("ops", []))   # append-only op log

    signal: Optional[str] = None

    if not in_cycle:
        # Trigger: today < min of past `lookback` days (rolling window).
        if last_prices and price < min(last_prices):
            sh = layer_yuan_value / price if price > 0 else 0.0
            entries.append({"date": date_str, "price": float(price), "shares": sh})
            ops.append({
                "date": date_str, "op": "open", "source": "auto",
                "price": float(price), "shares": sh,
                "note": "窗口突破开仓",
            })
            in_cycle = True
            signal = "buy"
    else:
        last_price = entries[-1]["price"]
        # Pyramid: drop >= step_pct from last buy
        if price <= last_price * (1 - step_pct / 100):
            sh = layer_yuan_value / price if price > 0 else 0.0
            entries.append({"date": date_str, "price": float(price), "shares": sh})
            ops.append({
                "date": date_str, "op": "add", "source": "auto",
                "price": float(price), "shares": sh,
                # len(entries) post-append = the new layer count (1=open,
                # 2=first pyramid, ...).  Matches manual_op convention.
                "note": f"金字塔第{len(entries)}层",
            })
            signal = "buy"
        # Take-profit: close >= (avg_cost or first_buy_price) * (1 + tp_pct/100)
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        if total_shares > 0:
            avg_cost = total_invested / total_shares
            if tp_basis == "first_buy":
                tp_target = entries[0]["price"] * (1 + tp_pct / 100)
            else:
                tp_target = avg_cost * (1 + tp_pct / 100)
            if price >= tp_target:
                split = split_take_profit(total_shares, total_invested,
                                          float(price))
                merged_shares += split["merged_shares"]
                # Capture per-event exit stats BEFORE clearing `entries`.
                # `shares` is what was actually SOLD (回本所需), not the whole
                # position — the rest was merged into the rebalance portfolio.
                # Both are logged so the ops table reconciles against the
                # trade ledger.
                ops.append({
                    "date": date_str, "op": "tp", "source": "auto",
                    "price": float(price), "shares": split["shares_sold"],
                    "note": "自动止盈·归并再平衡",
                    "pnl": float(split["pnl"]),
                    "num_buys_exited": len(entries),
                    # "出场"的全部份额 = 卖出的 + 归并出去的 (== 原持仓)
                    "total_shares_exited": float(split["shares_sold"]
                                                 + split["merged_shares"]),
                    "shares_to_merge": float(split["merged_shares"]),
                })
                cum_realized += split["pnl"]
                in_cycle = False
                entries = []
                signal = "sell"

    # Update rolling window: append today, then trim to `lookback` entries.
    last_prices.append(float(price))
    if len(last_prices) > lookback:
        last_prices = last_prices[-lookback:]

    new_state = {
        "in_cycle": in_cycle,
        "entries": entries,
        "last_prices": last_prices,
        "cum_realized": cum_realized,
        "ops": ops,
        "merged_shares": merged_shares,
    }
    return new_state, signal


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
    the portfolio — `src/record.py:evaluate` does exactly that.
    """
    entries = state.get("entries", [])
    in_cycle = bool(state.get("in_cycle", False))
    # 份额先定到 2 位小数, 再由它算市值 —— 这样页面/邮件上"多少份"和
    # "多少钱"是自洽的 (用户拿 3.89 份 × 103 验算能对上 400.67)。
    merged_shares = round(float(state.get("merged_shares", 0.0) or 0.0), 2)
    merged_value = round(merged_shares * float(current_price), 2)
    if in_cycle and entries:
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        avg_cost = total_invested / total_shares if total_shares else 0.0
        current_value = total_shares * current_price
        unrealized = current_value - total_invested
        return {
            "in_cycle": True,
            "num_buys": len(entries),
            "avg_cost": round(avg_cost, 4),
            "total_invested": round(total_invested, 2),
            "current_value": round(current_value, 2),
            "unrealized_pnl": round(unrealized, 2),
            "return_pct": round(unrealized / total_invested * 100, 2) if total_invested else 0.0,
            "merged_shares": round(merged_shares, 2),
            "merged_value": round(merged_value, 2),
        }
    return {
        "in_cycle": False,
        "num_buys": 0,
        "avg_cost": 0.0,
        "total_invested": 0.0,
        "current_value": 0.0,
        "unrealized_pnl": 0.0,
        "return_pct": 0.0,
        "merged_shares": round(merged_shares, 2),
        "merged_value": round(merged_value, 2),
    }


# -----------------------------------------------------------------------------
# Manual operations (driven by the Pages editor, not by daily advancement)
# -----------------------------------------------------------------------------

def manual_op(state: dict, op: str, today: str,
              price: float, shares: float) -> dict:
    """Apply a manual grid operation. Returns a NEW state dict (does not mutate).

    op ∈ {"open", "add", "tp"}:
      - open: in_cycle must be False. Append an entry, set in_cycle=True,
        and push `price` to the rolling `last_prices` window (so the manual
        buy is also seen as a known-low for the next daily run).
      - add:  in_cycle must be True. Append an entry. `last_prices` is NOT
        touched. `entries[-1].price` becomes the new "加仓参考价" for the
        next auto pyramid, matching advance_one_day's behavior.
      - tp:   in_cycle must be True. Applies the "sell cost only" rule
        (split_take_profit): sell back exactly the principal, merge the
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
    in_cycle = bool(state.get("in_cycle", False))
    if op == "open" and in_cycle:
        raise ValueError("open requires no open cycle (use tp to close first)")
    if op in ("add", "tp") and not in_cycle:
        raise ValueError(f"{op} requires an open cycle")

    new_state = {
        "in_cycle": in_cycle,
        "entries": list(state.get("entries", [])),
        "last_prices": list(state.get("last_prices", [])),
        "cum_realized": float(state.get("cum_realized", 0.0)),
        "ops": list(state.get("ops", [])),
        "merged_shares": float(state.get("merged_shares", 0.0)),
    }

    if op in ("open", "add"):
        new_state["entries"].append({
            "date": today, "price": float(price), "shares": float(shares),
        })
        new_state["in_cycle"] = True
        if op == "open":
            new_state["last_prices"].append(float(price))
            if len(new_state["last_prices"]) > GRID_LOOKBACK:
                new_state["last_prices"] = new_state["last_prices"][-GRID_LOOKBACK:]
        # Note for manual ops: empty string. The user-supplied `note` lives
        # in the caller (apply_grid.inline_manual_op), which appends it to
        # the op dict via the `note` kwarg.  manual_op's signature is
        # unchanged so existing callers don't break.
        new_state["ops"].append({
            "date": today, "op": op, "source": "manual",
            "price": float(price), "shares": float(shares),
            "note": "",
        })
    else:  # tp
        entries = new_state["entries"]
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        split = split_take_profit(total_shares, total_invested, float(price))
        new_state["merged_shares"] += split["merged_shares"]
        # Capture per-event exit stats BEFORE clearing `entries`.
        new_state["ops"].append({
            "date": today, "op": "tp", "source": "manual",
            "price": float(price), "shares": float(split["shares_sold"]),
            "note": "",
            "pnl": float(split["pnl"]),
            "num_buys_exited": len(entries),
            # "出场"的全部份额 = 卖出的 + 归并出去的 (== 原持仓)
            "total_shares_exited": float(split["shares_sold"]
                                         + split["merged_shares"]),
            "shares_to_merge": float(split["merged_shares"]),
        })
        new_state["cum_realized"] += split["pnl"]
        new_state["entries"] = []
        new_state["in_cycle"] = False

    return new_state
