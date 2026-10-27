"""One-shot backfill of grid_state.json's last_prices window using K-line lows.

History: grid_state.json was originally persisted with a single
"last_120d_min" scalar (old schema). The current live rule uses a
rolling 120-day window of past K-line lows (the trigger is
`price < min(last_prices)`, see src/domain/grid/grid_position.py:advance_one_day). After
the schema switch, the new `last_prices` queue was bootstrapped with
only the most recent ~2 days of data, which causes false triggers
(any 1-day dip looks like a "new 120-day low" when the window is short).

This script fetches the actual past GRID_LOOKBACK K-line LOWS (matches
同花顺/通达信 stock-software numbers) for each asset and rewrites
`last_prices` in data/grid_state.json. Existing in-cycle entries are
cleared so the live rule re-evaluates from a clean window.

WARNING: this is a one-shot script with a hard-coded end date
(`today = 2026-10-16` below). Re-running with the date unchanged is
safe; updating the date and re-running will rewrite the window to
end on the new date, which is usually NOT what you want (the live
rule appends today's price each run — manually re-pushing past K-lines
can desync the rolling window). Run ONCE, locally, after the schema
switch. If the live rule ever needs re-seeding, write a new script
that respects the daily append semantics.

Run:
    python scripts/backfill_grid_window.py
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    import logging
    logging.getLogger(_n).setLevel(logging.ERROR)

import pandas as pd

from src.simulation import grid_simulator as grid_mod
from src.infrastructure.quote_providers import fetch_kline_history
ASSETS = [
    ("黄金ETF博时",   "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
    ("纳斯达克ETF华夏", "sh513300", "513300"),
]
LOOKBACK = grid_mod.GRID_LOOKBACK
GRID_STATE = ROOT / "data" / "grid_state.json"


def main() -> None:
    state = json.loads(GRID_STATE.read_text(encoding="utf-8"))
    today = pd.Timestamp("2026-10-16")
    start = today - pd.tseries.offsets.BDay(LOOKBACK + 5)  # buffer for holidays
    start_str = start.strftime("%Y-%m-%d")
    end_str = today.strftime("%Y-%m-%d")

    print(f"回填窗口: {start_str} → {end_str}  (lookback={LOOKBACK}, 用 K 线最低价)\n")
    for name, _sina, code in ASSETS:
        if name not in state:
            print(f"[{name}] 不在 grid_state 里, 跳过")
            continue
        s = state[name]
        df = fetch_kline_history(code, start=start_str, end=end_str)
        if df is None or df.empty:
            print(f"[{name}] K 线拉取失败, 跳过")
            continue
        lows = df["low"].dropna().sort_index()
        window = lows[lows.index <= today].tail(LOOKBACK)
        old_entries = s.get("entries", [])
        s["in_cycle"] = False
        s["entries"] = []
        s["cum_realized"] = 0.0
        # ⚠️ D1: 窗口**带日期** —— `[[日期, 价格], ...]`。同一天重复运行是
        # 覆盖而不是追加, 窗口深度只由交易日决定 (见 price_window.py)。
        s["last_prices"] = [[idx.strftime("%Y-%m-%d"), float(p)]
                            for idx, p in window.items()]
        prices = [p for _d, p in s["last_prices"]]
        print(f"[{name}]  清 {len(old_entries)} 旧 entries, "
              f"K线 {len(df)} 行 → 取末 {LOOKBACK} = {len(window)} 行, "
              f"min={min(prices):.4f}  max={max(prices):.4f}  "
              f"末值={prices[-1]:.4f}  "
              f"窗口 {window.index[0].date()} → {window.index[-1].date()}")

    atomic = GRID_STATE.with_suffix(".json.tmp")
    atomic.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    atomic.replace(GRID_STATE)
    print(f"\n已写入 {GRID_STATE}")


if __name__ == "__main__":
    main()
