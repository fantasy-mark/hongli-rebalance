"""
网格策略领域单元测试

覆盖:
- GridTrigger / GridCostModel 值对象不变量
- GridState 网格状态机与档位计算
- GridEngine 网格触发规则（跌破前低、档位推进、资金约束、止盈）
- PriceCalculator 的 ATR / 前N日最低价计算
- 网格与再平衡的资金隔离
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict

import pandas as pd

from src.domain.price.entities import AssetCode, PricePoint, AdjustedPriceSeries, AssetType, DataSource
from src.domain.price.services import PriceCalculator
from src.domain.grid.entities import (
    GridState,
    GridStatus,
    GridStrategy,
    GridStrategyStatus,
    floor_to_lot,
)
from src.domain.grid.services import GridEngine
from src.domain.grid.value_objects import (
    GridAction,
    GridAnchor,
    GridCostModel,
    GridMarketSnapshot,
    GridOrderSide,
    GridPriceBasis,
    GridTrigger,
)
from src.application.grid.grid_backtester import (
    GridAssetConfig,
    GridBacktester,
    GridConfig,
)
from src.application.rebalance.backtest_engine import RebalanceConfig, AssetConfig
from src.application.grid.combined_backtester import (
    CombinedBacktester,
    CombinedConfig,
)
from src.domain.allocation.entities import AllocationPlan
from src.domain.portfolio.entities import Portfolio
from src.domain.grid.value_objects import GridTradeType


# ----------------------------------------------------------------------
# 测试工具
# ----------------------------------------------------------------------

def make_series(closes, lows=None, highs=None, start=date(2024, 1, 1)):
    """构造测试用价格序列"""
    asset_code = AssetCode.from_string("515180")
    prices = []
    for i, close in enumerate(closes):
        close_d = Decimal(str(close))
        low = Decimal(str(lows[i])) if lows else close_d - Decimal("0.05")
        high = Decimal(str(highs[i])) if highs else close_d + Decimal("0.05")
        prices.append(PricePoint(
            date=start + timedelta(days=i),
            open=close_d,
            high=high,
            low=low,
            close=close_d,
            volume=1000000,
        ))
    return AdjustedPriceSeries(
        asset_code=asset_code,
        asset_type=AssetType.ETF,
        prices=prices,
        source=DataSource.EASTMONEY_HFQ,
    )


class FakePriceProvider:
    """离线价格提供者（测试用）"""

    def __init__(self, data: Dict[str, pd.DataFrame]):
        self.data = data
        self.calls = []

    def get_adjusted_prices(self, code, start_date=None, end_date=None, use_cache=True):
        self.calls.append(code)
        return self.data.get(code, pd.DataFrame())


def make_price_df(closes, lows=None, highs=None, start=date(2024, 1, 1)):
    """构造测试用价格 DataFrame"""
    rows = []
    for i, close in enumerate(closes):
        close_d = Decimal(str(close))
        rows.append({
            'date': start + timedelta(days=i),
            'open': float(close_d),
            'high': float(Decimal(str(highs[i])) if highs else close_d + Decimal("0.05")),
            'low': float(Decimal(str(lows[i])) if lows else close_d - Decimal("0.05")),
            'close': float(close_d),
            'volume': 1000000,
            'amount': 0.0,
        })
    return pd.DataFrame(rows).set_index('date')


# ----------------------------------------------------------------------
# 值对象测试
# ----------------------------------------------------------------------

class TestGridTrigger(unittest.TestCase):
    """网格触发参数"""

    def test_default_values(self):
        """默认参数符合需求: 120日窗口/120日ATR/10000元"""
        trigger = GridTrigger()
        self.assertEqual(trigger.lookback_days, 120)
        self.assertEqual(trigger.atr_period, 120)
        self.assertEqual(trigger.buy_amount, Decimal("10000"))
        self.assertEqual(trigger.step_ratio, Decimal("0.5"))
        self.assertEqual(trigger.anchor, GridAnchor.TRIGGER_PRICE)

    def test_invalid_step_ratio(self):
        """ATR比例必须>0"""
        with self.assertRaises(ValueError):
            GridTrigger(step_ratio=Decimal("0"))

    def test_invalid_buy_amount(self):
        """买入金额必须>0"""
        with self.assertRaises(ValueError):
            GridTrigger(buy_amount=Decimal("-1"))

    def test_invalid_lookback(self):
        """回看天数必须>=1"""
        with self.assertRaises(ValueError):
            GridTrigger(lookback_days=0)

    def test_serialization_roundtrip(self):
        """序列化/反序列化往返一致"""
        trigger = GridTrigger(
            lookback_days=60,
            atr_period=30,
            step_ratio=Decimal("0.8"),
            buy_amount=Decimal("20000"),
            lot_size=100,
            anchor=GridAnchor.PRIOR_LOW,
            take_profit_ratio=Decimal("2"),
            max_levels=5,
        )
        restored = GridTrigger.from_dict(trigger.to_dict())

        self.assertEqual(restored.lookback_days, 60)
        self.assertEqual(restored.step_ratio, Decimal("0.8"))
        self.assertEqual(restored.buy_amount, Decimal("20000"))
        self.assertEqual(restored.anchor, GridAnchor.PRIOR_LOW)
        self.assertEqual(restored.take_profit_ratio, Decimal("2"))
        self.assertEqual(restored.max_levels, 5)


class TestGridCostModel(unittest.TestCase):
    """网格成本模型"""

    def test_cost_calculation(self):
        """成本 = 佣金 + 滑点"""
        model = GridCostModel(
            commission_rate=Decimal("0.0003"),
            slippage_rate=Decimal("0.001"),
            min_commission=Decimal("5"),
        )
        # 10000 * 0.0003 = 3 -> 最低佣金 5；滑点 10
        self.assertEqual(model.calculate_cost(Decimal("10000")), Decimal("15"))

    def test_zero_amount(self):
        """零金额无成本"""
        self.assertEqual(GridCostModel().calculate_cost(Decimal("0")), Decimal("0"))


class TestFloorToLot(unittest.TestCase):
    """整手取整"""

    def test_round_down(self):
        """向下取整到100股整数倍"""
        self.assertEqual(floor_to_lot(Decimal("6666.67"), 100), Decimal("6600"))
        self.assertEqual(floor_to_lot(Decimal("99"), 100), Decimal("0"))
        self.assertEqual(floor_to_lot(Decimal("100"), 100), Decimal("100"))


# ----------------------------------------------------------------------
# 价格领域：ATR 与前低
# ----------------------------------------------------------------------

class TestPriceIndicators(unittest.TestCase):
    """网格依赖的价格指标"""

    def test_atr_series_length(self):
        """ATR序列长度 = 价格数 - 周期 + 1"""
        series = make_series([10 + i * 0.1 for i in range(30)])
        atr_series = PriceCalculator.calculate_atr_series(series, 14)

        self.assertEqual(len(atr_series), 30 - 14 + 1)
        self.assertEqual(atr_series[0][0], series.prices[13].date)

    def test_atr_constant_range(self):
        """每日振幅恒定时 ATR 等于振幅"""
        highs = [10.5] * 25
        lows = [9.5] * 25
        closes = [10.0] * 25
        series = make_series(closes, lows=lows, highs=highs)

        atr = PriceCalculator.calculate_atr(series, 14)
        self.assertAlmostEqual(float(atr), 1.0, places=6)

    def test_prior_low_excludes_current_day(self):
        """前N日最低价不含当日"""
        closes = [10, 11, 12, 9, 8]
        lows = [9.5, 10.5, 11.5, 8.5, 7.5]
        series = make_series(closes, lows=lows)

        # 最后一天：前2日最低价 = min(11.5, 8.5) = 8.5（不含当日 7.5）
        prior_low = PriceCalculator.calculate_prior_low(series, 2, index=-1)
        self.assertEqual(prior_low, Decimal("8.5"))

    def test_prior_low_window_excludes_current_day(self):
        """前N日最低价窗口严格不含当日"""
        closes = [10, 11, 12, 9, 8]
        lows = [9.5, 10.5, 11.5, 8.5, 7.5]
        series = make_series(closes, lows=lows)

        # 最后一天 low=7.5 是全序列最低，但不应进入窗口
        prior_low = PriceCalculator.calculate_prior_low(series, 4, index=-1)
        self.assertEqual(prior_low, Decimal("8.5"))

    def test_prior_low_series_insufficient_history(self):
        """历史不足时为 None"""
        series = make_series([10, 11, 12])
        lows = PriceCalculator.calculate_prior_low_series(series, 5)

        self.assertEqual(lows[0][1], None)
        self.assertEqual(lows[4][1], None) if len(lows) > 4 else None


# ----------------------------------------------------------------------
# 网格状态
# ----------------------------------------------------------------------

class TestGridState(unittest.TestCase):
    """网格状态实体"""

    def test_initial_status(self):
        """初始为未监控"""
        state = GridState(asset_code="515180")
        self.assertEqual(state.status, GridStatus.IDLE)

    def test_start_monitoring(self):
        """开始监控"""
        state = GridState(asset_code="515180")
        state.start_monitoring(date(2024, 1, 2))
        self.assertEqual(state.status, GridStatus.MONITORING)

    def test_activate_sets_anchor_and_step(self):
        """激活网格设置锚点与步长"""
        state = GridState(asset_code="515180")
        state.start_monitoring(date(2024, 1, 2))
        state.activate(
            anchor_price=Decimal("1.000"),
            atr=Decimal("0.050"),
            on_date=date(2024, 1, 2),
            step=Decimal("0.025"),
        )

        self.assertEqual(state.status, GridStatus.ACTIVE)
        self.assertEqual(state.anchor_price, Decimal("1.000"))
        self.assertEqual(state.step, Decimal("0.025"))
        self.assertEqual(state.next_level, 1)

    def test_level_price_formula(self):
        """档位价格 = 锚点 - 步长 * 档位"""
        state = GridState(asset_code="515180")
        state.activate(
            anchor_price=Decimal("1.000"),
            atr=Decimal("0.050"),
            on_date=date(2024, 1, 2),
            step=Decimal("0.025"),
        )

        self.assertEqual(state.level_price(1), Decimal("0.975"))
        self.assertEqual(state.level_price(2), Decimal("0.950"))
        self.assertEqual(state.next_level_price(), Decimal("0.975"))

    def test_activate_validation(self):
        """激活参数必须合法"""
        state = GridState(asset_code="515180")
        with self.assertRaises(ValueError):
            state.activate(Decimal("0"), Decimal("0.05"), date(2024, 1, 2), Decimal("0.025"))


# ----------------------------------------------------------------------
# 网格引擎
# ----------------------------------------------------------------------

class TestGridEngine(unittest.TestCase):
    """网格引擎核心规则"""

    def setUp(self):
        self.trigger = GridTrigger(
            lookback_days=5,
            atr_period=3,
            step_ratio=Decimal("0.5"),
            buy_amount=Decimal("10000"),
            lot_size=100,
        )
        self.strategy = GridStrategy(
            name="测试网格",
            trigger=self.trigger,
            asset_codes=["515180"],
            portfolio=Portfolio(cash=Decimal("100000"), name="网格组合"),
        )

    def test_monitor_when_not_broken(self):
        """未跌破前低时只监控不买入"""
        snapshot = GridMarketSnapshot(
            date=date(2024, 1, 10),
            close=Decimal("1.100"),
            prior_low=Decimal("1.050"),
            atr=Decimal("0.040"),
        )
        decision = GridEngine.evaluate(self.strategy, "515180", snapshot)

        self.assertEqual(decision.action, GridAction.MONITOR)
        self.assertFalse(decision.has_orders)
        self.assertEqual(self.strategy.state_for("515180").status, GridStatus.MONITORING)

    def test_activate_when_broken(self):
        """跌破前低时激活网格并计算步长"""
        snapshot = GridMarketSnapshot(
            date=date(2024, 1, 10),
            close=Decimal("1.000"),
            prior_low=Decimal("1.050"),
            atr=Decimal("0.040"),
        )
        decision = GridEngine.evaluate(self.strategy, "515180", snapshot)

        state = self.strategy.state_for("515180")
        self.assertEqual(state.status, GridStatus.ACTIVE)
        self.assertEqual(state.anchor_price, Decimal("1.000"))
        # step = ATR * ratio = 0.040 * 0.5
        self.assertEqual(state.step, Decimal("0.020"))
        self.assertEqual(decision.action, GridAction.ACTIVATE)

    def test_buy_on_first_level(self):
        """下跌一个步长后买入10000元"""
        # 激活: close=1.000, atr=0.040 -> step=0.020
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))

        # 次日跌到 0.980 <= 1.000 - 0.020 = 0.980 -> 触发第1档
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.980"),
            prior_low=Decimal("0.990"), atr=Decimal("0.040"),
        ))

        self.assertEqual(decision.action, GridAction.BUY)
        self.assertEqual(len(decision.orders), 1)

        order = decision.orders[0]
        self.assertEqual(order.side, GridOrderSide.BUY)
        self.assertEqual(order.level, 1)
        self.assertEqual(order.price, Decimal("0.980"))
        # 10000 / 0.980 = 10204.08 -> 向下取整到 10200 股
        self.assertEqual(order.shares, Decimal("10200"))
        self.assertEqual(order.amount, Decimal("9996"))

        state = self.strategy.state_for("515180")
        self.assertEqual(state.filled_levels, [1])
        self.assertEqual(state.next_level, 2)
        self.assertEqual(self.strategy.portfolio.shares_of("515180"), Decimal("10200"))

    def test_no_buy_above_level(self):
        """未跌到档位价格不买入"""
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))

        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.990"),
            prior_low=Decimal("0.995"), atr=Decimal("0.040"),
        ))

        self.assertEqual(decision.action, GridAction.HOLD)
        self.assertFalse(decision.has_orders)

    def test_multiple_levels_in_one_day(self):
        """跳空下跌时连续触发多档"""
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))

        # 一步跌到 0.940 -> 同时跌破第1档(0.980)、第2档(0.960)、第3档(0.940)
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.940"),
            prior_low=Decimal("0.950"), atr=Decimal("0.040"),
        ))

        self.assertEqual(len(decision.orders), 3)
        self.assertEqual([o.level for o in decision.orders], [1, 2, 3])
        self.assertEqual(self.strategy.state_for("515180").next_level, 4)

    def test_insufficient_cash_skips_level(self):
        """网格资金不足时跳过档位，且不会动用再平衡资金"""
        self.strategy.portfolio.cash = Decimal("5000")

        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))

        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.940"),
            prior_low=Decimal("0.950"), atr=Decimal("0.040"),
        ))

        self.assertFalse(decision.has_orders)
        self.assertGreaterEqual(self.strategy.portfolio.cash, Decimal("0"))
        self.assertEqual(self.strategy.state_for("515180").filled_levels, [])

    def test_anchor_prior_low_variant(self):
        """锚点可配置为前N日最低价"""
        self.strategy.trigger = GridTrigger(
            lookback_days=5, atr_period=3, step_ratio=Decimal("0.5"),
            buy_amount=Decimal("10000"), lot_size=100, anchor=GridAnchor.PRIOR_LOW,
        )

        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))

        state = self.strategy.state_for("515180")
        self.assertEqual(state.anchor_price, Decimal("1.050"))

    def test_take_profit_sell(self):
        """止盈后按批次卖出"""
        self.strategy.trigger = GridTrigger(
            lookback_days=5, atr_period=3, step_ratio=Decimal("0.5"),
            buy_amount=Decimal("10000"), lot_size=100,
            take_profit_ratio=Decimal("1"),
        )

        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.980"),
            prior_low=Decimal("0.990"), atr=Decimal("0.040"),
        ))

        state = self.strategy.state_for("515180")
        self.assertEqual(self.strategy.portfolio.shares_of("515180"), Decimal("10200"))

        # 止盈价 = 0.980 + 0.020 * 1 = 1.000
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 12), close=Decimal("1.010"),
            prior_low=Decimal("0.985"), atr=Decimal("0.040"),
        ))

        # 本轮完全止盈：动作为 CLOSED，成交明细中含卖出
        self.assertEqual(decision.action, GridAction.CLOSED)
        self.assertTrue(decision.round_closed)
        self.assertEqual(len(decision.orders), 1)
        self.assertEqual(decision.orders[0].side, GridOrderSide.SELL)
        self.assertEqual(self.strategy.portfolio.shares_of("515180"), Decimal("0"))

    def test_paused_strategy_does_not_trade(self):
        """策略暂停后不再推进网格"""
        self.strategy.status = GridStrategyStatus.PAUSED

        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
        ))

        self.assertEqual(decision.action, GridAction.HOLD)
        self.assertEqual(self.strategy.state_for("515180").status, GridStatus.IDLE)

    def test_insufficient_history_keeps_monitoring(self):
        """历史数据不足时保持监控"""
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=None, atr=None,
        ))

        self.assertEqual(decision.action, GridAction.MONITOR)
        self.assertEqual(self.strategy.state_for("515180").status, GridStatus.MONITORING)


# ----------------------------------------------------------------------
# 应用层：网格回测
# ----------------------------------------------------------------------

class TestGridBacktester(unittest.TestCase):
    """网格回测引擎（离线数据）"""

    def _build_provider(self):
        """构造一段先横盘后下跌的价格数据"""
        closes = [1.00] * 30 + [0.98, 0.96, 0.94, 0.92, 0.90]
        lows = [c - 0.01 for c in closes]
        highs = [c + 0.01 for c in closes]
        df = make_price_df(closes, lows=lows, highs=highs)
        return FakePriceProvider({"515180": df}), df

    def test_backtest_generates_buy_orders(self):
        """跌破前低后产生网格买入"""
        provider, _ = self._build_provider()

        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
            ),
            initial_capital=Decimal("100000"),
        )

        result = GridBacktester(config, price_provider=provider).run()

        self.assertGreater(result.buy_count, 0)
        self.assertGreater(len(result.snapshots), 0)
        self.assertGreaterEqual(result.strategy.portfolio.cash, Decimal("0"))

        # 网格持仓与网格资金独立核算
        final_snapshot = result.snapshots[-1]
        self.assertEqual(
            final_snapshot.total_value,
            final_snapshot.cash + final_snapshot.market_value,
        )

    def test_grid_cash_is_isolated(self):
        """网格只消耗自己的资金池"""
        provider, _ = self._build_provider()

        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
            ),
            initial_capital=Decimal("50000"),
        )

        result = GridBacktester(config, price_provider=provider).run()

        # 网格现金永远不能为负（不会去动再平衡的钱）
        self.assertGreaterEqual(result.strategy.portfolio.cash, Decimal("0"))
        self.assertLessEqual(result.strategy.portfolio.cash, config.initial_capital)

    def test_strategy_serialization_roundtrip(self):
        """网格策略可序列化/反序列化（用于实盘状态持久化）"""
        strategy = GridStrategy(
            name="测试网格",
            trigger=GridTrigger(lookback_days=10, atr_period=5),
            asset_codes=["515180"],
            portfolio=Portfolio(cash=Decimal("50000"), name="网格组合"),
        )
        state = strategy.state_for("515180")
        state.start_monitoring(date(2024, 1, 2))
        state.activate(Decimal("1.0"), Decimal("0.05"), date(2024, 1, 2), Decimal("0.025"))

        restored = GridStrategy.from_dict(strategy.to_dict())

        self.assertEqual(restored.name, "测试网格")
        self.assertEqual(restored.portfolio.cash, Decimal("50000"))
        self.assertEqual(restored.states["515180"].status, GridStatus.ACTIVE)
        self.assertEqual(restored.states["515180"].step, Decimal("0.025"))


class TestGridPriceBasis(unittest.TestCase):
    """价格口径（收盘价 / 最低价）"""

    def _make_strategy(self, basis):
        trigger = GridTrigger(
            lookback_days=5,
            atr_period=3,
            step_ratio=Decimal("0.5"),
            buy_amount=Decimal("10000"),
            lot_size=100,
            price_basis=basis,
        )
        return GridStrategy(
            name="口径测试",
            trigger=trigger,
            asset_codes=["515180"],
            portfolio=Portfolio(cash=Decimal("100000"), name="网格组合"),
        )

    def test_low_basis_triggers_on_intraday_low(self):
        """LOW 口径：最低价跌破前低即触发（即使收盘价未跌破）"""
        strategy = self._make_strategy(GridPriceBasis.LOW)

        decision = GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10),
            close=Decimal("1.020"),      # 收盘价未跌破前低
            low=Decimal("0.990"),        # 最低价跌破前低
            open=Decimal("1.030"),
            high=Decimal("1.035"),
            prior_low=Decimal("1.000"),
            atr=Decimal("0.040"),
        ))

        state = strategy.state_for("515180")
        self.assertEqual(state.status, GridStatus.ACTIVE)
        # LOW 口径锚点取触发日最低价
        self.assertEqual(state.anchor_price, Decimal("0.990"))

    def test_close_basis_ignores_intraday_low(self):
        """CLOSE 口径：只有收盘价跌破前低才触发"""
        strategy = self._make_strategy(GridPriceBasis.CLOSE)

        decision = GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10),
            close=Decimal("1.020"),
            low=Decimal("0.990"),
            open=Decimal("1.030"),
            high=Decimal("1.035"),
            prior_low=Decimal("1.000"),
            atr=Decimal("0.040"),
        ))

        self.assertEqual(decision.action, GridAction.MONITOR)
        self.assertEqual(strategy.state_for("515180").status, GridStatus.MONITORING)

    def test_low_basis_limit_fill_price(self):
        """LOW 口径：买入按档位限价成交"""
        strategy = self._make_strategy(GridPriceBasis.LOW)

        # 触发日：最低价 0.990 跌破前低 1.000，锚点 0.990，步长 0.020
        GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10),
            close=Decimal("0.995"), low=Decimal("0.990"), open=Decimal("1.000"),
            high=Decimal("1.010"), prior_low=Decimal("1.000"), atr=Decimal("0.040"),
        ))

        # 次日最低价 0.965 <= 第1档 0.970 -> 触发
        decision = GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11),
            close=Decimal("0.980"), low=Decimal("0.965"), open=Decimal("0.985"),
            high=Decimal("0.990"), prior_low=Decimal("0.995"), atr=Decimal("0.040"),
        ))

        self.assertEqual(len(decision.orders), 1)
        order = decision.orders[0]
        # 成交价 = 档位限价 0.970（开盘价 0.985 未跳空）
        self.assertEqual(order.price, Decimal("0.970"))
        self.assertEqual(order.shares, Decimal("10300"))  # 10000 / 0.97 = 10309 -> 10300

    def test_low_basis_gap_down_fills_at_open(self):
        """LOW 口径：跳空低开时按开盘价成交"""
        strategy = self._make_strategy(GridPriceBasis.LOW)

        GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10),
            close=Decimal("0.995"), low=Decimal("0.990"), open=Decimal("1.000"),
            high=Decimal("1.010"), prior_low=Decimal("1.000"), atr=Decimal("0.040"),
        ))

        decision = GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11),
            close=Decimal("0.940"), low=Decimal("0.935"), open=Decimal("0.935"),
            high=Decimal("0.960"), prior_low=Decimal("0.995"), atr=Decimal("0.040"),
        ))

        self.assertTrue(decision.has_orders)
        # 开盘 0.935 低于档位限价 0.970 -> 按开盘价成交
        self.assertEqual(decision.orders[0].price, Decimal("0.935"))


class TestCombinedIsolation(unittest.TestCase):
    """网格与再平衡的持仓隔离"""

    def _build_provider(self):
        """构造一段下跌行情"""
        closes = [1.00] * 30 + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88]
        lows = [c - 0.01 for c in closes]
        highs = [c + 0.01 for c in closes]
        df = make_price_df(closes, lows=lows, highs=highs)
        return FakePriceProvider({"515180": df})

    def test_sleeves_are_independent(self):
        """两个子组合的现金与持仓互不影响"""
        provider = self._build_provider()

        # 两个策略共用同一份总资产配置
        allocation = AllocationPlan.equal_weight(
            [("515180", "红利ETF")], name="单资产配置"
        )

        combined = CombinedBacktester(
            config=CombinedConfig(
                allocation=allocation,
                rebalance_initial_capital=Decimal("500000"),
                grid_initial_capital=Decimal("100000"),
                grid_trigger=GridTrigger(
                    lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                    buy_amount=Decimal("10000"), lot_size=100,
                    price_basis=GridPriceBasis.LOW,
                ),
            ),
            price_service=provider,
        ).run()

        rebalance_result = combined.rebalance_result
        grid_result = combined.grid_result

        # 网格产生了买入成交
        self.assertGreater(grid_result.buy_count, 0)

        # 再平衡子组合现金与网格子组合现金相互独立
        self.assertNotEqual(
            rebalance_result.snapshots[-1].cash,
            grid_result.snapshots[-1].cash,
        )

        # 组合市值 = 两个子组合市值之和
        last = combined.daily_values.iloc[-1]
        self.assertAlmostEqual(
            float(last['total_value']),
            float(last['rebalance_value'] + last['grid_value']),
            places=6,
        )

        # 网格子组合的现金从不为负（不会挪用再平衡资金）
        for snapshot in grid_result.snapshots:
            self.assertGreaterEqual(snapshot.cash, Decimal("0"))


class TestSharedAllocation(unittest.TestCase):
    """两个策略共用总资产配置"""

    def test_equal_weight_allocation(self):
        """等权配置"""
        plan = AllocationPlan.equal_weight(
            [("515180", "红利ETF"), ("159937", "黄金ETF")], name="等权"
        )

        self.assertEqual(plan.asset_codes, ["515180", "159937"])
        self.assertEqual(plan.asset_names["515180"], "红利ETF")
        self.assertEqual(sum(plan.target_weights.values()), Decimal("1"))

    def test_invalid_weight_sum(self):
        """权重之和必须为1"""
        from src.domain.allocation.entities import AssetAllocation
        with self.assertRaises(ValueError):
            AllocationPlan(allocations=[
                AssetAllocation(code="515180", name="红利ETF", target_weight=Decimal("0.5")),
            ])

    def test_grid_and_rebalance_share_assets(self):
        """网格监控的资产与再平衡配置的资产完全一致"""
        plan = AllocationPlan.equal_weight(
            [("515180", "红利ETF"), ("159937", "黄金ETF")], name="等权"
        )

        grid_config = GridConfig.from_allocation(plan)
        rebalance_config = RebalanceConfig.from_allocation(plan)

        self.assertEqual(grid_config.asset_codes, plan.asset_codes)
        self.assertEqual(
            [a.code for a in rebalance_config.assets], plan.asset_codes
        )

    def test_combined_config_builds_both(self):
        """组合配置由总资产配置派生出两个子配置"""
        plan = AllocationPlan.equal_weight(
            [("515180", "红利ETF")], name="单资产"
        )
        config = CombinedConfig(allocation=plan)

        self.assertEqual(config.build_grid_config().asset_codes, ["515180"])
        self.assertEqual(
            [a.code for a in config.build_rebalance_config().assets], ["515180"]
        )


class TestGridRoundLifecycle(unittest.TestCase):
    """网格轮次生命周期：跌破前低 -> 完全止盈 -> 重新跌破才开新轮"""

    def setUp(self):
        self.trigger = GridTrigger(
            lookback_days=5,
            atr_period=3,
            step_ratio=Decimal("0.5"),
            buy_amount=Decimal("10000"),
            lot_size=100,
            price_basis=GridPriceBasis.CLOSE,
            take_profit_ratio=Decimal("1"),
        )
        self.strategy = GridStrategy(
            name="轮次测试",
            trigger=self.trigger,
            asset_codes=["515180"],
            portfolio=Portfolio(cash=Decimal("100000"), name="网格组合"),
        )

    def _activate(self, close="1.000", prior_low="1.050", atr="0.040", on=date(2024, 1, 10)):
        return GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=on, close=Decimal(close), prior_low=Decimal(prior_low),
            atr=Decimal(atr), open=Decimal(close), high=Decimal(close),
            low=Decimal(close),
        ))

    def test_round_completes_after_full_take_profit(self):
        """全部持仓止盈后本轮结束"""
        self._activate()

        # 第1档买入
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.980"),
            prior_low=Decimal("0.990"), atr=Decimal("0.040"),
            open=Decimal("0.980"), high=Decimal("0.980"), low=Decimal("0.980"),
        ))
        state = self.strategy.state_for("515180")
        self.assertEqual(state.status, GridStatus.ACTIVE)
        self.assertEqual(state.round_no, 1)

        # 次日上涨到止盈价 (0.980 + 0.020 = 1.000) -> 卖出
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 12), close=Decimal("1.000"),
            prior_low=Decimal("0.985"), atr=Decimal("0.040"),
            open=Decimal("0.990"), high=Decimal("1.005"), low=Decimal("0.990"),
        ))

        self.assertEqual(decision.action, GridAction.CLOSED)
        self.assertTrue(decision.round_closed)
        self.assertEqual(self.strategy.state_for("515180").status, GridStatus.COMPLETED)
        self.assertEqual(self.strategy.state_for("515180").rounds_completed, 1)

    def test_next_round_requires_new_break(self):
        """本轮结束后，必须重新跌破前低才开启下一轮"""
        self._activate()
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.980"),
            prior_low=Decimal("0.990"), atr=Decimal("0.040"),
            open=Decimal("0.980"), high=Decimal("0.980"), low=Decimal("0.980"),
        ))
        GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 12), close=Decimal("1.000"),
            prior_low=Decimal("0.985"), atr=Decimal("0.040"),
            open=Decimal("0.990"), high=Decimal("1.005"), low=Decimal("0.990"),
        ))

        state = self.strategy.state_for("515180")
        self.assertEqual(state.status, GridStatus.COMPLETED)

        # 次日：价格未跌破前低 -> 不会开启新轮
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 15), close=Decimal("1.010"),
            prior_low=Decimal("0.995"), atr=Decimal("0.040"),
            open=Decimal("1.005"), high=Decimal("1.015"), low=Decimal("1.000"),
        ))
        self.assertEqual(decision.action, GridAction.MONITOR)
        self.assertEqual(self.strategy.state_for("515180").round_no, 1)

        # 之后再次跌破前低 -> 开启第2轮
        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 20), close=Decimal("0.900"),
            prior_low=Decimal("1.000"), atr=Decimal("0.040"),
            open=Decimal("0.990"), high=Decimal("0.995"), low=Decimal("0.900"),
        ))
        self.assertEqual(decision.action, GridAction.ACTIVATE)
        self.assertEqual(self.strategy.state_for("515180").round_no, 2)
        self.assertEqual(self.strategy.state_for("515180").status, GridStatus.ACTIVE)

    def test_t1_rule_same_day_sell_forbidden(self):
        """T+1：当日买入当日不可卖出"""
        self._activate()

        decision = GridEngine.evaluate(self.strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.980"),
            prior_low=Decimal("0.990"), atr=Decimal("0.040"),
            open=Decimal("0.980"), high=Decimal("1.010"), low=Decimal("0.980"),
        ))

        # 只买不卖
        self.assertEqual(len(decision.orders), 1)
        self.assertEqual(decision.orders[0].side, GridOrderSide.BUY)
        self.assertEqual(self.strategy.portfolio.shares_of("515180"), Decimal("10200"))


class TestOneShotTakeProfit(unittest.TestCase):
    """止盈为整轮一次性清仓（不产生减仓）"""

    def setUp(self):
        self.strategy = GridStrategy(
            name="整轮止盈测试",
            trigger=GridTrigger(
                lookback_days=5, atr_period=3, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.CLOSE, take_profit_ratio=Decimal("1"),
            ),
            asset_codes=["515180"],
            portfolio=Portfolio(cash=Decimal("100000"), name="网格组合"),
        )

    def _snap(self, on, close, prior_low="1.050", atr="0.040"):
        c = Decimal(close)
        return GridMarketSnapshot(
            date=on, close=c, prior_low=Decimal(prior_low), atr=Decimal(atr),
            open=c, high=c, low=c,
        )

    def test_all_tranches_sold_at_once(self):
        """止盈触发时本轮全部批次同日清仓"""
        # 激活: close=1.000, atr=0.040 -> step=0.020
        GridEngine.evaluate(self.strategy, "515180",
                            self._snap(date(2024, 1, 10), "1.000"))

        # 连跌触发第1、2档
        GridEngine.evaluate(self.strategy, "515180",
                            self._snap(date(2024, 1, 11), "0.960", prior_low="0.990"))

        state = self.strategy.state_for("515180")
        self.assertEqual(state.filled_levels, [1, 2])
        self.assertEqual(len(state.tranches), 2)
        self.assertGreater(self.strategy.portfolio.shares_of("515180"), 0)

        # 止盈价 = 开仓价(第1档 0.960) + 0.020 = 0.980
        decision = GridEngine.evaluate(self.strategy, "515180",
                                       self._snap(date(2024, 1, 12), "0.985", prior_low="0.970"))

        sells = [o for o in decision.orders if o.side == GridOrderSide.SELL]
        self.assertEqual(len(sells), 2)               # 两个批次同时卖出
        self.assertEqual(self.strategy.portfolio.shares_of("515180"),
                         Decimal("0"))   # 一次清仓
        self.assertEqual(len(state.tranches), 0)
        self.assertTrue(decision.round_closed)

    def test_no_take_profit_below_target(self):
        """未达止盈价时仍持仓，且不产生部分卖出"""
        GridEngine.evaluate(self.strategy, "515180",
                            self._snap(date(2024, 1, 10), "1.000"))
        GridEngine.evaluate(self.strategy, "515180",
                            self._snap(date(2024, 1, 11), "0.960", prior_low="0.990"))

        state = self.strategy.state_for("515180")
        shares_before = self.strategy.portfolio.shares_of("515180")

        # 0.975 < 止盈价 0.980，不应卖出
        decision = GridEngine.evaluate(self.strategy, "515180",
                                       self._snap(date(2024, 1, 12), "0.975", prior_low="0.970"))

        self.assertFalse([o for o in decision.orders if o.side == GridOrderSide.SELL])
        self.assertEqual(self.strategy.portfolio.shares_of("515180"), shares_before)

    def test_no_reduce_in_backtest(self):
        """回测成交中不存在减仓类型"""
        closes = [1.00] * 30 + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.95, 1.00, 1.02]
        df = make_price_df(closes, lows=[c - 0.01 for c in closes],
                           highs=[c + 0.01 for c in closes])
        provider = FakePriceProvider({"515180": df})

        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("100000"),
        )

        result = GridBacktester(config, price_provider=provider).run()

        # 所有卖出均为止盈
        for trade in result.trades:
            if trade.side == 'sell':
                self.assertEqual(trade.trade_type, GridTradeType.TAKE_PROFIT)

        # 不存在 REDUCE 枚举
        self.assertFalse(hasattr(GridTradeType, 'REDUCE'))


class TestTriggerLogAggregation(unittest.TestCase):
    """触发日志：同日同轮止盈聚合"""

    def _run(self):
        # 先横盘，再持续下跌触发多档，最后反弹一次性止盈
        closes = ([1.00] * 30
                  + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88]
                  + [0.95, 1.00, 1.02])
        df = make_price_df(closes, lows=[c - 0.01 for c in closes],
                           highs=[c + 0.01 for c in closes])
        provider = FakePriceProvider({"515180": df})

        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("100000"),
        )
        return GridBacktester(config, price_provider=provider).run()

    def test_aggregates_same_day_take_profit(self):
        """同日同轮的多笔止盈聚合为一行"""
        from src.application.grid.grid_chart import GridChartRenderer

        result = self._run()
        sells = [t for t in result.trades if t.side == 'sell']
        self.assertGreater(len(sells), 1, "需要多笔止盈才能验证聚合")

        aggregated = GridChartRenderer.trigger_log(result)
        raw = GridChartRenderer.trigger_log(result, aggregate_take_profit=False)

        tp_rows = [l for l in aggregated.split('\n') if '| 止盈' in l]
        groups = {(t.date, t.asset_code, t.round_no) for t in sells}

        self.assertEqual(len(tp_rows), len(groups))
        self.assertLess(len(tp_rows), len(sells))            # 确实折叠了
        self.assertEqual(
            len([l for l in raw.split('\n') if '| 止盈' in l]), len(sells)
        )

    def test_aggregated_shares_and_amount(self):
        """聚合行的股数/价格等于明细之和与加权均价"""
        from src.application.grid.grid_chart import GridChartRenderer

        result = self._run()
        sells = [t for t in result.trades if t.side == 'sell']
        key = (sells[0].date, sells[0].asset_code, sells[0].round_no)
        group = [t for t in sells
                 if (t.date, t.asset_code, t.round_no) == key]

        total_shares = sum(t.shares for t in group)
        total_amount = sum(t.amount for t in group)
        avg_price = total_amount / total_shares

        log = GridChartRenderer.trigger_log(result)
        row = next(l for l in log.split('\n')
                   if '| 止盈' in l and str(key[0]) in l)

        self.assertIn(f"{float(total_shares):,.0f}", row)
        self.assertIn(f"{float(avg_price):.4f}", row)

    def test_format_levels(self):
        """档位文本：连续区间 / 离散枚举 / 单档 / 空"""
        from src.application.grid.grid_chart import GridChartRenderer

        self.assertEqual(GridChartRenderer._format_levels([1, 2, 3, 4, 5]), "1-5")
        self.assertEqual(GridChartRenderer._format_levels([1, 3, 5]), "1,3,5")
        self.assertEqual(GridChartRenderer._format_levels([2]), "2")
        self.assertEqual(GridChartRenderer._format_levels([]), "-")


class TestGridValuationCarryForward(unittest.TestCase):
    """
    多资产估值：某资产当日无行情时，其持仓应沿用最近收盘价估值

    不同资产的交易日历不同，若直接跳过当日无行情的资产，
    组合市值会被漏算，净值曲线出现虚假断崖。
    """

    class _OffsetCalendarProvider:
        """159937 隔日才有行情，515180 每日都有"""

        def __init__(self, days=40):
            self.days = days

        def get_adjusted_prices(self, code, start_date=None, end_date=None,
                                use_cache=True):
            import pandas as pd
            rows = []
            for i in range(self.days):
                d = date(2024, 1, 1) + pd.Timedelta(days=i)
                if code == '159937' and i % 2 == 1:
                    continue
                close = 10.0 if i < 10 else 10.0 - (min(i, 29) - 9) * 0.15 if i < 30 else 6.7
                rows.append({
                    'date': d, 'open': close, 'high': close + 0.05,
                    'low': close - 0.05, 'close': close,
                    'volume': 1000, 'amount': 0.0,
                })
            return pd.DataFrame(rows).set_index('date')

    def test_valuation_uses_last_known_close(self):
        """持仓市值 = 份额 × 最近收盘价（不漏算）"""
        plan = AllocationPlan.equal_weight(
            [('515180', 'A'), ('159937', 'B')], name='双资产'
        )
        config = GridConfig.from_allocation(
            plan,
            trigger=GridTrigger(
                lookback_days=3, atr_period=3, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("1000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("100000"),
        )

        result = GridBacktester(
            config, price_provider=self._OffsetCalendarProvider()
        ).run()

        snapshot = result.snapshots[-1]
        last_row = result.daily_values.iloc[-1]

        expected = Decimal("0")
        for code, shares in snapshot.positions.items():
            close = Decimal(str(last_row[f'{code}_close']))
            expected += shares * close

        self.assertGreater(len(snapshot.positions), 0, "应当有持仓")
        self.assertAlmostEqual(float(snapshot.market_value), float(expected), places=2)

    def test_total_value_equals_cash_plus_positions(self):
        """总市值恒等于 现金 + 持仓市值"""
        plan = AllocationPlan.equal_weight(
            [('515180', 'A'), ('159937', 'B')], name='双资产'
        )
        config = GridConfig.from_allocation(
            plan,
            trigger=GridTrigger(
                lookback_days=3, atr_period=3, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("1000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("100000"),
        )

        result = GridBacktester(
            config, price_provider=self._OffsetCalendarProvider()
        ).run()

        for snapshot in result.snapshots:
            self.assertAlmostEqual(
                float(snapshot.total_value),
                float(snapshot.cash + snapshot.market_value),
                places=2,
            )



# ----------------------------------------------------------------------
# 持仓辅助（持仓与成本在 Portfolio 中，批次在 GridState 中）
# ----------------------------------------------------------------------

def state_portfolio_shares(state):
    """从 GridState 取持仓数量（持仓在 Portfolio 中）"""
    return state.portfolio.shares_of(state.asset_code)


def state_portfolio_cost(state):
    return state.portfolio.cost_basis_of(state.asset_code)


def state_portfolio_realized(state):
    return state.portfolio.realized_pnl()


def state_portfolio_unrealized(state, price):
    return state.portfolio.position_for(state.asset_code).unrealized_pnl(price)


def state_portfolio_total(state, price):
    position = state.portfolio.position_for(state.asset_code)
    return position.realized_pnl + position.unrealized_pnl(price)


class TestTradeTypeInDomain(unittest.TestCase):
    """
    交易类型判定与盈亏核算的职责划分

    - 持仓数量/成本/已实现盈亏 -> Portfolio（共享内核，与再平衡同构）
    - 网格轮次/持仓批次/交易类型 -> GridState（网格专属）
    应用层不再持有影子持仓，避免两份真相来源。
    """

    def _state(self):
        """GridState（轮次/批次）+ Portfolio（持仓/成本）"""
        from src.domain.grid.entities import GridState
        state = GridState(asset_code="515180")
        state.portfolio = Portfolio(cash=Decimal("100000"), name="网格组合")
        return state

    def _order(self, side, shares, price, level, on=date(2024, 1, 2),
               cost=Decimal("0")):
        from src.domain.grid.value_objects import GridOrder, GridOrderSide
        return GridOrder(
            asset_code="515180",
            side=side,
            price=Decimal(str(price)),
            shares=Decimal(str(shares)),
            amount=Decimal(str(price)) * Decimal(str(shares)),
            cost=Decimal(str(cost)),
            date=on,
            level=level,
        )

    def _buy(self, state, shares=100, price="10.00", level=1,
             cost=Decimal("0"), on=date(2024, 1, 2)):
        """模拟一次买入：Portfolio 记账 + GridState 记批次"""
        order = self._order(GridOrderSide.BUY, shares, price, level, on=on, cost=cost)
        was_flat = state.portfolio.is_flat(order.asset_code)
        state.portfolio.buy(order.asset_code, order.shares,
                            order.amount, order.cost)
        return state.record_buy(order, was_flat)

    def _sell(self, state, shares=100, price="11.00", level=1,
              cost=Decimal("0"), on=date(2024, 1, 3)):
        """模拟一次卖出：Portfolio 记账（按批次精确释放成本）+ GridState 移除批次"""
        order = self._order(GridOrderSide.SELL, shares, price, level, on=on, cost=cost)
        tranche = next((t for t in state.tranches if t.level == level), None)
        released = tranche.total_outlay if tranche else None
        state.portfolio.sell(order.asset_code, order.shares,
                             order.amount, order.cost,
                             cost_basis_released=released)
        return state.record_sell(order)

    # ------------------------------------------------------------------

    def test_first_buy_is_open(self):
        """持仓为 0 时买入 -> 开仓"""
        from src.domain.grid.value_objects import GridTradeType

        state = self._state()
        trade_type = self._buy(state)

        self.assertEqual(trade_type, GridTradeType.OPEN)
        self.assertEqual(state_portfolio_shares(state), Decimal("100"))

    def test_buy_while_holding_is_add(self):
        """持仓 > 0 时再买 -> 加仓（即使换了新轮次也不影响该规则）"""
        from src.domain.grid.value_objects import GridTradeType

        state = self._state()
        first = self._buy(state, level=1)
        second = self._buy(state, price="9.80", level=2)

        self.assertEqual(first, GridTradeType.OPEN)
        self.assertEqual(second, GridTradeType.ADD)
        self.assertEqual(state_portfolio_shares(state), Decimal("200"))

    def test_cost_basis_includes_commission(self):
        """持仓成本含手续费"""
        from src.domain.grid.value_objects import GridOrderSide

        state = self._state()
        self._buy(state, cost=Decimal("5"))

        self.assertEqual(state_portfolio_cost(state), Decimal("1005"))

    def test_realized_pnl_nets_commission(self):
        """已实现盈亏 = 卖出净额 - 买入总支出（两侧均扣手续费）"""
        from src.domain.grid.value_objects import GridTradeType

        state = self._state()
        # 买入：1000 + 手续费 5 = 1005 支出
        self._buy(state, cost=Decimal("5"))
        # 卖出：1100 - 手续费 6 = 1094 净收入
        trade_type = self._sell(state, cost=Decimal("6"))

        self.assertEqual(trade_type, GridTradeType.TAKE_PROFIT)
        self.assertEqual(state_portfolio_realized(state), Decimal("89"))
        self.assertEqual(state_portfolio_shares(state), Decimal("0"))
        self.assertEqual(state_portfolio_cost(state), Decimal("0"))

    def test_sell_more_than_held_raises(self):
        """卖出数量超过持仓 -> 拒绝（由 Portfolio 保护）"""
        from src.domain.grid.value_objects import GridOrderSide

        state = self._state()
        self._buy(state)
        with self.assertRaises(ValueError):
            state.portfolio.sell(
                "515180", Decimal("200"), Decimal("2200"), Decimal("0"))

    def test_total_pnl_is_realized_plus_floating(self):
        """总盈亏 = 已实现 + 浮动"""
        state = self._state()
        self._buy(state)

        # 持仓 100 股，现价 10.5 -> 浮动 +50
        self.assertEqual(state_portfolio_unrealized(state, Decimal("10.5")),
                         Decimal("50.0"))
        self.assertEqual(state_portfolio_realized(state), Decimal("0"))
        self.assertEqual(state_portfolio_total(state, Decimal("10.5")),
                         Decimal("50.0"))

    def test_order_carries_trade_type_and_shares_after(self):
        """引擎记账后把交易类型与成交后持仓回填到订单上"""
        from src.domain.grid.entities import GridStrategy
        from src.domain.grid.services import GridEngine

        strategy = GridStrategy(
            name="回填测试",
            trigger=GridTrigger(
                lookback_days=5, atr_period=3, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.CLOSE, take_profit_ratio=Decimal("1"),
            ),
            asset_codes=["515180"],
            portfolio=Portfolio(cash=Decimal("100000"), name="网格组合"),
        )

        GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 10), close=Decimal("1.000"),
            prior_low=Decimal("1.050"), atr=Decimal("0.040"),
            open=Decimal("1.000"), high=Decimal("1.000"), low=Decimal("1.000"),
        ))
        # 跌穿两档 -> 同日两笔买入
        decision = GridEngine.evaluate(strategy, "515180", GridMarketSnapshot(
            date=date(2024, 1, 11), close=Decimal("0.940"),
            prior_low=Decimal("0.950"), atr=Decimal("0.040"),
            open=Decimal("0.940"), high=Decimal("0.940"), low=Decimal("0.940"),
        ))

        self.assertGreaterEqual(len(decision.orders), 2)
        self.assertEqual(decision.orders[0].trade_type, GridTradeType.OPEN)
        self.assertEqual(decision.orders[1].trade_type, GridTradeType.ADD)
        # shares_after 逐笔递增（不是当日最终值）
        self.assertLess(decision.orders[0].shares_after,
                        decision.orders[1].shares_after)


class TestGridPnlAccounting(unittest.TestCase):
    """网格盈亏核算"""

    def _build_provider(self):
        closes = [1.00] * 30 + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.92, 0.96, 1.00]
        lows = [c - 0.01 for c in closes]
        highs = [c + 0.01 for c in closes]
        df = make_price_df(closes, lows=lows, highs=highs)
        return FakePriceProvider({"515180": df})

    def _run(self):
        provider = self._build_provider()
        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("100000"),
        )
        return GridBacktester(config, price_provider=provider).run()

    def test_pnl_identity(self):
        """恒等式: 总市值 = 初始资金 + 总盈亏（已实现+浮动）"""
        result = self._run()
        df = result.daily_values

        expected = float(result.config.initial_capital) + df['total_pnl']
        diff = (df['total_value'] - expected).abs().max()
        self.assertLess(float(diff), 0.000001)

    def test_pnl_columns_exist(self):
        """盈亏列已输出"""
        result = self._run()
        df = result.daily_values

        for column in ('realized_pnl', 'floating_pnl', 'total_pnl'):
            self.assertIn(column, df.columns)

        # 已实现盈亏只在平仓时增加（阶梯函数）
        self.assertGreaterEqual(float(result.realized_pnl), 0)


class TestTradeTypeClassification(unittest.TestCase):
    """交易类型：开仓 / 加仓 / 止盈（无减仓）"""

    def test_trade_type_labels(self):
        """中文标签"""
        self.assertEqual(GridTradeType.OPEN.label, "开仓")
        self.assertEqual(GridTradeType.ADD.label, "加仓")
        self.assertEqual(GridTradeType.TAKE_PROFIT.label, "止盈")

    def test_open_only_when_flat(self):
        """开仓 = 买入前持仓为0；持仓>0 再买必为加仓"""
        closes = ([1.00] * 30
                  + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88]
                  + [0.95, 1.00, 1.02]          # 反弹止盈淸仓
                  + [0.99, 0.97, 0.95, 0.93])   # 再次下跌
        df = make_price_df(closes, lows=[c - 0.01 for c in closes],
                           highs=[c + 0.01 for c in closes])
        provider = FakePriceProvider({"515180": df})

        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("100000"),
        )

        result = GridBacktester(config, price_provider=provider).run()

        for trade in result.trades:
            if trade.side != 'buy':
                continue
            position_before = trade.shares_after - trade.shares
            if position_before == 0:
                self.assertEqual(trade.trade_type, GridTradeType.OPEN)
            else:
                self.assertEqual(trade.trade_type, GridTradeType.ADD)

    def test_backtest_classifies_trades(self):
        """回测结果中的交易类型分类正确"""
        provider = FakePriceProvider({
            "515180": make_price_df(
                [1.00] * 30 + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.92, 0.96, 1.00],
                lows=[c - 0.01 for c in [1.00] * 30 + [0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.92, 0.96, 1.00]],
            )
        })

        config = GridConfig(
            assets=[GridAssetConfig(code="515180", name="红利ETF")],
            trigger=GridTrigger(
                lookback_days=10, atr_period=5, step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"), lot_size=100,
                price_basis=GridPriceBasis.LOW, take_profit_ratio=Decimal("1"),
            ),
            initial_capital=Decimal("200000"),
        )

        result = GridBacktester(config, price_provider=provider).run()

        types = [t.trade_type for t in result.trades]
        self.assertIn(GridTradeType.OPEN, types)

        # 第一笔买入是开仓，后续是加仓
        self.assertEqual(result.trades[0].trade_type, GridTradeType.OPEN)

        # 卖出类型：只有止盈（整轮一次性清仓，不产生减仓）
        for trade in result.trades:
            if trade.side == 'sell':
                self.assertEqual(trade.trade_type, GridTradeType.TAKE_PROFIT)

        # 轮次事件已记录
        self.assertGreaterEqual(result.total_rounds, 1)


if __name__ == '__main__':
    unittest.main()
