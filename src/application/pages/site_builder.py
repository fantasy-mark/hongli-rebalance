"""
GitHub Pages 站点构建用例（应用层）

职责（只编排，不做 IO 细节）:
1. 跑一次组合回测（再平衡 + 网格，持仓隔离）
2. 把回测结果翻译成领域对象 `StrategyOverview`
3. 把回测结果翻译成页面视图模型（summary / trades / holdings）
4. 交给基础设施层落盘（JSON 仓储 + 渲染器）

不依赖 matplotlib、不直接写文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from src.application.grid.combined_backtester import (
    CombinedBacktester,
    CombinedBacktestResult,
    CombinedConfig,
)
from src.domain.allocation.entities import AllocationPlan
from src.domain.chart.overview import (
    CapitalOccupation,
    CurveRole,
    ReturnCurve,
    StrategyOverview,
)
from src.domain.grid.value_objects import GridPriceBasis, GridTrigger

ZERO = Decimal("0")
HUNDRED = Decimal("100")


# --------------------------------------------------------------------- 视图模型
@dataclass(frozen=True)
class PositionView:
    """单个持仓"""

    code: str
    name: str
    shares: Decimal
    price: Decimal
    market_value: Decimal
    weight: Decimal  # 占总市值比例（0~1）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "shares": float(self.shares),
            "price": float(self.price),
            "market_value": float(self.market_value),
            "weight": float(self.weight),
        }


@dataclass(frozen=True)
class StrategyView:
    """单个策略（再平衡 / 网格）的视图"""

    key: str
    label: str
    initial_capital: Decimal
    total_value: Decimal
    cash: Decimal
    market_value: Decimal
    total_return: Decimal
    annualized_return: Decimal
    max_drawdown: Decimal
    sharpe_ratio: Decimal
    volatility: Decimal
    trade_count: int
    positions: List[PositionView] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "initial_capital": float(self.initial_capital),
            "total_value": float(self.total_value),
            "cash": float(self.cash),
            "market_value": float(self.market_value),
            "total_return": float(self.total_return),
            "annualized_return": float(self.annualized_return),
            "max_drawdown": float(self.max_drawdown),
            "sharpe_ratio": float(self.sharpe_ratio),
            "volatility": float(self.volatility),
            "trade_count": self.trade_count,
            "positions": [p.to_dict() for p in self.positions],
        }
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class TradeView:
    """一笔交易"""

    date: str
    asset_code: str
    asset_name: str
    action: str          # trade_type：open / add / reduce / take_profit
    side: str            # buy / sell
    round_no: int
    price: Decimal
    shares: Decimal
    amount: Decimal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "asset_code": self.asset_code,
            "asset_name": self.asset_name,
            "action": self.action,
            "side": self.side,
            "round_no": self.round_no,
            "price": float(self.price),
            "shares": float(self.shares),
            "amount": float(self.amount),
        }


@dataclass(frozen=True)
class SummaryView:
    """站点汇总数据"""

    generated_at: str
    window_start: date
    window_end: date
    combined: StrategyView
    rebalance: StrategyView
    grid: StrategyView

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
            },
            "combined": self.combined.to_dict(),
            "rebalance": self.rebalance.to_dict(),
            "grid": self.grid.to_dict(),
        }


# --------------------------------------------------------------------- 用例
class BuildPagesSite:
    """构建 GitHub Pages 站点数据的用例"""

    def __init__(
        self,
        config: CombinedConfig,
        backtester: Optional[CombinedBacktester] = None,
    ) -> None:
        self.config = config
        self._backtester = backtester or CombinedBacktester(config)
        self._asset_names: Dict[str, str] = {
            a.code: a.name for a in config.allocation.allocations
        }

    # -------------------------------------------------------------- 主流程
    def execute(self) -> "PagesSiteData":
        """跑回测并产出领域对象 + 视图模型"""
        result = self._backtester.run()

        overview = self.build_overview(result)
        summary = self.build_summary(result)
        trades = self.build_trades(result)

        return PagesSiteData(
            overview=overview,
            summary=summary,
            trades=trades,
            result=result,
        )

    # -------------------------------------------------------------- 全景图
    def build_overview(self, result: CombinedBacktestResult) -> StrategyOverview:
        """把回测结果翻译成策略全景图"""
        grid_dv = result.grid_result.daily_values
        rebalance_dv = result.rebalance_result.daily_values

        grid_dates = [self._as_date(i) for i in grid_dv.index]
        rebalance_dates = [self._as_date(i) for i in rebalance_dv.index]

        curves: List[ReturnCurve] = []

        # 1) 各资产买入持有（用网格侧的收盘价，与占用同源）
        for asset in self.config.allocation.allocations:
            col = f"{asset.code}_close"
            if col not in grid_dv.columns:
                continue
            levels = grid_dv[col].astype(float).tolist()
            curves.append(
                ReturnCurve.from_levels(
                    name=asset.name,
                    role=CurveRole.ASSET,
                    dates=grid_dates,
                    levels=levels,
                )
            )

        # 2) 再平衡组合净收益率
        curves.append(
            ReturnCurve.from_levels(
                name=f"{float(self.config.threshold_pp):g}pp 再平衡",
                role=CurveRole.REBALANCE,
                dates=rebalance_dates,
                levels=rebalance_dv["total_value"].astype(float).tolist(),
            )
        )

        # 3) 资金占用（网格侧：shares × close）
        occupations: List[CapitalOccupation] = []
        for asset in self.config.allocation.allocations:
            shares_col = f"{asset.code}_shares"
            close_col = f"{asset.code}_close"
            if shares_col not in grid_dv.columns or close_col not in grid_dv.columns:
                continue
            shares = grid_dv[shares_col].astype(float)
            close = grid_dv[close_col].astype(float)
            values = tuple(
                (Decimal(str(s)) * Decimal(str(c))).quantize(Decimal("0.01"))
                for s, c in zip(shares.tolist(), close.tolist())
            )
            occupations.append(
                CapitalOccupation(
                    asset_code=asset.code,
                    asset_name=asset.name,
                    dates=tuple(grid_dates),
                    values=values,
                )
            )

        totals = [
            sum((occ.values[i] for occ in occupations), ZERO)
            for i in range(len(grid_dates))
        ]
        peak_occupation = max(totals) if totals else ZERO

        # 4) 网格 ROI = 网格累计收益 / 占用峰值
        pnl = [Decimal(str(v)) for v in grid_dv["total_pnl"].astype(float).tolist()]
        if peak_occupation > 0:
            roi_values = tuple(
                (v / peak_occupation * HUNDRED).quantize(Decimal("0.0001"))
                for v in pnl
            )
        else:
            roi_values = tuple(ZERO for _ in pnl)
        curves.append(
            ReturnCurve(
                name="网格 ROI",
                role=CurveRole.GRID_ROI,
                dates=tuple(grid_dates),
                values=roi_values,
            )
        )

        # 5) 下 panel 虚线：网格累计收益
        curves.append(
            ReturnCurve(
                name="网格累计收益",
                role=CurveRole.GRID_PROFIT,
                dates=tuple(grid_dates),
                values=tuple(v.quantize(Decimal("0.01")) for v in pnl),
            )
        )

        asset_codes = list(self.config.allocation.asset_codes)
        occupation_order = {code: i for i, code in enumerate(asset_codes)}
        occupations.sort(key=lambda o: occupation_order.get(o.asset_code, 999))

        return StrategyOverview(
            window_start=grid_dates[0],
            window_end=grid_dates[-1],
            return_curves=tuple(curves),
            occupations=tuple(occupations),
            peak_occupation=peak_occupation,
            grid_rounds_completed=int(result.grid_result.completed_rounds),
            grid_rounds_total=int(result.grid_result.total_rounds),
        )

    # -------------------------------------------------------------- 汇总
    def build_summary(self, result: CombinedBacktestResult) -> SummaryView:
        """构建 summary 视图"""
        last_grid = result.grid_result.daily_values.iloc[-1]
        last_rebalance = result.rebalance_result.daily_values.iloc[-1]

        rebalance = self._build_strategy_view(
            key="rebalance",
            label="再平衡组合",
            stats=result.rebalance_result,
            snapshot=result.rebalance_result.snapshots[-1],
            last_row=last_rebalance,
            initial_capital=self.config.rebalance_initial_capital,
        )
        grid = self._build_strategy_view(
            key="grid",
            label="网格组合",
            stats=result.grid_result,
            snapshot=result.grid_result.snapshots[-1],
            last_row=last_grid,
            initial_capital=self.config.grid_initial_capital,
            is_grid=True,
        )
        grid = StrategyView(
            key=grid.key,
            label=grid.label,
            initial_capital=grid.initial_capital,
            total_value=grid.total_value,
            cash=grid.cash,
            market_value=grid.market_value,
            total_return=grid.total_return,
            annualized_return=grid.annualized_return,
            max_drawdown=grid.max_drawdown,
            sharpe_ratio=grid.sharpe_ratio,
            volatility=grid.volatility,
            trade_count=grid.trade_count,
            positions=grid.positions,
            extra={
                "buy_count": int(result.grid_result.buy_count),
                "sell_count": int(result.grid_result.sell_count),
                "completed_rounds": int(result.grid_result.completed_rounds),
                "total_rounds": int(result.grid_result.total_rounds),
            },
        )

        combined = self._build_combined_view(result, rebalance, grid)

        combined_dv = result.daily_values
        return SummaryView(
            generated_at=date.today().isoformat(),
            window_start=self._as_date(combined_dv.index[0]),
            window_end=self._as_date(combined_dv.index[-1]),
            combined=combined,
            rebalance=rebalance,
            grid=grid,
        )

    def _build_combined_view(
        self,
        result: CombinedBacktestResult,
        rebalance: StrategyView,
        grid: StrategyView,
    ) -> StrategyView:
        # 两个策略可能持同一标的，按代码合并股数与市值
        merged: Dict[str, Dict[str, Any]] = {}
        for p in list(rebalance.positions) + list(grid.positions):
            slot = merged.setdefault(
                p.code,
                {"name": p.name, "shares": ZERO, "market_value": ZERO},
            )
            slot["shares"] += p.shares
            slot["market_value"] += p.market_value

        total_value = rebalance.total_value + grid.total_value
        cash_total = rebalance.cash + grid.cash

        positions: List[PositionView] = []
        for code, slot in sorted(merged.items()):
            shares = slot["shares"]
            market_value = slot["market_value"]
            positions.append(
                PositionView(
                    code=code,
                    name=slot["name"],
                    shares=shares,
                    price=(market_value / shares) if shares else ZERO,
                    market_value=market_value,
                    weight=(market_value / total_value) if total_value else ZERO,
                )
            )

        max_dd, _start, _end = result.max_drawdown
        return StrategyView(
            key="combined",
            label="组合合计",
            initial_capital=result.config.initial_capital,
            total_value=total_value,
            cash=cash_total,
            market_value=total_value - cash_total,
            total_return=result.total_return,
            annualized_return=result.annualized_return,
            max_drawdown=max_dd,
            sharpe_ratio=result.sharpe_ratio,
            volatility=result.volatility,
            trade_count=rebalance.trade_count + grid.trade_count,
            positions=positions,
        )

    def _build_strategy_view(
        self,
        key: str,
        label: str,
        stats: Any,
        snapshot: Any,
        last_row: Any,
        initial_capital: Decimal,
        is_grid: bool = False,
    ) -> StrategyView:
        position_shares: Dict[str, Decimal]
        if is_grid:
            position_shares = {
                k: Decimal(str(v)) for k, v in snapshot.positions.items()
            }
        else:
            position_shares = {
                k: Decimal(str(v)) for k, v in snapshot.asset_shares.items()
            }

        total_value = Decimal(str(snapshot.total_value))
        cash = Decimal(str(snapshot.cash))

        positions: List[PositionView] = []
        for code, shares in position_shares.items():
            if shares <= 0:
                continue
            if is_grid:
                price = Decimal(str(last_row.get(f"{code}_close", 0) or 0))
            else:
                price = (
                    Decimal(str(last_row[f"{code}_value"])) / shares
                    if shares else ZERO
                )
            market_value = shares * price
            positions.append(
                PositionView(
                    code=code,
                    name=self._asset_names.get(code, code),
                    shares=shares,
                    price=price,
                    market_value=market_value,
                    weight=(market_value / total_value) if total_value else ZERO,
                )
            )
        positions.sort(key=lambda p: p.code)

        max_dd, _s, _e = stats.max_drawdown
        trade_count = (
            len(stats.trades) if is_grid else len(stats.all_trades)
        )

        return StrategyView(
            key=key,
            label=label,
            initial_capital=Decimal(str(initial_capital)),
            total_value=total_value,
            cash=cash,
            market_value=total_value - cash,
            total_return=stats.total_return,
            annualized_return=stats.annualized_return,
            max_drawdown=max_dd,
            sharpe_ratio=stats.sharpe_ratio,
            volatility=stats.volatility,
            trade_count=trade_count,
            positions=positions,
        )

    # -------------------------------------------------------------- 交易记录
    def build_trades(self, result: CombinedBacktestResult) -> List[TradeView]:
        """构建交易记录视图（网格成交明细）"""
        views: List[TradeView] = []
        for t in result.grid_result.trades:
            code = getattr(t, "asset_code", "")
            trade_type = getattr(t, "trade_type", None)
            action = (
                trade_type.value if hasattr(trade_type, "value") else str(trade_type or "")
            )
            views.append(
                TradeView(
                    date=t.date.isoformat() if hasattr(t.date, "isoformat") else str(t.date),
                    asset_code=code,
                    asset_name=self._asset_names.get(code, code),
                    action=action,
                    side=getattr(t, "side", ""),
                    round_no=int(getattr(t, "round_no", 0) or 0),
                    price=Decimal(str(getattr(t, "price", 0) or 0)),
                    shares=Decimal(str(getattr(t, "shares", 0) or 0)),
                    amount=Decimal(str(getattr(t, "amount", 0) or 0)),
                )
            )
        return views

    # -------------------------------------------------------------- 工具
    @staticmethod
    def _as_date(value: Any) -> date:
        if isinstance(value, date):
            return value
        if hasattr(value, "date"):
            return value.date()
        return date.fromisoformat(str(value)[:10])


@dataclass
class PagesSiteData:
    """用例输出"""

    overview: StrategyOverview
    summary: SummaryView
    trades: List[TradeView]
    result: CombinedBacktestResult


# --------------------------------------------------------------------- 默认配置
def default_config(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> CombinedConfig:
    """站点默认回测配置（4 资产等权 + 3pp 再平衡 + 网格）"""
    plan = AllocationPlan.equal_weight(
        [
            ("515180", "红利ETF"),
            ("159937", "黄金ETF"),
            ("511380", "可转债ETF"),
            ("513300", "纳斯达克ETF"),
        ]
    )
    return CombinedConfig(
        allocation=plan,
        threshold_pp=Decimal("3"),
        rebalance_initial_capital=Decimal("1000000"),
        rebalance_transaction_cost_bps=Decimal("5"),
        grid_initial_capital=Decimal("200000"),
        grid_trigger=GridTrigger(
            lookback_days=120,
            atr_period=120,
            step_ratio=Decimal("0.5"),
            buy_amount=Decimal("10000"),
            price_basis=GridPriceBasis.LOW,
            take_profit_ratio=Decimal("1"),
        ),
        start_date=start_date or date(2020, 11, 5),
        end_date=end_date,
    )
