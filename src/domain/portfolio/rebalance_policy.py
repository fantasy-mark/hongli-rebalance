"""Rebalance policy — when does the portfolio need re-balancing? (BC-B)

Pure domain logic, no I/O, no globals:

  - `total`    = Σ position value + cash_rebalance + cash_grid
  - `target_w` = 1 / len(positions)  ← **dynamic basket** (3 / 4 / 5 assets
                 all work without touching the code)
  - `triggered`= ∃ asset : |w − target_w| × 100 > threshold_pp
  - when triggered the plan is a **full snap-back** to the target weights,
    not an incremental top-up.

⚠️ The threshold used to be a module-level environment variable read at
import time. It is now an explicit parameter, so several policies can
coexist in one process and tests don't have to mutate the environment.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from ..shared.money import DeviationPP, Money


def evaluate(positions: list[dict], cash_rebalance: float, cash_grid: float,
              threshold_pp: float = 3.0,
              ) -> tuple[float, list[float], bool, list[dict]]:
    """Return (total, weights, triggered, plan). plan is empty when not triggered.

    total = etf_value + cash_rebalance + cash_grid.  The grid pool is
    part of the portfolio (so weights and trigger check include it),
    but the grid engine itself is fully virtual and doesn't touch the
    cash pool — see src/domain/grid/grid_position.py.

    `positions` must already include the grid's merged shares (see
    merge_grid_shares_into_positions) — they are portfolio holdings now,
    so they have to be in the value the weights are computed on.

    The target weight is dynamic: 1/len(positions) — works for 3, 4, 5
    asset configurations.
    """
    cash_total = cash_rebalance + cash_grid
    total = sum(p["value"] for p in positions) + cash_total
    n = len(positions)
    target_w = 1.0 / n if n > 0 else 0.0
    weights = [p["value"] / total if total > 0 else 0 for p in positions]
    triggered = any(abs(w - target_w) * 100 > threshold_pp for w in weights)
    plan: list[dict] = []
    if triggered and total > 0:
        for p, w in zip(positions, weights):
            target_value = total * target_w
            plan.append({
                "name": p["name"],
                "code": p["code"],
                "current_value": Money(p["value"]),
                "target_value": Money(target_value),
                "delta_value": Money(target_value - p["value"]),
            })
    return total, weights, triggered, plan


def target_weight(n_positions: int) -> float:
    """The equal-weight target for a basket of `n_positions` assets."""
    return 1.0 / n_positions if n_positions > 0 else 0.0


def max_deviation_pp(weights: Sequence[float], target_w: float) -> float:
    """Largest absolute deviation from the target, in percentage points."""
    if not weights:
        return 0.0
    return DeviationPP(max(abs(w - target_w) * 100 for w in weights))
