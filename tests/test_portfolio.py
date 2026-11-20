"""
组合与持仓模型测试（离线）

覆盖:
- Position / Portfolio 的记账与不变量
- 再平衡组合与网格组合的持仓隔离
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from decimal import Decimal

from src.strategy.portfolio import Portfolio, Position


class TestPosition(unittest.TestCase):

    def test_average_cost(self):
        """持仓均价含费用"""
        position = Position(asset_code="515180", shares=Decimal("100"),
                            cost_basis=Decimal("1005"))
        self.assertAlmostEqual(float(position.average_cost), 10.05, places=6)

    def test_empty_position_average_cost_zero(self):
        self.assertEqual(Position(asset_code="515180").average_cost, Decimal("0"))

    def test_negative_shares_rejected(self):
        with self.assertRaises(ValueError):
            Position(asset_code="515180", shares=Decimal("-1"))


class TestPortfolio(unittest.TestCase):

    def setUp(self):
        self.portfolio = Portfolio(portfolio_id="test", name="测试组合",
                                   cash=Decimal("100000"))

    def test_buy_updates_cash_and_position(self):
        """买入扣现金、加持仓与成本"""
        position = self.portfolio.buy("515180", Decimal("100"),
                                      Decimal("1000"), Decimal("5"))

        self.assertEqual(self.portfolio.cash, Decimal("98995"))
        self.assertEqual(position.shares, Decimal("100"))
        self.assertEqual(position.cost_basis, Decimal("1005"))

    def test_sell_updates_cash_and_realized_pnl(self):
        """卖出加现金、减持仓、结算已实现盈亏"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("5"))
        position = self.portfolio.sell("515180", Decimal("100"),
                                       Decimal("1100"), Decimal("6"))

        self.assertEqual(self.portfolio.cash, Decimal("98995") + Decimal("1094"))
        self.assertEqual(position.shares, Decimal("0"))
        self.assertEqual(position.cost_basis, Decimal("0"))
        # 1094 - 1005
        self.assertEqual(position.realized_pnl, Decimal("89"))

    def test_sell_more_than_held_raises(self):
        """卖出超过持仓 -> 拒绝（关键不变量）"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("5"))
        with self.assertRaises(ValueError):
            self.portfolio.sell("515180", Decimal("200"), Decimal("2000"))

    def test_sell_without_position_raises(self):
        """空仓卖出 -> 拒绝"""
        with self.assertRaises(ValueError):
            self.portfolio.sell("515180", Decimal("1"), Decimal("10"))

    def test_negative_shares_rejected(self):
        with self.assertRaises(ValueError):
            self.portfolio.buy("515180", Decimal("0"), Decimal("0"))

    def test_market_value_and_total_value(self):
        """市值与总市值"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("5"))
        prices = {"515180": Decimal("11")}

        self.assertEqual(self.portfolio.market_value(prices), Decimal("1100"))
        self.assertEqual(self.portfolio.total_value(prices),
                         self.portfolio.cash + Decimal("1100"))

    def test_unrealized_and_total_pnl(self):
        """浮动盈亏 = 市值 - 成本；总盈亏含已实现"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("5"))
        prices = {"515180": Decimal("11")}

        self.assertEqual(self.portfolio.unrealized_pnl(prices), Decimal("95"))
        self.assertEqual(self.portfolio.realized_pnl(), Decimal("0"))

        self.portfolio.sell("515180", Decimal("50"), Decimal("550"), Decimal("3"))
        # 已实现 = (550-3) - 1005*0.5 = 547 - 502.5 = 44.5
        self.assertAlmostEqual(float(self.portfolio.realized_pnl()), 44.5, places=6)

    def test_weights(self):
        """权重按总市值计算"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("0"))
        self.portfolio.buy("159937", Decimal("100"), Decimal("1000"), Decimal("0"))
        weights = self.portfolio.weights({"515180": Decimal("10"),
                                           "159937": Decimal("10")})

        # 现金 98000 + 持仓 2000 = 100000 -> 各占 1000/100000
        self.assertAlmostEqual(float(weights["515180"]), 0.01, places=6)
        self.assertAlmostEqual(float(weights["159937"]), 0.01, places=6)

    def test_position_snapshot(self):
        """持仓快照"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("0"))
        self.assertEqual(self.portfolio.position_snapshot(),
                         {"515180": Decimal("100")})

    def test_is_flat(self):
        """空仓判定"""
        self.assertTrue(self.portfolio.is_flat("515180"))
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("0"))
        self.assertFalse(self.portfolio.is_flat("515180"))

    def test_cost_basis_released_exact(self):
        """可指定精确释放的持仓成本（网格按批次平仓）"""
        self.portfolio.buy("515180", Decimal("100"), Decimal("1000"), Decimal("5"))
        position = self.portfolio.sell("515180", Decimal("100"),
                                       Decimal("1100"), Decimal("6"),
                                       cost_basis_released=Decimal("1005"))
        self.assertEqual(position.cost_basis, Decimal("0"))
        self.assertEqual(position.realized_pnl, Decimal("89"))


class TestPortfolioIsolation(unittest.TestCase):
    """
    两个策略的持仓隔离

    再平衡组合与网格组合各持一个 Portfolio 实例，
    持仓、现金、盈亏互不影响（类型层面的隔离）。
    """

    def test_two_portfolios_are_independent(self):
        rebalance = Portfolio(portfolio_id="rebalance", name="再平衡组合",
                              cash=Decimal("1000000"))
        grid = Portfolio(portfolio_id="grid", name="网格组合",
                         cash=Decimal("200000"))

        rebalance.buy("515180", Decimal("1000"), Decimal("10000"), Decimal("50"))
        grid.buy("515180", Decimal("100"), Decimal("1000"), Decimal("5"))

        self.assertEqual(rebalance.shares_of("515180"), Decimal("1000"))
        self.assertEqual(grid.shares_of("515180"), Decimal("100"))

        self.assertEqual(rebalance.cash, Decimal("990000") - Decimal("50"))
        self.assertEqual(grid.cash, Decimal("199000") - Decimal("5"))

        # 各自盈亏独立
        prices = {"515180": Decimal("11")}
        self.assertEqual(rebalance.market_value(prices), Decimal("11000"))
        self.assertEqual(grid.market_value(prices), Decimal("1100"))

    def test_sell_in_one_does_not_affect_other(self):
        rebalance = Portfolio(portfolio_id="rebalance", name="再平衡组合",
                              cash=Decimal("100000"))
        grid = Portfolio(portfolio_id="grid", name="网格组合",
                         cash=Decimal("100000"))

        rebalance.buy("515180", Decimal("100"), Decimal("1000"), Decimal("0"))
        grid.buy("515180", Decimal("100"), Decimal("1000"), Decimal("0"))

        rebalance.sell("515180", Decimal("100"), Decimal("1100"), Decimal("0"))

        self.assertEqual(rebalance.shares_of("515180"), Decimal("0"))
        self.assertEqual(grid.shares_of("515180"), Decimal("100"))
        self.assertEqual(rebalance.realized_pnl(), Decimal("100"))
        self.assertEqual(grid.realized_pnl(), Decimal("0"))

    def test_grid_and_rebalance_use_distinct_portfolios(self):
        """网格策略持有自己的 Portfolio（与再平衡组合不同实例）"""
        from src.strategy.grid.models import GridStrategy
        from src.strategy.grid.models import GridTrigger

        strategy = GridStrategy(
            name="网格",
            trigger=GridTrigger(),
            asset_codes=["515180"],
            portfolio=Portfolio(portfolio_id="grid", name="网格组合",
                                cash=Decimal("200000")),
        )

        self.assertEqual(strategy.portfolio.portfolio_id, "grid")
        self.assertNotEqual(strategy.portfolio.portfolio_id, "rebalance")
        self.assertEqual(strategy.portfolio.name, "网格组合")


if __name__ == '__main__':
    unittest.main()
