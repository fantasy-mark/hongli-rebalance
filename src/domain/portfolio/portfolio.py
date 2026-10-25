"""The portfolio aggregate — what the rebalance strategy actually holds (BC-B).

A `Position` is made of one or more `PositionLot`s. Splitting the lot from
the position is what lets us say "this holding is real, that one came out
of the grid as a simulation" — instead of hanging a `merged_shares` float
off the side of a position dict.

⚠️ The grid's merged shares **do** count toward the portfolio's weights and
can therefore trigger a real rebalance. That is the user's explicit design
decision (2026-10-24); the model just makes it visible. `Provenance` marks
it; the presentation layer must disclose it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..shared.provenance import Provenance


@dataclass(frozen=True)
class PositionLot:
    """A batch of shares with a known origin.

    `cost` is the money already sunk into this lot:
      - REAL lots: what was actually paid (price × shares + fee)
      - SIMULATED_GRID lots: **0** — the grid handed them over after
        recovering its principal, so their cost basis is zero by
        construction (the "sell only the cost amount" exit rule).
    """

    shares: float
    cost: float
    provenance: Provenance = Provenance.REAL

    @property
    def is_simulated(self) -> bool:
        return self.provenance.is_simulated


@dataclass
class Position:
    """One asset's holding. Identity = its asset code."""

    code: str
    name: str
    price: float = 0.0
    lots: list[PositionLot] = field(default_factory=list)

    # -- share accounting -------------------------------------------------
    @property
    def real_shares(self) -> float:
        """Shares bought with real money (the ledger's view)."""
        return round(sum(l.shares for l in self.lots
                         if l.provenance is Provenance.REAL), 2)

    @property
    def simulated_shares(self) -> float:
        """Shares handed over by the grid (simulated)."""
        return round(sum(l.shares for l in self.lots
                         if l.provenance.is_simulated), 2)

    @property
    def total_shares(self) -> float:
        """All shares, real + simulated — the number the weights use."""
        return round(sum(l.shares for l in self.lots), 2)

    @property
    def merged_shares(self) -> float:
        """Alias for `simulated_shares` — the grid's contribution."""
        return self.simulated_shares

    @property
    def merged_value(self) -> float:
        return round(self.simulated_shares * self.price, 2)

    @property
    def value(self) -> float:
        """Market value of the whole position."""
        return round(self.total_shares * self.price, 2)

    @property
    def merged_only(self) -> bool:
        """True when the position is *only* simulated shares."""
        return self.real_shares <= 0 and self.simulated_shares > 0

    def to_record(self) -> dict:
        """The JSON shape `data/history.json` and the page expect.

        ⚠️ This dict is a **persistence/presentation contract** — the page
        (app.js) and the email template read these exact keys. Changing it
        means changing both consumers in the same commit.
        """
        return {
            "name": self.name,
            "code": self.code,
            "shares": self.real_shares,
            "price": round(self.price, 4),
            "date": "",
            "value": self.value,
            "merged_shares": self.merged_shares,
            "total_shares": self.total_shares,
            "merged_value": self.merged_value,
        }


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

    Returns a new list of position dicts (the input is not mutated).  Each
    position gains:
        merged_shares  — shares handed over by the grid (模拟口径)
        merged_value   — their market value
        total_shares   — real shares + merged shares
    and `value` becomes total_shares × price, so the 3pp weight and the
    portfolio total see the merged exposure.

    A code that has merged shares but no real holding becomes a position
    of its own (shares = 0, all of it merged) — the portfolio genuinely
    holds it.  Its price comes from `extra_prices`; a code we couldn't
    price is skipped (and reported by the caller as an error).
    """
    extra_prices = extra_prices or {}
    out: list[dict] = []
    seen: set[str] = set()
    for p in positions:
        code = str(p.get("code", ""))
        # 份额先定到 2 位小数, 再由它算市值 —— 页面上的"多少份"和"多少钱"
        # 必须自洽 (用户拿显示的份额 × 价格验算要能对上)。
        merged = round(float(merged_by_code.get(code, 0.0) or 0.0), 2)
        seen.add(code)
        real_shares = float(p.get("shares", 0.0))
        price = float(p.get("price", 0.0))
        new = dict(p)
        new["merged_shares"] = merged
        new["total_shares"] = round(real_shares + merged, 2)
        new["merged_value"] = round(merged * price, 2)
        new["value"] = round(new["total_shares"] * price, 2)
        out.append(new)
    # Codes with merged shares but no real holding → new position rows.
    from .asset_universe import ETF_DEFS

    for code, merged in merged_by_code.items():
        if code in seen:
            continue
        price = extra_prices.get(code)
        if not price:
            continue
        name = ETF_DEFS[code][2]
        merged = round(float(merged), 2)
        merged_value = round(merged * float(price), 2)
        out.append({
            "name": name,
            "code": code,
            "shares": 0.0,
            "merged_shares": merged,
            "total_shares": merged,
            "merged_value": merged_value,
            "price": round(float(price), 4),
            "date": "",
            "value": merged_value,
            "merged_only": True,
        })
    return out
