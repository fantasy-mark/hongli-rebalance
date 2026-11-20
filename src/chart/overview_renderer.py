"""
策略全景图渲染器（图表模块）

把 `src.chart.overview.StrategyOverview` 渲染成 PNG。

版式对齐 master 分支 `chart_renderer.py`:
- 上 panel: 各资产买入持有收益（虚线） + 3pp 再平衡（黑粗线） + 网格 ROI（绿线），
            收益 % 对数刻度，右端贪心避让标签
- 下 panel: 各资产网格资金占用堆叠面积 + 网格累计收益虚线

只依赖 matplotlib，不做任何取数或业务计算。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np

from src.chart.overview import CurveRole, StrategyOverview

# 资产配色（对齐 master）
ASSET_COLORS: Dict[str, str] = {
    "515180": "#C0392B",
    "159937": "#C9A961",
    "511380": "#2C5F8D",
    "513300": "#48C9B0",
}

# 未知资产的备用配色
EXTRA_PALETTE: Sequence[str] = (
    "#8E44AD", "#D35400", "#16A085", "#7F8C8D", "#2E86C1", "#B03A2E",
)

REBALANCE_COLOR = "#1A1A1A"
GRID_ROI_COLOR = "#27AE60"
PROFIT_COLOR = "#B03A2E"
OCCUPATION_LABEL_COLOR = "#1B4F72"


def pct_to_log(pct: float) -> float:
    """收益率百分比 → 对数刻度坐标（log1p），负收益只能到 -100%"""
    return float(np.log1p(max(pct, -99.99) / 100.0))


class StrategyOverviewRenderer:
    """策略全景图渲染器（无状态）"""

    def __init__(self, dpi: int = 150, figsize: tuple = (14, 9.5)) -> None:
        self.dpi = dpi
        self.figsize = figsize

    # ------------------------------------------------------------------ 对外
    def render(self, overview: StrategyOverview, out_path: Path | str) -> Path:
        """渲染并保存为 PNG，返回实际路径"""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with plt.rc_context(self._rc()):
            fig = self._build_figure(overview)
            fig.tight_layout()
            fig.savefig(out_path, dpi=self.dpi, bbox_inches="tight")
            plt.close(fig)

        return out_path

    # ------------------------------------------------------------------ 样式
    @staticmethod
    def _rc() -> dict:
        return {
            "font.sans-serif": [
                "Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                "WenQuanYi Zen Hei", "DejaVu Sans",
            ],
            "axes.unicode_minus": False,
            "figure.autolayout": False,
        }

    def _asset_color(self, index: int, code: str | None = None) -> str:
        if code and code in ASSET_COLORS:
            return ASSET_COLORS[code]
        return EXTRA_PALETTE[index % len(EXTRA_PALETTE)]

    # ------------------------------------------------------------------ 绘制
    def _build_figure(self, overview: StrategyOverview):
        fig, (ax1, ax2) = plt.subplots(
            2, 1,
            figsize=self.figsize,
            sharex=True,
            gridspec_kw={"height_ratios": [3, 1.6]},
        )
        fig.subplots_adjust(hspace=0.10)

        self._draw_top_panel(ax1, overview)
        self._draw_bottom_panel(ax2, overview)

        return fig

    # -- 上 panel ---------------------------------------------------------
    def _draw_top_panel(self, ax, overview: StrategyOverview) -> None:
        asset_curves = [
            c for c in overview.return_curves if c.role is CurveRole.ASSET
        ]
        rebalance = overview.curve(CurveRole.REBALANCE)
        grid_roi = overview.curve(CurveRole.GRID_ROI)

        # 资产名 → 代码，用于取稳定配色（与下 panel 占用一致）
        name_to_code = {o.asset_name: o.asset_code for o in overview.occupations}
        label_meta: List[dict] = []

        # 1) 单资产买入持有（虚线，按资产注册顺序配色）
        for i, curve in enumerate(asset_curves):
            code = name_to_code.get(curve.name)
            color = self._asset_color(i, code)
            y = [pct_to_log(float(v)) for v in curve.values]
            ax.plot(
                curve.dates, y,
                color=color, lw=1.4, ls="--", alpha=0.85,
                label=curve.name,
            )
            label_meta.append({
                "name": curve.name,
                "y": y[-1],
                "pct": float(curve.final_return),
                "color": color,
                "bold": False,
            })

        # 2) 再平衡组合（黑色粗实线，主角）
        if rebalance is not None:
            y = [pct_to_log(float(v)) for v in rebalance.values]
            ax.plot(
                rebalance.dates, y,
                color=REBALANCE_COLOR, lw=2.6, ls="-", alpha=1.0,
                label=rebalance.name,
            )
            label_meta.append({
                "name": rebalance.name,
                "y": y[-1],
                "pct": float(rebalance.final_return),
                "color": REBALANCE_COLOR,
                "bold": True,
            })

        # 3) 网格 ROI（绿色）
        if grid_roi is not None:
            y = [pct_to_log(float(v)) for v in grid_roi.values]
            ax.plot(
                grid_roi.dates, y,
                color=GRID_ROI_COLOR, lw=2.4, ls="-", alpha=0.95,
                label=grid_roi.name,
            )
            label_meta.append({
                "name": grid_roi.name,
                "y": y[-1],
                "pct": float(grid_roi.final_return),
                "color": GRID_ROI_COLOR,
                "bold": True,
            })

        ax.axhline(0, color="grey", lw=0.6, alpha=0.4)

        ylim_min, ylim_max = self._place_right_labels(ax, label_meta, overview.window_end)
        ax.set_ylim(ylim_min, ylim_max)
        self._format_log_yaxis(ax, ylim_min, ylim_max)

        ax.set_title(
            self._title(overview),
            fontsize=13, fontweight="bold", pad=14,
        )
        ax.set_ylabel("收益 % (对数刻度)", fontsize=11)
        ax.grid(True, which="major", color="#888888", alpha=0.4, ls=":", lw=0.7)
        ax.grid(True, which="minor", color="#AAAAAA", alpha=0.2, ls=":", lw=0.5)
        ax.set_axisbelow(True)
        leg = ax.legend(loc="upper left", frameon=True, framealpha=0.92,
                        fontsize=10, edgecolor="grey")
        if leg is not None:
            leg.get_frame().set_linewidth(0.6)

    def _place_right_labels(self, ax, label_meta: List[dict],
                            end_date) -> tuple[float, float]:
        """右端标签贪心避让（对齐 master 的 label_meta + min_gap 算法）"""
        if not label_meta:
            return 0.0, 1.0

        label_meta.sort(key=lambda d: d["y"])
        y_min = min(d["y"] for d in label_meta)
        y_max = max(d["y"] for d in label_meta)
        span = y_max - y_min
        pad = span * 0.05

        ylim_min = max(y_min - pad, 0.0)
        ylim_max = y_max + pad * 3
        ylim_range = ylim_max - ylim_min
        min_gap = ylim_range * 0.06

        step = ylim_range * 0.04
        candidates = [0.0]
        for i in range(1, 26):
            candidates.extend([i * step, -i * step])

        placed: List[float] = []
        for d in label_meta:
            chosen = d["y"]
            for delta in candidates:
                cand = d["y"] + delta
                if cand < 0:
                    continue
                if all(abs(cand - p) >= min_gap for p in placed):
                    chosen = cand
                    break
            placed.append(chosen)
            d["y_text"] = chosen

        for d in label_meta:
            ax.annotate(
                f"{d['name']} {d['pct']:+.2f}%",
                xy=(end_date, d["y_text"]),
                xytext=(8, 0), textcoords="offset points",
                fontsize=10,
                fontweight="bold" if d["bold"] else "normal",
                color=d["color"], va="center",
            )

        # 给右侧标签留出横向空间
        x_left, x_right = ax.get_xlim()
        ax.set_xlim(x_left, x_right + (x_right - x_left) * 0.02)

        return ylim_min, ylim_max

    @staticmethod
    def _format_log_yaxis(ax, ylim_min: float, ylim_max: float) -> None:
        def fmt(x, _pos):
            pct = float(np.expm1(x)) * 100
            if abs(pct) < 0.1:
                return "0%"
            if abs(pct) >= 100:
                return f"{pct:+.0f}%"
            return f"{pct:+.1f}%"

        ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt))
        ticks = [
            pct_to_log(p)
            for p in (-50, -20, -10, 0, 5, 10, 20, 50, 100, 200)
        ]
        ticks = [t for t in ticks if ylim_min <= t <= ylim_max]
        if ticks:
            ax.set_yticks(ticks)

    @staticmethod
    def _title(overview: StrategyOverview) -> str:
        return (
            f"策略全景图 · 再平衡 vs 单资产持有 + 网格资金占用\n"
            f"区间 {overview.window_start} → {overview.window_end} "
            f"({overview.duration_years():.2f} 年) — 收益 % (起点=0%)"
        )

    # -- 下 panel ---------------------------------------------------------
    def _draw_bottom_panel(self, ax, overview: StrategyOverview) -> None:
        occupations = list(overview.occupations)
        dates = overview.dates()

        if occupations:
            series = []
            labels = []
            colors = []
            for i, occ in enumerate(occupations):
                series.append([float(v) for v in occ.values])
                labels.append(f"{occ.asset_name} (占用)")
                colors.append(self._asset_color(i, occ.asset_code))

            ax.stackplot(
                dates, *series,
                labels=labels, colors=colors,
                alpha=0.95, edgecolor="white", linewidth=0.8,
            )

        # 网格累计收益（虚线，与占用同轴）
        profit = overview.curve(CurveRole.GRID_PROFIT)
        if profit is not None:
            ax.plot(
                profit.dates, [float(v) for v in profit.values],
                color=PROFIT_COLOR, lw=2.0, ls="--", alpha=0.95,
                label="网格累计收益 (¥)",
            )

        ax.set_facecolor("#FAFAFA")

        totals = overview.total_occupation_series
        if totals:
            last = float(totals[-1])
            ax.annotate(
                f" 占用 ¥{last:,.0f}",
                xy=(overview.window_end, last),
                xytext=(8, 4), textcoords="offset points",
                fontsize=10, color=OCCUPATION_LABEL_COLOR,
                fontweight="bold", va="bottom",
            )

        ax.set_ylabel("资金占用 / 累计收益 (¥)", fontsize=10,
                      color=OCCUPATION_LABEL_COLOR)
        ax.tick_params(axis="y", labelcolor=OCCUPATION_LABEL_COLOR)
        ax.set_xlabel("日期", fontsize=11)
        ax.grid(True, which="major", color="#888888", alpha=0.55, ls=":", lw=0.9)
        ax.grid(True, which="minor", color="#AAAAAA", alpha=0.30, ls=":", lw=0.6)
        ax.set_axisbelow(True)

        # y 轴上限：占用峰值与累计收益较大者 × 1.2
        tops = [float(overview.peak_occupation)]
        if totals:
            tops.append(max(float(t) for t in totals))
        if profit is not None and profit.values:
            tops.append(max(float(v) for v in profit.values))
        ax.set_ylim(0, max(max(tops) * 1.2, 1.0))

        leg = ax.legend(loc="upper left", frameon=True, framealpha=0.92,
                        fontsize=10, edgecolor="grey")
        if leg is not None:
            leg.get_frame().set_linewidth(0.6)

        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 10]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
