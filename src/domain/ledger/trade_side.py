"""The 8 trade sides of the ledger (BC-A vocabulary).

A `TradeSide` says **what kind of movement** a ledger entry represents.
Four of them move cash between the two pools, two move cash in/out of the
book, and two move ETF shares.

The two cash pools are:
  - `cash_rebalance` — **real** money, used for ETF buys/sells
  - `cash_grid`      — **virtual** budget slot for grid accounting
"""
from __future__ import annotations

from enum import Enum


class TradeSide(str, Enum):
    """The direction of a ledger entry. Values match `data/trades.json`."""

    BUY = "buy"
    SELL = "sell"
    DEPOSIT_REBALANCE = "deposit_rebalance"
    WITHDRAW_REBALANCE = "withdraw_rebalance"
    DEPOSIT_GRID = "deposit_grid"
    WITHDRAW_GRID = "withdraw_grid"
    TRANSFER_TO_GRID = "transfer_to_grid"
    TRANSFER_TO_REBALANCE = "transfer_to_rebalance"

    def __str__(self) -> str:  # so f-strings / JSON dumps keep the wire value
        return self.value

    @property
    def moves_shares(self) -> bool:
        """True for the two sides that change ETF share counts."""
        return self in (TradeSide.BUY, TradeSide.SELL)

    @property
    def moves_cash(self) -> bool:
        """True for the six sides that change a cash pool."""
        return not self.moves_shares

    @property
    def is_transfer(self) -> bool:
        """True for the two pool-to-pool transfers."""
        return self in (TradeSide.TRANSFER_TO_GRID, TradeSide.TRANSFER_TO_REBALANCE)


#: Sides that need `code` + `shares` + `price` (+ optional `fee`).
SHARE_SIDES: frozenset[str] = frozenset({TradeSide.BUY.value, TradeSide.SELL.value})

#: Sides that need `amount` only.
AMOUNT_SIDES: frozenset[str] = frozenset(
    s.value for s in TradeSide if s.moves_cash
)
