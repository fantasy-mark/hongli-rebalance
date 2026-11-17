"""
网格策略回测引擎（应用层）

职责:
- 编排价格领域（ATR / 前N日最低价）与网格领域（GridEngine）
- 维护网格资金池与网格持仓（与再平衡持仓完全隔离）
- 输出回测结果与绩效指标

隔离约定:
- 本引擎只管理"网格子组合"：独立的现金池 + 独立的持仓账本
- 再平衡组合的现金/持仓不会被读取或修改，两者互不影响
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Protocol, Tuple
import logging

import pandas as pd
import numpy as np

from src.domain.price.entities import AdjustedPriceSeries, AssetCode, DataSource
from src.domain.price.services import PriceCalculator
from src.domain.grid.entities import GridStrategy, GridStrategyStatus
from src.domain.grid.services import GridEngine
from src.domain.performance.value_objects import PerformanceCalculator, ReturnStatistics
from src.domain.trading.value_objects import LotSize
from src.domain.portfolio.entities import Portfolio
from src.domain.grid.value_objects import (
    GridCostModel,
    GridMarketSnapshot,
    GridOrder,
    GridOrderSide,
    GridPriceBasis,
    GridTradeType,
    GridTrigger,
)
from src.application.price.query_service import PriceQueryService

logger = logging.getLogger(__name__)

# 指标预热：ATR(120) 与前N日最低价需要历史数据，
# 回测开始日前额外多取一段行情用于指标预热（不产生交易）
WARMUP_TRADING_DAYS = 150


class PriceDataProvider(Protocol):
    """价格数据提供者协议（便于测试注入离线数据）"""
    def get_adjusted_prices(self, code: str,
                            start_date: Optional[str] = None,
                            end_date: Optional[str] = None,
                            use_cache: bool = True) -> pd.DataFrame:
        ...


@dataclass
class GridAssetConfig:
    """网格监控资产配置"""
    code: str
    name: str

    def __post_init__(self):
        if not self.code:
            raise ValueError("资产代码不能为空")


@dataclass
class GridConfig:
    """网格策略配置（应用层 DTO）"""
    assets: List[GridAssetConfig] = field(default_factory=list)
    trigger: GridTrigger = field(default_factory=GridTrigger)
    cost_model: GridCostModel = field(default_factory=GridCostModel)
    initial_capital: Decimal = Decimal("100000")  # 网格子组合初始资金
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    def __post_init__(self):
        if not isinstance(self.initial_capital, Decimal):
            self.initial_capital = Decimal(str(self.initial_capital))
        if self.initial_capital < 0:
            raise ValueError(f"初始资金不能为负: {self.initial_capital}")
        if not self.assets:
            raise ValueError("监控资产列表不能为空")

    @classmethod
    def from_allocation(cls,
                        plan,
                        trigger: Optional[GridTrigger] = None,
                        cost_model: Optional[GridCostModel] = None,
                        initial_capital: Decimal = Decimal("100000"),
                        start_date: Optional[date] = None,
                        end_date: Optional[date] = None) -> 'GridConfig':
        """
        从共享的总资产配置创建网格配置
        
        网格只监控总资产配置中的资产（与再平衡策略完全一致）
        
        Args:
            plan: AllocationPlan 总资产配置（与再平衡策略共用）
        """
        assets = [
            GridAssetConfig(code=a.code, name=a.name)
            for a in plan.allocations
        ]
        return cls(
            assets=assets,
            trigger=trigger or GridTrigger(),
            cost_model=cost_model or GridCostModel(),
            initial_capital=initial_capital,
            start_date=start_date,
            end_date=end_date,
        )

    @property
    def asset_codes(self) -> List[str]:
        return [a.code for a in self.assets]

    @property
    def asset_names(self) -> Dict[str, str]:
        return {a.code: a.name for a in self.assets}


@dataclass
class GridTradeRecord:
    """网格成交记录"""
    date: date
    asset_code: str
    side: str            # 'buy' / 'sell'
    level: Optional[int]
    price: Decimal
    shares: Decimal
    amount: Decimal
    cost: Decimal
    cash_after: Decimal
    trade_type: GridTradeType = GridTradeType.ADD  # 开仓/加仓/止盈
    round_no: int = 0                              # 所属网格轮次
    shares_after: Decimal = Decimal("0")           # 成交后的持仓数量

    @property
    def type_label(self) -> str:
        """交易类型中文标签"""
        return self.trade_type.label

    def to_dict(self) -> dict:
        return {
            'date': self.date,
            'asset_code': self.asset_code,
            'side': self.side,
            'trade_type': self.trade_type.value,
            'type_label': self.type_label,
            'level': self.level,
            'price': float(self.price),
            'shares': float(self.shares),
            'amount': float(self.amount),
            'cost': float(self.cost),
            'cash_after': float(self.cash_after),
            'round_no': self.round_no,
            'shares_after': float(self.shares_after),
        }


@dataclass
class GridRoundEvent:
    """
    网格轮次事件

    一个轮次 = 从跌破前N日最低价开始，到该轮持仓完全止盈结束
    """
    asset_code: str
    round_no: int
    start_date: date
    end_date: Optional[date] = None      # 完全止盈日（未结束为 None）
    levels: int = 0                      # 本轮触发档位数
    outcome: str = 'open'                # 'open' 进行中 / 'completed' 已完全止盈
    atr: Optional[Decimal] = None        # 触发日 ATR
    step: Optional[Decimal] = None       # 本轮网格步长
    anchor_price: Optional[Decimal] = None  # 本轮锚点价格

    @property
    def closed(self) -> bool:
        return self.outcome == 'completed'

    @property
    def atr_pct(self) -> Optional[Decimal]:
        """触发日 ATR 占锚点价格的比例"""
        if self.atr is None or not self.anchor_price:
            return None
        return self.atr / self.anchor_price

    def to_dict(self) -> dict:
        return {
            'asset_code': self.asset_code,
            'round_no': self.round_no,
            'start_date': self.start_date.isoformat(),
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'levels': self.levels,
            'outcome': self.outcome,
            'atr': float(self.atr) if self.atr is not None else None,
            'step': float(self.step) if self.step is not None else None,
            'anchor_price': float(self.anchor_price) if self.anchor_price is not None else None,
            'atr_pct': float(self.atr_pct) if self.atr_pct is not None else None,
        }


@dataclass
class GridSnapshot:
    """网格组合每日快照"""
    date: date
    cash: Decimal
    positions: Dict[str, Decimal]        # 资产代码 -> 持仓数量
    market_value: Decimal
    total_value: Decimal
    trades: List[GridTradeRecord] = field(default_factory=list)
    realized_pnl: Decimal = Decimal("0")  # 累计已实现交易盈亏
    floating_pnl: Decimal = Decimal("0")  # 当日浮动盈亏
    total_pnl: Decimal = Decimal("0")     # 累计总盈亏（已实现+浮动）

    def to_dict(self) -> dict:
        return {
            'date': self.date,
            'cash': float(self.cash),
            'positions': {k: float(v) for k, v in self.positions.items()},
            'market_value': float(self.market_value),
            'total_value': float(self.total_value),
            'realized_pnl': float(self.realized_pnl),
            'floating_pnl': float(self.floating_pnl),
            'total_pnl': float(self.total_pnl),
        }


@dataclass
class GridBacktestResult:
    """网格策略回测结果"""
    config: GridConfig
    strategy: GridStrategy
    snapshots: List[GridSnapshot]
    trades: List[GridTradeRecord]
    daily_values: pd.DataFrame
    round_events: List[GridRoundEvent] = field(default_factory=list)
    _statistics: Optional[ReturnStatistics] = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------
    # 绩效指标
    # ------------------------------------------------------------------

    @property
    def initial_capital(self) -> Decimal:
        return self.config.initial_capital

    @property
    def final_value(self) -> Decimal:
        return self.snapshots[-1].total_value if self.snapshots else self.initial_capital

    @property
    def statistics(self) -> ReturnStatistics:
        """绩效统计（统一口径；基准为网格初始资金）"""
        if self._statistics is None:
            self._statistics = PerformanceCalculator.calculate(
                dates=[s.date for s in self.snapshots],
                values=[s.total_value for s in self.snapshots],
                base_value=self.initial_capital,
            )
        return self._statistics

    @property
    def total_return(self) -> Decimal:
        """累计收益率"""
        return self.statistics.total_return

    @property
    def annualized_return(self) -> Decimal:
        """年化收益率（CAGR）"""
        return self.statistics.annualized_return

    @property
    def max_drawdown(self) -> tuple:
        """最大回撤 (幅度, 峰值日, 谷底日)"""
        stats = self.statistics
        return (stats.max_drawdown, stats.max_drawdown_start, stats.max_drawdown_end)

    @property
    def volatility(self) -> Decimal:
        """年化波动率"""
        return self.statistics.volatility

    @property
    def sharpe_ratio(self) -> Decimal:
        """夏普比率（无风险利率 3%）"""
        return self.statistics.sharpe_ratio

    @property
    def buy_count(self) -> int:
        return sum(1 for t in self.trades if t.side == 'buy')

    @property
    def sell_count(self) -> int:
        return sum(1 for t in self.trades if t.side == 'sell')

    @property
    def total_transaction_cost(self) -> Decimal:
        return sum((t.cost for t in self.trades), Decimal("0"))

    @property
    def total_buy_amount(self) -> Decimal:
        return sum((t.amount for t in self.trades if t.side == 'buy'), Decimal("0"))

    @property
    def total_sell_amount(self) -> Decimal:
        return sum((t.amount for t in self.trades if t.side == 'sell'), Decimal("0"))

    @property
    def completed_rounds(self) -> int:
        """已完全止盈结束的网格轮次数"""
        return sum(1 for e in self.round_events if e.closed)

    @property
    def total_rounds(self) -> int:
        """开启过的网格轮次数"""
        return len(self.round_events)

    @property
    def unrealized_pnl(self) -> Decimal:
        """网格持仓浮动盈亏"""
        return self.strategy.unrealized_pnl(self._last_prices())

    @property
    def realized_pnl(self) -> Decimal:
        """已实现交易盈亏（已平仓批次）"""
        if self.daily_values.empty or 'realized_pnl' not in self.daily_values.columns:
            return Decimal("0")
        return Decimal(str(self.daily_values['realized_pnl'].iloc[-1]))

    @property
    def total_pnl(self) -> Decimal:
        """总盈亏（已实现 + 浮动）"""
        if self.daily_values.empty or 'total_pnl' not in self.daily_values.columns:
            return Decimal("0")
        return Decimal(str(self.daily_values['total_pnl'].iloc[-1]))

    def _last_prices(self) -> Dict[str, Decimal]:
        """最后一天的收盘价"""
        if not self.snapshots:
            return {}
        last = self.daily_values.iloc[-1]
        prices = {}
        for col in last.index:
            if col.endswith('_close'):
                code = col[:-len('_close')]
                prices[code] = Decimal(str(last[col]))
        return prices

    def summary(self) -> str:
        """生成回测摘要"""
        max_dd, dd_start, dd_end = self.max_drawdown
        trigger = self.config.trigger

        lines = [
            "网格策略回测结果",
            "=" * 60,
            f"回测期间: {self.snapshots[0].date} ~ {self.snapshots[-1].date}" if self.snapshots else "无数据",
            f"网格初始资金: ¥{self.initial_capital:,.0f}",
            f"网格最终市值: ¥{self.final_value:,.0f}",
            "",
            "网格参数:",
            f"  前低窗口: {trigger.lookback_days}日",
            f"  ATR周期: {trigger.atr_period}日",
            f"  ATR比例: {trigger.step_ratio}",
            f"  单格买入: ¥{trigger.buy_amount:,.0f}",
            f"  锚点来源: {trigger.anchor.value}",
            f"  价格口径: {trigger.price_basis.value}",
            "",
            "收益指标:",
            f"  累计收益: {self.total_return:.2%}",
            f"  年化收益: {self.annualized_return:.2%}",
            f"  波动率:   {self.volatility:.2%}",
            f"  夏普比率: {self.sharpe_ratio:.2f}",
            f"  最大回撤: {max_dd:.2%} ({dd_start} ~ {dd_end})",
            "",
            "网格统计:",
            f"  买入次数: {self.buy_count}",
            f"  卖出次数: {self.sell_count}",
            f"  买入总额: ¥{self.total_buy_amount:,.0f}",
            f"  卖出总额: ¥{self.total_sell_amount:,.0f}",
            f"  总成本:   ¥{self.total_transaction_cost:,.2f}",
            f"  浮动盈亏: ¥{self.unrealized_pnl:,.2f}",
            f"  网格轮次: {self.completed_rounds}/{self.total_rounds} 轮已完全止盈",
            "",
            "各资产网格状态:",
        ]

        for code, state in self.strategy.states.items():
            name = self.config.asset_names.get(code, code)
            lines.append(
                f"  {name}({code}): {state.status.value}, "
                f"轮次{state.round_no}(完成{state.rounds_completed}), "
                f"持仓{self.strategy.portfolio.shares_of(code)}股"
            )

        return "\n".join(lines)


class GridBacktester:
    """
    网格策略回测器

    策略逻辑:
    1. 每日收盘后检查各资产收盘价
    2. 若收盘价跌破前 N 个交易日最低价 -> 激活网格（锚点 + 步长）
    3. 之后每下跌一个网格步长 -> 买入固定金额
    4. 网格资金与网格持仓独立于再平衡组合
    """

    def __init__(self,
                 config: GridConfig,
                 strategy: Optional[GridStrategy] = None,
                 price_provider: Optional[PriceDataProvider] = None):
        self.config = config
        self.price_provider = price_provider or PriceQueryService()
        self.strategy = strategy or self._build_strategy()
        self.calculator = PriceCalculator()

    def _build_strategy(self) -> GridStrategy:
        """根据配置构建网格策略聚合根"""
        return GridStrategy(
            name="网格策略",
            trigger=self.config.trigger,
            asset_codes=self.config.asset_codes,
            cost_model=self.config.cost_model,
            portfolio=Portfolio(
                portfolio_id='grid',
                name='网格组合',
                cash=Decimal(str(self.config.initial_capital)),
            ),
            status=GridStrategyStatus.ACTIVE,
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self) -> GridBacktestResult:
        """运行网格回测"""
        logger.info(
            f"开始网格回测: {len(self.config.assets)}个资产, "
            f"前低窗口={self.config.trigger.lookback_days}日, "
            f"ATR周期={self.config.trigger.atr_period}日, "
            f"单格买入=¥{self.config.trigger.buy_amount:,.0f}"
        )

        # 1. 加载价格与指标
        market_data = self._load_market_data()
        if not market_data:
            raise ValueError("没有可用的价格数据")

        # 2. 汇总交易日
        trading_days = self._collect_trading_days(market_data)
        logger.info(f"回测期间: {trading_days[0]} ~ {trading_days[-1]}, 共{len(trading_days)}个交易日")

        # 3. 逐日推进网格
        snapshots: List[GridSnapshot] = []
        trades: List[GridTradeRecord] = []
        round_events: List[GridRoundEvent] = []
        # 记录每只资产当前轮次的事件对象，便于在完全止盈时闭合
        open_rounds: Dict[str, GridRoundEvent] = {}
        # 各资产最近一次收盘价（估值用）：
        # 不同资产的交易日历不同，某资产当日可能无行情，
        # 此时其持仓应沿用最近收盘价估值，否则组合市值会被漏算、净值出现虚假断崖。
        last_close: Dict[str, Decimal] = {}
        # 说明：持仓 / 成本 / 已实现盈亏 / 交易类型均由领域层 GridState 维护，
        # 应用层不再持有影子账本，避免出现两份真相来源。

        for current_date in trading_days:
            day_trades: List[GridTradeRecord] = []

            for code in self.config.asset_codes:
                asset_data = market_data.get(code)
                if not asset_data:
                    continue

                snapshot = asset_data.snapshot_at(current_date)
                if snapshot is None:
                    continue

                # 记录该资产最新收盘价（供估值沿用）
                last_close[code] = snapshot.close

                state = self.strategy.state_for(code)
                before_round_no = state.round_no

                decision = GridEngine.evaluate(self.strategy, code, snapshot)

                # ---- 轮次事件跟踪 ----
                if state.round_no > before_round_no:
                    # 新开启一轮（跌破前N日最低价）
                    event = GridRoundEvent(
                        asset_code=code,
                        round_no=state.round_no,
                        start_date=current_date,
                        outcome='open',
                        atr=state.atr_at_trigger,
                        step=state.step,
                        anchor_price=state.anchor_price,
                    )
                    round_events.append(event)
                    open_rounds[code] = event

                for order in decision.orders:
                    # 交易类型与盈亏已由领域层在记账时判定，应用层只做展示
                    trade_type = order.trade_type or GridTradeType.ADD

                    trade = GridTradeRecord(
                        date=order.date,
                        asset_code=order.asset_code,
                        side=order.side.value,
                        level=order.level,
                        price=order.price,
                        shares=order.shares,
                        amount=order.amount,
                        cost=order.cost,
                        cash_after=self.strategy.portfolio.cash,
                        trade_type=trade_type,
                        round_no=state.round_no,
                        shares_after=(order.shares_after
                                      if order.shares_after is not None
                                      else self.strategy.portfolio.shares_of(code)),
                    )
                    day_trades.append(trade)

                # 更新进行中轮次的档位数
                event = open_rounds.get(code)
                if event is not None:
                    event.levels = len(state.filled_levels)

                if decision.round_closed:
                    # 本轮完全止盈 -> 闭合轮次
                    event = open_rounds.pop(code, None)
                    if event is None:
                        event = GridRoundEvent(
                            asset_code=code,
                            round_no=state.round_no,
                            start_date=current_date,
                            outcome='open',
                        )
                        round_events.append(event)
                    event.end_date = current_date
                    event.outcome = 'completed'
                    event.levels = len(state.filled_levels) or event.levels

            trades.extend(day_trades)

            # 记录当日网格组合快照
            # 估值价格：沿用各资产最近一次收盘价（当日无行情的资产不被漏算）
            prices = {
                code: last_close[code]
                for code in self.config.asset_codes
                if code in last_close
            }
            positions = {
                code: shares
                for code, shares in self.strategy.portfolio.position_snapshot().items()
                if shares > 0
            }
            market_value = self.strategy.positions_value(prices)

            # ---- 网格交易盈亏（已实现 + 浮动，取自组合）----
            portfolio = self.strategy.portfolio
            cost_basis = portfolio.total_cost_basis()
            realized_pnl = portfolio.realized_pnl()
            floating_pnl = market_value - cost_basis
            total_pnl = realized_pnl + floating_pnl

            snapshots.append(GridSnapshot(
                date=current_date,
                cash=self.strategy.portfolio.cash,
                positions=positions,
                market_value=market_value,
                total_value=self.strategy.portfolio.cash + market_value,
                trades=day_trades,
                realized_pnl=realized_pnl,
                floating_pnl=floating_pnl,
                total_pnl=total_pnl,
            ))

        # 4. 构建每日价值序列
        daily_values = self._build_daily_values(snapshots, market_data)

        result = GridBacktestResult(
            config=self.config,
            strategy=self.strategy,
            snapshots=snapshots,
            trades=trades,
            daily_values=daily_values,
            round_events=round_events,
        )

        logger.info(
            f"网格回测完成: 累计收益{result.total_return:.2%}, "
            f"买入{result.buy_count}次, 卖出{result.sell_count}次"
        )

        return result

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def _load_market_data(self) -> Dict[str, 'AssetMarketData']:
        """加载价格数据并计算网格所需指标"""
        market_data: Dict[str, AssetMarketData] = {}

        for asset in self.config.assets:
            try:
                df = self.price_provider.get_adjusted_prices(
                    asset.code,
                    start_date=self._fetch_start_date(),
                    end_date=self.config.end_date.isoformat() if self.config.end_date else None,
                )
            except Exception as e:
                logger.error(f"加载 {asset.code} 价格失败: {e}")
                continue

            if df is None or df.empty:
                logger.warning(f"{asset.code} 无价格数据")
                continue

            series = self._build_series(asset.code, df)
            if series.count == 0:
                logger.warning(f"{asset.code} 价格数据无效")
                continue

            market_data[asset.code] = self._build_asset_market_data(series)
            logger.info(f"加载 {asset.code} 价格数据: {series.count}条")

        return market_data

    def _fetch_start_date(self) -> Optional[str]:
        """
        实际拉取行情的起始日期

        比回测开始日提前一段，用于 ATR / 前N日最低价的指标预热，
        避免回测初期因历史不足而无法触发网格
        """
        if not self.config.start_date:
            return None

        # 预留约 1.6 倍日历天数（交易日 * 1.6 ≈ 日历天数）
        buffer_days = int(WARMUP_TRADING_DAYS * 1.6)
        warmup_start = self.config.start_date - timedelta(days=buffer_days)
        return warmup_start.isoformat()

    def _build_series(self, code: str, df: pd.DataFrame) -> AdjustedPriceSeries:
        """DataFrame -> 领域价格序列"""
        data = df.copy()

        if 'volume' not in data.columns:
            data['volume'] = 0
        if 'amount' not in data.columns:
            data['amount'] = 0

        data = data.drop(columns=['source'], errors='ignore')

        return AdjustedPriceSeries.from_dataframe(
            asset_code=AssetCode.from_string(code),
            df=data,
            source=DataSource.EASTMONEY_HFQ,
        )

    def _build_asset_market_data(self, series: AdjustedPriceSeries) -> 'AssetMarketData':
        """计算单个资产的网格指标"""
        trigger = self.config.trigger

        atr_map = dict(self.calculator.calculate_atr_series(series, trigger.atr_period))
        low_map = dict(self.calculator.calculate_prior_low_series(series, trigger.lookback_days))

        closes: Dict[date, Decimal] = {}
        atrs: Dict[date, Optional[Decimal]] = {}
        prior_lows: Dict[date, Optional[Decimal]] = {}
        opens: Dict[date, Decimal] = {}
        highs: Dict[date, Decimal] = {}
        lows: Dict[date, Decimal] = {}

        for point in series.prices:
            closes[point.date] = point.close
            atrs[point.date] = atr_map.get(point.date)
            prior_lows[point.date] = low_map.get(point.date)
            opens[point.date] = point.open
            highs[point.date] = point.high
            lows[point.date] = point.low

        return AssetMarketData(
            closes=closes,
            atrs=atrs,
            prior_lows=prior_lows,
            opens=opens,
            highs=highs,
            lows=lows,
        )

    def _collect_trading_days(self, market_data: Dict[str, 'AssetMarketData']) -> List[date]:
        """汇总所有资产的交易日（并集）"""
        all_days = set()
        for asset_data in market_data.values():
            all_days.update(asset_data.closes.keys())

        days = sorted(all_days)

        if self.config.start_date:
            days = [d for d in days if d >= self.config.start_date]
        if self.config.end_date:
            days = [d for d in days if d <= self.config.end_date]

        return days

    def _build_daily_values(self,
                            snapshots: List[GridSnapshot],
                            market_data: Dict[str, 'AssetMarketData']) -> pd.DataFrame:
        """构建每日价值 DataFrame"""
        records = []
        for snapshot in snapshots:
            record = {
                'date': snapshot.date,
                'total_value': float(snapshot.total_value),
                'cash': float(snapshot.cash),
                'market_value': float(snapshot.market_value),
                'realized_pnl': float(snapshot.realized_pnl),
                'floating_pnl': float(snapshot.floating_pnl),
                'total_pnl': float(snapshot.total_pnl),
            }
            for code in self.config.asset_codes:
                # 统一记录每个资产的份额（无持仓记 0），便于按资产绘图
                record[f'{code}_shares'] = float(snapshot.positions.get(code, Decimal("0")))
            for code, asset_data in market_data.items():
                close = asset_data.close_at(snapshot.date)
                if close is not None:
                    record[f'{code}_close'] = float(close)
            records.append(record)

        df = pd.DataFrame(records)
        if df.empty:
            return df

        df.set_index('date', inplace=True)

        # 补充缺失的收盘价列（保持列结构稳定）
        for code in self.config.asset_codes:
            col = f'{code}_close'
            if col not in df.columns:
                df[col] = np.nan
            else:
                df[col] = df[col].ffill()

        # 份额列缺失补 0
        for code in self.config.asset_codes:
            col = f'{code}_shares'
            if col not in df.columns:
                df[col] = 0.0
            else:
                df[col] = df[col].fillna(0.0)

        return df


@dataclass
class AssetMarketData:
    """单资产行情与指标数据（应用层内部结构）"""
    closes: Dict[date, Decimal] = field(default_factory=dict)
    atrs: Dict[date, Optional[Decimal]] = field(default_factory=dict)
    prior_lows: Dict[date, Optional[Decimal]] = field(default_factory=dict)
    opens: Dict[date, Decimal] = field(default_factory=dict)
    highs: Dict[date, Decimal] = field(default_factory=dict)
    lows: Dict[date, Decimal] = field(default_factory=dict)

    def close_at(self, target_date: date) -> Optional[Decimal]:
        return self.closes.get(target_date)

    def snapshot_at(self, target_date: date) -> Optional[GridMarketSnapshot]:
        """构造某交易日的行情快照"""
        close = self.closes.get(target_date)
        if close is None:
            return None

        return GridMarketSnapshot(
            date=target_date,
            close=close,
            prior_low=self.prior_lows.get(target_date),
            atr=self.atrs.get(target_date),
            open=self.opens.get(target_date),
            high=self.highs.get(target_date),
            low=self.lows.get(target_date),
        )
