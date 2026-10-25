"""Asset universe — the registry of ETFs this project tracks (BC-D boundary).

**Why this is its own module** (2026-10-26, DDD refactor):
`data/holdings.json` used to serve three roles at once — the trade-derived
position cache, the fallback source of positions, *and* the de-facto list
of "which assets do we track". The third role belongs to neither: the
universe is a **registry**, not a holding. It is now stated explicitly
here, and every consumer (daily snapshot, chart renderer, grid engine)
derives its asset list from this module instead of reverse-engineering it
from a holdings file.

Code → (sina_symbol, eastmoney_symbol, display_name, is_qdii).
QDII flags drive the premium-rate fetch & alert in the presentation layer.
"""

# Code → (sina_symbol, eastmoney_symbol, display_name, is_qdii)
ETF_DEFS: dict[str, tuple[str, str, str, bool]] = {
    "515180": ("sh515180", "515180", "红利ETF易方达", False),
    "159937": ("sz159937", "159937", "黄金ETF博时", False),
    "511380": ("sh511380", "511380", "可转债ETF博时", False),
    "513300": ("sh513300", "513300", "纳斯达克ETF华夏", True),   # QDII
}

ASSET_NAMES: list[str] = [v[2] for v in ETF_DEFS.values()]
NAME_TO_CODE: dict[str, str] = {v[2]: code for code, v in ETF_DEFS.items()}
QDII_CODES: set[str] = {code for code, v in ETF_DEFS.items() if v[3]}


def display_name(code: str) -> str:
    """Human-readable name for a code; falls back to the code itself."""
    entry = ETF_DEFS.get(str(code))
    return entry[2] if entry else str(code)
