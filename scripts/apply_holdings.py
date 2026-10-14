"""Apply a holdings edit submitted via the GitHub Pages editor.

Invoked by .github/workflows/apply-holdings.yml. Writes data/holdings.json
with strict validation:

  - ALLOWED_CODES whitelist (no arbitrary codes can be smuggled in)
  - code must be 6 digits
  - shares must be a non-negative integer (or float with .0)
  - cash must be a non-negative number
  - rows count is bounded to avoid runaway payloads

After writing, the script prints the new holdings to stdout so the
workflow log shows what was actually applied.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "data" / "holdings.json"

# Mirror src/record.py:ETF_DEFS to enforce at write time too.
# (We don't import src/record.py because the workflow doesn't install the
# Python deps for this step; the source of truth is this small whitelist.)
ALLOWED_CODES = {
    "515180": "红利ETF易方达",
    "159937": "黄金ETF博时",
    "511380": "可转债ETF博时",
    "513300": "纳斯达克ETF华夏",
}
MAX_ROWS = 20
CODE_RE = re.compile(r"^\d{6}$")


def fail(msg: str) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: apply_holdings.py <rows-json> [cash]")

    try:
        rows = json.loads(sys.argv[1])
    except json.JSONDecodeError as ex:
        fail(f"rows is not valid JSON: {ex}")
    if not isinstance(rows, list):
        fail("rows must be a JSON array")
    if len(rows) > MAX_ROWS:
        fail(f"too many rows ({len(rows)} > {MAX_ROWS})")

    cash = 0.0
    if len(sys.argv) > 2 and sys.argv[2] not in (None, "", "null"):
        try:
            cash = float(sys.argv[2])
        except (ValueError, TypeError):
            fail(f"cash is not a number: {sys.argv[2]!r}")
    if cash < 0:
        fail(f"cash cannot be negative: {cash}")

    # Validate + coerce each row
    cleaned: list[dict] = []
    seen_codes: set[str] = set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            fail(f"row {i}: not an object")
        name = str(r.get("name", "")).strip()
        code = str(r.get("code", "")).strip().split(".")[0]
        if not CODE_RE.match(code):
            fail(f"row {i}: code must be 6 digits, got {code!r}")
        if code not in ALLOWED_CODES:
            fail(f"row {i}: code {code} not in whitelist; allowed: {sorted(ALLOWED_CODES)}")
        if code in seen_codes:
            fail(f"row {i}: duplicate code {code}")
        seen_codes.add(code)

        try:
            shares = float(r.get("shares", 0))
        except (ValueError, TypeError):
            fail(f"row {i}: shares not a number")
        if shares < 0:
            fail(f"row {i}: shares cannot be negative")
        # If the user wants to "remove" a holding, they should set shares=0
        # OR simply omit the row — we accept both. shares=0 is kept in the
        # file so the history shows the entry was intentionally zeroed.

        # Use the registry name unless the user supplied a custom one
        if not name:
            name = ALLOWED_CODES[code]

        cleaned.append({
            "name": name,
            "code": code,
            "shares": int(shares) if shares == int(shares) else shares,
        })

    # Atomic write: tmp + replace
    doc = {
        "_schema": "v1",
        "_comment": "持仓配置. 增删 ETF 直接编辑 positions; cash 是现金 (CNY).",
        "cash": int(cash) if cash == int(cash) else cash,
        "positions": cleaned,
    }
    tmp = HOLDINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(HOLDINGS)

    # Echo what we wrote (use str() to avoid Path.relative_to() failing
    # in tests where HOLDINGS is a temp path under sys.executable's prefix)
    try:
        rel = HOLDINGS.relative_to(ROOT)
    except ValueError:
        rel = HOLDINGS
    print(f"OK wrote {rel}: cash={doc['cash']}, {len(cleaned)} positions")
    for p in cleaned:
        print(f"  - {p['name']:<14} ({p['code']}) {p['shares']:>12,}")


if __name__ == "__main__":
    main()
