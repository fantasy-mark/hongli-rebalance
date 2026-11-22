"""
账本投影单元测试（策略模块）

验证「交易账本 -> 站点交易记录」的翻译：
- 交易类型判定（建仓/加仓/减仓/止盈）
- 交易后持仓数量与市值
- 纯现金流水（入金/出金）不进交易记录
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date
from decimal import Decimal

from src.strategy.ledger.book import TradeLedger
from src.strategy.ledger.projection import (
    classify_action,
    project_ledger,
)
from src.strategy.ledger.trade import (
    CashFlowType,
    Trade,
    TradeId,
    TradeSide,
)


def make_trade(trade_id: str,
               on_date: date,
               side: TradeSide,
               code: str = "",
               shares: str = "0",
               price: str = "0",
               pool: CashFlowType = CashFlowType.REAL,
               note: str = "",
               amount: str = "") -> Trade:
    return Trade(
        id=TradeId(trade_id),
        on_date=on_date,
        side=side,
        pool=pool,
        asset_code=code or None,
        shares=Decimal(shares),
        price=Decimal(price),
        amount=Decimal(amount) if amount else Decimal("0"),
        note=note,
    )


class TestClassifyAction(unittest.TestCase):
    """交易类型判定规则"""

    def test_first_buy_is_open(self):
        """买入前无持仓 -> 建仓"""
        self.assertEqual(
            classify_action(TradeSide.BUY, Decimal("0"), Decimal("1000")),
            "open",
        )

    def test_buy_with_position_is_add(self):
        """买入前已有持仓 -> 加仓"""
        self.assertEqual(
            classify_action(TradeSide.BUY, Decimal("1000"), Decimal("2000")),
            "add",
        )

    def test_sell_to_zero_is_take_profit(self):
        """卖出后清零 -> 止盈"""
        self.assertEqual(
            classify_action(TradeSide.SELL, Decimal("1000"), Decimal("0")),
            "take_profit",
        )

    def test_partial_sell_is_reduce(self):
        """卖出后仍有持仓 -> 减仓"""
        self.assertEqual(
            classify_action(TradeSide.SELL, Decimal("1000"), Decimal("400")),
            "reduce",
        )

    def test_cash_flow_has_no_action(self):
        """纯现金流水不进交易记录"""
        self.assertIsNone(classify_action(TradeSide.DEPOSIT, Decimal("0"), Decimal("0")))
        self.assertIsNone(classify_action(TradeSide.WITHDRAW, Decimal("0"), Decimal("0")))


class TestProjectLedger(unittest.TestCase):
    """账本 -> 站点交易记录"""

    def _ledger(self) -> TradeLedger:
        return TradeLedger(trades=[
            make_trade("2026-10-12-deposit-001", date(2026, 10, 12),
                       TradeSide.DEPOSIT, amount="100000"),
            make_trade("2026-10-12-buy-002", date(2026, 10, 12),
                       TradeSide.BUY, "515180", "1000", "1.446"),
            make_trade("2026-10-20-buy-003", date(2026, 10, 20),
                       TradeSide.BUY, "515180", "500", "1.500"),
            make_trade("2026-10-25-sell-004", date(2026, 10, 25),
                       TradeSide.SELL, "515180", "1500", "1.600"),
        ])

    def test_cash_flow_not_in_trades(self):
        """入金不产生交易记录"""
        rows = project_ledger(self._ledger())
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["action"] in ("open", "add", "reduce", "take_profit")
                            for r in rows))

    def test_action_sequence(self):
        """建仓 -> 加仓 -> 止盈"""
        rows = project_ledger(self._ledger())
        self.assertEqual([r["action"] for r in rows],
                         ["open", "add", "take_profit"])
        self.assertEqual([r["action_label"] for r in rows],
                         ["建仓", "加仓", "止盈"])

    def test_position_shares_and_value(self):
        """交易后的持仓数量与市值"""
        rows = project_ledger(self._ledger())

        self.assertEqual(rows[0]["position_shares"], 1000.0)
        self.assertEqual(rows[0]["position_value"], 1000.0 * 1.446)

        self.assertEqual(rows[1]["position_shares"], 1500.0)
        self.assertEqual(rows[2]["position_shares"], 0.0)
        self.assertEqual(rows[2]["position_value"], 0.0)

    def test_pool_maps_to_strategy(self):
        """资金池 -> 策略"""
        ledger = TradeLedger(trades=[
            make_trade("2026-10-12-buy-001", date(2026, 10, 12),
                       TradeSide.BUY, "515180", "1000", "1.446",
                       pool=CashFlowType.REAL),
            make_trade("2026-10-12-buy-002", date(2026, 10, 12),
                       TradeSide.BUY, "159937", "1000", "8.5",
                       pool=CashFlowType.GRID),
        ])
        rows = project_ledger(ledger)
        self.assertEqual(rows[0]["strategy"], "rebalance")
        self.assertEqual(rows[1]["strategy"], "grid")

    def test_pools_are_isolated(self):
        """两个资金池的持仓互不影响"""
        ledger = TradeLedger(trades=[
            make_trade("2026-10-12-buy-001", date(2026, 10, 12),
                       TradeSide.BUY, "515180", "1000", "1.446",
                       pool=CashFlowType.REAL),
            make_trade("2026-10-12-buy-002", date(2026, 10, 12),
                       TradeSide.BUY, "515180", "1000", "1.446",
                       pool=CashFlowType.GRID),
        ])
        rows = project_ledger(ledger)
        # 两笔都是各自池的首次买入 -> 都是建仓
        self.assertEqual([r["action"] for r in rows], ["open", "open"])

    def test_legacy_deposit_side_is_normalized(self):
        """兼容旧账本的 deposit_rebalance / deposit_grid"""
        legacy = {
            "trades": [
                {"id": "2026-10-12-deposit_rebalance-001", "date": "2026-10-12",
                 "side": "deposit_rebalance", "amount": 175900.6},
                {"id": "2026-10-12-buy-002", "date": "2026-10-12",
                 "side": "buy", "code": "515180", "shares": 1000,
                 "price": 1.446, "fee": 0.0},
            ]
        }
        ledger = TradeLedger.from_dict(legacy)
        rows = project_ledger(ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["strategy"], "rebalance")


if __name__ == "__main__":
    unittest.main()
