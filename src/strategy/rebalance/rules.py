"""
再平衡策略规则（策略模块）

纯业务规则，无 IO、无状态：

- should_rebalance      权重偏离是否超过阈值（是否该调仓）
- initial_build_orders  首次建仓：按目标权重买入
- plan_rebalance_orders 再平衡调仓：先卖超配，再买低配

回测引擎（strategy/rebalance/backtest.py）只负责"逐日编排 + 记账"，
"何时交易、买多少、卖多少"的判断全部收敛在这里，
保证规则只有一处实现（策略规格文档也指向这里）。
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Mapping

from src.strategy.ledger.values import LotSize

# ETF 最小交易单位（100 股一手）
DEFAULT_LOT_SIZE = 100


@dataclass(frozen=True)
class RebalanceOrder:
    """
    再平衡订单（值对象）

    action: 'buy' 或 'sell'
    """

    date: date
    asset_code: str
    action: str
    shares: Decimal
    price: Decimal
    amount: Decimal
    cost: Decimal

    @property
    def net_amount(self) -> Decimal:
        """净金额（扣除成本）"""
        if self.action == 'buy':
            return self.amount + self.cost
        return self.amount - self.cost


def transaction_cost(amount: Decimal, cost_bps: Decimal) -> Decimal:
    """交易成本 = 成交金额 × 单边费率（基点）"""
    return amount * cost_bps / Decimal("10000")


def should_rebalance(target_weights: Mapping[str, Decimal],
                     current_weights: Mapping[str, Decimal],
                     threshold_pp: Decimal) -> bool:
    """
    是否触发再平衡

    规则：任一标的的实际权重偏离目标权重超过 threshold_pp 个百分点即触发。

    Args:
        target_weights:  目标权重 {code: weight}
        current_weights: 当前权重 {code: weight}
        threshold_pp:    阈值（百分点，如 3 表示 3%）
    """
    threshold = threshold_pp / Decimal("100")
    for code, target in target_weights.items():
        current = current_weights.get(code, Decimal("0"))
        if abs(current - target) > threshold:
            return True
    return False


def initial_build_orders(target_weights: Mapping[str, Decimal],
                         prices: Mapping[str, Decimal],
                         cash: Decimal,
                         cost_bps: Decimal,
                         on_date: date,
                         lot_size: int = DEFAULT_LOT_SIZE) -> List[RebalanceOrder]:
    """
    首次建仓：按目标权重把初始资金分配到各标的

    股数向下取整到 lot_size 的整数倍。
    """
    orders: List[RebalanceOrder] = []

    for code, weight in target_weights.items():
        price = prices.get(code)
        if not price:
            continue

        shares = LotSize(lot_size).floor(cash * weight / price)
        if shares <= 0:
            continue

        amount = shares * price
        orders.append(
            RebalanceOrder(
                date=on_date,
                asset_code=code,
                action='buy',
                shares=shares,
                price=price,
                amount=amount,
                cost=transaction_cost(amount, cost_bps),
            )
        )

    return orders


def plan_rebalance_orders(target_weights: Mapping[str, Decimal],
                          current_values: Mapping[str, Decimal],
                          prices: Mapping[str, Decimal],
                          total_value: Decimal,
                          cost_bps: Decimal,
                          on_date: date,
                          lot_size: int = DEFAULT_LOT_SIZE) -> List[RebalanceOrder]:
    """
    再平衡调仓：卖出超配部分，买入低配部分

    规则：
    - 目标市值 = 总市值 × 目标权重
    - 当前市值 > 目标市值 -> 卖出差额
    - 当前市值 < 目标市值 -> 买入差额
    - 先卖后买（卖出释放现金）
    - 股数向下取整到 lot_size 的整数倍

    Args:
        target_weights:  目标权重 {code: weight}
        current_values:  当前市值 {code: amount}
        prices:          当日收盘价 {code: price}
        total_value:     组合总市值
        cost_bps:        单边交易费率（基点）
        on_date:         成交日期
        lot_size:        最小交易单位
    """
    orders: List[RebalanceOrder] = []

    target_values = {
        code: total_value * weight for code, weight in target_weights.items()
    }

    # ---- 先卖超配 ----
    for code, target_value in target_values.items():
        price = prices.get(code)
        if not price:
            continue

        current_value = current_values.get(code, Decimal("0"))
        if current_value <= target_value:
            continue

        shares = LotSize(lot_size).floor((current_value - target_value) / price)
        if shares <= 0:
            continue

        amount = shares * price
        orders.append(
            RebalanceOrder(
                date=on_date,
                asset_code=code,
                action='sell',
                shares=shares,
                price=price,
                amount=amount,
                cost=transaction_cost(amount, cost_bps),
            )
        )

    # ---- 再买低配 ----
    for code, target_value in target_values.items():
        price = prices.get(code)
        if not price:
            continue

        current_value = current_values.get(code, Decimal("0"))
        if current_value >= target_value:
            continue

        shares = LotSize(lot_size).floor((target_value - current_value) / price)
        if shares <= 0:
            continue

        amount = shares * price
        orders.append(
            RebalanceOrder(
                date=on_date,
                asset_code=code,
                action='buy',
                shares=shares,
                price=price,
                amount=amount,
                cost=transaction_cost(amount, cost_bps),
            )
        )

    return orders
