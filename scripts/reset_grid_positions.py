"""One-shot: 清空自动开仓的实盘仓位, 网格从零开始。

用法:
    python -X utf8 scripts/reset_grid_positions.py            # 写盘
    python -X utf8 scripts/reset_grid_positions.py --dry-run  # 只打印

背景: 复合网格引擎**不再自动交易** —— 它只滚动 120 日窗口并给出建议,
真正的开仓/加仓/回本全部由用户在「交易记录 → 复合网格」提交 (走
scripts/apply_grid.py)。自动时代引擎自己开出来的实盘周期因此不再是
"用户下的单", 按用户要求清空, 网格从零开始。

做什么 (逐资产):
    in_cycle  -> False
    entries   -> []
    ops       -> 删掉 source == "auto" 的行 (自动时代的产物)
    cum_realized / merged_shares -> 保持原值 (当前都是 0)
    last_prices -> **保留** (距开仓建议靠它; 删了等于把窗口重新灌满 120 天)
    source == "backtest" 的行 -> 原样保留 (历史模拟, 与实盘无关)

一个周期 = N 笔买入 + 1 笔止盈, 买入份额 − 止盈卖出份额 = 归并进再平衡的
merged_shares。这条规则由 src/domain/grid/grid_position.py:manual_op 落地。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID_STATE = ROOT / "data" / "grid_state.json"

# 引擎自动时代的 source 值。手动时代只有 "manual" (实盘) 和 "backtest" (模拟)。
AUTO_SOURCE = "auto"


def reset_asset(name: str, asset: dict) -> tuple[dict, list[str]]:
    """Reset one asset's live position. Returns (new_asset, changes)."""
    changes: list[str] = []
    new = dict(asset)

    if new.get("in_cycle"):
        changes.append("in_cycle True -> False")
        new["in_cycle"] = False

    entries = new.get("entries") or []
    if entries:
        changes.append(f"entries {len(entries)} -> 0")
        new["entries"] = []

    ops = list(new.get("ops") or [])
    kept = [o for o in ops if o.get("source") != AUTO_SOURCE]
    dropped = len(ops) - len(kept)
    if dropped:
        changes.append(f"ops 删掉 {dropped} 条 source==\"{AUTO_SOURCE}\" 的行")
    new["ops"] = kept

    # cum_realized / merged_shares / last_prices **原样保留**。
    # last_prices 是"距开仓建议"的唯一依据, 删了窗口要重新灌满 120 天。
    return new, changes


def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]

    if not GRID_STATE.exists():
        print(f"ERROR {GRID_STATE} 不存在", file=sys.stderr)
        sys.exit(1)

    try:
        state = json.loads(GRID_STATE.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"ERROR grid_state.json 读不出来: {ex}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(state, dict):
        print("ERROR grid_state.json 根必须是对象", file=sys.stderr)
        sys.exit(1)

    out: dict = {}
    touched = 0
    for name, asset in state.items():
        if not isinstance(asset, dict):
            out[name] = asset
            continue
        new_asset, changes = reset_asset(name, asset)
        out[name] = new_asset
        if changes:
            touched += 1
            print(f"[{name}]")
            for c in changes:
                print(f"  - {c}")

    if touched == 0:
        print("没有任何资产需要重置 (已经是干净状态)")
        return

    if dry_run:
        print(f"\n[dry-run] 不会写盘。涉及 {touched} 个资产。")
        return

    tmp = GRID_STATE.with_suffix(GRID_STATE.suffix + ".tmp")
    # 不加尾随换行 —— src/infrastructure/json_repositories.py:save_grid_state 也是这个口径, 免得两条
    # 写盘路径互相制造「只有一行之差」的假 diff。
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, GRID_STATE)
    print(f"\nOK 已写回 {GRID_STATE.relative_to(ROOT)} ({touched} 个资产)")


if __name__ == "__main__":
    main()
