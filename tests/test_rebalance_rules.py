"""
再平衡规则单元测试（策略模块）

覆盖 `src/strategy/rebalance/rules.py` 的三条核心规则：
- should_rebalance      权重偏离是否超阈值
- initial_build_orders  首次建仓（按目标权重、整手取整）
- plan_rebalance_orders 调仓（先卖超配、再买低配）

全部离线，不访问网络。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date
from decimal import Decimal

from src.strategy.rebalance.rules import (
    RebalanceOrder,
    initial_build_orders,
    plan_rebalance_orders,
    should_rebalance,
    transaction_cost,
)

TODAY = date(2024, 6, 5)
COST_BPS = Decimal("5")          # 0.05%
LOT = 100


class TestShouldRebalance(unittest.TestCase):
    """触发判定：任一标的偏离目标权重超过阈值即触发"""

    def test_no_deviation_no_trigger(self):
        target = {"515180": Decimal("0.5"), "159937": Decimal("0.5")}
        current = {"515180": Decimal("0.5"), "159937": Decimal("0.5")}
        self.assertFalse(should_rebalance(target, current, Decimal("3")))

    def test_deviation_below_threshold_no_trigger(self):
        target = {"515180": Decimal("0.5"), "159937": Decimal("0.5")}
        current = {"515180": Decimal("0.52"), "159937": Decimal("0.48")}
        self.assertFalse(should_rebalance(target, current, Decimal("3")))

    def test_deviation_above_threshold_triggers(self):
        target = {"515180": Decimal("0.5"), "159937": Decimal("0.5")}
        current = {"515180": Decimal("0.55"), "159937": Decimal("0.45")}
        self.assertTrue(should_rebalance(target, current, Decimal("3")))

    def test_deviation_exactly_at_threshold_does_not_trigger(self):
        """规则是「超过」阈值，等于阈值不触发"""
        target = {"515180": Decimal("0.5")}
        current = {"515180": Decimal("0.53")}
        self.assertFalse(should_rebalance(target, current, Decimal("3")))

    def test_missing_position_counts_as_zero_weight(self):
        target = {"515180": Decimal("1")}
        current = {}
        self.assertTrue(should_rebalance(target, current, Decimal("3")))


class TestInitialBuild(unittest.TestCase):
    """首次建仓：按目标权重分配初始资金"""

    def test_allocates_by_target_weight(self):
        orders = initial_build_orders(
            target_weights={"515180": Decimal("0.5"), "159937": Decimal("0.5")},
            prices={"515180": Decimal("2"), "159937": Decimal("4")},
            cash=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
            lot_size=LOT,
        )

        self.assertEqual(len(orders), 2)
        by_code = {o.asset_code: o for o in orders}

        # 5 万 / 2 元 = 25000 股 -> 整手取整 25000
        self.assertEqual(by_code["515180"].shares, Decimal("25000"))
        self.assertEqual(by_code["515180"].amount, Decimal("50000"))
        self.assertEqual(by_code["515180"].action, "buy")

        # 5 万 / 4 元 = 12500 股 -> 整手取整 12500
        self.assertEqual(by_code["159937"].shares, Decimal("12500"))
        self.assertEqual(by_code["159937"].amount, Decimal("50000"))

    def test_shares_floor_to_lot(self):
        """股数向下取整到整手"""
        orders = initial_build_orders(
            target_weights={"515180": Decimal("1")},
            prices={"515180": Decimal("3")},
            cash=Decimal("1000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
            lot_size=LOT,
        )

        # 1000 / 3 = 333.33 股 -> 300 股
        self.assertEqual(orders[0].shares, Decimal("300"))
        self.assertEqual(orders[0].amount, Decimal("900"))

    def test_skips_asset_without_price(self):
        orders = initial_build_orders(
            target_weights={"515180": Decimal("0.5"), "159937": Decimal("0.5")},
            prices={"515180": Decimal("2")},
            cash=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
        )
        self.assertEqual([o.asset_code for o in orders], ["515180"])

    def test_cost_is_amount_times_bps(self):
        orders = initial_build_orders(
            target_weights={"515180": Decimal("1")},
            prices={"515180": Decimal("2")},
            cash=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
        )
        self.assertEqual(orders[0].cost, Decimal("100000") * COST_BPS / Decimal("10000"))


class TestPlanRebalanceOrders(unittest.TestCase):
    """调仓：先卖超配，再买低配"""

    def test_sells_overweight_before_buying_underweight(self):
        orders = plan_rebalance_orders(
            target_weights={"515180": Decimal("0.5"), "159937": Decimal("0.5")},
            current_values={"515180": Decimal("70000"), "159937": Decimal("30000")},
            prices={"515180": Decimal("2"), "159937": Decimal("2")},
            total_value=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
            lot_size=LOT,
        )

        # 先卖超配的 515180
        self.assertEqual(orders[0].asset_code, "515180")
        self.assertEqual(orders[0].action, "sell")
        # (70000 - 50000) / 2 = 10000 股
        self.assertEqual(orders[0].shares, Decimal("10000"))

        # 再买低配的 159937
        self.assertEqual(orders[1].asset_code, "159937")
        self.assertEqual(orders[1].action, "buy")
        # (50000 - 30000) / 2 = 10000 股
        self.assertEqual(orders[1].shares, Decimal("10000"))

    def test_no_orders_when_weights_match(self):
        orders = plan_rebalance_orders(
            target_weights={"515180": Decimal("1")},
            current_values={"515180": Decimal("100000")},
            prices={"515180": Decimal("2")},
            total_value=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
        )
        self.assertEqual(orders, [])

    def test_skips_asset_without_price(self):
        orders = plan_rebalance_orders(
            target_weights={"515180": Decimal("0.5"), "159937": Decimal("0.5")},
            current_values={"515180": Decimal("70000"), "159937": Decimal("30000")},
            prices={"515180": Decimal("2")},   # 159937 无行情
            total_value=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
        )
        self.assertEqual([o.asset_code for o in orders], ["515180"])

    def test_zero_amount_is_skipped(self):
        """差额不足一手时不产生订单"""
        orders = plan_rebalance_orders(
            target_weights={"515180": Decimal("1")},
            current_values={"515180": Decimal("100000")},
            prices={"515180": Decimal("2")},
            total_value=Decimal("100000"),
            cost_bps=COST_BPS,
            on_date=TODAY,
        )
        self.assertEqual(orders, [])

    def test_order_is_immutable_value_object(self):
        order = RebalanceOrder(
            date=TODAY,
            asset_code="515180",
            action="buy",
            shares=Decimal("100"),
            price=Decimal("2"),
            amount=Decimal("200"),
            cost=Decimal("1"),
        )
        with self.assertRaises(Exception):
            order.shares = Decimal("0")

    def test_net_amount_accounts_for_cost(self):
        buy = RebalanceOrder(TODAY, "515180", "buy", Decimal("100"),
                             Decimal("2"), Decimal("200"), Decimal("1"))
        sell = RebalanceOrder(TODAY, "515180", "sell", Decimal("100"),
                              Decimal("2"), Decimal("200"), Decimal("1"))
        self.assertEqual(buy.net_amount, Decimal("201"))
        self.assertEqual(sell.net_amount, Decimal("199"))


class TestTransactionCost(unittest.TestCase):
    """交易成本：金额 × 基点"""

    def test_zero_amount(self):
        self.assertEqual(transaction_cost(Decimal("0"), COST_BPS), Decimal("0"))

    def test_bps_conversion(self):
        self.assertEqual(
            transaction_cost(Decimal("10000"), Decimal("5")),
            Decimal("5"),
        )


if __name__ == "__main__":
    unittest.main()
