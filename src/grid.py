"""Compound (pyramid) grid engine + daily-state advancement.

Rules (per asset, independent):
  - Trigger: today's close < min(close of past `lookback` days) → start a
    new cycle, buying `layer_yuan` worth.
  - Pyramid: while in a cycle, every additional `step_pct` % drop from
    the LAST buy price adds another `layer_yuan` layer.
  - Take-profit: when close >= avg_cost * (1 + tp_pct/100), sell ALL
    shares, lock in realized P&L, then wait for the next trigger.

Layer sizing:
  layer_yuan(d) = GRID_BASE_YUAN + max(pool_value - recent_min, 0) * GRID_FLOATING_FRACTION
  where recent_min = min(pool_value over the past GRID_FLOATING_WINDOW trading days).

State persistence: `advance_one_day()` mutates a JSON-serializable dict so
the Action can commit `data/grid_state.json` after each run.
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
    open_cycle, open_avg_cost, open_invested, open_shares.
    """
    import pandas as pd  # local import keeps the module lightweight

    in_cycle = False
    entries: list[dict] = []
    cycles: list[dict] = []
    cum_realized = 0.0
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
                exit_value = total_shares * price
                pnl = exit_value - total_invested
                cum_realized += pnl
                cycles.append({
                    "entries": entries,
                    "exit_date": date,
                    "exit_price": float(price),
                    "num_buys": len(entries),
                    "avg_cost": avg_cost,
                    "total_shares": total_shares,
                    "total_invested": total_invested,
                    "exit_value": exit_value,
                    "pnl": pnl,
                    "return_pct": pnl / total_invested * 100 if total_invested else 0.0,
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

        history.append({
            "date": date,
            "in_cycle": in_cycle,
            "num_buys": len(entries),
            "avg_cost": avg_cost,
            "total_invested": total_invested,
            "current_value": current_value,
            "unrealized_pnl": unrealized,
            "cum_realized": cum_realized,
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
    }


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
        price:        today's close.
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

    signal: Optional[str] = None

    if not in_cycle:
        # Trigger: today < min of past `lookback` days (rolling window).
        if last_prices and price < min(last_prices):
            sh = layer_yuan_value / price if price > 0 else 0.0
            entries.append({"date": date_str, "price": float(price), "shares": sh})
            in_cycle = True
            signal = "buy"
    else:
        last_price = entries[-1]["price"]
        # Pyramid: drop >= step_pct from last buy
        if price <= last_price * (1 - step_pct / 100):
            sh = layer_yuan_value / price if price > 0 else 0.0
            entries.append({"date": date_str, "price": float(price), "shares": sh})
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
                exit_value = total_shares * price
                pnl = exit_value - total_invested
                cum_realized += pnl
                # Cycle closed: record exits in a transient log; the next call
                # with in_cycle=False starts a new cycle from scratch.
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
    }
    return new_state, signal


# -----------------------------------------------------------------------------
# Snapshot helpers (for chart + notify)
# -----------------------------------------------------------------------------

def asset_snapshot(state: dict, current_price: float) -> dict:
    """Return a read-only summary for a single asset's state."""
    entries = state.get("entries", [])
    in_cycle = bool(state.get("in_cycle", False))
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
        }
    return {
        "in_cycle": False,
        "num_buys": 0,
        "avg_cost": 0.0,
        "total_invested": 0.0,
        "current_value": 0.0,
        "unrealized_pnl": 0.0,
        "return_pct": 0.0,
    }
