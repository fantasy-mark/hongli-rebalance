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

⚠️ This file is now a **thin CLI shell** (Phase 5c-2). The use case lives in
`src/application/apply_grid_op.py` (validate → apply → persist), and the grid
rules live in `src/domain/grid/*` — the **single** implementation, so the
workflow doesn't need to install pandas/akshare (src/domain/grid/grid_position.py's
module level only imports os/typing; pandas is a local import inside
run_compound_grid). The take-profit rule: a `tp` sells only
`total_invested / price` shares (recovering exactly the principal) and merges
the remainder into the rebalance portfolio (`merged_shares`), so realized P&L
is 0 on a normal exit.

⚠️ This is the **only** live path for grid positions — the engine itself
no longer trades (it only reports suggestions).

Validation rules (owned by `src/application/apply_grid_op.py`):
  - schema == "grid-ops-v1"
  - len(ops) <= MAX_OPS
  - per-op field rules (see validate_op)
  - date is ISO yyyy-mm-dd, not in the future (today + 1 day tolerated)
  - op must be in {open, add, tp} (the vocabulary of `TriggerLevel`)
  - code must be in ALLOWED_CODES whitelist
  - price > 0; for open/add: shares > 0; tp: shares is ignored
  - open requires no in_cycle; add/tp require in_cycle=True
    (enforced inside manual_op)
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 用例层 + 领域层都是 stdlib-only (pandas 是 run_compound_grid 里的局部 import)
# → workflow 不需要装 pandas/akshare。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.application.apply_grid_op import (  # noqa: E402
    MAX_OPS,
    SCHEMA,
    VALID_OPS,
    GridOpError,
    GridOpBatch,
    apply_grid_ops,
    inline_manual_op,
)
from src.domain.events.domain_events import EventStream  # noqa: E402,F401
from src.domain.grid.exit_policy import ExitAtCost, cost_only_exit  # noqa: E402,F401
from src.domain.grid.grid_position import manual_op, split_take_profit  # noqa: E402,F401
from src.domain.shared.identifiers import AssetCode, TradingDate  # noqa: E402,F401
# GitHub Actions 的写法 (::error:: annotation) 住在适配器里 (D12) --
# 这个脚本不再自己拼 workflow command。
from src.infrastructure.github_actions import error as annotate_error  # noqa: E402
# 数据文件的路径只有一个来源 (D13) -- src/infrastructure/paths.py。
from src.infrastructure.paths import GRID_STATE_FILE as GRID_STATE  # noqa: E402
# cost_only_exit / ExitAtCost 是出场规则的**本体** (src/domain/grid/exit_policy.py);
# split_take_profit 是兼容别名 (返回旧版的 dict 形状), 保留是为了既有 import 不断。
# AssetCode / TradingDate 是**代码格式与日期格式的唯一实现** (以前这里跟
# apply_holdings.py 各带一份正则, 改一处漏一处)。
# `inline_manual_op` 从用例层转出来 —— 既有调用方与测试从这里 import。

# Mirror scripts/apply_holdings.py — same whitelist as src/domain/portfolio/asset_universe.py:ETF_DEFS.
ALLOWED_CODES = {
    "515180": "红利ETF易方达",
    "159937": "黄金ETF博时",
    "511380": "可转债ETF博时",
    "513300": "纳斯达克ETF华夏",
}
NAME_LOOKUP = ALLOWED_CODES  # code -> Chinese display name (used as state key)
CODE_TO_NAME = ALLOWED_CODES


def fail(msg: str) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    # ::error:: 让失败原因直接显示在 Actions UI 和失败通知里 ——
    # 否则用户只在页面看到"已提交"却找不到哪里错了。
    annotate_error(msg)
    sys.exit(1)


# -----------------------------------------------------------------------------
# Echo
# -----------------------------------------------------------------------------

def _echo(batch: GridOpBatch) -> None:
    """把这一批的结果打印出来 —— 给人看的。"""
    try:
        rel = GRID_STATE.relative_to(ROOT)
    except ValueError:
        rel = GRID_STATE
    # ⚠️ "across N asset(s)" 的 N 跟 op 数一样 —— 这是**旧脚本就有的**措辞
    # (它数的是 summary 行数, 也就是 op 数)。保留原样, 别在重构里改文案。
    n_ops = len(batch.summary)
    print(f"OK wrote {rel}: {n_ops} op(s) applied across {n_ops} asset(s)")
    for s in batch.summary:
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
    # 领域事件摘要 (Phase 5b) —— 「这一批操作到底改了什么」。
    print(f"  events: {batch.events.summary_line()}")
    for e in batch.events:
        if e.type == "MergedSharesTransferred":
            print(f"    ↳ 归并移交 {e.code} {e.shares:,.2f} 份 "
                  f"(provenance={e.provenance})")


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

    # 用例层: 校验整批 → 读盘 → 应用 → 写盘 (整批 all-or-nothing)
    try:
        batch = apply_grid_ops(
            payload, GRID_STATE,
            today=datetime.date.today(),
            allowed_codes=ALLOWED_CODES,
        )
    except GridOpError as ex:
        fail(str(ex))

    _echo(batch)


if __name__ == "__main__":
    main()
