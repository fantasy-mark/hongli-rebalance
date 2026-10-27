"""One-shot backfill of grid_state.json's `ops` log from the chart replay.

WHY THE LOG WAS EMPTY: the live grid engine has never opened a cycle (all
4 assets sit at `in_cycle: false`), and grid_state.json predates the ops-log
migration, so it had no `ops` key at all. But data/grid_chart.png's 资金占用
panel *does* show grid trades — that panel is `src/infrastructure/chart_renderer.py:_replay_grid_for_chart`,
which calls `run_compound_grid(s, trigger_mode="window")` over the full price
window and keeps ONLY `result["history"]`, throwing away `result["cycles"]`
and `result["open_cycle"]`. The data is computed, then discarded.

This script re-runs that same replay and converts its cycles into per-event
ops rows via `src.simulation.grid_simulator.ops_from_cycles`, so the 复合网格 操作记录 table can be
cross-referenced against the committed PNG.

THESE OPS ARE SIMULATED (source="backtest"), NOT LIVE:
  - the replay starts 2020-11-05; the live engine only began in 2026;
  - the replay sizes every layer flat at GRID_BASE_YUAN (¥10,000), while the
    live rule uses layer_yuan() = ¥10,000 + 5% of the pool surplus;
  - the replay trades on CLOSES (chart.py takes `close`), while the live
    trigger watches K-line LOWS (see scripts/backfill_grid_window.py).
So the share counts will NOT match what the live grid would actually place.

EXIT RULE: exits use the "sell cost only" rule (src/domain/grid/exit_policy.py:
cost_only_exit) — the replay sells just enough shares to recover the
principal and merges the remainder into the rebalance portfolio
(`merged_shares`). So every tp row here has pnl == 0 and a non-zero
`shares_to_merge`; the grid's entire gain lives in the merged shares'
market value, which the portfolio now owns.

Idempotent: re-running drops every existing source=="backtest" op and
regenerates them, preserving any live source=="auto"/"manual" ops.

Run:
    python scripts/backfill_grid_ops.py --check    # dry run, prints the
                                                  # reconciliation table only
    python scripts/backfill_grid_ops.py            # fetch, then write
    python scripts/backfill_grid_ops.py --offline  # reuse the price snapshot,
                                                  # no network
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.ERROR)

import pandas as pd

from src.simulation import grid_simulator as grid_mod
GRID_STATE = ROOT / "data" / "grid_state.json"
PRICE_SNAPSHOT = ROOT / "data" / "grid_prices_snapshot.json"


def _load_prices(offline: bool) -> dict:
    """Price series per asset, as {asset: pd.Series indexed by Timestamp}.

    Deliberately routed through chart.py's own fetchers rather than
    src.infrastructure.quote_providers.fetch_kline_history: the ops must be provably derived from
    the same prices that produced the committed grid_chart.png.
    """
    if offline and PRICE_SNAPSHOT.exists():
        raw = json.loads(PRICE_SNAPSHOT.read_text(encoding="utf-8"))
        return {
            name: pd.Series(
                {pd.Timestamp(d): float(p) for d, p in series.items()}
            ).sort_index()
            for name, series in raw.items()
        }

    from src.infrastructure.chart_renderer import _fetch_asset_window, _load_chart_assets
    prices = _fetch_asset_window(_load_chart_assets())
    snapshot = {
        name: {ts.strftime("%Y-%m-%d"): float(p) for ts, p in s.items()}
        for name, s in prices.items()
    }
    PRICE_SNAPSHOT.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入价格快照 {PRICE_SNAPSHOT.name} "
          f"({len(snapshot)} 个资产, {len(next(iter(snapshot.values())))} 个交易日)")
    return prices


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="只打印对账表, 不写 grid_state.json")
    ap.add_argument("--offline", action="store_true",
                    help=f"读 {PRICE_SNAPSHOT.name}, 不联网")
    args = ap.parse_args()

    prices = _load_prices(args.offline)
    state = json.loads(GRID_STATE.read_text(encoding="utf-8"))

    first = min(s.index[0] for s in prices.values())
    last = max(s.index[-1] for s in prices.values())
    print(f"\n回放区间 {first.date()} → {last.date()}   "
          f"每层固定 ¥{grid_mod.GRID_BASE_YUAN:,.0f} (回测口径)\n")
    header = (f"{'资产':<16}{'周期':>5}{'层数':>5}{'op':>5}"
              f"{'投入合计':>12}{'归并市值':>12}{'资金占用':>12}")
    print(header)
    print("-" * len(header.encode("gbk", errors="replace")))

    all_ops = {}
    total_ops = total_invested = total_merged = total_capital = 0.0
    for name, s in prices.items():
        result = grid_mod.run_compound_grid(s, trigger_mode="window")
        cycles, open_cycle = result["cycles"], result["open_cycle"]
        ops = grid_mod.ops_from_cycles(cycles, open_cycle)
        all_ops[name] = ops

        invested = sum(c["total_invested"] for c in cycles)
        if open_cycle:
            invested += result["open_invested"]
        layers = sum(c["num_buys"] for c in cycles) + len(open_cycle or [])
        last_px = s.iloc[-1]
        # 资金占用 = 网格**在周期内**的层 (出场后本金全额收回, 占用回落);
        # 归并出去的份额归再平衡组合管, 不占网格的钱。
        open_value = result["open_shares"] * last_px if open_cycle else 0.0
        merged_value = result["merged_shares"] * last_px

        total_ops += len(ops)
        total_invested += invested
        total_merged += merged_value
        total_capital += open_value
        print(f"{name:<16}{len(cycles):>5}{layers:>5}{len(ops):>5}"
              f"{invested:>12,.0f}{merged_value:>12,.0f}{open_value:>12,.0f}")
        print(f"{'':<16}{'':>5}{'':>5}{'':>5}"
              f"{'  ↳ 层数 × ¥10,000 =':>12}{layers * grid_mod.GRID_BASE_YUAN:>11,.0f}"
              f"{'已实现 (恒为 0)':>12}{result['cum_realized']:>12,.0f}")

    print("-" * len(header.encode("gbk", errors="replace")))
    print(f"{'合计':<16}{'':>5}{'':>5}{int(total_ops):>5}"
          f"{total_invested:>12,.0f}{total_merged:>12,.0f}{total_capital:>12,.0f}")

    # Reconciliation: every layer costs exactly GRID_BASE_YUAN, so the sum of
    # entry shares × price must equal layers × base.
    expected = sum(len([o for o in ops if o["op"] in ("open", "add")])
                   for ops in all_ops.values()) * grid_mod.GRID_BASE_YUAN
    print(f"\n对账: 入场 {expected:,.0f} 元 / 周期表 "
          f"{total_invested:,.0f} 元  "
          f"{'✓ 一致' if abs(expected - total_invested) < 1e-6 else '✗ 不一致'}")

    if args.check:
        print("\n--check: 未写入任何文件")
        return

    for name, ops in all_ops.items():
        grid_mod.merge_backtest_ops(state, name, ops)

    # Atomic write — same pattern as backfill_grid_window.py, so a crash
    # mid-write can't leave record.py with unreadable JSON.
    tmp = GRID_STATE.with_suffix(GRID_STATE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, GRID_STATE)
    print(f"\n已写入 {GRID_STATE}  (共 {int(total_ops)} 条回测 op)")


if __name__ == "__main__":
    main()