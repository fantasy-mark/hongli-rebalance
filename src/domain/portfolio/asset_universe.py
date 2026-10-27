"""Asset universe — the registry of ETFs this project tracks (BC-D boundary).

**Why this is its own module** (2026-10-26, DDD refactor):
`data/holdings.json` used to serve three roles at once — the trade-derived
position cache, the fallback source of positions, *and* the de-facto list
of "which assets do we track". The third role belongs to neither: the
universe is a **registry**, not a holding. It is now stated explicitly
here, and every consumer (daily snapshot, chart renderer, grid engine)
derives its asset list from this module instead of reverse-engineering it
from a holdings file.

**Why it's a class** (2026-10-27, Phase 4): a module full of module-level
constants is not a domain concept — you can't pass "the universe" around,
you can only reach for the globals. `AssetUniverse` makes it a **value**:
one instance, passed explicitly, with the derived views (`names`,
`qdii_codes`, `name_to_code`) computed from a single source instead of
being four lists that can drift apart.

The module-level names (`ETF_DEFS` / `ASSET_NAMES` / `NAME_TO_CODE` /
`QDII_CODES`) are kept as **aliases of the singleton** — every existing
consumer keeps working, and they all still point at the same data.

Code → (sina_symbol, eastmoney_symbol, display_name, is_qdii).
QDII flags drive the premium-rate fetch & alert in the presentation layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Mapping

#: Code → (sina_symbol, eastmoney_symbol, display_name, is_qdii)
_DEFS: dict[str, tuple[str, str, str, bool]] = {
    "515180": ("sh515180", "515180", "红利ETF易方达", False),
    "159937": ("sz159937", "159937", "黄金ETF博时", False),
    "511380": ("sh511380", "511380", "可转债ETF博时", False),
    "513300": ("sh513300", "513300", "纳斯达克ETF华夏", True),   # QDII
}


@dataclass(frozen=True)
class AssetUniverse:
    """The registry of assets this project tracks.

    Immutable: `defs` is the raw registry (code → 4-tuple); every derived
    view is computed from it, so there is exactly **one** place where an
    asset's identity (name / quote symbols / QDII flag) is written down.

    Attributes:
        defs: code → (sina_symbol, eastmoney_symbol, display_name, is_qdii)
    """

    defs: Mapping[str, tuple[str, str, str, bool]]

    # -- derived views ----------------------------------------------------
    @property
    def codes(self) -> list[str]:
        """Every tracked code, in registry order."""
        return list(self.defs.keys())

    @property
    def names(self) -> list[str]:
        """Every tracked display name, in registry order."""
        return [v[2] for v in self.defs.values()]

    @property
    def name_to_code(self) -> dict[str, str]:
        """Display name → code (the grid's state is keyed by name)."""
        return {v[2]: code for code, v in self.defs.items()}

    @property
    def qdii_codes(self) -> set[str]:
        """Codes flagged QDII — they get a premium-rate fetch & alert."""
        return {code for code, v in self.defs.items() if v[3]}

    # -- queries ----------------------------------------------------------
    def display_name(self, code: str) -> str:
        """Human-readable name for a code; falls back to the code itself."""
        entry = self.defs.get(str(code))
        return entry[2] if entry else str(code)

    def contains(self, code: str) -> bool:
        return str(code) in self.defs

    def __contains__(self, code: object) -> bool:
        return str(code) in self.defs

    def __len__(self) -> int:
        return len(self.defs)

    def __iter__(self) -> Iterator[str]:
        return iter(self.defs)


#: The one universe this project tracks.
UNIVERSE = AssetUniverse(_DEFS)

# --- 模块级别名 (指向同一个单例的数据) -------------------------------------
# 既有调用方 (`daily_snapshot` / `valuation` / `chart_renderer` /
# `quote_providers` / `ledger`) 全都继续用这些名字 —— 不用跟着改。
ETF_DEFS: dict[str, tuple[str, str, str, bool]] = UNIVERSE.defs
ASSET_NAMES: list[str] = UNIVERSE.names
NAME_TO_CODE: dict[str, str] = UNIVERSE.name_to_code
QDII_CODES: set[str] = UNIVERSE.qdii_codes


def display_name(code: str) -> str:
    """Human-readable name for a code; falls back to the code itself."""
    return UNIVERSE.display_name(code)


__all__ = [
    "AssetUniverse", "UNIVERSE",
    "ETF_DEFS", "ASSET_NAMES", "NAME_TO_CODE", "QDII_CODES",
    "display_name",
]
