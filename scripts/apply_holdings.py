"""Apply a trade-ledger edit submitted via the GitHub Pages editor.

Invoked by .github/workflows/apply-holdings.yml. Receives a single JSON
payload on argv[1] with shape:

    {
      "schema": "trades-v1",
      "trades": [
        {"date": "2026-10-17", "side": "buy",   "code": "515180",
         "shares": 1000, "price": 1.40, "note": "..."},
        {"date": "2026-10-17", "side": "deposit", "amount": 5000, "note": "..."}
      ]
    }

Validates and appends the new trades to `data/trades.json`, then rebuilds
`data/holdings.json` (schema v2) by replaying the full ledger. The script
intentionally does NOT import src/record.py (the workflow doesn't install
its deps); all replay logic is inlined as pure stdlib.

Validation rules:
  - schema == "trades-v1"
  - len(trades) ≤ MAX_ROWS
  - per-trade field rules per `data/trades.json` schema (see README)
  - date is ISO yyyy-mm-dd, not in the future (today + 1 day tolerated)
  - sell never exceeds current held shares
  - cash after replay ≥ 0 (no negative balance)
  - all buy/sell codes are in ALLOWED_CODES whitelist
  - id is auto-assigned server-side as `<date>-<side>-<seq>` (zero-padded)

Old `{rows, cash}` payload format is REJECTED with a clear message; the
editor has been upgraded and the user must refresh their browser.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADES = ROOT / "data" / "trades.json"
HOLDINGS = ROOT / "data" / "holdings.json"

# Mirror src/record.py:ETF_DEFS — whitelist of tradeable codes.
# Source of truth for "what's tradeable". We don't import src/record.py
# because the workflow doesn't install the Python deps for this step.
ALLOWED_CODES = {
    "515180": "红利ETF易方达",
    "159937": "黄金ETF博时",
    "511380": "可转债ETF博时",
    "513300": "纳斯达克ETF华夏",
}
NAME_LOOKUP = ALLOWED_CODES  # 6-digit code → Chinese display name

MAX_ROWS = 20
CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_SIDES = {"buy", "sell", "deposit", "withdraw"}


def fail(msg: str) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    sys.exit(1)


# -----------------------------------------------------------------------------
# Pure-Python replay helpers (intentionally duplicated from src/record.py so
# this script has no external imports beyond stdlib).
# -----------------------------------------------------------------------------

def inline_apply(state: dict, t: dict) -> None:
    """In-place: state = {"cash": float, "positions": {code: shares}}."""
    side = t["side"]
    if side == "deposit":
        state["cash"] += float(t["amount"])
    elif side == "withdraw":
        state["cash"] -= float(t["amount"])
    elif side == "buy":
        code = str(t["code"])
        sh = float(t["shares"])
        px = float(t["price"])
        fee = float(t.get("fee", 0.0))
        state["positions"][code] = state["positions"].get(code, 0.0) + sh
        state["cash"] -= sh * px + fee
    elif side == "sell":
        code = str(t["code"])
        sh = float(t["shares"])
        px = float(t["price"])
        fee = float(t.get("fee", 0.0))
        held = state["positions"].get(code, 0.0)
        if held < sh - 1e-9:
            fail(f"oversell {code}: held={held:.4f}, sell={sh:.4f}")
        state["positions"][code] = held - sh
        state["cash"] += sh * px - fee
    else:
        fail(f"unknown side: {side!r}")
    state["cash"] = round(state["cash"], 2)
    for c in list(state["positions"].keys()):
        v = round(state["positions"][c], 2)
        if v <= 0:
            del state["positions"][c]
        else:
            state["positions"][c] = v


def inline_rebuild(trades: list[dict]) -> tuple[dict, float]:
    """Replay `trades`; return (state, cash). state['positions'] is {code: shares}."""
    state: dict = {"cash": 0.0, "positions": {}}
    indexed = list(enumerate(trades))
    indexed.sort(key=lambda kv: (kv[1].get("date", ""), kv[0]))
    for _, t in indexed:
        inline_apply(state, t)
    return state, state["cash"]


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------

def _coerce_number(v, name: str, allow_zero: bool = False) -> float:
    try:
        n = float(v)
    except (ValueError, TypeError):
        fail(f"{name} is not a number: {v!r}")
    if n < 0 or (not allow_zero and n <= 0):
        fail(f"{name} must be > 0, got {n}")
    return n


def _validate_trade(t: dict, idx: int, today: str) -> dict:
    if not isinstance(t, dict):
        fail(f"trade {idx}: not an object")
    side = str(t.get("side", "")).strip()
    if side not in VALID_SIDES:
        fail(f"trade {idx}: side must be one of {sorted(VALID_SIDES)}, got {side!r}")
    date = str(t.get("date", "")).strip()
    if not DATE_RE.match(date):
        fail(f"trade {idx}: date must be YYYY-MM-DD, got {date!r}")
    if date > today:
        # Tolerate date == today+1d (timezone buffer)
        max_date = (datetime.date.fromisoformat(today) +
                    datetime.timedelta(days=1)).isoformat()
        if date > max_date:
            fail(f"trade {idx}: date {date} is in the future (today={today})")
    note = str(t.get("note", "") or "").strip()

    out: dict = {"side": side, "date": date, "note": note}
    if side in ("buy", "sell"):
        code = str(t.get("code", "")).strip().split(".")[0]
        if not CODE_RE.match(code):
            fail(f"trade {idx}: code must be 6 digits, got {code!r}")
        if code not in ALLOWED_CODES:
            fail(f"trade {idx}: code {code} not in whitelist; allowed: "
                 f"{sorted(ALLOWED_CODES)}")
        out["code"] = code
        out["shares"] = _coerce_number(t.get("shares"), f"trade {idx}.shares")
        out["price"] = _coerce_number(t.get("price"), f"trade {idx}.price")
        fee = t.get("fee", 0.0)
        if fee is None or fee == "":
            fee = 0.0
        out["fee"] = _coerce_number(fee, f"trade {idx}.fee", allow_zero=True)
    else:  # deposit / withdraw
        if "code" in t and str(t.get("code", "")).strip():
            fail(f"trade {idx}: {side} must not have a code")
        if "shares" in t and str(t.get("shares", "")).strip():
            fail(f"trade {idx}: {side} must not have shares")
        if "price" in t and str(t.get("price", "")).strip():
            fail(f"trade {idx}: {side} must not have a price")
        out["amount"] = _coerce_number(t.get("amount"),
                                       f"trade {idx}.amount")
    return out


def _assign_ids(new_trades: list[dict], existing: list[dict]) -> list[dict]:
    """Server-side id = <date>-<side>-<seq>; seq counts same-(date, side)
    entries in existing+already-assigned new trades."""
    counts: dict[tuple[str, str], int] = {}
    for t in existing:
        key = (t.get("date", ""), t.get("side", ""))
        if key[0] and key[1]:
            counts[key] = max(counts.get(key, 0),
                              _parse_seq(t.get("id", "")))
    for t in new_trades:
        key = (t["date"], t["side"])
        counts[key] = counts.get(key, 0) + 1
        t["id"] = f"{key[0]}-{key[1]}-{counts[key]:03d}"
    return new_trades


def _parse_seq(id_str: str) -> int:
    """Extract trailing seq number from an existing id like '2026-10-12-buy-002'.
    Returns 0 if the id is malformed (so the next id is at least 001)."""
    m = re.search(r"-(\d+)$", id_str)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------

def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: apply_holdings.py <payload-json>")

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as ex:
        fail(f"payload is not valid JSON: {ex}")
    if not isinstance(payload, dict):
        fail(f"payload must be a JSON object, got {type(payload).__name__}")

    # Reject the legacy {rows, cash} format up front.
    if "rows" in payload and "trades" not in payload:
        fail("editor 已升级为交易表单. 请刷新页面 (Ctrl+Shift+R) 重新加载 app.js.")

    schema = payload.get("schema", "")
    if schema != "trades-v1":
        fail(f"schema mismatch: only 'trades-v1' accepted, got {schema!r}")

    raw_trades = payload.get("trades", [])
    if not isinstance(raw_trades, list):
        fail(f"trades must be a JSON array, got {type(raw_trades).__name__}")
    if len(raw_trades) > MAX_ROWS:
        fail(f"too many trades ({len(raw_trades)} > {MAX_ROWS})")
    if not raw_trades:
        fail("no trades to apply")

    today = datetime.date.today().isoformat()
    cleaned: list[dict] = []
    for i, t in enumerate(raw_trades):
        cleaned.append(_validate_trade(t, i, today))

    # Read existing trades (or empty if missing)
    if TRADES.exists():
        try:
            existing_doc = json.loads(TRADES.read_text(encoding="utf-8"))
            existing: list[dict] = list(existing_doc.get("trades", []))
        except Exception as ex:
            fail(f"existing trades.json unreadable: {ex}")
    else:
        existing = []

    # Assign server-side ids BEFORE the validation that walks the merged
    # ledger (oversell check). We need ids to compute per-(date, side) seq.
    cleaned = _assign_ids(cleaned, existing)

    # Replay merged ledger: oversell check fires inside inline_apply.
    merged = existing + cleaned
    state, cash = inline_rebuild(merged)

    # Reject negative cash (per user requirement).
    if cash < 0:
        fail(f"cash would go negative after this batch: "
             f"current={state['cash']:.2f}")

    # Persist trades.json
    _atomic_write(TRADES, {
        "_schema": "trades-v1",
        "_comment": ("Append-only trade ledger. Source of truth for cash "
                     "and positions[].shares. Do not reorder; corrections "
                     "are new entries."),
        "trades": merged,
    })

    # Persist holdings.json (schema v2 — derived cache)
    positions: list[dict] = []
    for code, sh in state["positions"].items():
        positions.append({
            "name": NAME_LOOKUP.get(code, code),
            "code": code,
            "shares": int(sh) if sh == int(sh) else sh,
        })
    _atomic_write(HOLDINGS, {
        "_schema": "v2",
        "_comment": ("REBUILT from data/trades.json. 编辑持仓请用页面"
                     "交易表单 (会同时更新 trades.json); 直接改这个文件"
                     "会被下一次 apply-holdings 覆盖."),
        "cash": state["cash"],
        "positions": positions,
    })

    # Echo
    try:
        rel_t = TRADES.relative_to(ROOT)
        rel_h = HOLDINGS.relative_to(ROOT)
    except ValueError:
        rel_t, rel_h = TRADES, HOLDINGS
    print(f"OK wrote {rel_t}: {len(merged)} trades (+{len(cleaned)} new)")
    print(f"OK wrote {rel_h}: cash=CNY {state['cash']:.2f}, {len(positions)} positions")
    for p in positions:
        print(f"  - {p['name']:<14} ({p['code']}) {p['shares']:>12,}")
    # Show the new trades that were just appended
    print("\nNew trades appended:")
    for t in cleaned:
        if t["side"] in ("buy", "sell"):
            print(f"  - {t['id']}  {t['side']:<7} {t['code']}  "
                  f"{t['shares']:>10} @ CNY {t['price']:.4f}  "
                  f"date={t['date']}  note={t.get('note','')}")
        else:
            print(f"  - {t['id']}  {t['side']:<7} amount=CNY {t['amount']:>10.2f}  "
                  f"date={t['date']}  note={t.get('note','')}")


if __name__ == "__main__":
    main()
