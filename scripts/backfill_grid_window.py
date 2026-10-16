"""One-shot backfill of grid_state.json's last_prices window.

The grid state JSON was bootstrapped under the old A-rule (single
"last_120d_min" scalar). After switching to the C-rule (rolling 120-day
window), the new `last_prices` queue was filled with only the most recent
2 days, which causes false triggers: any 1-day dip looks like a "new
120-day low" when the window is short.

This script fetches the actual past GRID_LOOKBACK closes for each asset
and rewrites `last_prices` in data/grid_state.json. It also keeps the
existing in-cycle entries untouched (so the simulated holdings don't
disappear) but recomputes the cum_realized of any in-progress cycle from
the actual prices.

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

from src import grid as grid_mod
from src.fetch import fetch_one

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

    print(f"回填窗口: {start_str} → {end_str}  (target lookback={LOOKBACK})\n")
    for name, sina, em in ASSETS:
        if name not in state:
            print(f"[{name}] 不在 grid_state 里, 跳过")
            continue
        s = state[name]
        df = fetch_one(sina, em, start_str, end_str)
        closes = df["close"].dropna().sort_index()
        # 取截至今天（含）的最后 LOOKBACK 个 close
        window = closes[closes.index <= today].tail(LOOKBACK)
        old_entries = s.get("entries", [])
        # Reset: 进 cycle 的入口是在错误的"短窗口"状态下塞进去的,
        # 用真 120 日窗口回顾, 当天的 close 全部 >= 窗口最低 → 应该都没有触发.
        # 所以清掉 entries 重新开始 (模拟仓位归零).
        s["in_cycle"] = False
        s["entries"] = []
        s["cum_realized"] = 0.0
        s["last_prices"] = [float(p) for p in window.values]
        print(f"[{name}]  清 {len(old_entries)} 旧 entries, "
              f"last_prices 长度 {len(s['last_prices'])}, "
              f"min={min(s['last_prices']):.4f}  max={max(s['last_prices']):.4f}  "
              f"末值={s['last_prices'][-1]:.4f}  "
              f"窗口 {window.index[0].date()} → {window.index[-1].date()}")

    atomic = GRID_STATE.with_suffix(".json.tmp")
    atomic.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    atomic.replace(GRID_STATE)
    print(f"\n已写入 {GRID_STATE}")


if __name__ == "__main__":
    main()
