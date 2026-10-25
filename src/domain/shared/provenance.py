"""Where a share came from — the marker that keeps simulation out of reality.

The grid is a **virtual** strategy: it never touches real money, and the
shares it hands back to the rebalance portfolio on a cost-only exit are
**simulated** (they are deliberately not written to `data/trades.json`,
which records real fills only).

But those simulated shares *do* count toward the portfolio's weights — and
therefore can trigger a **real** rebalance trade. That is the user's
explicit design choice, not a bug. What was missing was a way to *say so
in the code*; `Provenance` is that way.

`PositionLot.provenance` makes the boundary visible:
  - `REAL`            — bought with real money, recorded in the ledger
  - `SIMULATED_GRID`  — came out of the grid's cost-only exit, cost basis 0

The presentation layer is required to disclose the difference
(docs/domain_model.md §10, invariant I15).
"""
from __future__ import annotations

from enum import Enum


class Provenance(str, Enum):
    """Where a lot of shares came from."""

    REAL = "real"
    """Bought with real money; recorded in `data/trades.json`."""

    SIMULATED_GRID = "simulated_grid"
    """Handed over by the grid's cost-only exit; simulated, never in the ledger."""

    def __str__(self) -> str:
        return self.value

    @property
    def is_simulated(self) -> bool:
        return self is not Provenance.REAL
