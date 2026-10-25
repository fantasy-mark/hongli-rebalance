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

For each op, the math is run via `src/grid.py:manual_op` — the **single**
implementation of the grid rules, so the workflow doesn't need to install
pandas/akshare (src/grid.py's module level only imports os/typing; pandas
is a local import inside run_compound_grid). The take-profit rule: a `tp`
sells only `total_invested / price` shares (recovering exactly the
principal) and merges the remainder into the rebalance portfolio
(`merged_shares`), so realized P&L is 0 on a normal exit. State is read from
data/grid_state.json, mutated in memory, and written back atomically.

⚠️ This is the **only** live path for grid positions — the engine itself
no longer trades (it only reports suggestions).

Validation rules:
  - schema == "grid-ops-v1"
  - len(ops) <= MAX_OPS
  - per-op field rules (see _validate_op)
  - date is ISO yyyy-mm-dd, not in the future (today + 1 day tolerated)
  - op must be in {open, add, tp}
  - code must be in ALLOWED_CODES whitelist
  - price > 0; for open/add: shares > 0; tp: shares is ignored
  - open requires no in_cycle; add/tp require in_cycle=True
    (enforced inside manual_op)
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID_STATE = ROOT / "data" / "grid_state.json"

# 复用 src/grid.py 的唯一实现。src/grid.py 模块级只 import os/typing
# (pandas 是 run_compound_grid 里的局部 import) → 仍然是 stdlib-only,
# workflow 不需要装 pandas/akshare。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.grid import manual_op, split_take_profit  # noqa: E402,F401
# split_take_profit 是 re-export (兼容既有 import), 规则本体在 src/grid.py。

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


def fail(msg: str) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    # ::error:: 让失败原因直接显示在 Actions UI 和失败通知里 ——
    # 否则用户只在页面看到"已提交"却找不到哪里错了。
    print(f"::error::{msg}")
    sys.exit(1)


# -----------------------------------------------------------------------------
# Pure-Python mutator (thin wrapper over src/grid.py:manual_op)
# -----------------------------------------------------------------------------

def inline_manual_op(state: dict, op: str, today: str,
                     price: float, shares: float,
                     note: str = "") -> tuple[dict, float]:
    """Apply a manual grid operation. Returns (new_state, pnl_delta).

    Thin wrapper over `src.grid.manual_op` — **单一实现**, 不再复制规则。
    保留 (state, pnl_delta) 的返回形状, 兼容既有调用方与测试。

    For `tp` the "sell cost only" rule applies (see split_take_profit):
    pnl_delta is 0 on a normal above-cost exit, because only the principal
    is sold and the remainder is merged into the rebalance portfolio
    (`merged_shares`). It is negative only if the user exits BELOW cost,
    where we sell out and book the loss. For `open`/`add` it is 0.

    Raises ValueError on precondition failure.
    """
    new_state = manual_op(state, op=op, today=today, price=price,
                          shares=shares, note=note)
    pnl_delta = 0.0
    if op == "tp":
        pnl_delta = float(new_state["ops"][-1].get("pnl", 0.0))
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
    # 不加尾随换行 —— 与 src/record.py:save_grid_state 同一口径, 否则两条
    # 写盘路径会互相制造「只有一行之差」的假 diff。
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2,
                              default=str),
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
                "merged_shares": 0.0,  # 归并份额, 止盈时并入再平衡持仓
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
