"""Valuation — turning shares into money (BC-B).

The domain doesn't know **where** prices come from; it asks a
`PriceProvider` for the latest price of a code and does the arithmetic
itself.  The concrete provider (sina / eastmoney / tencent / akshare)
lives in `src/infrastructure/quote_providers.py` — the dependency points
**inwards**.

Why the inversion: the old `record.py` called `latest()` from the
middle of the valuation loop, which meant the valuation could not be
exercised without the network, and "who owns the price" had no answer
in the code.  Now the *what* (shares × price) is domain, the *where*
(quote provider) is infrastructure.

Domain layer rule: **no pandas, no akshare, no I/O.**
"""
from __future__ import annotations

import datetime
from typing import Callable, Optional, Protocol, Sequence, Tuple

from ..events.domain_events import EventStream, PositionValued
from ..shared.money import Money, Price
from .asset_universe import ETF_DEFS


class PriceProvider(Protocol):
    """Returns `(price, as_of_date)` for a given ETF code.

    Implementations may raise; the caller collects the failure as an
    error tuple instead of aborting the whole valuation.
    """

    def __call__(self, code: str) -> Tuple[float, datetime.date]: ...


def value_positions(
    holdings: Sequence[dict],
    price_provider: PriceProvider,
    universe: Optional[dict] = None,
    events: Optional[EventStream] = None,
) -> Tuple[list[dict], float, list[Tuple[str, str]]]:
    """Value every holding at the latest price.

    Returns `(positions, etf_total, errors)`:
      - `positions` — one dict per holding that could be priced:
            {name, code, shares, price, date, value}
            ⚠️ This dict is a **persistence contract** — it goes straight
            into `data/history.json` and the page reads these keys.
      - `etf_total` — Σ value of the priced positions
      - `errors`     — [(code, reason)] for holdings we couldn't price

    `events` (optional) — 每个成功估值的持仓发一个 `PositionValued`。
    """
    universe = ETF_DEFS if universe is None else universe
    positions: list[dict] = []
    etf_total = 0.0
    errors: list[Tuple[str, str]] = []
    for h in holdings:
        code = str(h.get("code", ""))
        if code not in universe:
            errors.append((code, "unknown code (not in ETF_DEFS)"))
            continue
        try:
            price, as_of = price_provider(code)
            price = float(price)
            value = float(h.get("shares", 0.0)) * price
            etf_total += value
            positions.append({
                "name": h.get("name", ""),
                "code": code,
                "shares": h.get("shares", 0.0),
                "price": Price(price),
                "date": as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of),
                "value": Money(value),
            })
            if events is not None:
                events.record(PositionValued(
                    date=positions[-1]["date"],
                    code=code,
                    name=str(h.get("name", "")),
                    price=price,
                    shares=float(h.get("shares", 0.0) or 0.0),
                    value=float(positions[-1]["value"]),
                ))
        except Exception as ex:
            errors.append((code, str(ex)))
    return positions, etf_total, errors


def value_extra_codes(
    codes: Sequence[str],
    price_provider: PriceProvider,
    universe: Optional[dict] = None,
) -> Tuple[dict[str, float], list[Tuple[str, str]]]:
    """Value codes that the portfolio doesn't hold but still owns exposure to.

    A code can end up here when the grid merged simulated shares into an
    asset the rebalance portfolio never bought: the portfolio genuinely
    holds it, so it needs a price — but it has no holding row to hang
    the price on.

    Returns `(price_by_code, errors)`.
    """
    universe = ETF_DEFS if universe is None else universe
    prices: dict[str, float] = {}
    errors: list[Tuple[str, str]] = []
    for code in codes:
        if code not in universe:
            errors.append((code, "unknown code (not in ETF_DEFS)"))
            continue
        try:
            price, _as_of = price_provider(code)
            prices[code] = float(price)
        except Exception as ex:
            errors.append((code, str(ex)))
    return prices, errors


__all__ = ["PriceProvider", "value_positions", "value_extra_codes"]
