"""账本的**投影** —— 把只追加的账本重放成「此刻的持仓 + 两个现金池」。

账本是唯一的事实源 (BC-A); `data/holdings.json` 只是它的**缓存**, 不是事实。
这个模块把「读」这一侧从「写」那一侧 (`ledger.py`) 里分出来:

    ledger.py        写侧 —— apply_trade: 追加一笔, 校验不变量 (I1 / I2)
    projection.py    读侧 —— project_ledger: 重放整个账本, 得到投影

⚠️ **重放只有一份实现** —— 每日快照 (`daily_snapshot.replay_trade_ledger`)
与写路径 (`apply_trade.apply_batch`) 都调这一个函数。两份实现只可能长歪。

⚠️ **stdlib-only** —— 写路径 (`scripts/apply_holdings.py`) 在不装
pandas/akshare 的 GitHub Actions job 里 import 这条链。
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from ..events.domain_events import EventStream
from .ledger import apply_trade


class LedgerProjection(NamedTuple):
    """账本在某一刻的投影。

    ⚠️ 是 `NamedTuple` 而不是普通 dataclass —— 现有的调用方都是
    `positions, cash_rebalance, grid_budget = project_ledger(...)` 那样解包的。
    把它变成**有名字**的东西不该逼着调用方改形状 (那只是一次改名,
    不是一次建模); 想用具名属性的人写 `proj.positions` 就行。

    Attributes:
        positions:      持仓 [{name, code, shares}] —— 只含份额 > 0 的
        cash_rebalance: 再平衡现金池 (真钱)
        grid_budget:    复合网格的**虚拟预算** (D3) —— 不是真钱
    """

    positions: list[dict]
    cash_rebalance: float
    grid_budget: float


def project_ledger(
    trades: list[dict],
    as_of_date: str | None = None,
    name_lookup: Optional[dict[str, str]] = None,
    events: Optional[EventStream] = None,
) -> LedgerProjection:
    """Replay `trades` chronologically → the projection.

    Only includes codes with shares > 0 in the returned positions list.
    `as_of_date` (ISO yyyy-mm-dd) is inclusive — trades dated after it are
    skipped. `name_lookup` defaults to the asset universe's names.

    `events` (optional) collects a `TradeRecorded` / `CashTransferred`
    event per applied trade — 账本重放**顺手**把「发生过什么」讲出来。

    ⚠️ 同一天的交易按**追加顺序**播放 (先按日期排, 同日保持原序) ——
    「先买后卖」和「先卖后买」的现金结果不一样。
    """
    if name_lookup is None:
        # 局部 import —— 账本上下文 (BC-A) 不该在**模块级**依赖资产清单
        # (BC-D); 只有真的没给名字表时才去问它。
        from ..portfolio.asset_universe import ETF_DEFS
        name_lookup = {code: v[2] for code, v in ETF_DEFS.items()}
    state: dict = {"cash_rebalance": 0.0, "grid_budget": 0.0, "positions": {}}
    seen_codes: list[str] = []

    # Sort by (date, original_index) so same-day trades play in append order.
    indexed = list(enumerate(trades))
    indexed.sort(key=lambda kv: (kv[1].get("date", ""), kv[0]))

    for _, t in indexed:
        if as_of_date and t.get("date", "") > as_of_date:
            continue
        side = t.get("side")
        code = t.get("code")
        if side in ("buy", "sell") and code not in seen_codes:
            seen_codes.append(code)
        apply_trade(state, t, events)

    positions = [
        {"name": name_lookup.get(c, c), "code": c,
         "shares": state["positions"][c]}
        for c in seen_codes
        if state["positions"].get(c, 0.0) > 0
    ]
    return LedgerProjection(
        positions=positions,
        cash_rebalance=state["cash_rebalance"],
        grid_budget=state["grid_budget"],
    )


__all__ = ["LedgerProjection", "project_ledger"]
