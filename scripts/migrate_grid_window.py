"""One-shot: 网格的 120 日窗口改成**带日期**的格式 (D1)。

用法:
    python -X utf8 scripts/migrate_grid_window.py            # 写盘
    python -X utf8 scripts/migrate_grid_window.py --dry-run  # 只打印

背景 (D1): `apply-grid.yml` 提交后会 re-trigger `daily.yml`, **同一天**
`record.py` 可能跑两次 → 原来那个「纯数字队列」把同一天 append 两次 →
窗口深度少一天。而窗口是「建议开仓」的**唯一**依据 (条件 =
`price < min(window)`), 少一天就少一天的历史, 而且**看不出来**。

改法: 窗口的每一根 K 线带上日期 —— `[["2026-10-28", 8.12], ...]`。
同一天再跑一次是**覆盖** (留更低的那个 —— 那才是那天的最低价), 不是追加。
窗口深度只由**交易日**决定。

⚠️ **老数据的日期不可恢复**: 窗口里存的是**前复权**之后的 K 线低点, 而
分红除权会让历史价格**重新基准化** —— 同一个「历史低点」在不同日子算出来
是不同的数。所以老数据只能迁成**无日期**的 bar (`["", 8.12]`), 日期靠之后
的每日运行**自己长出来** (约 120 个交易日后, 窗口全是带日期的)。

无日期的 bar 排在**最前面** —— 它们是窗口里最老的那批。

做什么 (逐资产):
    last_prices: [1.331, 1.34, ...]  ->  [["", 1.331], ["", 1.34], ...]

**价格一个都不改** —— 纯格式迁移, 不重新取数, 不碰前复权 (免得引入
qfq 漂移)。其余 5 个 key 原样保留。

幂等: 已经迁过的资产 (值是 `[日期, 价格]` 对) 直接跳过。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID_STATE = ROOT / "data" / "grid_state.json"

#: 「日期不可恢复」的哨兵 —— 与 src/domain/grid/price_window.py:UNDATED 一致。
UNDATED = ""


def migrate_window(raw) -> tuple[list, bool]:
    """Migrate one asset's window. Returns (new_window, changed)."""
    if not raw:
        return [], False
    out = []
    changed = False
    for item in raw:
        if isinstance(item, (list, tuple)):
            # 已经是新格式 —— 原样保留
            out.append([str(item[0]), float(item[1])])
            continue
        # 旧格式: 裸数字 —— 日期不可恢复
        out.append([UNDATED, float(item)])
        changed = True
    return out, changed


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
        new_asset = dict(asset)
        window, changed = migrate_window(new_asset.get("last_prices") or [])
        if changed:
            new_asset["last_prices"] = window
            touched += 1
            print(f"[{name}] last_prices {len(window)} 根 → 带日期 (无日期)")
        out[name] = new_asset

    if touched == 0:
        print("没有资产需要迁移 (窗口已经是带日期的格式)")
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
