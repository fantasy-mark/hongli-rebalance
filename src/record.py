"""Daily double-strategy snapshot: 3pp rebalance + compound grid.

Flow (each Action run):
  1. Read holdings.xlsx (positions + cash)
  2. Fetch latest price for each ETF (3-tier: realtime → spot → history)
  3. Evaluate 3pp rebalance: any |weight - 1/3| > 3pp → trigger
  4. Build a `pool` series (today's 3pp total appended to history.totals)
     and compute recent_min = min(pool[-757:])  →  drives grid layer size
  5. For each asset, advance grid state by one day (JSON-persistent)
  6. Save: data/history.json (record) + data/grid_state.json (per-asset)
  7. Render data/grid_chart.png  (consumed by GitHub Pages <img>)
  8. Emit $GITHUB_OUTPUT for downstream steps
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

from . import grid as grid_mod
from .fetch import latest, fetch_qdii_premium, fetch_kline_history

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "data" / "holdings.json"   # v2: plain JSON (was holdings.xlsx)
HISTORY = ROOT / "data" / "history.json"
GRID_STATE = ROOT / "data" / "grid_state.json"
GRID_CHART = ROOT / "data" / "grid_chart.png"

# Code → (sina_symbol, eastmoney_symbol, display_name, is_qdii)
# QDII flags drive the premium-rate fetch & alert in notify.py.
ETF_DEFS = {
    "515180": ("sh515180", "515180", "红利ETF易方达", False),
    "159937": ("sz159937", "159937", "黄金ETF博时", False),
    "511380": ("sh511380", "511380", "可转债ETF博时", False),
    "513300": ("sh513300", "513300", "纳斯达克ETF华夏", True),   # QDII
}
ASSET_NAMES = [v[2] for v in ETF_DEFS.values()]
QDII_CODES = {code for code, v in ETF_DEFS.items() if v[3]}

# 3pp parameters
TARGET_W = 1.0 / 3
THRESHOLD_PP = float(os.environ.get("THRESHOLD_PP", "3.0"))


# -----------------------------------------------------------------------------
# Holdings / pricing (preserved from v1)
# -----------------------------------------------------------------------------

def read_holdings(json_path: Path | None = None) -> Tuple[list[dict], float]:
    """Parse holdings.json.

    Schema (data/holdings.json):
        {
            "_schema": "v1",          # optional, ignored
            "_comment": "...",        # optional, ignored
            "cash": <number>,
            "positions": [
                {"name": "...", "code": "xxxxxx", "shares": <number>},
                ...
            ]
        }

    For backwards compatibility, falls back to the legacy
    data/holdings.xlsx (5-row header layout) if holdings.json is missing.
    """
    if json_path is None:
        json_path = HOLDINGS
    if not json_path.exists():
        # Backwards-compat: fall back to xlsx
        legacy = json_path.with_suffix(".xlsx")
        if legacy.exists():
            return _read_holdings_legacy_xlsx(legacy)
        return [], 0.0
    try:
        with open(json_path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as ex:
        print(f"WARN holdings.json unreadable: {ex}", file=sys.stderr)
        return [], 0.0
    cash = float(doc.get("cash", 0) or 0)
    holdings: list[dict] = []
    for p in doc.get("positions", []):
        if not isinstance(p, dict): continue
        name = str(p.get("name", "")).strip()
        code = str(p.get("code", "")).strip().split(".")[0]
        if not name or not code: continue
        try:
            shares = float(p.get("shares", 0))
        except (ValueError, TypeError):
            continue
        holdings.append({"name": name, "code": code, "shares": shares})
    return holdings, cash


def _read_holdings_legacy_xlsx(xlsx_path: Path) -> Tuple[list[dict], float]:
    """Legacy v1: read 5-row-header xlsx. Kept for backwards compat."""
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
        sina, em, _, _ = ETF_DEFS[h["code"]]
        try:
            price, pdate = latest(sina, em)
            value = h["shares"] * price
            etf_total += value
            positions.append({
                "name": h["name"], "code": h["code"], "shares": h["shares"],
                "price": round(price, 4), "date": pdate.date().isoformat(),
                "value": round(value, 2),
            })
            age_days = (pd.Timestamp.today().normalize() - pdate).days
            print(f"  {h['name']:<14} ({h['code']}) ¥{price:>9.4f}  "
                  f"as_of {pdate.date()} (Δ {age_days:+d}d)", file=sys.stderr)
        except Exception as ex:
            errors.append((h["code"], str(ex)))
    return positions, etf_total, errors


# -----------------------------------------------------------------------------
# 3pp evaluation
# -----------------------------------------------------------------------------

def evaluate(positions: list[dict], cash: float) -> Tuple[float, list[float], bool, list[dict]]:
    """Return (total, weights, triggered, plan). plan is empty when not triggered.

    TARGET_W is dynamic: 1/len(positions) — works for 3, 4, 5 asset configs.
    """
    total = sum(p["value"] for p in positions) + cash
    n = len(positions)
    target_w = 1.0 / n if n > 0 else 0.0
    weights = [p["value"] / total if total > 0 else 0 for p in positions]
    triggered = any(abs(w - target_w) * 100 > THRESHOLD_PP for w in weights)
    plan: list[dict] = []
    if triggered and total > 0:
        for p, w in zip(positions, weights):
            target_value = total * target_w
            plan.append({
                "name": p["name"],
                "code": p["code"],
                "current_value": round(p["value"], 2),
                "target_value": round(target_value, 2),
                "delta_value": round(target_value - p["value"], 2),
            })
    return total, weights, triggered, plan


# -----------------------------------------------------------------------------
# Grid state (JSON-persistent)
# -----------------------------------------------------------------------------

def load_grid_state() -> dict:
    """Load grid state from JSON. Returns {asset_name: asset_state} or {}."""
    if not GRID_STATE.exists():
        return {}
    try:
        return json.loads(GRID_STATE.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"WARN grid_state.json unreadable: {ex}", file=sys.stderr)
        return {}


def save_grid_state(state: dict) -> None:
    GRID_STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# -----------------------------------------------------------------------------
# History (JSON-persistent)
# -----------------------------------------------------------------------------

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
        json.dumps(history, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


# -----------------------------------------------------------------------------
# Build & emit
# -----------------------------------------------------------------------------

def write_github_output(date_str: str, total: float, triggered: bool) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"date={date_str}\n")
        f.write(f"total={total:.2f}\n")
        f.write(f"trigger={'yes' if triggered else 'no'}\n")


def build_record(today: str, positions: list[dict], cash: float,
                 total: float, weights: list[float],
                 triggered_3pp: bool, plan: list[dict],
                 grid_signals: list[dict], grid_state: dict,
                 qdii_premiums: list[dict] | None = None) -> dict:
    etf_total = sum(p["value"] for p in positions)
    n = len(positions)
    target_w = 1.0 / n if n > 0 else 0.0
    return {
        "date": today,
        "total": round(total, 2),
        "etf_value": round(etf_total, 2),
        "cash": round(cash, 2),
        "positions": positions,
        "weights": [round(w, 4) for w in weights],
        "target_w": target_w,
        "threshold_pp": THRESHOLD_PP,
        "triggered": triggered_3pp,
        "plan": plan,
        "grid_signals": grid_signals,
        "grid_state": grid_state,
        "qdii_premiums": qdii_premiums or [],
    }


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def advance_all_grids(state: dict, today: str, prices_by_name: dict[str, float],
                      pool_value: float, recent_min: float) -> tuple[dict, list[dict]]:
    """Advance grid state for all 3 assets, returning (new_state, signals)."""
    layer = grid_mod.layer_yuan(pool_value, recent_min)
    new_state: dict = {}
    signals: list[dict] = []
    for name in ASSET_NAMES:
        asset_state = state.get(name) or grid_mod.fresh_asset_state()
        price = prices_by_name.get(name)
        if price is None:
            new_state[name] = asset_state
            continue
        new_asset_state, signal = grid_mod.advance_one_day(
            asset_state, today, float(price), layer,
        )
        new_state[name] = new_asset_state
        snap = grid_mod.asset_snapshot(new_asset_state, float(price))
        # Compute "distance to next trigger":
        # - in_cycle=True  → 距离止盈位 (first_buy × 1.03) 还有多远
        # - in_cycle=False → 距离开仓位 (窗口最低) 还有多远
        if snap["in_cycle"]:
            entries = new_asset_state.get("entries", [])
            first_buy = entries[0]["price"] if entries else 0.0
            tp_target = first_buy * (1 + grid_mod.GRID_TP_PCT / 100) if first_buy else 0.0
            if tp_target > 0 and price > 0:
                trigger_pct = (tp_target - price) / price * 100
                trigger_kind = "tp"
            else:
                trigger_pct = 0.0
                trigger_kind = "tp"
        else:
            window = new_asset_state.get("last_prices", [])
            wmin = min(window) if window else None
            if wmin and price > 0:
                trigger_pct = (price - wmin) / wmin * 100
                trigger_kind = "open"
            else:
                trigger_pct = 0.0
                trigger_kind = "open"
        signals.append({
            "name": name,
            "price": round(float(price), 4),
            "layer_yuan": round(layer, 2),
            "signal": signal,                          # "buy" | "sell" | None
            "in_cycle": snap["in_cycle"],
            "num_buys": snap["num_buys"],
            "avg_cost": snap["avg_cost"],
            "current_value": snap["current_value"],
            "unrealized_pnl": snap["unrealized_pnl"],
            "return_pct": snap["return_pct"],
            "trigger_pct": round(trigger_pct, 2),
            "trigger_kind": trigger_kind,
        })
    return new_state, signals


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

    # QDII premium rate: enrich position dicts with premium info
    # (best-effort: failure does not abort the run)
    qdii_top: list[dict] = []      # tracked-but-not-held QDII premiums
    for p in positions:
        if p["code"] in QDII_CODES:
            try:
                prem = fetch_qdii_premium(p["code"])
                if prem:
                    p["premium"] = prem   # {price, iopv, premium_pct, nav_date, ...}
                    flag = "⚠" if abs(prem["premium_pct"]) > 5 else "·"
                    print(
                        f"  QDII {p['name']:<14} premium={prem['premium_pct']:+.2f}%  "
                        f"(市价 {prem['price']:.4f} / IOPV {prem['iopv']:.4f}@{prem['nav_date']})  {flag}",
                        file=sys.stderr,
                    )
            except Exception as ex:
                print(f"WARN QDII premium {p['code']} failed: {ex}", file=sys.stderr)

    # Also fetch premium for QDII codes known to ETF_DEFS but NOT held today —
    # so the email can still warn ("don't buy, premium too high").
    held_codes = {p["code"] for p in positions}
    for code, (sina, em, name, is_qdii) in ETF_DEFS.items():
        if not is_qdii or code in held_codes:
            continue
        try:
            prem = fetch_qdii_premium(code)
            if prem:
                qdii_top.append({
                    "code": code, "name": name, "in_portfolio": False,
                    **prem,
                })
                flag = "⚠" if abs(prem["premium_pct"]) > 5 else "·"
                print(
                    f"  QDII {name:<14} (未持仓) premium={prem['premium_pct']:+.2f}%  "
                    f"(市价 {prem['price']:.4f} / IOPV {prem['iopv']:.4f}@{prem['nav_date']})  {flag}",
                    file=sys.stderr,
                )
        except Exception as ex:
            print(f"WARN QDII unheld premium {code} failed: {ex}", file=sys.stderr)

    total, weights, triggered_3pp, plan = evaluate(positions, cash)

    # 3pp pool for grid layer sizing (history.totals + today)
    history = load_history()
    pool = pd.Series(
        [r["total"] for r in history if "total" in r] + [total],
        index=pd.to_datetime(
            [r["date"] for r in history if "total" in r] + [today]
        ),
    ).sort_index()
    window = grid_mod.GRID_FLOATING_WINDOW + 1
    recent_min = float(pool.tail(window).min()) if len(pool) else 0.0

    # Advance grid
    # Today uses the K-line LOW (intraday low) so the trigger can fire on
    # intraday dips even if the close is above yesterday's close.
    state = load_grid_state()
    prices_by_name = {p["name"]: p["price"] for p in positions if p["name"] in ASSET_NAMES}
    lows_by_name: dict[str, float] = {}
    for p in positions:
        if p["name"] not in ASSET_NAMES:
            continue
        code = p["code"]
        kf = fetch_kline_history(code, start=today, end=today)
        if kf is not None and not kf.empty:
            lows_by_name[p["name"]] = float(kf["low"].iloc[-1])
    # If K-line fetch failed for an asset, fall back to the realtime close
    grid_prices = {n: lows_by_name.get(n, prices_by_name.get(n)) for n in ASSET_NAMES}
    new_state, grid_signals = advance_all_grids(
        state, today, grid_prices, total, recent_min,
    )
    # Annotate signals with the (intraday) low that drove the decision so
    # the daily record shows the actual trigger price, not the close.
    for s in grid_signals:
        if s["name"] in lows_by_name:
            s["intraday_low"] = round(lows_by_name[s["name"]], 4)
    save_grid_state(new_state)

    # Persist record
    record = build_record(
        today=today, positions=positions, cash=cash,
        total=total, weights=weights,
        triggered_3pp=triggered_3pp, plan=plan,
        grid_signals=grid_signals, grid_state=new_state,
        qdii_premiums=qdii_top,
    )
    history = [r for r in history if r["date"] != today]
    history.append(record)
    save_history(history)

    # Render chart (best-effort: chart may fail if network blocked; record is already saved)
    try:
        from .chart import render_chart
        render_chart(history, GRID_CHART)
    except Exception as ex:
        print(f"WARN chart render failed: {ex}", file=sys.stderr)

    any_signal = triggered_3pp or any(s["signal"] for s in grid_signals)
    dyn_target = 1.0 / len(positions) if positions else TARGET_W
    print(f"DATE={today} TOTAL={total:.2f} STATUS={'TRIGGER' if any_signal else 'ok'}")
    print(f"  3pp: {'⚠ TRIGGERED' if triggered_3pp else 'ok'} (max Δ {max(abs(w - dyn_target) * 100 for w in weights):.2f}pp)")
    for p, w in zip(positions, weights):
        delta = (w - dyn_target) * 100
        flag = " ⚠" if abs(delta) > THRESHOLD_PP else ""
        print(f"    {p['name']:<14} {w:>6.2%}  Δ={delta:+.2f}pp{flag}")
    print(f"  grid layer: ¥{grid_mod.layer_yuan(total, recent_min):,.0f} "
          f"(pool ¥{total:,.0f}, recent_min ¥{recent_min:,.0f})")
    for s in grid_signals:
        tag = s["signal"] or "hold"
        print(f"    {s['name']:<14} {tag:<4}  layer=¥{s['layer_yuan']:,.0f}  "
              f"in_cycle={s['in_cycle']}  buys={s['num_buys']}  "
              f"unreal={s['unrealized_pnl']:+,.0f}")

    write_github_output(today, total, any_signal)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
