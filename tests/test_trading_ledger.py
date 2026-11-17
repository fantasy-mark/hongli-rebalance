"""
交易账本与流水测试（离线）

覆盖:
- Trade 值对象不变量与推导（cash_delta / shares_delta）
- 旧账本方向字符串兼容（deposit_rebalance -> DEPOSIT + REAL 池）
- 双资金池隔离
- 追加式与大账本重演
- 账本自洽性校验（卖出超持仓）
- 用真实参考账本回归：重演结果与持仓文件一致
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date
from decimal import Decimal

from src.domain.trading.trade import (
    CashFlowType,
    Trade,
    TradeId,
    TradeSide,
    infer_pool,
)
from src.domain.trading.ledger import LedgerViolation, TradeLedger


def make_trade(side, on=date(2024, 1, 2), pool=CashFlowType.REAL,
               code="515180", shares="100", price="10", amount="0",
               fee="0", seq=1, note=""):
    """构造流水"""
    return Trade(
        id=TradeId.build(on, side, seq),
        on_date=on,
        side=side,
        pool=pool,
        asset_code=code if side.is_security else None,
        shares=Decimal(shares) if side.is_security else Decimal("0"),
        price=Decimal(price) if side.is_security else Decimal("0"),
        amount=Decimal(amount),
        fee=Decimal(fee),
        note=note,
    )


class TestTradeId(unittest.TestCase):

    def test_build_format(self):
        tid = TradeId.build(date(2026, 10, 19), TradeSide.SELL, 2)
        self.assertEqual(str(tid), "2026-10-19-sell-002")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            TradeId("  ")


class TestTrade(unittest.TestCase):

    def test_buy_cash_delta(self):
        """买入：-(成交金额 + 费用)"""
        trade = make_trade(TradeSide.BUY, shares="100", price="10", fee="5")
        self.assertEqual(trade.cash_delta, Decimal("-1005"))
        self.assertEqual(trade.shares_delta, Decimal("100"))

    def test_sell_cash_delta(self):
        """卖出：+(成交金额 - 费用)"""
        trade = make_trade(TradeSide.SELL, shares="100", price="11", fee="6")
        self.assertEqual(trade.cash_delta, Decimal("1094"))
        self.assertEqual(trade.shares_delta, Decimal("-100"))

    def test_deposit_cash_delta(self):
        """入金：+金额，不动持仓"""
        trade = make_trade(TradeSide.DEPOSIT, amount="10000")
        self.assertEqual(trade.cash_delta, Decimal("10000"))
        self.assertEqual(trade.shares_delta, Decimal("0"))

    def test_withdraw_cash_delta(self):
        """出金：-金额"""
        trade = make_trade(TradeSide.WITHDRAW, amount="5000")
        self.assertEqual(trade.cash_delta, Decimal("-5000"))

    def test_dividend_cash_delta(self):
        """分红：+金额，不动持仓"""
        trade = make_trade(TradeSide.DIVIDEND, amount="120")
        self.assertEqual(trade.cash_delta, Decimal("120"))
        self.assertEqual(trade.shares_delta, Decimal("0"))

    def test_security_requires_code(self):
        """证券买卖必须有代码"""
        with self.assertRaises(ValueError):
            Trade(id=TradeId("x"), on_date=date(2024, 1, 1),
                  side=TradeSide.BUY, asset_code=None,
                  shares=Decimal("100"), price=Decimal("10"))

    def test_security_requires_positive_shares(self):
        with self.assertRaises(ValueError):
            Trade(id=TradeId("x"), on_date=date(2024, 1, 1),
                  side=TradeSide.BUY, asset_code="515180",
                  shares=Decimal("0"), price=Decimal("10"))

    def test_cash_flow_requires_amount(self):
        """出入金金额不能为 0"""
        with self.assertRaises(ValueError):
            Trade(id=TradeId("x"), on_date=date(2024, 1, 1),
                  side=TradeSide.DEPOSIT, amount=Decimal("0"))

    def test_amount_derived_from_shares_and_price(self):
        """未指定 amount 时按 shares*price 推导"""
        trade = make_trade(TradeSide.BUY, shares="100", price="10")
        self.assertEqual(trade.amount, Decimal("1000"))

    def test_negative_fee_rejected(self):
        with self.assertRaises(ValueError):
            make_trade(TradeSide.BUY, fee="-1")


class TestLegacySideCompat(unittest.TestCase):
    """旧账本方向字符串的归一化"""

    def test_deposit_rebalance_maps_to_real_pool(self):
        raw = {
            'id': '2026-10-12-deposit_rebalance-001',
            'date': '2026-10-12',
            'side': 'deposit_rebalance',
            'amount': 175900.6,
        }
        ledger = TradeLedger.from_dict({'trades': [raw]})
        trade = ledger.trades[0]

        self.assertEqual(trade.side, TradeSide.DEPOSIT)
        self.assertEqual(trade.pool, CashFlowType.REAL)

    def test_deposit_grid_maps_to_grid_pool(self):
        raw = {
            'id': '2026-10-30-deposit_grid-001',
            'date': '2026-10-30',
            'side': 'deposit_grid',
            'amount': 15000.0,
        }
        ledger = TradeLedger.from_dict({'trades': [raw]})
        trade = ledger.trades[0]

        self.assertEqual(trade.side, TradeSide.DEPOSIT)
        self.assertEqual(trade.pool, CashFlowType.GRID)

    def test_infer_pool(self):
        self.assertEqual(infer_pool('deposit_grid'), CashFlowType.GRID)
        self.assertEqual(infer_pool('deposit_rebalance'), CashFlowType.REAL)
        self.assertEqual(infer_pool('buy'), CashFlowType.REAL)


class TestLedgerReplay(unittest.TestCase):

    def test_deposit_then_buy(self):
        """入金 + 买入 -> 现金减少、持仓增加"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="10000", seq=1))
        ledger.record(make_trade(TradeSide.BUY, shares="100", price="10",
                                 fee="5", seq=2))

        snap = ledger.replay()
        portfolio = snap.portfolio(CashFlowType.REAL)

        self.assertEqual(portfolio.cash, Decimal("10000") - Decimal("1005"))
        self.assertEqual(portfolio.shares_of("515180"), Decimal("100"))

    def test_realized_pnl_from_sell(self):
        """卖出结算已实现盈亏（含费用）"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="10000", seq=1))
        ledger.record(make_trade(TradeSide.BUY, shares="100", price="10",
                                 fee="5", seq=2))
        ledger.record(make_trade(TradeSide.SELL, on=date(2024, 1, 3),
                                 shares="100", price="11", fee="6", seq=1))

        snap = ledger.replay()
        balance = snap.balances[CashFlowType.REAL]

        # (1100-6) - 1005 = 89
        self.assertEqual(balance.realized_pnl, Decimal("89"))

    def test_two_pools_are_independent(self):
        """双资金池互不挪用"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="100000", seq=1))
        ledger.record(make_trade(TradeSide.DEPOSIT, pool=CashFlowType.GRID,
                                 amount="20000", seq=2))
        ledger.record(make_trade(TradeSide.BUY, shares="100", price="10", seq=3))
        ledger.record(make_trade(TradeSide.BUY, pool=CashFlowType.GRID,
                                 shares="50", price="10", seq=4))

        snap = ledger.replay()

        self.assertEqual(snap.portfolio(CashFlowType.REAL).cash,
                         Decimal("99000"))
        self.assertEqual(snap.portfolio(CashFlowType.GRID).cash,
                         Decimal("19500"))
        self.assertEqual(snap.portfolio(CashFlowType.REAL).shares_of("515180"),
                         Decimal("100"))
        self.assertEqual(snap.portfolio(CashFlowType.GRID).shares_of("515180"),
                         Decimal("50"))

    def test_sell_exceeding_position_rejected(self):
        """卖出超持仓 -> 重演时报错"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="10000", seq=1))
        ledger.record(make_trade(TradeSide.BUY, shares="100", price="10", seq=2))
        ledger.record(make_trade(TradeSide.SELL, on=date(2024, 1, 3),
                                 shares="200", price="10", seq=1))

        with self.assertRaises(LedgerViolation):
            ledger.replay()

    def test_duplicate_id_rejected(self):
        ledger = TradeLedger()
        trade = make_trade(TradeSide.DEPOSIT, amount="1000")
        ledger.record(trade)
        with self.assertRaises(ValueError):
            ledger.record(trade)

    def test_replay_as_of_filters_by_date(self):
        """按截止日期重演"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="10000", seq=1))
        ledger.record(make_trade(TradeSide.BUY, on=date(2024, 2, 1),
                                 shares="100", price="10", seq=1))

        snap = ledger.replay(as_of=date(2024, 1, 31))
        self.assertEqual(snap.portfolio(CashFlowType.REAL).cash,
                         Decimal("10000"))
        self.assertEqual(snap.portfolio(CashFlowType.REAL).shares_of("515180"),
                         Decimal("0"))

    def test_total_positions_merges_pools(self):
        """跨池持仓合计"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="100000", seq=1))
        ledger.record(make_trade(TradeSide.DEPOSIT, pool=CashFlowType.GRID,
                                 amount="100000", seq=2))
        ledger.record(make_trade(TradeSide.BUY, shares="100", price="10", seq=3))
        ledger.record(make_trade(TradeSide.BUY, pool=CashFlowType.GRID,
                                 shares="50", price="10", seq=4))

        snap = ledger.replay()
        self.assertEqual(snap.total_positions()["515180"], Decimal("150"))

    def test_serialization_roundtrip(self):
        """账本序列化往返"""
        ledger = TradeLedger()
        ledger.record(make_trade(TradeSide.DEPOSIT, amount="10000", seq=1))
        ledger.record(make_trade(TradeSide.BUY, shares="100", price="10", seq=2))

        restored = TradeLedger.from_dict(ledger.to_dict())

        self.assertEqual(len(restored), 2)
        snap = restored.replay()
        self.assertEqual(snap.portfolio(CashFlowType.REAL).shares_of("515180"),
                         Decimal("100"))


class TestReferenceLedger(unittest.TestCase):
    """
    用真实参考账本做回归

    参考账本由 13 笔流水构成，其重演结果必须与随附的持仓快照一致——
    这验证了"持仓是流水的派生视图"这一核心设计。
    """

    REFERENCE_TRADES = [
        # 入金（真实池）
        {'id': 'd1', 'date': '2026-10-12', 'side': 'deposit_rebalance',
         'amount': 175900.6},
        # 建仓
        {'id': 'b1', 'date': '2026-10-12', 'side': 'buy', 'code': '515180',
         'shares': 40600, 'price': 1.446, 'fee': 0.0},
        {'id': 'b2', 'date': '2026-10-12', 'side': 'buy', 'code': '159937',
         'shares': 6900, 'price': 8.565, 'fee': 0.0},
        {'id': 'b3', 'date': '2026-10-12', 'side': 'buy', 'code': '511380',
         'shares': 4500, 'price': 12.765, 'fee': 0.0},
        # 再平衡
        {'id': 's1', 'date': '2026-10-19', 'side': 'sell', 'code': '159937',
         'shares': 1700.0, 'price': 8.522, 'fee': 0.0},
        {'id': 's2', 'date': '2026-10-19', 'side': 'sell', 'code': '511380',
         'shares': 1000.0, 'price': 12.474, 'fee': 0.0},
        {'id': 's3', 'date': '2026-10-19', 'side': 'sell', 'code': '515180',
         'shares': 8600.0, 'price': 1.382, 'fee': 0.0},
        {'id': 'b4', 'date': '2026-10-19', 'side': 'buy', 'code': '513300',
         'shares': 14200.0, 'price': 2.769, 'fee': 0.0},
        {'id': 's4', 'date': '2026-10-19', 'side': 'sell', 'code': '513300',
         'shares': 14100.0, 'price': 2.786, 'fee': 0.0},
        {'id': 'b5', 'date': '2026-10-20', 'side': 'buy', 'code': '513300',
         'shares': 14300.0, 'price': 2.748, 'fee': 0.0},
        # 追加入金 + 网格预算
        {'id': 'd2', 'date': '2026-10-30', 'side': 'deposit_rebalance',
         'amount': 5000.0},
        {'id': 'd3', 'date': '2026-10-30', 'side': 'deposit_grid',
         'amount': 15000.0},
        {'id': 'b6', 'date': '2026-10-30', 'side': 'buy', 'code': '513300',
         'shares': 1800.0, 'price': 2.687, 'fee': 0.0},
    ]

    def test_replay_matches_reference_holdings(self):
        """重演结果与参考持仓快照一致"""
        ledger = TradeLedger.from_dict({'trades': self.REFERENCE_TRADES})
        snap = ledger.replay()

        real = snap.portfolio(CashFlowType.REAL)
        grid = snap.portfolio(CashFlowType.GRID)

        # 现金
        self.assertAlmostEqual(float(real.cash), 328.40, places=2)
        self.assertAlmostEqual(float(grid.cash), 15000.00, places=2)

        # 持仓数量
        expected = {'515180': 32000, '159937': 5200,
                    '511380': 3500, '513300': 16200}
        actual = {code: int(p.shares) for code, p in real.positions.items()
                  if p.shares > 0}
        self.assertEqual(actual, expected)

        # 网格池此时尚未交易
        self.assertEqual(grid.positions, {})

    def test_fees_and_deposits_accumulate(self):
        """累计入金与费用统计"""
        ledger = TradeLedger.from_dict({'trades': self.REFERENCE_TRADES})
        snap = ledger.replay()
        balance = snap.balances[CashFlowType.REAL]

        self.assertAlmostEqual(float(balance.deposits), 180900.60, places=2)
        self.assertEqual(balance.fees, Decimal("0"))
        self.assertAlmostEqual(
            float(balance.net_deposit), 180900.60, places=2)


if __name__ == '__main__':
    unittest.main()
