"""图表领域值对象 + Pages 用例测试

覆盖:
- domain/chart/overview.py 的不变量与派生量
- application/pages/site_builder.py 的翻译逻辑（用桩回测结果，不联网）
"""

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.chart.overview import (
    CapitalOccupation,
    CurveRole,
    ReturnCurve,
    StrategyOverview,
)


D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)


# ------------------------------------------------------------------ ReturnCurve
class TestReturnCurve:
    def test_from_levels_normalizes_to_zero_percent(self):
        curve = ReturnCurve.from_levels(
            name="组合",
            role=CurveRole.REBALANCE,
            dates=[D1, D2, D3],
            levels=[100.0, 110.0, 90.0],
        )
        assert curve.values[0] == Decimal("0")
        assert curve.values[1] == Decimal("10.00")
        assert curve.values[2] == Decimal("-10.00")
        assert curve.final_return == Decimal("-10.00")

    def test_rejects_length_mismatch(self):
        with pytest.raises(ValueError, match="长度不一致"):
            ReturnCurve(
                name="x", role=CurveRole.ASSET,
                dates=(D1, D2), values=(Decimal("0"),),
            )

    def test_rejects_empty_dates(self):
        with pytest.raises(ValueError, match="日期序列不能为空"):
            ReturnCurve(
                name="x", role=CurveRole.ASSET, dates=(), values=(),
            )

    def test_start_end_date(self):
        curve = ReturnCurve.from_levels(
            "a", CurveRole.ASSET, [D1, D2, D3], [1, 2, 3],
        )
        assert curve.start_date == D1
        assert curve.end_date == D3


# ------------------------------------------------------------- CapitalOccupation
class TestCapitalOccupation:
    def test_peak_and_current(self):
        occ = CapitalOccupation(
            asset_code="515180",
            asset_name="红利ETF",
            dates=(D1, D2, D3),
            values=(Decimal("0"), Decimal("5000"), Decimal("2000")),
        )
        assert occ.peak == Decimal("5000")
        assert occ.current == Decimal("2000")

    def test_rejects_negative_occupation(self):
        with pytest.raises(ValueError, match="不能为负"):
            CapitalOccupation(
                asset_code="515180", asset_name="红利ETF",
                dates=(D1,), values=(Decimal("-1"),),
            )

    def test_rejects_length_mismatch(self):
        with pytest.raises(ValueError, match="长度不一致"):
            CapitalOccupation(
                asset_code="x", asset_name="x",
                dates=(D1, D2), values=(Decimal("0"),),
            )


# --------------------------------------------------------------- StrategyOverview
def _overview() -> StrategyOverview:
    dates = (D1, D2, D3)
    return StrategyOverview(
        window_start=D1,
        window_end=D3,
        return_curves=(
            ReturnCurve.from_levels("红利ETF", CurveRole.ASSET, dates, [10, 11, 12]),
            ReturnCurve.from_levels("3pp 再平衡", CurveRole.REBALANCE, dates, [100, 105, 110]),
            ReturnCurve(
                name="网格 ROI", role=CurveRole.GRID_ROI,
                dates=dates, values=(Decimal("0"), Decimal("1"), Decimal("2")),
            ),
        ),
        occupations=(
            CapitalOccupation("515180", "红利ETF", dates,
                              (Decimal("0"), Decimal("1000"), Decimal("500"))),
            CapitalOccupation("159937", "黄金ETF", dates,
                              (Decimal("0"), Decimal("2000"), Decimal("0"))),
        ),
        peak_occupation=Decimal("3000"),
        grid_rounds_completed=2,
        grid_rounds_total=3,
    )


class TestStrategyOverview:
    def test_curve_lookup_by_role(self):
        ov = _overview()
        assert ov.curve(CurveRole.REBALANCE).name == "3pp 再平衡"
        assert ov.curve(CurveRole.GRID_ROI).final_return == Decimal("2")
        assert ov.curve(CurveRole.GRID_PROFIT) is None

    def test_total_occupation_series(self):
        ov = _overview()
        assert ov.total_occupation_series == [
            Decimal("0"), Decimal("3000"), Decimal("500"),
        ]

    def test_current_occupation(self):
        ov = _overview()
        assert ov.current_occupation == Decimal("500")

    def test_occupation_by_code(self):
        ov = _overview()
        assert set(ov.occupation_by_code()) == {"515180", "159937"}

    def test_duration_years(self):
        ov = _overview()
        assert ov.duration_years() == pytest.approx(2 / 365.25, rel=1e-6)

    def test_rejects_empty_curves(self):
        with pytest.raises(ValueError, match="至少需要一条收益率曲线"):
            StrategyOverview(
                window_start=D1, window_end=D3,
                return_curves=(), occupations=(),
                peak_occupation=Decimal("0"),
            )

    def test_rejects_negative_peak(self):
        with pytest.raises(ValueError, match="峰值资金占用不能为负"):
            StrategyOverview(
                window_start=D1, window_end=D3,
                return_curves=(ReturnCurve.from_levels(
                    "a", CurveRole.ASSET, (D1,), [1]),),
                occupations=(),
                peak_occupation=Decimal("-1"),
            )

    def test_rejects_reversed_window(self):
        with pytest.raises(ValueError, match="结束日期不能早于开始日期"):
            StrategyOverview(
                window_start=D3, window_end=D1,
                return_curves=(ReturnCurve.from_levels(
                    "a", CurveRole.ASSET, (D1,), [1]),),
                occupations=(), peak_occupation=Decimal("0"),
            )


# ------------------------------------------------------- BuildPagesSite 翻译逻辑
class _StubBacktester:
    """不联网的假回测器，返回构造好的 daily_values / snapshot"""

    def __init__(self, result):
        self._result = result

    def run(self):
        return self._result


def _stub_combined_result():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])

    grid_dv = pd.DataFrame(
        {
            "total_value": [200000.0, 201000.0, 202000.0],
            "cash": [200000.0, 190000.0, 192000.0],
            "market_value": [0.0, 11000.0, 10000.0],
            "realized_pnl": [0.0, 0.0, 500.0],
            "floating_pnl": [0.0, 100.0, 200.0],
            "total_pnl": [0.0, 100.0, 700.0],
            "515180_shares": [0.0, 1000.0, 1000.0],
            "159937_shares": [0.0, 1000.0, 1000.0],
            "515180_close": [1.0, 1.0, 1.0],
            "159937_close": [10.0, 10.0, 9.0],
        },
        index=dates,
    )

    rebalance_dv = pd.DataFrame(
        {
            "total_value": [1000000.0, 1010000.0, 1020000.0],
            "515180_value": [500000.0, 505000.0, 510000.0],
            "159937_value": [500000.0, 505000.0, 510000.0],
            "515180_weight": [0.5, 0.5, 0.5],
            "159937_weight": [0.5, 0.5, 0.5],
        },
        index=dates,
    )

    grid_snapshot = SimpleNamespace(
        total_value=Decimal("202000"),
        cash=Decimal("192000"),
        market_value=Decimal("10000"),
        positions={"515180": Decimal("1000"), "159937": Decimal("1000")},
    )
    rebalance_snapshot = SimpleNamespace(
        total_value=Decimal("1020000"),
        cash=Decimal("0"),
        asset_shares={"515180": Decimal("510000"), "159937": Decimal("51000")},
        asset_values={},
    )

    stats = SimpleNamespace(
        total_return=Decimal("0.01"),
        annualized_return=Decimal("0.02"),
        max_drawdown=(Decimal("-0.05"), dates[0], dates[-1]),
        sharpe_ratio=Decimal("0.5"),
        volatility=Decimal("0.1"),
    )

    grid_result = SimpleNamespace(
        daily_values=grid_dv,
        snapshots=[grid_snapshot],
        trades=[],
        buy_count=2,
        sell_count=1,
        completed_rounds=1,
        total_rounds=2,
        total_return=stats.total_return,
        annualized_return=stats.annualized_return,
        max_drawdown=stats.max_drawdown,
        sharpe_ratio=stats.sharpe_ratio,
        volatility=stats.volatility,
    )
    rebalance_result = SimpleNamespace(
        daily_values=rebalance_dv,
        snapshots=[rebalance_snapshot],
        all_trades=[],
        total_return=Decimal("0.02"),
        annualized_return=stats.annualized_return,
        max_drawdown=stats.max_drawdown,
        sharpe_ratio=stats.sharpe_ratio,
        volatility=stats.volatility,
    )

    combined_dv = pd.DataFrame(
        {
            "rebalance_value": rebalance_dv["total_value"],
            "grid_value": grid_dv["total_value"],
            "total_value": rebalance_dv["total_value"] + grid_dv["total_value"],
        },
        index=dates,
    )

    config = SimpleNamespace(
        allocation=SimpleNamespace(
            asset_codes=["515180", "159937"],
            allocations=[
                SimpleNamespace(code="515180", name="红利ETF"),
                SimpleNamespace(code="159937", name="黄金ETF"),
            ],
        ),
        threshold_pp=Decimal("3"),
        rebalance_initial_capital=Decimal("1000000"),
        grid_initial_capital=Decimal("200000"),
        initial_capital=Decimal("1200000"),
    )

    return SimpleNamespace(
        config=config,
        grid_result=grid_result,
        rebalance_result=rebalance_result,
        daily_values=combined_dv,
        total_return=Decimal("0.015"),
        annualized_return=Decimal("0.02"),
        max_drawdown=(Decimal("-0.04"), dates[0], dates[-1]),
        sharpe_ratio=Decimal("0.6"),
        volatility=Decimal("0.09"),
    )


class TestBuildPagesSite:
    @pytest.fixture()
    def site_data(self):
        from src.application.pages.site_builder import BuildPagesSite

        result = _stub_combined_result()
        builder = BuildPagesSite(config=result.config,
                                 backtester=_StubBacktester(result))
        return builder.execute()

    def test_overview_has_all_curve_roles(self, site_data):
        ov = site_data.overview
        roles = {c.role for c in ov.return_curves}
        assert CurveRole.ASSET in roles
        assert CurveRole.REBALANCE in roles
        assert CurveRole.GRID_ROI in roles
        assert CurveRole.GRID_PROFIT in roles

    def test_overview_asset_curves_match_allocation(self, site_data):
        names = [
            c.name for c in site_data.overview.return_curves
            if c.role is CurveRole.ASSET
        ]
        assert names == ["红利ETF", "黄金ETF"]

    def test_occupation_uses_shares_times_close(self, site_data):
        by_code = site_data.overview.occupation_by_code()
        # 515180: 1000 股 × 1.0 = 1000；159937: 1000 股 × 9.0 = 9000
        assert by_code["515180"].current == Decimal("1000.00")
        assert by_code["159937"].current == Decimal("9000.00")

    def test_peak_occupation_is_sum_peak(self, site_data):
        # 次日合计 1000 + 10000 = 11000
        assert site_data.overview.peak_occupation == Decimal("11000.00")

    def test_grid_roi_uses_peak_denominator(self, site_data):
        roi = site_data.overview.curve(CurveRole.GRID_ROI)
        # 末端 total_pnl=700 / 11000 * 100
        assert roi.final_return == pytest.approx(Decimal("6.3636"), abs=1e-3)

    def test_summary_isolates_two_strategies(self, site_data):
        s = site_data.summary
        assert s.rebalance.key == "rebalance"
        assert s.grid.key == "grid"
        assert s.rebalance.cash == Decimal("0")
        assert s.grid.cash == Decimal("192000")

    def test_summary_combined_merges_duplicate_codes(self, site_data):
        combined = site_data.summary.combined
        codes = [p.code for p in combined.positions]
        assert len(codes) == len(set(codes)), "组合持仓不应重复同一标的"

    def test_grid_extra_fields_present(self, site_data):
        extra = site_data.summary.grid.extra
        assert extra["buy_count"] == 2
        assert extra["sell_count"] == 1
        assert extra["completed_rounds"] == 1
        assert extra["total_rounds"] == 2

    def test_summary_serializes_to_json(self, site_data):
        import json

        payload = site_data.summary.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        assert "rebalance" in text and "grid" in text
        assert isinstance(payload["rebalance"]["positions"], list)


# --------------------------------------------------------------- 渲染器冒烟测试
class TestRendererSmoke:
    def test_renders_png(self, tmp_path):
        matplotlib = pytest.importorskip("matplotlib")
        matplotlib.use("Agg")

        from src.infrastructure.chart.strategy_overview_renderer import (
            StrategyOverviewRenderer,
        )

        out = StrategyOverviewRenderer(dpi=50).render(
            _overview(), tmp_path / "chart.png"
        )
        assert out.exists()
        assert out.stat().st_size > 1000

    def test_pct_to_log_handles_minus_100(self):
        from src.infrastructure.chart.strategy_overview_renderer import pct_to_log

        assert pct_to_log(0) == pytest.approx(0.0)
        assert pct_to_log(100) == pytest.approx(0.6931, abs=1e-3)
        # -100% 不应抛出
        assert pct_to_log(-100) < -3
