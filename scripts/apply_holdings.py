"""Apply a trade-ledger edit submitted via the GitHub Pages editor.

Invoked by .github/workflows/apply-holdings.yml. Receives a single JSON
payload on argv[1] with shape:

    {
      "schema": "trades-v1",
      "trades": [
        {"date": "2026-10-17", "side": "buy",   "code": "515180",
         "shares": 1000, "price": 1.40, "note": "..."},
        {"date": "2026-10-17", "side": "deposit_rebalance", "amount": 5000, "note": "..."}
      ]
    }

Validates and appends the new trades to `data/trades.json`, then rebuilds
`data/holdings.json` (schema v2, two cash pools) by replaying the full
ledger.

⚠️ This file is now a **thin CLI shell** (Phase 5c-2). The use case lives in
`src/application/apply_trade.py` (validate → assign ids → replay → persist),
and the ledger rules live in `src/domain/ledger/*` — the **single**
implementation, shared with the daily snapshot. The workflow doesn't install
the Python deps, so that whole chain is stdlib-only.

Validation rules (owned by `src/application/apply_trade.py`):
  - schema == "trades-v1"
  - len(trades) ≤ MAX_ROWS
  - per-trade field rules per `data/trades.json` schema (see README)
  - date is ISO yyyy-mm-dd, not in the future (today + 1 day tolerated)
  - sell never exceeds current held shares
  - cash_rebalance + grid_budget both ≥ 0 after replay (no negative balance)
  - all buy/sell codes are in ALLOWED_CODES whitelist
  - id is auto-assigned server-side as `<date>-<side>-<seq>` (zero-padded)

Old `{rows, cash}` payload format is REJECTED with a clear message; the
editor has been upgraded and the user must refresh their browser.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 用例层 + 领域层都是 stdlib-only → 这个 job 不装任何依赖也能跑。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.application.apply_trade import (  # noqa: E402
    CASH_SIDES,
    ETF_SIDES,
    MAX_ROWS,
    SCHEMA,
    VALID_SIDES,
    TradeBatch,
    TradeError,
    apply_trades,
)
from src.domain.shared.identifiers import AssetCode, TradingDate  # noqa: E402,F401
# GitHub Actions 的写法 (::error:: annotation) 住在适配器里 (D12) --
# 这个脚本不再自己拼 workflow command。
from src.infrastructure.github_actions import error as annotate_error  # noqa: E402
# 数据文件的路径只有一个来源 (D13) -- src/infrastructure/paths.py。
from src.infrastructure.paths import (  # noqa: E402
    HOLDINGS_FILE as HOLDINGS,
    TRADES_FILE as TRADES,
)
# AssetCode / TradingDate 是**代码格式与日期格式的唯一实现** (以前这里跟
# apply_grid.py 各带一份正则, 改一处漏一处)。

# Mirror src/domain/portfolio/asset_universe.py:ETF_DEFS — whitelist of tradeable codes.
# Source of truth for "what's tradeable". We don't import the asset universe
# module (src/domain/portfolio/asset_universe.py) because the workflow
# doesn't install the Python deps for this step.
ALLOWED_CODES = {
    "515180": "红利ETF易方达",
    "159937": "黄金ETF博时",
    "511380": "可转债ETF博时",
    "513300": "纳斯达克ETF华夏",
}
NAME_LOOKUP = ALLOWED_CODES  # 6-digit code → Chinese display name


def fail(msg: str) -> None:
    print(f"ERROR {msg}", file=sys.stderr)
    # ::error:: 让失败原因直接显示在 Actions UI 和失败通知里 ——
    # apply_grid.py 一直有, apply_holdings.py 没有 (D12: 现在两边一致)。
    annotate_error(msg)
    sys.exit(1)


# -----------------------------------------------------------------------------
# Echo
# -----------------------------------------------------------------------------

def _echo(batch: TradeBatch) -> None:
    """把这一批的结果打印出来 —— 给人看的。"""
    try:
        rel_t = TRADES.relative_to(ROOT)
        rel_h = HOLDINGS.relative_to(ROOT)
    except ValueError:
        rel_t, rel_h = TRADES, HOLDINGS
    print(f"OK wrote {rel_t}: {len(batch.merged)} trades "
          f"(+{len(batch.new_trades)} new)")
    print(f"OK wrote {rel_h}: cash_rebalance=CNY {batch.cash_rebalance:.2f}, "
          f"cash_grid=CNY {batch.grid_budget:.2f}, {len(batch.positions)} positions")
    for p in batch.positions:
        print(f"  - {p['name']:<14} ({p['code']}) {p['shares']:>12,}")
    # Show the new trades that were just appended
    print("\nNew trades appended:")
    for t in batch.new_trades:
        if t["side"] in ("buy", "sell"):
            print(f"  - {t['id']}  {t['side']:<7} {t['code']}  "
                  f"{t['shares']:>10} @ CNY {t['price']:.4f}  "
                  f"date={t['date']}  note={t.get('note','')}")
        else:
            print(f"  - {t['id']}  {t['side']:<21} amount=CNY {t['amount']:>10.2f}  "
                  f"date={t['date']}  note={t.get('note','')}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        fail("usage: apply_holdings.py <payload-json>")

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as ex:
        fail(f"payload is not valid JSON: {ex}")
    if not isinstance(payload, dict):
        fail(f"payload must be a JSON object, got {type(payload).__name__}")

    # 用例层: 校验整批 → 读账本 → 发 id → 重放 → 写两份文件
    # (整批 all-or-nothing —— 任何一步不过, 两份文件都不动)
    try:
        batch = apply_trades(
            payload, TRADES, HOLDINGS,
            today=datetime.date.today(),
            allowed_codes=ALLOWED_CODES,
            name_lookup=NAME_LOOKUP,
        )
    except TradeError as ex:
        fail(str(ex))

    _echo(batch)


if __name__ == "__main__":
    main()
