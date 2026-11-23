"""
账本统计与资金约束测试（离线）

覆盖:
- 初始资金是总金额（不是每张卡片各算一份）
- 建仓 / 加仓不得超过总金额（现金不得为负）
- 现金余额 = 剩余的钱（初始资金 − 买入支出 + 卖出收入）
- 累计收益率 = (总资产 − 初始资金) / 初始资金
- 各资产市值与占最新总市值的权重
- 网格池与真实现金池互不挤占
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import unittest
from datetime import date
from decimal import Decimal

from src.strategy.ledger.book import TradeLedger
from src.strategy.ledger.capital import (
    DEFAULT_INITIAL_CAPITALS,
    GRID_INITIAL_CAPITAL,
    REAL_INITIAL_CAPITAL,
)
from src.strategy.ledger.stats import compute_pool_stats
from src.strategy.ledger.trade import CashFlowType, Trade, TradeId, TradeSide

CAPITAL = REAL_INITIAL_CAPITAL
PRICES = {
    "515180": Decimal("1.761"),
    "159937": Decimal("7.881"),
    "511380": Decimal("12.047"),
    "513300": Decimal("2.523"),
}


def make_trade(side, code="515180", shares="100", price="10",
               on=date(2026, 10, 19), pool=CashFlowType.REAL, seq=1,
               fee="0"):
    return Trade(
        id=TradeId.build(on, side, seq),
        on_date=on,
        side=side,
        pool=pool,
        asset_code=code if side.is_security else None,
        shares=Decimal(shares) if side.is_security else Decimal("0"),
        price=Decimal(price) if side.is_security else Decimal("0"),
        fee=Decimal(fee),
    )


def make_ledger(trades, capitals=None):
    ledger = TradeLedger()
    for trade in trades:
        ledger.record(trade)
    ledger.initial_capitals = dict(
        capitals if capitals is not None else DEFAULT_INITIAL_CAPITALS)
    return ledger


class TestInitialCapitalIsTotal(unittest.TestCase):
    """初始资金是总金额"""

    def test_default_capitals_are_shared_single_source(self):
        """两个池的初始资金都有唯一定义，且都是 20 万"""
        self.assertEqual(REAL_INITIAL_CAPITAL, Decimal("200000"))
        self.assertEqual(GRID_INITIAL_CAPITAL, Decimal("200000"))
        self.assertEqual(
            DEFAULT_INITIAL_CAPITALS[CashFlowType.REAL], REAL_INITIAL_CAPITAL)
        self.assertEqual(
            DEFAULT_INITIAL_CAPITALS[CashFlowType.GRID], GRID_INITIAL_CAPITAL)

    def test_cash_starts_at_initial_capital(self):
        """没有任何流水时，现金 = 初始资金"""
        stats = compute_pool_stats(
            CashFlowType.REAL, [], PRICES, CAPITAL)
        self.assertEqual(stats.cash, CAPITAL)
        self.assertEqual(stats.total_value, CAPITAL)
        self.assertEqual(stats.total_return, Decimal("0"))

    def test_pool_does_not_double_count_capital(self):
        """再平衡与网格各有自己的池，初始资金不互相叠加到同一账户"""
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180",
                       shares="32000", price="1.396", seq=1),
        ])
        real = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        grid = compute_pool_stats(
            CashFlowType.GRID, ledger.trades_of(pool=CashFlowType.GRID),
            PRICES, CAPITAL)

        # 网格池没有流水 -> 现金仍是自己的完整额度，没被再平衡吃掉
        self.assertEqual(grid.cash, GRID_INITIAL_CAPITAL)
        self.assertEqual(real.cash, CAPITAL - Decimal("32000") * Decimal("1.396"))


class TestCashIsRemainingMoney(unittest.TestCase):
    """现金 = 剩余的钱"""

    def test_cash_decreases_on_buy(self):
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180",
                       shares="32000", price="1.396", seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        self.assertEqual(stats.cash, CAPITAL - Decimal("44672"))
        self.assertEqual(stats.cash, Decimal("155328"))

    def test_cash_increases_on_sell(self):
        """卖出所得回到现金"""
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180",
                       shares="32000", price="1.396", seq=1),
            make_trade(TradeSide.SELL, code="515180", shares="1000",
                       price="1.4", on=date(2026, 10, 20), seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        expected = CAPITAL - Decimal("44672") + Decimal("1400")
        self.assertEqual(stats.cash, expected)

    def test_fee_reduces_cash(self):
        """费用从现金里扣"""
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="100",
                       price="10", fee="5", seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        self.assertEqual(stats.cash, CAPITAL - Decimal("1000") - Decimal("5"))

    def test_cash_uses_shares_times_price(self):
        """
        账本 JSON 对证券流水不写 amount（只存数量/价格），
        现金必须按 数量 × 价格 算，否则现金永远是初始资金
        """
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180",
                       shares="32000", price="1.396", seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        self.assertNotEqual(stats.cash, CAPITAL)
        self.assertEqual(stats.cash, CAPITAL - Decimal("44672"))


class TestPositionMarketValueAndWeight(unittest.TestCase):
    """各资产最新市值与占最新总市值的权重"""

    def setUp(self):
        self.ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", seq=1),
            make_trade(TradeSide.BUY, code="511380", shares="3500",
                       price="13.073", seq=2),
        ])
        self.stats = compute_pool_stats(
            CashFlowType.REAL, self.ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)

    def test_market_value_uses_latest_price(self):
        """市值 = 数量 × 最新价（不是成交价）"""
        positions = {p.code: p for p in self.stats.positions}
        self.assertEqual(positions["515180"].market_value,
                         Decimal("32000") * Decimal("1.761"))
        self.assertEqual(positions["511380"].market_value,
                         Decimal("3500") * Decimal("12.047"))
        self.assertEqual(
            self.stats.market_value,
            Decimal("32000") * Decimal("1.761")
            + Decimal("3500") * Decimal("12.047"))

    def test_weights_sum_to_one(self):
        """各资产权重之和 = 100%"""
        total = sum(p.weight for p in self.stats.positions)
        self.assertAlmostEqual(float(total), 1.0, places=9)

    def test_weight_is_ratio_of_market_value(self):
        positions = {p.code: p for p in self.stats.positions}
        expected = (positions["515180"].market_value / self.stats.market_value)
        self.assertAlmostEqual(
            float(positions["515180"].weight), float(expected), places=9)

    def test_falls_back_to_last_trade_price(self):
        """没有行情价时用该资产最近一笔成交价"""
        stats = compute_pool_stats(
            CashFlowType.REAL, self.ledger.trades_of(pool=CashFlowType.REAL),
            {}, CAPITAL)
        positions = {p.code: p for p in stats.positions}
        self.assertEqual(positions["515180"].price, Decimal("1.396"))


class TestTotalReturn(unittest.TestCase):
    """累计收益率 = (总资产 − 初始资金) / 初始资金"""

    def test_total_return_formula(self):
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        expected = (stats.total_value - CAPITAL) / CAPITAL
        self.assertEqual(stats.total_return, expected)

    def test_total_value_is_cash_plus_market_value(self):
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        self.assertEqual(stats.total_value, stats.cash + stats.market_value)

    def test_profitable_trade_gives_positive_return(self):
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", seq=1),
        ])
        stats = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        self.assertGreater(stats.total_return, 0)


class TestCannotExceedTotalCapital(unittest.TestCase):
    """建仓 / 加仓不得超过总金额"""

    def test_buy_beyond_total_capital_is_rejected(self):
        """建仓金额超过总金额 -> 账本不自洽"""
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="30000",
                       price="7.5", seq=1),
        ])
        errors = ledger.validate()
        self.assertTrue(errors)
        self.assertTrue(any("现金不足" in e for e in errors), errors)

    def test_buy_within_total_capital_is_accepted(self):
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", seq=1),
        ])
        self.assertEqual(ledger.validate(), [])

    def test_sell_then_buy_can_reuse_proceeds(self):
        """卖出所得可再用于买入"""
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", seq=1),
            make_trade(TradeSide.SELL, code="515180", shares="32000",
                       price="1.5", on=date(2026, 10, 20), seq=1),
            make_trade(TradeSide.BUY, code="159937", shares="10000",
                       price="7.5", on=date(2026, 10, 21), seq=1),
        ])
        self.assertEqual(ledger.validate(), [])

    def test_grid_pool_has_its_own_budget(self):
        """网格池用网格虚拟预算额度，不挤占真实现金"""
        ledger = make_ledger([
            make_trade(TradeSide.BUY, code="515180", shares="32000",
                       price="1.396", on=date(2026, 10, 19), seq=1),
            make_trade(TradeSide.BUY, code="159937", shares="20000",
                       price="8.57", on=date(2026, 10, 20), seq=1,
                       pool=CashFlowType.GRID),
        ])
        self.assertEqual(ledger.validate(), [])
        real = compute_pool_stats(
            CashFlowType.REAL, ledger.trades_of(pool=CashFlowType.REAL),
            PRICES, CAPITAL)
        self.assertEqual(real.cash, CAPITAL - Decimal("44672"))


class TestRepositoryKeepsLedgerIntact(unittest.TestCase):
    """仓储不破坏用户账本"""

    def test_load_does_not_rewrite_file(self):
        """load() 只读，绝不写盘（避免改写用户账本）"""
        import tempfile
        import shutil
        from src.strategy.ledger.store import JsonFileTradeLedgerRepository

        tmp = tempfile.mkdtemp()
        try:
            path = Path(tmp) / "trades.json"
            original = '{"trades": [{"id": "x", "date": "2026-10-19", ' \
                       '"side": "buy", "pool": "real", "code": "515180", ' \
                       '"shares": 100, "price": 1.0, "fee": 0.0, "note": ""}]}'
            path.write_text(original, encoding="utf-8")

            repo = JsonFileTradeLedgerRepository(tmp)
            repo.load()
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_rejects_ledger_exceeding_capital(self):
        """超总金额的账本不落盘"""
        import tempfile
        import shutil
        from src.strategy.ledger.store import JsonFileTradeLedgerRepository

        tmp = tempfile.mkdtemp()
        try:
            repo = JsonFileTradeLedgerRepository(tmp)
            ledger = repo.load()
            ledger.record(make_trade(TradeSide.BUY, code="515180",
                                     shares="30000", price="7.5", seq=1))
            with self.assertRaises(ValueError):
                repo.save(ledger)
            self.assertFalse((Path(tmp) / "trades.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCapitalSingleSource(unittest.TestCase):
    """初始资金只有一个来源，前后端不许各写一份"""

    def test_site_config_uses_ledger_capital(self):
        """站点回测配置直接用账本的初始资金常量"""
        from src.chart.site import default_config

        config = default_config()
        self.assertEqual(config.rebalance_initial_capital, REAL_INITIAL_CAPITAL)
        self.assertEqual(config.grid_initial_capital, GRID_INITIAL_CAPITAL)

    def test_summary_publishes_real_initial_capital(self):
        """summary.json 带上真实账户各池初始资金，供页面读取"""
        from src.chart.site import SummaryView, StrategyView

        def make_view(key):
            return StrategyView(
                key=key, label=key, initial_capital=REAL_INITIAL_CAPITAL,
                total_value=Decimal("0"), cash=Decimal("0"),
                market_value=Decimal("0"), total_return=Decimal("0"),
                annualized_return=Decimal("0"), max_drawdown=Decimal("0"),
                sharpe_ratio=Decimal("0"), volatility=Decimal("0"),
                trade_count=0, positions=[], extra={},
            )

        view = SummaryView(
            generated_at="2026-01-01T00:00:00",
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 2),
            combined=make_view("combined"),
            rebalance=make_view("rebalance"),
            grid=make_view("grid"),
            real_initial_capital={
                CashFlowType.REAL.value: float(REAL_INITIAL_CAPITAL),
                CashFlowType.GRID.value: float(GRID_INITIAL_CAPITAL),
            },
        )
        payload = view.to_dict()
        self.assertEqual(
            payload["real_initial_capital"][CashFlowType.REAL.value],
            float(REAL_INITIAL_CAPITAL))
        self.assertEqual(
            payload["real_initial_capital"][CashFlowType.GRID.value],
            float(GRID_INITIAL_CAPITAL))

    def test_frontend_does_not_hardcode_capital(self):
        """前端不再写死 200000，改为读 summary.json"""
        app_js = (Path(__file__).parent.parent / "app.js").read_text(
            encoding="utf-8")
        self.assertNotIn(
            "INITIAL_CAPITAL = 200000", app_js,
            "app.js 不该再硬编码初始资金")
        self.assertIn("real_initial_capital", app_js,
                      "app.js 应从 summary.json 读初始资金")


class TestRealHoldingsUseRawPrices(unittest.TestCase):
    """
    真实持仓的市值必须用**实际成交价**（不复权）

    账本里记的是实际成交价、持仓是真实份额。
    若用后复权价算市值，会把**买入之前**的分红也算进持仓市值：
    红利ETF 515180 于 2026-10-16 除权（后复权倍率 1.2889），
    32,000 份后复权市值 ¥55,968，实际只有 ¥43,424，虚增 ¥12,544。
    """

    def test_market_value_uses_given_price(self):
        """市值 = 份额 × 给定价（调用方给实际价）"""
        from src.strategy.ledger.stats import compute_pool_stats
        from src.strategy.ledger.trade import Trade, TradeId, TradeSide

        trade = Trade(
            id=TradeId.build(date(2026, 10, 19), TradeSide.BUY, 1),
            on_date=date(2026, 10, 19),
            side=TradeSide.BUY,
            pool=CashFlowType.REAL,
            asset_code="515180",
            shares=Decimal("32000"),
            price=Decimal("1.396"),
            amount=Decimal("44672"),
        )

        raw = compute_pool_stats(
            CashFlowType.REAL, [trade], {"515180": Decimal("1.357")},
            Decimal("200000"))
        hfq = compute_pool_stats(
            CashFlowType.REAL, [trade], {"515180": Decimal("1.749")},
            Decimal("200000"))

        # 实际价：32,000 × 1.357 = 43,424
        self.assertEqual(raw.market_value, Decimal("43424"))
        # 后复权价会把买入前的分红算进来，虚增 12,544
        self.assertEqual(hfq.market_value, Decimal("55968"))
        self.assertEqual(hfq.market_value - raw.market_value, Decimal("12544"))

    def test_prices_file_publishes_both_bases(self):
        """prices.json 同时给出两种口径，页面按用途取用"""
        prices_path = (Path(__file__).parent.parent
                       / "data" / "site" / "prices.json")
        if not prices_path.exists():
            self.skipTest("尚未构建站点数据")
        payload = json.loads(prices_path.read_text(encoding="utf-8"))
        self.assertIn("prices", payload)
        self.assertIn("raw_prices", payload)
        self.assertIn("price_dates", payload)
        self.assertIn("raw_price_dates", payload)
        for code in payload["raw_prices"]:
            self.assertIn(code, payload["raw_price_dates"])

    def test_frontend_uses_raw_prices_for_real_holdings(self):
        """前端真实持仓用实际价，不用后复权价"""
        app_js = (Path(__file__).parent.parent / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("raw_prices", app_js,
                      "app.js 应读取 raw_prices")
        self.assertIn("state.rawPrices || state.prices", app_js,
                      "真实持仓应优先用实际成交价")


if __name__ == '__main__':
    unittest.main()
