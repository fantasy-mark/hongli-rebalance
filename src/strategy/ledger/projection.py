"""
账本投影（策略模块 · 共享内核）

把**交易账本**（追加式流水，真实交易的唯一真相源）翻译成
「站点交易记录」视图行，供图表模块展示。

为什么需要这一层：
- 账本只记「买了/卖了多少」，不知道业务上这是**建仓/加仓/减仓/止盈**
- 站点需要业务语义（交易类型）+ 交易后的持仓数量/市值
- 这些判定属于业务规则，因此放在策略模块，而不是前端

判定规则（与网格策略 GridState 的判定一致）：
- 买入：该资产当前无持仓 -> 建仓(open)；已有持仓 -> 加仓(add)
- 卖出：卖出后该资产清零 -> 止盈(take_profit)；仍有持仓 -> 减仓(reduce)
- 入金/出金/分红：纯现金流水，不产生持仓交易，不进交易记录
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.strategy.ledger.book import TradeLedger
from src.strategy.ledger.trade import CashFlowType, TradeSide

# 资金池 -> 站点策略标识
POOL_TO_STRATEGY = {
    CashFlowType.REAL: "rebalance",
    CashFlowType.GRID: "grid",
}

# 资金池 -> 策略显示名
POOL_TO_STRATEGY_NAME = {
    CashFlowType.REAL: "阈值再平衡",
    CashFlowType.GRID: "复合网格",
}

# 交易类型标签（前端直接用中文，不再二次映射）
ACTION_LABELS = {
    "open": "建仓",
    "add": "加仓",
    "reduce": "减仓",
    "take_profit": "止盈",
}


def classify_action(side: TradeSide,
                    shares_before: Decimal,
                    shares_after: Decimal) -> Optional[str]:
    """
    判定交易类型（业务规则）

    Args:
        side:          流水方向
        shares_before: 本笔成交**前**的持仓数量
        shares_after:  本笔成交**后**的持仓数量

    Returns:
        'open' / 'add' / 'reduce' / 'take_profit'；
        纯现金流水返回 None（不进交易记录）
    """
    if side is TradeSide.BUY:
        # 买入前无持仓 -> 建仓；已有持仓 -> 加仓
        return "open" if shares_before <= 0 else "add"
    if side is TradeSide.SELL:
        # 卖出后清零 -> 止盈；仍有持仓 -> 减仓
        return "take_profit" if shares_after <= 0 else "reduce"
    return None


def project_ledger(ledger: TradeLedger) -> List[Dict[str, Any]]:
    """
    账本 -> 站点交易记录行

    与账本同序（追加式，先发生的在前）；
    每行带交易后的持仓数量与市值。

    Returns:
        [{"date","strategy","strategy_name","asset_code","asset_name",
          "action","action_label","side","price","shares","amount",
          "position_shares","position_value","note"}, ...]
    """
    rows: List[Dict[str, Any]] = []

    # 资金池 -> {资产代码: 持仓数量}
    holdings: Dict[CashFlowType, Dict[str, Decimal]] = {
        pool: {} for pool in CashFlowType
    }

    for trade in ledger.trades:
        if not trade.side.is_security:
            # 纯现金流水：不影响持仓，不进交易记录
            continue

        code = trade.asset_code or ""
        pool_holdings = holdings[trade.pool]
        shares_before = pool_holdings.get(code, Decimal("0"))

        if trade.side is TradeSide.BUY:
            pool_holdings[code] = shares_before + trade.shares
        else:
            pool_holdings[code] = shares_before - trade.shares

        shares_after = pool_holdings[code]
        action = classify_action(trade.side, shares_before, shares_after)
        if action is None:
            continue

        rows.append({
            "id": trade.id.value,
            "date": trade.on_date.isoformat(),
            "strategy": POOL_TO_STRATEGY[trade.pool],
            "strategy_name": POOL_TO_STRATEGY_NAME[trade.pool],
            "asset_code": code,
            "asset_name": code,
            "action": action,
            "action_label": ACTION_LABELS[action],
            "side": "buy" if trade.side is TradeSide.BUY else "sell",
            "round_no": 0,
            "price": float(trade.price),
            "shares": float(trade.shares),
            "amount": float(trade.amount),
            "fee": float(trade.fee),
            "step": None,
            "position_shares": float(shares_after),
            "position_value": float(shares_after * trade.price),
            "note": trade.note,
        })

    return rows
