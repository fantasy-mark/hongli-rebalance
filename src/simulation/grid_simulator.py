"""Compound grid **simulator** (BC-E) — replays historical prices.

⚠️ This is a **separate bounded context** from the live grid
(`src/domain/grid/grid_position.py`). It answers "what would the grid have
done over this price history?", not "what should I do today?".

**The two are NOT reconcilable line-by-line**, and that is by design:

| | 图表回放 (this module) | 实盘 (manual ops) |
|---|---|---|
| 起点 | 2020-11-05 全历史 | 用户自己提交的 op |
| 层额 | 固定 `GRID_BASE_YUAN` ¥10,000 | 浮动层额 / 用户填的份额 |
| 驱动价 | **收盘价** | **K 线最低价** |
| 成交 | 模拟器自己算 | 真实成交 |

The output lands in `data/grid_state.json` as `source="backtest"` ops
(never as live trades) — see `ops_from_cycles` / `merge_backtest_ops`.

**Anti-corruption layer**: this module may *read* the grid state's shape but
must never write the live `data/grid_state.json` — the backfill script
(`scripts/backfill_grid_ops.py`) is the only writer, and it goes through
`merge_backtest_ops`, which preserves live ops.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..domain.grid.exit_policy import cost_only_exit
from ..domain.grid.grid_position import (
    GRID_BASE_YUAN,
    GRID_LOOKBACK,
    GRID_STEP_PCT,
    GRID_TP_BASIS,
    GRID_TP_PCT,
)
from ..domain.shared.money import Price, ShareCount


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
                split = cost_only_exit(total_shares, total_invested, float(price))
                merged_shares += split.merged_shares
                cum_realized += split.pnl
                cycles.append({
                    "entries": entries,
                    "exit_date": date,
                    "exit_price": float(price),
                    "num_buys": len(entries),
                    "avg_cost": avg_cost,
                    # 卖出的是"回本所需"那部分, 不是全部持仓 —— 见 cost_only_exit
                    "total_shares": split.shares_sold,
                    "shares_transferred": split.merged_shares,
                    "total_invested": total_invested,
                    "exit_value": split.proceeds,
                    "pnl": split.pnl,
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
                "price": Price(e["price"]),
                "shares": ShareCount(e["shares"]),
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
            "price": Price(c["exit_price"]),
            "shares": ShareCount(sold),
            "note": "回测·回本出场",
            "pnl": round(float(c["pnl"]), 2),
            "num_buys_exited": int(c["num_buys"]),
            "total_shares_exited": ShareCount(sold + to_merge),
            "shares_to_merge": ShareCount(to_merge),
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
