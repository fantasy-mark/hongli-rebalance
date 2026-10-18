"""Backfill history.json positions[] with 前复权 (qfq) close prices.

For each (record, code) pair, fetches the qfq close for that exact date
via Tencent K-line (`web.ifzq.gtimg.cn` with `,qfq`) and overwrites the
stored price + value.  This is the same qfq source the grid already uses
for its 120-day low trigger; values agree with Eastmoney `adjust="qfq"`.

Recomputes etf_value and total per record.

NEVER touches the rebalance trigger state: `triggered`, `weights`,
`target_w`, `threshold_pp` are left exactly as they were.  This is a
display-only rewrite — it does NOT re-evaluate strategy signals.

Idempotent: re-running produces the same output (Tencent qfq is
deterministic for a fixed date).
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.fetch import fetch_kline_history  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_qfq")

HISTORY = ROOT / "data" / "history.json"

# Codes referenced in history.  Add new ones here if the portfolio expands.
CODE_MAP = {
    "515180": "515180",
    "159937": "159937",
    "511380": "511380",
    "513300": "513300",
}


def _fetch_qfq_series(code: str, *, retries: int = 5) -> pd.DataFrame | None:
    """Tencent qfq K-line history for a single code, with retry+backoff."""
    for attempt in range(1, retries + 1):
        try:
            df = fetch_kline_history(code)
            if df is not None and not df.empty:
                return df
            log.warning("Tencent %s empty on attempt %d", code, attempt)
        except Exception as ex:
            log.warning("Tencent %s attempt %d: %s", code, attempt, ex)
        time.sleep(2.0 * attempt)
    return None


def _qfq_close(df: pd.DataFrame, date: str) -> float | None:
    """Pull a single qfq close from a pre-fetched DataFrame."""
    if df is None or df.empty:
        return None
    target = pd.Timestamp(date)
    if target not in df.index:
        # nearest earlier trading day (covers holidays / data lag)
        earlier = df[df.index <= target]
        if earlier.empty:
            return None
        target = earlier.index[-1]
    return float(df.loc[target, "close"])


def backfill(history_path: Path = HISTORY, *, dry_run: bool = False) -> dict:
    history = json.loads(history_path.read_text(encoding="utf-8"))

    # Discover codes referenced in history, then fetch each ONCE.
    needed = sorted({p.get("code", "") for r in history for p in r.get("positions", [])})
    series: dict[str, pd.DataFrame | None] = {}
    for code in needed:
        if code not in CODE_MAP:
            series[code] = None
            continue
        log.info("fetching %s (Tencent qfq kline) …", code)
        series[code] = _fetch_qfq_series(code)

    changes: list[dict] = []
    missing: list[tuple[str, str]] = []

    for rec in history:
        new_etf = 0.0
        for pos in rec.get("positions", []):
            code = pos.get("code", "")
            old_px = pos.get("price")
            new_px = _qfq_close(series.get(code), rec["date"])
            if new_px is None:
                missing.append((rec["date"], code))
                new_etf += float(pos.get("value", 0))
                continue
            shares = float(pos.get("shares", 0))
            new_val = round(shares * new_px, 2)
            if old_px is None or round(float(old_px), 4) != round(new_px, 4):
                changes.append({
                    "date": rec["date"], "code": code, "name": pos.get("name"),
                    "old_px": old_px, "new_px": round(new_px, 4),
                    "old_val": pos.get("value"), "new_val": new_val,
                })
            pos["price"] = round(new_px, 4)
            pos["value"] = new_val
            new_etf += new_val

        old_etf = rec.get("etf_value")
        rec["etf_value"] = round(new_etf, 2)
        cash = float(rec.get("cash", 0))
        old_total = rec.get("total")
        rec["total"] = round(new_etf + cash, 2)
        if old_etf is not None and round(float(old_etf), 2) != round(new_etf, 2):
            changes.append({
                "date": rec["date"], "code": "(etf_value)", "name": "",
                "old_px": None, "new_px": None,
                "old_val": old_etf, "new_val": rec["etf_value"],
            })
        if old_total is not None and round(float(old_total), 2) != round(rec["total"], 2):
            changes.append({
                "date": rec["date"], "code": "(total)", "name": "",
                "old_px": None, "new_px": None,
                "old_val": old_total, "new_val": rec["total"],
            })

    summary = {
        "records": len(history),
        "price_or_value_changes": len(changes),
        "missing_qfq": missing,
    }
    if dry_run:
        return {"summary": summary, "changes": changes, "history": None}
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"summary": summary, "changes": changes, "history": history}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute but don't write")
    args = ap.parse_args()
    out = backfill(dry_run=args.dry_run)
    s = out["summary"]
    print(f"\nrecords: {s['records']}")
    print(f"price/value changes: {s['price_or_value_changes']}")
    if s["missing_qfq"]:
        print(f"MISSING qfq (date, code): {s['missing_qfq']}")
    if out["changes"]:
        print("\n--- changes ---")
        for c in out["changes"]:
            if c["code"].startswith("("):
                print(f"  {c['date']}  {c['code']:>11}  {c['old_val']} → {c['new_val']}")
            else:
                print(
                    f"  {c['date']}  {c['code']}  {c['name']:<10}"
                    f"  px {c['old_px']} → {c['new_px']}"
                    f"  val {c['old_val']} → {c['new_val']}"
                )
    if args.dry_run:
        print("\n(dry-run; history.json NOT written)")
    else:
        print("\nwrote data/history.json")
