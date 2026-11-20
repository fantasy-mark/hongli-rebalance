"""
策略全景图（图表模块）

值对象与聚合根，只表达「图表要画什么」，不关心怎么画。

- ReturnCurve:          一条收益率曲线（起点 = 0%）
- CapitalOccupation:    某个资产的资金占用序列
- StrategyOverview:     策略全景图聚合根（上 panel 曲线 + 下 panel 占用）

不变量:
- 所有曲线的日期序列必须完全一致（同一回测区间）
- 占用序列非负
- 峰值占用 = 各资产占用之和的历史最大值
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Sequence, Tuple


class CurveRole(Enum):
    """曲线角色（决定样式与绘制顺序）"""

    ASSET = "asset"            # 单资产买入持有（虚线）
    REBALANCE = "rebalance"    # 再平衡组合（黑色粗实线）
    GRID_ROI = "grid_roi"      # 网格 ROI（绿色实线）
    GRID_PROFIT = "grid_profit"  # 网格累计收益（下 panel 虚线）


@dataclass(frozen=True)
class ReturnCurve:
    """
    收益率曲线（值对象）

    values 为**收益率百分比**（起点 0.0，即 12.5 表示 +12.5%），
    渲染层负责转成对数刻度。
    """

    name: str
    role: CurveRole
    dates: Tuple[date, ...]
    values: Tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("曲线名称不能为空")
        if len(self.dates) != len(self.values):
            raise ValueError(
                f"曲线 {self.name} 的 dates({len(self.dates)}) "
                f"与 values({len(self.values)}) 长度不一致"
            )
        if not self.dates:
            raise ValueError(f"曲线 {self.name} 的日期序列不能为空")

    @property
    def final_return(self) -> Decimal:
        """末端收益率（百分比）"""
        return self.values[-1]

    @property
    def start_date(self) -> date:
        return self.dates[0]

    @property
    def end_date(self) -> date:
        return self.dates[-1]

    @classmethod
    def from_levels(
        cls,
        name: str,
        role: CurveRole,
        dates: Sequence[date],
        levels: Sequence[float | Decimal],
    ) -> "ReturnCurve":
        """
        由「净值序列」构造收益率曲线。

        起点归一化为 0%，后续按 (level / first - 1) * 100 换算。
        """
        clean = [Decimal(str(v)) for v in levels]
        if not clean:
            raise ValueError(f"曲线 {name} 的净值序列不能为空")

        base = clean[0]
        if base == 0:
            pct = [Decimal("0") for _ in clean]
        else:
            pct = [(v / base - 1) * Decimal("100") for v in clean]

        return cls(
            name=name,
            role=role,
            dates=tuple(dates),
            values=tuple(pct),
        )


@dataclass(frozen=True)
class CapitalOccupation:
    """
    单资产资金占用序列（值对象）

    values 为每日占用金额（¥），非负。
    """

    asset_code: str
    asset_name: str
    dates: Tuple[date, ...]
    values: Tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if not self.asset_code:
            raise ValueError("资产代码不能为空")
        if len(self.dates) != len(self.values):
            raise ValueError(
                f"{self.asset_code} 的 dates({len(self.dates)}) "
                f"与 values({len(self.values)}) 长度不一致"
            )
        if any(v < 0 for v in self.values):
            raise ValueError(f"{self.asset_code} 的资金占用不能为负")

    @property
    def peak(self) -> Decimal:
        return max(self.values) if self.values else Decimal("0")

    @property
    def current(self) -> Decimal:
        return self.values[-1] if self.values else Decimal("0")


@dataclass(frozen=True)
class StrategyOverview:
    """
    策略全景图（聚合根）

    上 panel:  return_curves（单资产 / 再平衡 / 网格 ROI）
    下 panel:  occupations（各资产资金占用堆叠）+ profit_curve（网格累计收益）

    不变量:
    - 至少一条收益率曲线
    - 所有曲线与占用序列的日期完全一致
    - peak_occupation 等于各资产占用合计的最大值
    """

    window_start: date
    window_end: date
    return_curves: Tuple[ReturnCurve, ...]
    occupations: Tuple[CapitalOccupation, ...]
    peak_occupation: Decimal
    grid_rounds_completed: int = 0
    grid_rounds_total: int = 0

    def __post_init__(self) -> None:
        if not self.return_curves:
            raise ValueError("策略全景图至少需要一条收益率曲线")
        if self.window_end < self.window_start:
            raise ValueError("结束日期不能早于开始日期")
        if self.peak_occupation < 0:
            raise ValueError("峰值资金占用不能为负")

    def curve(self, role: CurveRole) -> ReturnCurve | None:
        """按角色取曲线（同角色只允许一条）"""
        for c in self.return_curves:
            if c.role is role:
                return c
        return None

    @property
    def current_occupation(self) -> Decimal:
        """当前占用合计"""
        return sum((o.current for o in self.occupations), Decimal("0"))

    @property
    def total_occupation_series(self) -> List[Decimal]:
        """各资产占用逐日合计"""
        if not self.occupations:
            return []
        length = len(self.occupations[0].values)
        totals = [Decimal("0")] * length
        for occ in self.occupations:
            for i, v in enumerate(occ.values):
                totals[i] += v
        return totals

    def dates(self) -> Tuple[date, ...]:
        """统一日期轴"""
        return self.return_curves[0].dates

    def occupation_by_code(self) -> Dict[str, CapitalOccupation]:
        return {o.asset_code: o for o in self.occupations}

    def duration_years(self) -> float:
        return (self.window_end - self.window_start).days / 365.25
