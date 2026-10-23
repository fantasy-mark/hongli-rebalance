"""Apply a manual grid operation submitted via the GitHub Pages editor.

Invoked by .github/workflows/apply-grid.yml. Receives a single JSON
payload on argv[1] with shape:

    {
      "schema": "grid-ops-v1",
      "ops": [
        {"date": "2026-10-17", "code": "515180", "op": "add",
         "price": 1.30, "shares": 1000, "note": "manual add"},
        {"date": "2026-10-17", "code": "515180", "op": "tp",
         "price": 1.50, "shares": 0, "note": "take profit"}
      ]
    }

For each op, the math is run inlined (mirroring scripts/apply_holdings.py)
so the workflow doesn't need to install pandas/akshare. The mutator is
the same as src/grid.py:manual_op, INCLUDING the take-profit rule: a `tp`
sells only `total_invested / price` shares (recovering exactly the
principal) and transfers the remainder to permanently-held `free_shares`,
so realized P&L is 0 on a normal exit. State is read from
data/grid_state.json, mutated in memory, and written back atomically.

Validation rules:
  - schema == "grid-ops-v1"
  - len(ops) <= MAX_OPS
  - per-op field rules (see _validate_op)
  - date is ISO yyyy-mm-dd, not in the future (today + 1 day tolerated)
  - op must be in {open, add, tp}
  - code must be in ALLOWED_CODES whitelist
  - price > 0; for open/add: shares > 0; tp: shares is ignored
  - open requires no in_cycle; add/tp require in_cycle=True
    (enforced inside inline_manual_op)
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID_STATE = ROOT / "data" / "grid_state.json"

# Mirror scripts/apply_holdings.py — same whitelist as src/record.py:ETF_DEFS.
ALLOWED_CODES = {
    "515180": "红利ETF易方达",
    "159937": "黄金ETF博时",
    "511380": "可转债ETF博时",
    "513300": "纳斯达克ETF华夏",
}
NAME_LOOKUP = ALLOWED_CODES  # code -> Chinese display name (used as state key)
CODE_TO_NAME = ALLOWED_CODES

MAX_OPS = 20
CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_OPS = {"open", "add", "tp"}
# Rolling-window length must match src/grid.py:GRID_LOOKBACK (120). The
# apply_grid script doesn't import src/grid.py, so it duplicates the constant.
GRID_LOOKBACK = 120


def fail(msg: str) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    sys.exit(1)


# -----------------------------------------------------------------------------
# Pure-Python mutator (intentionally duplicated from src/grid.py:manual_op
# so this script has no external imports beyond stdlib).
# -----------------------------------------------------------------------------

def split_take_profit(total_shares: float, total_invested: float,
                      price: float) -> dict:
    """MIRROR of src/grid.py:split_take_profit — keep the two in sync.

    This script cannot import src/grid.py (stdlib only, so the workflow
    needs no pandas/akshare), so the rule is duplicated deliberately:
    sell `total_invested / price` shares to recover exactly the principal,
    transfer the remainder to permanently-held zero-cost shares. Realized
    P&L is therefore 0 unless the exit is below cost, in which case we
    sell everything and book the loss (reachable only via a manual tp —
    the auto trigger price is always above avg cost).
    """
    if not price > 0:
        raise ValueError(f"price must be > 0, got {price}")
    if total_shares <= 0:
        return {"shares_sold": 0.0, "proceeds": 0.0, "free_gained": 0.0, "pnl": 0.0}
    want = total_invested / price
    if want >= total_shares:
        proceeds = total_shares * price
        return {"shares_sold": total_shares, "proceeds": proceeds,
                "free_gained": 0.0, "pnl": proceeds - total_invested}
    return {"shares_sold": want, "proceeds": total_invested,
            "free_gained": total_shares - want, "pnl": 0.0}


def inline_manual_op(state: dict, op: str, today: str,
                     price: float, shares: float,
                     note: str = "") -> tuple[dict, float]:
    """Apply a manual grid operation. Returns (new_state, pnl_delta).

    For `tp` the "sell cost only" rule applies (see split_take_profit):
    pnl_delta is 0 on a normal above-cost exit, because only the principal
    is sold and the remainder moves to permanently-held `free_shares`. It
    is negative only if the user exits BELOW cost, where we sell out and
    book the loss. For `open`/`add` it is 0.

    Raises ValueError on precondition failure.
    """
    if op not in VALID_OPS:
        raise ValueError(f"unknown op: {op!r}")
    if not (price > 0):
        raise ValueError(f"price must be > 0, got {price}")
    if op in ("open", "add") and not (shares > 0):
        raise ValueError(f"{op} requires shares > 0, got {shares}")

    in_cycle = bool(state.get("in_cycle", False))
    if op == "open" and in_cycle:
        raise ValueError("open requires no open cycle (use tp to close first)")
    if op in ("add", "tp") and not in_cycle:
        raise ValueError(f"{op} requires an open cycle")

    new_state = {
        "in_cycle": in_cycle,
        "entries": list(state.get("entries", [])),
        "last_prices": list(state.get("last_prices", [])),
        "cum_realized": float(state.get("cum_realized", 0.0)),
        "ops": list(state.get("ops", [])),
        "free_shares": float(state.get("free_shares", 0.0)),
    }
    pnl_delta = 0.0

    if op in ("open", "add"):
        new_state["entries"].append({
            "date": today, "price": float(price), "shares": float(shares),
        })
        new_state["in_cycle"] = True
        if op == "open":
            new_state["last_prices"].append(float(price))
            if len(new_state["last_prices"]) > GRID_LOOKBACK:
                new_state["last_prices"] = new_state["last_prices"][-GRID_LOOKBACK:]
        # Append to ops log so the page's "复合网格 操作记录" table reflects
        # manual ops. note = user-supplied string (may be empty).
        new_state["ops"].append({
            "date": today, "op": op, "source": "manual",
            "price": float(price), "shares": float(shares),
            "note": note,
        })
    else:  # tp
        entries = new_state["entries"]
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        split = split_take_profit(total_shares, total_invested, float(price))
        new_state["free_shares"] += split["free_gained"]
        # Capture per-event exit stats BEFORE clearing `entries`.
        new_state["ops"].append({
            "date": today, "op": "tp", "source": "manual",
            "price": float(price), "shares": float(split["shares_sold"]),
            "note": note,
            "pnl": float(split["pnl"]),
            "num_buys_exited": len(entries),
            # "出场"的全部份额 = 卖出的 + 转成零成本的 (== 原持仓)
            "total_shares_exited": float(split["shares_sold"]
                                         + split["free_gained"]),
            "shares_to_free": float(split["free_gained"]),
        })
        new_state["cum_realized"] += split["pnl"]
        new_state["entries"] = []
        new_state["in_cycle"] = False
        pnl_delta = split["pnl"]

    return new_state, pnl_delta


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


def _validate_op(op: dict, idx: int, today: str) -> dict:
    if not isinstance(op, dict):
        fail(f"op {idx}: not an object")
    kind = str(op.get("op", "")).strip()
    if kind not in VALID_OPS:
        fail(f"op {idx}: op must be one of {sorted(VALID_OPS)}, got {kind!r}")
    date = str(op.get("date", "")).strip()
    if not DATE_RE.match(date):
        fail(f"op {idx}: date must be YYYY-MM-DD, got {date!r}")
    if date > today:
        max_date = (datetime.date.fromisoformat(today) +
                    datetime.timedelta(days=1)).isoformat()
        if date > max_date:
            fail(f"op {idx}: date {date} is in the future (today={today})")
    code = str(op.get("code", "")).strip().split(".")[0]
    if not CODE_RE.match(code):
        fail(f"op {idx}: code must be 6 digits, got {code!r}")
    if code not in ALLOWED_CODES:
        fail(f"op {idx}: code {code} not in whitelist; allowed: "
             f"{sorted(ALLOWED_CODES)}")
    price = _coerce_number(op.get("price"), f"op {idx}.price")
    note = str(op.get("note", "") or "").strip()
    out = {"op": kind, "date": date, "code": code, "price": price, "note": note}
    if kind in ("open", "add"):
        out["shares"] = _coerce_number(op.get("shares"), f"op {idx}.shares")
    else:  # tp
        # tp shares is ignored; accept any value
        sh_raw = op.get("shares", 0.0)
        try:
            out["shares"] = float(sh_raw) if sh_raw not in (None, "") else 0.0
        except (ValueError, TypeError):
            fail(f"op {idx}: shares must be a number, got {sh_raw!r}")
    return out


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------

def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                              default=str) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: apply_grid.py <payload-json>")

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as ex:
        fail(f"payload is not valid JSON: {ex}")
    if not isinstance(payload, dict):
        fail(f"payload must be a JSON object, got {type(payload).__name__}")

    schema = payload.get("schema", "")
    if schema != "grid-ops-v1":
        fail(f"schema mismatch: only 'grid-ops-v1' accepted, got {schema!r}")

    raw_ops = payload.get("ops", [])
    if not isinstance(raw_ops, list):
        fail(f"ops must be a JSON array, got {type(raw_ops).__name__}")
    if len(raw_ops) > MAX_OPS:
        fail(f"too many ops ({len(raw_ops)} > {MAX_OPS})")
    if not raw_ops:
        fail("no ops to apply")

    today = datetime.date.today().isoformat()
    cleaned: list[dict] = []
    for i, o in enumerate(raw_ops):
        cleaned.append(_validate_op(o, i, today))

    # Read existing grid_state.json
    if GRID_STATE.exists():
        try:
            state = json.loads(GRID_STATE.read_text(encoding="utf-8"))
        except Exception as ex:
            fail(f"existing grid_state.json unreadable: {ex}")
    else:
        state = {}
    if not isinstance(state, dict):
        fail(f"grid_state.json root must be an object, got {type(state).__name__}")

    # Apply each op in order, mutating the asset state for its 6-digit code.
    # Note: multiple ops in one batch for the same asset are applied
    # sequentially — the second op sees the state left by the first.
    summary: list[dict] = []
    for o in cleaned:
        name = CODE_TO_NAME[o["code"]]
        # Fresh asset if missing
        if name not in state or not isinstance(state[name], dict):
            state[name] = {
                "in_cycle": False, "entries": [],
                "last_prices": [], "cum_realized": 0.0,
                "ops": [],
                "free_shares": 0.0,   # 零成本份额, 止盈时转入, 永不再卖
            }
        # Compat: legacy last_120d_min → seed last_prices with it
        if not state[name].get("last_prices") and state[name].get("last_120d_min") is not None:
            state[name]["last_prices"] = [float(state[name]["last_120d_min"])]

        before_in_cycle = bool(state[name].get("in_cycle", False))
        before_cum = float(state[name].get("cum_realized", 0.0))
        before_entries = len(state[name].get("entries", []))

        try:
            new_asset_state, pnl_delta = inline_manual_op(
                state[name], op=o["op"], today=o["date"],
                price=o["price"], shares=o.get("shares", 0.0),
                note=o["note"],
            )
        except ValueError as ex:
            fail(f"op {len(summary)} ({o['op']} {o['code']}): {ex}")

        state[name] = new_asset_state
        summary.append({
            "code": o["code"], "name": name, "op": o["op"],
            "date": o["date"], "price": o["price"], "shares": o.get("shares", 0.0),
            "note": o["note"],
            "before_in_cycle": before_in_cycle,
            "after_in_cycle": bool(new_asset_state["in_cycle"]),
            "before_cum_realized": before_cum,
            "after_cum_realized": float(new_asset_state["cum_realized"]),
            "num_entries_after": len(new_asset_state["entries"]),
            "pnl_delta": pnl_delta,
        })

    # Persist
    _atomic_write(GRID_STATE, state)

    # Echo
    try:
        rel = GRID_STATE.relative_to(ROOT)
    except ValueError:
        rel = GRID_STATE
    print(f"OK wrote {rel}: {len(cleaned)} op(s) applied across {len(summary)} asset(s)")
    for s in summary:
        op_kind = s["op"]
        if op_kind == "tp":
            print(f"  - [{s['date']}] {s['op']:<5} {s['code']} {s['name']} @ "
                  f"CNY {s['price']:.4f}  cum_realized "
                  f"{s['before_cum_realized']:.2f} -> {s['after_cum_realized']:.2f} "
                  f"(P&L: CNY {s['pnl_delta']:+.2f})  note={s['note']!r}")
        else:
            print(f"  - [{s['date']}] {s['op']:<5} {s['code']} {s['name']} @ "
                  f"CNY {s['price']:.4f} x {s['shares']:.0f}  "
                  f"in_cycle={s['after_in_cycle']}  entries={s['num_entries_after']}  "
                  f"note={s['note']!r}")


if __name__ == "__main__":
    main()
