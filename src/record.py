"""Daily portfolio snapshot: read holdings.xlsx, fetch prices, evaluate signal,
append to history.json. Designed to run unattended in GitHub Actions.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Tuple

import pandas as pd

from .fetch import latest

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "data" / "holdings.xlsx"
HISTORY = ROOT / "data" / "history.json"

# Code → (sina_symbol, eastmoney_symbol, display_name)
# Verified via Eastmoney FundSearch API (see hongli memory/etf-code-mapping.md)
ETF_DEFS = {
    "515180": ("sh515180", "515180", "红利ETF易方达"),
    "159937": ("sz159937", "159937", "黄金ETF博时"),
    "511380": ("sh511380", "511380", "可转债ETF博时"),
}

TARGET_W = 1.0 / 3
THRESHOLD_PP = float(os.environ.get("THRESHOLD_PP", "3.0"))


def read_holdings(xlsx_path: Path | None = None) -> Tuple[list[dict], float]:
    """Parse holdings.xlsx. Accept '现金' (current) or '资产' (legacy) label.

    Layout (Excel rows are 0-indexed):
        row 0: empty
        row 1: <现金|资产> | <cash amount> | (anything)
        row 2: empty
        row 3: header (名称 / 代码 / 数量)
        row 4..: (名称, 代码, 数量) per row

    Returns:
        (holdings, cash) where holdings = [{"name","code","shares"}, ...]
        cash is the value from the labeled cell (not total capital).
    """
    if xlsx_path is None:
        # Look up at call time so tests can swap record.HOLDINGS = ...
        xlsx_path = HOLDINGS
    df = pd.read_excel(xlsx_path, header=None)
    cash = 0.0
    for i in range(len(df)):
        label = str(df.iloc[i, 0]).strip()
        if label in ("现金", "资产") and not pd.isna(df.iloc[i, 1]):
            try:
                cash = float(df.iloc[i, 1])
                break
            except (ValueError, TypeError):
                continue
    holdings: list[dict] = []
    for i in range(4, len(df)):
        name_v, code_v, shares_v = df.iloc[i, 0], df.iloc[i, 1], df.iloc[i, 2]
        if pd.isna(name_v) or pd.isna(code_v) or pd.isna(shares_v):
            continue
        code = str(code_v).strip().split(".")[0]
        if code.lower() == "code":
            continue
        try:
            shares = float(shares_v)
        except (ValueError, TypeError):
            continue
        holdings.append({"name": str(name_v).strip(), "code": code, "shares": shares})
    return holdings, cash


def fetch_positions(holdings: list[dict]) -> Tuple[list[dict], float, list[Tuple[str, str]]]:
    """Fetch latest price for each holding; return (positions, etf_total, errors)."""
    positions: list[dict] = []
    etf_total = 0.0
    errors: list[Tuple[str, str]] = []
    for h in holdings:
        if h["code"] not in ETF_DEFS:
            errors.append((h["code"], "unknown code (not in ETF_DEFS)"))
            continue
        sina, em, _ = ETF_DEFS[h["code"]]
        try:
            price, pdate = latest(sina, em)
            value = h["shares"] * price
            etf_total += value
            positions.append({
                "name": h["name"], "code": h["code"], "shares": h["shares"],
                "price": round(price, 4), "date": pdate.date().isoformat(),
                "value": round(value, 2),
            })
            # Log price freshness so Action log surfaces stale-data issues
            age_days = (pd.Timestamp.today().normalize() - pdate).days
            print(f"  {h['name']:<14} ({h['code']}) ¥{price:>9.4f}  "
                  f"as_of {pdate.date()} (Δ {age_days:+d}d)", file=sys.stderr)
        except Exception as ex:
            errors.append((h["code"], str(ex)))
    return positions, etf_total, errors


def evaluate(positions: list[dict], cash: float) -> Tuple[float, list[float], bool]:
    """Return (total, weights, triggered)."""
    total = sum(p["value"] for p in positions) + cash
    weights = [p["value"] / total if total > 0 else 0 for p in positions]
    triggered = any(abs(w - TARGET_W) * 100 > THRESHOLD_PP for w in weights)
    return total, weights, triggered


def build_record(positions: list[dict], cash: float, date_str: str) -> dict:
    total, weights, triggered = evaluate(positions, cash)
    etf_total = sum(p["value"] for p in positions)
    return {
        "date": date_str,
        "total": round(total, 2),
        "etf_value": round(etf_total, 2),
        "cash": round(cash, 2),
        "positions": positions,
        "weights": [round(w, 4) for w in weights],
        "target_w": TARGET_W,
        "threshold_pp": THRESHOLD_PP,
        "triggered": triggered,
    }


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"WARN history.json unreadable: {ex}", file=sys.stderr)
        return []


def save_history(history: list[dict]) -> None:
    HISTORY.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_github_output(date_str: str, total: float, triggered: bool) -> None:
    """Write to $GITHUB_OUTPUT if running in Actions (so downstream steps can read)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"date={date_str}\n")
        f.write(f"total={total:.2f}\n")
        f.write(f"trigger={'yes' if triggered else 'no'}\n")


def main() -> None:
    today = datetime.date.today().isoformat()
    holdings, cash = read_holdings()
    if not holdings:
        print("ERROR holdings.xlsx has no positions", file=sys.stderr)
        sys.exit(1)

    positions, etf_total, errors = fetch_positions(holdings)
    for code, msg in errors:
        print(f"WARN {code}: {msg}", file=sys.stderr)
    if not positions:
        print("ERROR all fetches failed", file=sys.stderr)
        sys.exit(1)

    record = build_record(positions, cash, today)

    history = load_history()
    history = [r for r in history if r["date"] != today]
    history.append(record)
    save_history(history)

    status = "TRIGGER" if record["triggered"] else "ok"
    print(f"DATE={today} TOTAL={record['total']:.2f} STATUS={status}")
    print(f"  positions: {len(positions)}, cash: ¥{cash:,.0f}")
    for p, w in zip(positions, record["weights"]):
        delta = (w - record["target_w"]) * 100
        flag = " ⚠" if abs(delta) > record["threshold_pp"] else ""
        print(f"  {p['name']:<14} {w:>6.2%}  Δ={delta:+.2f}pp{flag}")

    write_github_output(today, record["total"], record["triggered"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)