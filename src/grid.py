"""Compound (pyramid) grid engine — **advisory only, it never trades**.

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
    and become ordinary portfolio holdings — see `split_take_profit`.
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
    意义, 此时退化为全部卖出并认亏。引擎**不再自动止盈**, 所以这个分支只能
    由**手动** tp 触发 —— 用户在低于成本处出场时会真的认亏。
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
    (source "manual" from the page editor — the only live source now that
    the engine stopped trading) survive untouched. No-op with a warning if
    the asset isn't in the state.

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
        # "建议开仓" condition is `price < min(window)` — i.e. a new 120-day
        # low. We persist a queue so the rule survives Action restarts and
        # JSON round-trips. This window is the **only** thing the engine
        # mutates now, so it must not be wiped by the migration.
        "last_prices": [],
        "cum_realized": 0.0,
        # Append-only log of every executed open/add/tp event.
        # Schema per op: {date, op, source, price, shares, note, (tp-only:
        # pnl, num_buys_exited, total_shares_exited, shares_to_merge)}.
        # The page renders this as the "复合网格 操作记录" table under the
        # strategy chart. Cleared on tp is `entries`, NOT `ops` — the op log
        # is the permanent record.
        #
        # `source` has two values now that the engine stopped trading:
        #   "manual"   — page editor via apply_grid.py (**唯一的实盘来源**)
        #   "backtest" — ops_from_cycles() replay of historical prices, i.e.
        #                SIMULATED (see scripts/backfill_grid_ops.py)
        # ("auto" is a legacy value from the auto-trading era; the migration
        #  scripts/reset_grid_positions.py removes those rows.)
        "ops": [],
        # 归并份额 (float, shares). 每次止盈时 `total_shares -
        # total_invested/price` 归并到这里 —— 这批份额**离开网格的账, 变成
        # 再平衡持仓的一部分** (模拟口径, 见 split_take_profit 的说明)。
        # 旧版 state 没有这个 key, 读取一律走 .get("merged_shares", 0.0)
        # 以保持向后兼容。
        #
        # ⚠️ state 是用**字面量 dict** 重建的 (见 advance_one_day /
        # manual_op), 少写一个 key 就会被静默丢弃而不报错。
        "merged_shares": 0.0,
    }


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

    Returns:
      signal  "open"|"add"|"tp"|None  —— 已触发的建议 (至多一个)
      kind    "open"|"add"|"tp"        —— **最近**的那个触发位
      pct     float                    —— 距 kind 的百分比 (≤0 = 已触发)
      price   float                    —— kind 的绝对价格
      dist_open_pct / dist_add_pct / dist_tp_pct: float|None
      open_price / add_price / tp_price: float|None
    """
    price = float(price)
    in_cycle = bool(state.get("in_cycle", False))
    entries: list[dict] = list(state.get("entries", []))
    window: list[float] = list(state.get("last_prices", []))
    if not window and state.get("last_120d_min") is not None:
        window = [float(state["last_120d_min"])]

    out: dict = {
        "signal": None,
        "kind": "open",
        "pct": 0.0,
        "price": 0.0,
        "dist_open_pct": None,
        "dist_add_pct": None,
        "dist_tp_pct": None,
        "open_price": None,
        "add_price": None,
        "tp_price": None,
    }

    # --- open 的触发位: 窗口最低 (待机时才用得到) ---
    wmin = min(window) if window else None
    if wmin and wmin > 0:
        out["open_price"] = float(wmin)
        out["dist_open_pct"] = (price - wmin) / wmin * 100

    if not in_cycle:
        # 待机: 只可能建议开仓
        if wmin and price < wmin:
            out["signal"] = "open"
            out["kind"] = "open"
            out["pct"] = out["dist_open_pct"]
            out["price"] = float(wmin)
        else:
            out["kind"] = "open"
            out["pct"] = out["dist_open_pct"] if out["dist_open_pct"] is not None else 0.0
            out["price"] = float(wmin) if wmin else 0.0
        return out

    # --- 周期中: 加仓位 + 出场位 ---
    total_shares = sum(e["shares"] for e in entries)
    total_invested = sum(e["shares"] * e["price"] for e in entries)
    avg_cost = total_invested / total_shares if total_shares else 0.0

    if entries:
        add_target = float(entries[-1]["price"]) * (1 - step_pct / 100)
        out["add_price"] = add_target
        if price > 0:
            out["dist_add_pct"] = (add_target - price) / price * 100

    if total_shares > 0 and avg_cost > 0:
        if tp_basis == "first_buy" and entries:
            tp_target = float(entries[0]["price"]) * (1 + tp_pct / 100)
        else:
            tp_target = avg_cost * (1 + tp_pct / 100)
        out["tp_price"] = tp_target
        if price > 0:
            out["dist_tp_pct"] = (tp_target - price) / price * 100

    # --- 判定 ---
    # 顺序: **先 tp 后 add**。正常金字塔里后一笔买价更低 (entries[-1] ≤
    # entries[0]), 所以 "再跌 3%" 与 "涨回 3%" 不可能同时成立; 只有用户手动
    # 在比首笔高 6.2% 以上加仓时才会撞上, 此时报 tp —— 一个周期以 1 笔出场
    # 收尾, 更贴合 "N 买 1 卖" 的口径。
    tp_hit = out["tp_price"] is not None and price >= out["tp_price"]
    add_hit = out["add_price"] is not None and price <= out["add_price"]

    if tp_hit:
        out["signal"] = "tp"
        out["kind"] = "tp"
        out["pct"] = out["dist_tp_pct"] if out["dist_tp_pct"] is not None else 0.0
        out["price"] = out["tp_price"]
    elif add_hit:
        out["signal"] = "add"
        out["kind"] = "add"
        out["pct"] = out["dist_add_pct"] if out["dist_add_pct"] is not None else 0.0
        out["price"] = out["add_price"]
    else:
        # 都没触发 → 取**更近**的那个
        cands = [(k, p) for k, p in (("add", out["dist_add_pct"]),
                                     ("tp", out["dist_tp_pct"]))
                 if p is not None]
        if cands:
            kind, pct = min(cands, key=lambda kv: abs(kv[1]))
            out["kind"] = kind
            out["pct"] = pct
            out["price"] = out["add_price"] if kind == "add" else out["tp_price"]
        else:
            out["kind"] = "tp"
            out["pct"] = 0.0
            out["price"] = out["tp_price"] or 0.0
    return out


def advisory(state: dict, price: float, layer_yuan_value: float,
             step_pct: float = GRID_STEP_PCT,
             tp_pct: float = GRID_TP_PCT,
             tp_basis: str = GRID_TP_BASIS) -> dict:
    """给页面/邮件用的完整建议。纯函数 (不改 state)。

    返回: signal / trigger_kind / trigger_pct / trigger_price /
          dist_open_pct / dist_add_pct / dist_tp_pct /
          suggest_price (= 今日 K 线最低价) / suggest_shares / suggest_amount

    ⚠️ `signal` 是**建议提交哪种 op**, 不是"已成交" —— 引擎不再下单。
    """
    trig = _triggers(state, float(price), step_pct, tp_pct, tp_basis)
    sig = trig["signal"]
    amount = float(layer_yuan_value) if sig in ("open", "add") else 0.0
    shares = (amount / float(price)) if (amount and price > 0) else 0.0

    def _r(v):
        return round(v, 2) if v is not None else None

    return {
        "signal": sig,
        "trigger_kind": trig["kind"],
        "trigger_pct": round(trig["pct"], 2),
        "trigger_price": round(trig["price"], 4),
        "dist_open_pct": _r(trig["dist_open_pct"]),
        "dist_add_pct": _r(trig["dist_add_pct"]),
        "dist_tp_pct": _r(trig["dist_tp_pct"]),
        "suggest_price": round(float(price), 4),
        "suggest_shares": round(shares, 2),
        "suggest_amount": round(amount, 2),
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
    """Advance one asset's grid state by one trading day — **只给建议, 不下单**。

    ⚠️ 引擎**不再自动交易**。它只做两件事:
      1) 滚动 120 日窗口 (`last_prices`) —— 距开仓建议要靠它;
      2) 算出今天"该做什么" (open / add / tp / None)。

    它**绝不**修改 `entries` / `in_cycle` / `cum_realized` / `merged_shares`,
    也**绝不**追加 `ops`。真正的开仓/加仓/回本只能来自用户在
    「交易记录 → 复合网格」提交的 ops (scripts/apply_grid.py → manual_op)。

    Args:
        state:        asset state dict (read-only here; a NEW dict is returned).
        date_str:     ISO date string for today (unused by the engine, kept
                      for signature compatibility).
        price:        today's price. The daily caller (record.py) passes the
                      K-line LOW so a suggestion can fire on an intraday dip
                      even if the close is above yesterday's close.
        layer_yuan_value: pre-computed buy amount for today (¥). Kept for
                      signature compatibility — the engine doesn't use it;
                      `advisory()` reports the suggested amount.
        lookback/step_pct/tp_pct/tp_basis: strategy parameters.

    Returns:
        (new_state, suggestion), suggestion ∈ {"open", "add", "tp", None}.
        `suggestion` says which op to **submit**, not what already traded.
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
    ops: list[dict] = list(state.get("ops", []))   # 只读回写, 永不追加

    # 建议基于**追加今天之前**的窗口算
    view = {"in_cycle": in_cycle, "entries": entries,
            "last_prices": last_prices, "last_120d_min": legacy_min}
    suggestion = _triggers(view, float(price), step_pct, tp_pct, tp_basis)["signal"]

    # 唯一的副作用: 滚动窗口
    last_prices.append(float(price))
    if len(last_prices) > lookback:
        last_prices = last_prices[-lookback:]

    # ⚠️ 固定 6 key 字面量 —— 少写一个 key 会被静默丢弃
    new_state = {
        "in_cycle": in_cycle,
        "entries": entries,
        "last_prices": last_prices,
        "cum_realized": cum_realized,
        "ops": ops,
        "merged_shares": merged_shares,
    }
    return new_state, suggestion


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
        new_state["ops"].append({
            "date": today, "op": op, "source": "manual",
            "price": float(price), "shares": float(shares),
            "note": note,
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
            "note": note,
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
