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

**Phase 4 (2026-10-27)**: the rule lives in the `RebalancePolicy` **class**
— a domain service with the threshold as *its* state, so "which threshold
are we using" is answerable by looking at an object instead of at a
module global. The module-level `evaluate()` below is a thin compatibility
wrapper over the same class (there is exactly **one** implementation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..shared.money import DeviationPP, Money
from .portfolio import Portfolio


@dataclass(frozen=True)
class TargetWeightPolicy:
    """The equal-weight target — `1 / n`, `n` = the current basket size.

    A separate policy because "what weight should each asset have" is a
    rule of its own: it is what makes the basket **dynamic** (3 / 4 / 5
    assets all work without touching the code) and it is what the page
    reads to render "目标 ±x%".
    """

    def target_weight(self, n_positions: int) -> float:
        """The equal-weight target for a basket of `n_positions` assets."""
        return 1.0 / n_positions if n_positions > 0 else 0.0

    def for_portfolio(self, portfolio: Portfolio) -> float:
        """The target weight for this portfolio's basket."""
        return self.target_weight(len(portfolio.positions))


@dataclass(frozen=True)
class RebalanceOutcome:
    """The result of judging a portfolio against the policy.

    Attributes:
        total:    portfolio total (positions + both cash pools)
        weights:  each position's weight — **raw floats**, not rounded
                   (rounding happens at the presentation boundary only)
        triggered: does the basket deviate more than the threshold?
        plan:     the snap-back plan; empty when not triggered
    """

    total: float
    weights: list[float]
    triggered: bool
    plan: list[dict] = field(default_factory=list)

    def as_tuple(self) -> tuple[float, list[float], bool, list[dict]]:
        """`(total, weights, triggered, plan)` — the old 4-tuple shape."""
        return self.total, self.weights, self.triggered, self.plan


@dataclass(frozen=True)
class RebalancePolicy:
    """When does this portfolio need re-balancing? **Pure domain service.**

    Attributes:
        threshold_pp: 偏离多少个百分点才触发 (default 3pp)
        target:       目标权重规则 (等权 1/n)
    """

    threshold_pp: float = 3.0
    target: TargetWeightPolicy = field(default_factory=TargetWeightPolicy)

    def evaluate(self, portfolio: Portfolio) -> RebalanceOutcome:
        """Judge `portfolio` against this policy.

        The grid pool is part of the portfolio (so weights and the trigger
        check include it), but the grid engine itself is fully virtual and
        doesn't touch the cash pool — see `domain/grid/grid_position.py`.

        The portfolio must already include the grid's merged shares — they
        are portfolio holdings now, so they have to be in the value the
        weights are computed on.
        """
        total = portfolio.total
        weights = [p.value / total if total > 0 else 0
                   for p in portfolio.positions]
        target_w = self.target.for_portfolio(portfolio)
        triggered = any(abs(w - target_w) * 100 > self.threshold_pp
                        for w in weights)
        plan: list[dict] = []
        if triggered and total > 0:
            for p in portfolio.positions:
                target_value = total * target_w
                plan.append({
                    "name": p.name,
                    "code": p.code,
                    "current_value": Money(p.value),
                    "target_value": Money(target_value),
                    "delta_value": Money(target_value - p.value),
                })
        return RebalanceOutcome(total=total, weights=weights,
                                triggered=triggered, plan=plan)


def evaluate(positions: list[dict], cash_rebalance: float, cash_grid: float,
              threshold_pp: float = 3.0,
              ) -> tuple[float, list[float], bool, list[dict]]:
    """Return (total, weights, triggered, plan). plan is empty when not triggered.

    ⚠️ **Compatibility wrapper** — the rule lives in `RebalancePolicy`.
    Kept because it is the shape half the codebase (and the tests) use;
    it delegates, so there is still only one implementation.
    """
    portfolio = Portfolio.from_records(positions,
                                       cash_rebalance=cash_rebalance,
                                       cash_grid=cash_grid)
    return RebalancePolicy(threshold_pp=threshold_pp).evaluate(
        portfolio).as_tuple()


def target_weight(n_positions: int) -> float:
    """The equal-weight target for a basket of `n_positions` assets."""
    return TargetWeightPolicy().target_weight(n_positions)


def max_deviation_pp(weights: Sequence[float], target_w: float) -> float:
    """Largest absolute deviation from the target, in percentage points."""
    if not weights:
        return 0.0
    return DeviationPP(max(abs(w - target_w) * 100 for w in weights))


__all__ = [
    "RebalancePolicy", "RebalanceOutcome", "TargetWeightPolicy",
    "evaluate", "target_weight", "max_deviation_pp",
]
