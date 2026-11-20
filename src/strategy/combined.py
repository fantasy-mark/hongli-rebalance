"""
网格 + 再平衡组合回测（策略模块）

隔离约定:
- 再平衡子组合: RebalanceBacktester 独立管理自己的现金与持仓
- 网格子组合:   GridBacktester 独立管理自己的现金与持仓
- 两者互不读取、互不修改对方的现金与持仓
- 组合总市值 = 再平衡子组合市值 + 网格子组合市值

共享配置:
- 两个策略共用同一份 **AllocationPlan（总资产配置）**
- 网格只监控总资产配置中的资产，保证与再平衡组合一致
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional
import logging

import pandas as pd

from src.data.service import PriceQueryService
from src.strategy.rebalance.backtest import (
    RebalanceBacktester,
    RebalanceConfig,
)
from src.strategy.grid.backtest import (
    GridBacktester,
    GridBacktestResult,
    GridConfig,
)
from src.strategy.allocation import AllocationPlan
from src.strategy.performance import PerformanceCalculator, ReturnStatistics
from src.strategy.grid.models import GridCostModel, GridTrigger

logger = logging.getLogger(__name__)


@dataclass
class CombinedConfig:
    """
    组合回测配置

    以 **总资产配置（AllocationPlan）** 为唯一资产来源，
    再平衡与网格两个策略从同一份配置派生，监控的资产完全一致。
    """
    allocation: AllocationPlan
    threshold_pp: Decimal = Decimal("3")                    # 再平衡触发阈值（百分点）
    rebalance_initial_capital: Decimal = Decimal("1000000")  # 再平衡子组合资金
    rebalance_transaction_cost_bps: Decimal = Decimal("5")   # 再平衡交易成本
    grid_initial_capital: Decimal = Decimal("200000")        # 网格子组合资金（独立资金池）
    grid_trigger: GridTrigger = field(default_factory=GridTrigger)
    grid_cost_model: GridCostModel = field(default_factory=GridCostModel)
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    @property
    def initial_capital(self) -> Decimal:
        """组合初始资金（两个子组合之和）"""
        return (Decimal(str(self.rebalance_initial_capital))
                + Decimal(str(self.grid_initial_capital)))

    def build_rebalance_config(self) -> RebalanceConfig:
        """由总资产配置派生再平衡配置"""
        return RebalanceConfig.from_allocation(
            plan=self.allocation,
            threshold_pp=self.threshold_pp,
            initial_capital=Decimal(str(self.rebalance_initial_capital)),
            transaction_cost_bps=Decimal(str(self.rebalance_transaction_cost_bps)),
            start_date=self.start_date,
            end_date=self.end_date,
        )

    def build_grid_config(self) -> GridConfig:
        """由总资产配置派生网格配置（监控同一批资产）"""
        return GridConfig.from_allocation(
            plan=self.allocation,
            trigger=self.grid_trigger,
            cost_model=self.grid_cost_model,
            initial_capital=Decimal(str(self.grid_initial_capital)),
            start_date=self.start_date,
            end_date=self.end_date,
        )


@dataclass
class CombinedBacktestResult:
    """组合回测结果"""
    config: CombinedConfig
    rebalance_result: object          # RebalanceBacktestResult
    grid_result: GridBacktestResult
    daily_values: pd.DataFrame        # rebalance_value / grid_value / total_value
    _statistics: Optional[ReturnStatistics] = field(default=None, repr=False, compare=False)

    @property
    def initial_capital(self) -> Decimal:
        return self.config.initial_capital

    @property
    def base_value(self) -> Decimal:
        """组合起始市值（两子组合共同区间的首日合计市值）"""
        if self.daily_values.empty:
            return self.initial_capital
        return Decimal(str(self.daily_values['total_value'].iloc[0]))

    @property
    def final_value(self) -> Decimal:
        if self.daily_values.empty:
            return self.base_value
        return Decimal(str(self.daily_values['total_value'].iloc[-1]))

    @property
    def statistics(self) -> ReturnStatistics:
        """绩效统计（统一口径；基准为共同区间首日合计市值）"""
        if self._statistics is None:
            index = self.daily_values.index
            values = [Decimal(str(v)) for v in self.daily_values['total_value'].tolist()]
            dates = [i.date() if hasattr(i, 'date') else i for i in index]
            self._statistics = PerformanceCalculator.calculate(
                dates=dates,
                values=values,
                base_value=self.base_value,
            )
        return self._statistics

    @property
    def total_return(self) -> Decimal:
        """组合累计收益率（相对共同区间首日市值）"""
        return self.statistics.total_return

    @property
    def annualized_return(self) -> Decimal:
        """组合年化收益率"""
        return self.statistics.annualized_return

    @property
    def max_drawdown(self) -> tuple:
        """组合最大回撤"""
        stats = self.statistics
        return (stats.max_drawdown, stats.max_drawdown_start, stats.max_drawdown_end)

    @property
    def volatility(self) -> Decimal:
        """组合年化波动率"""
        return self.statistics.volatility

    @property
    def sharpe_ratio(self) -> Decimal:
        """组合夏普比率"""
        return self.statistics.sharpe_ratio

    def position_breakdown(self) -> Dict[str, Dict[str, Any]]:
        """
        持仓明细（按策略拆分）

        展示两个策略各自的现金与持仓，直观验证持仓隔离。

        Returns:
            {策略名: {'cash': 金额, 'positions': {代码: 数量}, 'market_value': 金额}}
        """
        rebalance_snapshot = self.rebalance_result.snapshots[-1]
        grid_snapshot = self.grid_result.snapshots[-1]

        return {
            '再平衡组合': {
                'cash': rebalance_snapshot.cash,
                'positions': dict(rebalance_snapshot.asset_shares),
                'market_value': (rebalance_snapshot.total_value
                                 - rebalance_snapshot.cash),
            },
            '网格组合': {
                'cash': grid_snapshot.cash,
                'positions': dict(grid_snapshot.positions),
                'market_value': grid_snapshot.market_value,
            },
        }

    def summary(self) -> str:
        """生成组合摘要"""
        max_dd, dd_start, dd_end = self.max_drawdown

        lines = [
            "组合回测结果（再平衡 + 网格，持仓隔离）",
            "=" * 60,
            f"共享总资产配置: {self.config.allocation.name}",
            f"  资产: {', '.join(self.config.allocation.asset_codes)}",
            f"配置初始资金: ¥{self.initial_capital:,.0f}",
            f"共同区间起始市值: ¥{self.base_value:,.0f}",
            f"组合最终市值: ¥{self.final_value:,.0f}",
            f"共同区间: {self.daily_values.index[0]} ~ {self.daily_values.index[-1]}"
            if not self.daily_values.empty else "无共同区间",
            "",
            "收益指标:",
            f"  累计收益: {self.total_return:.2%}",
            f"  年化收益: {self.annualized_return:.2%}",
            f"  波动率:   {self.volatility:.2%}",
            f"  夏普比率: {self.sharpe_ratio:.2f}",
            f"  最大回撤: {max_dd:.2%} ({dd_start} ~ {dd_end})",
            "",
            "子组合归因:",
            f"  再平衡组合: 初始 ¥{self.config.rebalance_initial_capital:,.0f}, "
            f"收益 {self.rebalance_result.total_return:.2%}",
            f"  网格组合:   初始 ¥{self.config.grid_initial_capital:,.0f}, "
            f"收益 {self.grid_result.total_return:.2%}",
            f"  网格成交:   买入{self.grid_result.buy_count}次 / 卖出{self.grid_result.sell_count}次",
            "",
            "持仓明细（两策略隔离）:",
        ]

        for name, detail in self.position_breakdown().items():
            positions = {k: float(v) for k, v in detail["positions"].items() if v > 0}
            lines.append(
                f"  {name}: 现金 ¥{float(detail['cash']):,.2f}  "
                f"持仓市值 ¥{float(detail['market_value']):,.2f}  {positions}"
            )

        return "\n".join(lines)


class CombinedBacktester:
    """
    组合回测器

    分别运行再平衡与网格两个子组合，再合并净值曲线
    """

    def __init__(self,
                 config: CombinedConfig,
                 price_service: Optional[PriceQueryService] = None):
        self.config = config
        # 共享价格服务（复用缓存，避免重复拉取）
        self.price_service = price_service or PriceQueryService()

    def run(self) -> CombinedBacktestResult:
        """运行组合回测"""
        logger.info("开始组合回测: 再平衡 + 网格（共享总资产配置，持仓隔离）")

        # ---- 再平衡子组合 ----
        rebalance_backtester = RebalanceBacktester(
            self.config.build_rebalance_config(),
            price_service=self.price_service,   # 共享同一实例，复用缓存
        )
        rebalance_result = rebalance_backtester.run()

        # ---- 网格子组合（监控同一批资产）----
        grid_backtester = GridBacktester(
            config=self.config.build_grid_config(),
            price_provider=self.price_service,
        )
        grid_result = grid_backtester.run()

        # ---- 合并净值 ----
        daily_values = self._merge_values(rebalance_result, grid_result)

        result = CombinedBacktestResult(
            config=self.config,
            rebalance_result=rebalance_result,
            grid_result=grid_result,
            daily_values=daily_values,
        )

        logger.info(
            f"组合回测完成: 累计收益{result.total_return:.2%}, "
            f"再平衡{rebalance_result.total_return:.2%}, 网格{grid_result.total_return:.2%}"
        )

        return result

    def _merge_values(self, rebalance_result, grid_result) -> pd.DataFrame:
        """
        合并两个子组合的每日净值
        
        只取两子组合都有数据的共同区间，避免拼接失真
        """
        rebalance_df = rebalance_result.daily_values[['total_value']].copy()
        rebalance_df = rebalance_df.rename(columns={'total_value': 'rebalance_value'})

        grid_df = grid_result.daily_values[['total_value']].copy()
        grid_df = grid_df.rename(columns={'total_value': 'grid_value'})

        merged = rebalance_df.join(grid_df, how='inner').sort_index()
        merged = merged.dropna()
        merged['total_value'] = merged['rebalance_value'] + merged['grid_value']

        return merged
