"""The portfolio aggregate — what the rebalance strategy actually holds (BC-B).

A `Position` is made of one or more `PositionLot`s. Splitting the lot from
the position is what lets us say "this holding is real, that one came out
of the grid as a simulation" — instead of hanging a `merged_shares` float
off the side of a position dict.

⚠️ The grid's merged shares **do** count toward the portfolio's weights and
can therefore trigger a real rebalance. That is the user's explicit design
decision (2026-10-24); the model just makes it visible. `Provenance` marks
it; the presentation layer must disclose it.

**Phase 4 (2026-10-27)**: `Position` / `PositionLot` / `Provenance` used to
be *defined but never used* — the application layer passed position dicts
around and the aggregate sat unused in `domain/`. Now the aggregate is the
**only** thing that computes holdings, weights and values; the dicts in
`data/history.json` are its *output* (`Portfolio.to_records`).

That flip is the point: "模拟份额推着真实组合调仓" is no longer a fact you
learn by reading four sibling fields (`merged_shares` / `total_shares` /
`merged_value` / `merged_only`) — it is a fact you can read off the model
(`position.simulated_shares`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..shared.money import Money, Price, ShareCount
from ..shared.provenance import Provenance


@dataclass(frozen=True)
class PositionLot:
    """A batch of shares with a known origin.

    `cost` is the money already sunk into this lot:
      - REAL lots: what was actually paid (price × shares + fee)
      - SIMULATED_GRID lots: **0** — the grid handed them over after
        recovering its principal, so their cost basis is zero by
        construction (the "sell only the cost amount" exit rule).

    ⚠️ The ledger projection (`rebuild_holdings_from_trades`) does not carry
    a cost basis, so a REAL lot's `cost` is currently **0 = 未记录**, not
    "bought for free". Don't read anything into it until the projection
    grows a cost basis.
    """

    shares: float
    cost: float = 0.0
    provenance: Provenance = Provenance.REAL

    @property
    def is_simulated(self) -> bool:
        return self.provenance.is_simulated


def _lots_for(real_shares: float, simulated_shares: float) -> list[PositionLot]:
    """Split a position's share count into its lots by provenance.

    The SIMULATED lot is rounded to 2 dp (**份额就是 2 位小数** — the same
    `ShareCount` rule the ledger applies), the REAL lot is kept **as given**
    (the ledger already rounded it; re-rounding would change the record).
    """
    lots = [PositionLot(shares=real_shares,
                        cost=0.0,
                        provenance=Provenance.REAL)]
    if simulated_shares > 0:
        lots.append(PositionLot(shares=ShareCount(simulated_shares),
                                cost=0.0,
                                provenance=Provenance.SIMULATED_GRID))
    return lots


@dataclass
class Position:
    """One asset's holding. Identity = its asset code."""

    code: str
    name: str
    price: float = 0.0
    lots: list[PositionLot] = field(default_factory=list)
    #: The quote's as-of date (`""` when unknown) — carried into the record.
    date: str = ""
    #: QDII premium snapshot (BC-D data, passed through to the page).
    premium: Optional[dict] = None

    # -- share accounting -------------------------------------------------
    @property
    def real_shares(self) -> float:
        """Shares bought with real money (the ledger's view).

        ⚠️ **Not** rounded: this is the ledger's number, verbatim. The
        rounding discipline says a value object is constructed *where the
        original `round()` was* — and the original never rounded it.
        """
        return sum(l.shares for l in self.lots
                   if l.provenance is Provenance.REAL)

    @property
    def simulated_shares(self) -> float:
        """Shares handed over by the grid (simulated)."""
        return float(sum(l.shares for l in self.lots
                         if l.provenance.is_simulated))

    @property
    def total_shares(self) -> float:
        """All shares, real + simulated — the number the weights use."""
        return ShareCount(self.real_shares + self.simulated_shares)

    @property
    def merged_shares(self) -> float:
        """Alias for `simulated_shares` — the grid's contribution."""
        return self.simulated_shares

    @property
    def merged_value(self) -> float:
        return Money(self.simulated_shares * self.price)

    @property
    def value(self) -> float:
        """Market value of the whole position."""
        return Money(self.total_shares * self.price)

    @property
    def merged_only(self) -> bool:
        """True when the position is *only* simulated shares."""
        return self.real_shares <= 0 and self.simulated_shares > 0

    def to_record(self) -> dict:
        """The JSON shape `data/history.json` and the page expect.

        ⚠️ This dict is a **persistence/presentation contract** — the page
        (app.js) and the email template read these exact keys. Changing it
        means changing both consumers in the same commit.

        ⚠️ The **key order** is part of the contract too, in the sense that
        `data/history.json` is a committed artifact whose diffs the user
        reads. The grid-only row keeps its historical order (`merged_*`
        before `price`/`date`/`value`) so today's data stays byte-identical.
        """
        if self.merged_only:
            return {
                "name": self.name,
                "code": self.code,
                "shares": float(self.real_shares),
                "merged_shares": self.simulated_shares,
                "total_shares": self.total_shares,
                "merged_value": self.merged_value,
                "price": Price(self.price),
                "date": self.date,
                "value": self.merged_value,
                "merged_only": True,
            }
        rec: dict[str, Any] = {
            "name": self.name,
            "code": self.code,
            "shares": self.real_shares,
            "price": Price(self.price),
            "date": self.date,
            "value": self.value,
        }
        if self.premium is not None:
            rec["premium"] = self.premium
        rec["merged_shares"] = self.simulated_shares
        rec["total_shares"] = self.total_shares
        rec["merged_value"] = self.merged_value
        return rec


@dataclass
class Portfolio:
    """The portfolio aggregate root (BC-B) — what the 3pp strategy rebalances.

    Identity = the set of `Position`s it holds, plus the two cash pools it
    is measured against. The cash pools live here (not in the ledger
    aggregate) because `total` — the number the 3pp rule is judged on — is
    *this* aggregate's number.
    """

    positions: list[Position] = field(default_factory=list)
    cash_rebalance: float = 0.0
    cash_grid: float = 0.0

    # -- construction -----------------------------------------------------
    @classmethod
    def from_records(cls, records: Sequence[Mapping[str, Any]], *,
                     cash_rebalance: float = 0.0,
                     cash_grid: float = 0.0) -> "Portfolio":
        """Build a portfolio from position **records** (the JSON shape).

        Inverse of `to_records`. A record may or may not carry
        `merged_shares` — one that does is round-tripping back from
        `to_records()`; one that doesn't is a raw holding.
        """
        positions: list[Position] = []
        for rec in records:
            real = rec.get("shares", 0.0)
            real = 0.0 if real is None else real
            merged = rec.get("merged_shares", 0.0)
            merged = 0.0 if merged is None else merged
            positions.append(Position(
                code=str(rec.get("code", "")),
                name=str(rec.get("name", "")),
                price=float(rec.get("price", 0.0) or 0.0),
                lots=_lots_for(real, float(merged)),
                date=str(rec.get("date", "") or ""),
                premium=rec.get("premium"),
            ))
        return cls(positions=positions,
                   cash_rebalance=cash_rebalance,
                   cash_grid=cash_grid)

    @classmethod
    def from_holdings(cls, holdings: Sequence[Mapping[str, Any]], *,
                      merged_by_code: Optional[Mapping[str, float]] = None,
                      extra_prices: Optional[Mapping[str, float]] = None,
                      cash_rebalance: float = 0.0,
                      cash_grid: float = 0.0) -> "Portfolio":
        """Build a portfolio from ledger holdings + the grid's merged shares.

        `merged_by_code` — code → shares the grid handed over (simulated).
        `extra_prices` — price for codes the portfolio doesn't hold but the
        grid merged into (otherwise we couldn't value them).

        A code that has merged shares but no real holding becomes a
        position of its own (all of it simulated) — the portfolio genuinely
        holds it.  A code we couldn't price is skipped.
        """
        from .asset_universe import ETF_DEFS

        merged_by_code = merged_by_code or {}
        extra_prices = extra_prices or {}
        positions: list[Position] = []
        seen: set[str] = set()
        for h in holdings:
            code = str(h.get("code", ""))
            seen.add(code)
            real = h.get("shares", 0.0)
            real = 0.0 if real is None else real
            positions.append(Position(
                code=code,
                name=str(h.get("name", "")),
                price=float(h.get("price", 0.0) or 0.0),
                lots=_lots_for(real, float(merged_by_code.get(code, 0.0) or 0.0)),
                date=str(h.get("date", "") or ""),
                premium=h.get("premium"),
            ))
        # Codes with merged shares but no real holding → new positions.
        for code, merged in merged_by_code.items():
            if code in seen:
                continue
            price = extra_prices.get(code)
            if not price:
                continue
            positions.append(Position(
                code=code,
                name=ETF_DEFS[code][2],
                price=float(price),
                lots=_lots_for(0.0, float(merged)),
                date="",
            ))
        return cls(positions=positions,
                   cash_rebalance=cash_rebalance,
                   cash_grid=cash_grid)

    # -- valuation --------------------------------------------------------
    @property
    def etf_value(self) -> float:
        """Σ position values (the ETF part of `total`).

        ⚠️ **Not** wrapped in `Money`: there was no `round()` here before,
        so wrapping it would move the number (see the rounding-point
        discipline in `domain/shared/money.py`).
        """
        return sum(p.value for p in self.positions)

    @property
    def total(self) -> float:
        """`etf_value + cash_rebalance + cash_grid`.

        ⚠️ The addition order is `etf + (cr + cg)` — float addition isn't
        associative, and the old code computed exactly that. Keep the
        parentheses as they are.
        """
        cash_total = self.cash_rebalance + self.cash_grid
        return self.etf_value + cash_total

    def to_records(self) -> list[dict]:
        """The persistence/presentation shape — see `Position.to_record`."""
        return [p.to_record() for p in self.positions]


def merged_shares_by_code(grid_state: dict) -> dict[str, float]:
    """Map ETF code → shares the grid has merged into the portfolio.

    The grid's take-profit sells back only the principal; the leftover
    shares are handed over to the rebalance portfolio (模拟口径 — they are
    NOT written to trades.json, which records real fills only).  From that
    point on they are ordinary portfolio holdings and must be counted in
    the position's value, the 3pp weight and the total.
    """
    from .asset_universe import NAME_TO_CODE

    out: dict[str, float] = {}
    for name, asset in (grid_state or {}).items():
        if not isinstance(asset, dict):
            continue
        code = NAME_TO_CODE.get(name)
        if not code:
            continue
        shares = float(asset.get("merged_shares", 0.0) or 0.0)
        if shares > 0:
            out[code] = shares
    return out


def merge_grid_shares_into_positions(
    positions: list[dict],
    merged_by_code: dict[str, float],
    extra_prices: dict[str, float] | None = None,
) -> list[dict]:
    """Fold the grid's merged shares into the portfolio positions.

    ⚠️ **Compatibility wrapper** — the implementation now lives in
    `Portfolio.from_holdings` (the aggregate). This function still returns
    the same dicts, in the same key order, so existing callers don't move.

    A code that has merged shares but no real holding becomes a position
    of its own (shares = 0, all of it merged) — the portfolio genuinely
    holds it.  Its price comes from `extra_prices`; a code we couldn't
    price is skipped (and reported by the caller as an error).
    """
    return Portfolio.from_holdings(
        positions, merged_by_code=merged_by_code, extra_prices=extra_prices,
    ).to_records()


__all__ = [
    "Portfolio", "Position", "PositionLot",
    "merge_grid_shares_into_positions", "merged_shares_by_code",
]
