"""
账本统计（策略模块 · 账本的派生视图）

**不落盘**：全部由 `data/trades.json` 现算，账本仍是唯一真相源。

统计口径（前端与 API 共用同一套定义，避免两处算出不同数字）：

| 指标 | 定义 |
|---|---|
| 初始资金 | 该池的初始资金（真实现金 / 网格虚拟预算），**是总金额** |
| 现金余额 | 初始资金 + 卖出收入 − 买入支出 − 费用 |
| 持仓市值 | Σ(各资产 数量 × 最新价) |
| 总资产 | 现金余额 + 持仓市值 |
| 累计收益率 | (总资产 − 初始资金) / 初始资金 |

建仓 / 加仓不得让现金余额变负：超出总金额即拒绝（见 `TradeLedger.validate`）。
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from src.strategy.ledger.book import TradeLedger
from src.strategy.ledger.capital import DEFAULT_INITIAL_CAPITALS
from src.strategy.ledger.trade import CashFlowType, Trade, TradeSide


@dataclass(frozen=True)
class PositionView:
    """单个资产的持仓视图"""

    code: str
    shares: Decimal
    price: Decimal          # 最新价（无行情时回退到最近成交价）
    market_value: Decimal  # 数量 × 最新价
    weight: Decimal        # 占最新总市值比例
    cost: Decimal = Decimal("0")   # 持仓成本（含费用）
    average_cost: Decimal = Decimal("0")

    def to_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "shares": float(self.shares),
            "price": float(self.price),
            "market_value": float(self.market_value),
            "weight": float(self.weight),
            "cost": float(self.cost),
            "average_cost": float(self.average_cost),
        }


@dataclass(frozen=True)
class PoolStats:
    """单个资金池的统计"""

    pool: CashFlowType
    initial_capital: Decimal
    cash: Decimal
    market_value: Decimal
    total_value: Decimal
    total_return: Decimal
    positions: List[PositionView] = field(default_factory=list)
    buy_count: int = 0
    sell_count: int = 0
    trade_count: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "pool": self.pool.value,
            "label": self.pool.label,
            "initial_capital": float(self.initial_capital),
            "cash": float(self.cash),
            "market_value": float(self.market_value),
            "total_value": float(self.total_value),
            "total_return": float(self.total_return),
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "trade_count": self.trade_count,
            "positions": [p.to_dict() for p in self.positions],
        }


def _latest_price(
    code: str,
    trades: List[Trade],
    prices: Optional[Dict[str, Decimal]],
) -> Decimal:
    """最新价：优先用行情价；没有行情就用该资产最近一笔成交价"""
    if prices:
        price = prices.get(code)
        if price is not None:
            return Decimal(str(price))

    for trade in reversed(trades):
        if trade.asset_code == code and trade.price > 0:
            return trade.price

    return Decimal("0")


def compute_pool_stats(
    pool: CashFlowType,
    trades: List[Trade],
    prices: Optional[Dict[str, Decimal]] = None,
    initial_capital: Optional[Decimal] = None,
) -> PoolStats:
    """
    计算单个资金池的统计

    Args:
        pool: 资金池
        trades: 该池的流水（任意顺序，内部会按日期稳定排序）
        prices: 各资产最新价 {code: price}
        initial_capital: 该池初始资金（总金额）；缺省取默认配置
    """
    capital = Decimal(str(initial_capital)) if initial_capital is not None \
        else DEFAULT_INITIAL_CAPITALS.get(pool, Decimal("0"))

    ordered = sorted(trades, key=lambda t: t.on_date)

    cash = capital
    shares: Dict[str, Decimal] = {}
    cost: Dict[str, Decimal] = {}
    buy_count = 0
    sell_count = 0

    for trade in ordered:
        code = trade.asset_code or ""

        if trade.side is TradeSide.DEPOSIT:
            cash += trade.amount
            continue
        if trade.side is TradeSide.WITHDRAW:
            cash -= trade.amount
            continue
        if trade.side is TradeSide.DIVIDEND:
            cash += trade.amount
            continue

        if trade.side is TradeSide.BUY:
            cash -= trade.gross_amount + trade.fee
            shares[code] = shares.get(code, Decimal("0")) + trade.shares
            cost[code] = cost.get(code, Decimal("0")) + trade.gross_amount + trade.fee
            buy_count += 1
            continue

        if trade.side is TradeSide.SELL:
            cash += trade.gross_amount - trade.fee
            before = shares.get(code, Decimal("0"))
            if before > 0:
                # 按均价比例释放成本
                released = cost.get(code, Decimal("0")) * trade.shares / before
            else:
                released = Decimal("0")
            shares[code] = before - trade.shares
            cost[code] = cost.get(code, Decimal("0")) - released
            sell_count += 1

    # 持仓市值（用最新价）
    position_list: List[PositionView] = []
    market_value = Decimal("0")
    for code in sorted(shares.keys()):
        qty = shares[code]
        if qty <= 0:
            continue
        price = _latest_price(code, ordered, prices)
        value = qty * price
        market_value += value
        basis = cost.get(code, Decimal("0"))
        position_list.append(PositionView(
            code=code,
            shares=qty,
            price=price,
            market_value=value,
            weight=Decimal("0"),  # 稍后按总市值回填
            cost=basis,
            average_cost=basis / qty if qty > 0 else Decimal("0"),
        ))

    # 权重 = 各资产市值 / 最新总市值
    if market_value > 0:
        position_list = [
            PositionView(
                code=p.code, shares=p.shares, price=p.price,
                market_value=p.market_value,
                weight=p.market_value / market_value,
                cost=p.cost, average_cost=p.average_cost,
            )
            for p in position_list
        ]

    total_value = cash + market_value
    total_return = (total_value - capital) / capital if capital > 0 else Decimal("0")

    return PoolStats(
        pool=pool,
        initial_capital=capital,
        cash=cash,
        market_value=market_value,
        total_value=total_value,
        total_return=total_return,
        positions=position_list,
        buy_count=buy_count,
        sell_count=sell_count,
        trade_count=len(ordered),
    )


def compute_all_stats(
    ledger: TradeLedger,
    prices: Optional[Dict[str, Decimal]] = None,
) -> Dict[CashFlowType, PoolStats]:
    """计算所有资金池的统计"""
    result: Dict[CashFlowType, PoolStats] = {}
    for pool in CashFlowType:
        pool_trades = ledger.trades_of(pool=pool)
        capital = ledger.initial_capitals.get(pool) \
            if ledger.initial_capitals else DEFAULT_INITIAL_CAPITALS.get(pool)
        result[pool] = compute_pool_stats(pool, pool_trades, prices, capital)
    return result
