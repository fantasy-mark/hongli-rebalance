"""
网格策略图表渲染器（图表模块）

职责:
- 组合网格策略的多面板图表
- 明确区分「哪个资产」在「何时」买入/卖出

面板说明:
1. 网格组合净值 + 各资产买入点（按资产着色 + 标注代码）
2. 各资产网格持仓市值（买入▲/卖出▼）
3. 各资产价格走势 + 开仓标注（复用图表模型 ChartSeries）
4. 再平衡 vs 网格 vs 合计（持仓隔离对比）
"""

from decimal import Decimal
from typing import Dict, List, Tuple
import logging

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

from src.chart.models import (
    ChartConfig,
    ChartSeries,
    ChartStyle,
    SeriesRole,
)
from src.chart.renderer import ChartRenderer
from src.strategy.grid.models import GridTradeType

logger = logging.getLogger(__name__)

# 资产配色与标记形状（按顺序分配）
ASSET_COLORS = ['#e6194b', '#3cb44b', '#4363d8', '#f58231',
                '#911eb4', '#42d4f4', '#f032e6', '#bfef45',
                '#fabed4', '#469990']
ASSET_MARKERS = ['^', 'v', 's', 'D', 'o', 'P', 'X', '*', '<', '>']


class GridChartRenderer:
    """
    网格策略图表渲染器

    所有方法均为静态方法，无状态
    """

    @staticmethod
    def render(grid_result,
               output_path: str = 'grid_strategy_result.png',
               show: bool = False,
               dpi: int = 150) -> str:
        """
        渲染网格策略图表

        布局:
        - 上方: 各资产走势图（归一化到 100）+ 网格买卖点 + 轮次边界
        - 下方: 各资产网格持仓市值堆叠图 + 网格现金（右轴）

        Args:
            grid_result: GridBacktestResult
            output_path: 输出路径
            show: 是否显示窗口
            dpi: 图片分辨率

        Returns:
            输出文件路径
        """
        fig, axes = plt.subplots(
            2, 1, figsize=(16, 12),
            gridspec_kw={'height_ratios': [1.25, 1]},
        )

        names = grid_result.config.asset_names
        styles = GridChartRenderer._asset_styles(grid_result.config.asset_codes)

        # ---- 上方: 各资产走势图 + 买卖点 ----
        GridChartRenderer._plot_price_panel(axes[0], grid_result, styles, names)

        # ---- 下方: 各资产网格持仓堆叠图 ----
        GridChartRenderer._plot_position_stack_panel(axes[1], grid_result, styles, names)

        for ax in axes:
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
            ax.xaxis.set_major_locator(mdates.YearLocator(1))
            ax.tick_params(axis='x', rotation=0)

        plt.tight_layout()

        if output_path:
            fig.savefig(output_path, dpi=dpi, bbox_inches='tight')
            plt.close(fig)
            return output_path

        if show:
            plt.show()
            plt.close(fig)
        return ""

    @staticmethod
    def render_comparison(combined_result,
                           output_path: str = 'grid_strategy_comparison.png',
                           show: bool = False,
                           dpi: int = 150) -> str:
        """
        渲染组合对比图（再平衡 vs 网格，持仓隔离）
        
        只绘制归一化净值（起点=100），不绘制网格总市值等绝对金额曲线
        （两个子组合规模不同，绝对值对比没有意义）
        
        Args:
            combined_result: CombinedBacktestResult
            output_path: 输出路径
        """
        if combined_result is None or combined_result.daily_values.empty:
            return ""

        fig, ax = plt.subplots(figsize=(14, 7))
        cdf = combined_result.daily_values

        for column, label, color, style in (
            ('rebalance_value', '再平衡组合（独立持仓）', '#2ca02c', '-'),
            ('grid_value', '网格组合（独立持仓）', '#1f77b4', '-'),
        ):
            if column not in cdf.columns:
                continue
            series = cdf[column].astype(float)
            base = series.iloc[0]
            if not base:
                continue
            ax.plot(cdf.index, series / base * 100, style, linewidth=1.8,
                    color=color, label=label)

        ax.set_title('再平衡 vs 网格 净值对比（起点=100，持仓隔离）')
        ax.set_xlabel('日期')
        ax.set_ylabel('净值 (起点=100)')
        ax.grid(True, alpha=0.3)
        ax.legend(loc='upper left')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
        ax.xaxis.set_major_locator(mdates.YearLocator(1))

        plt.tight_layout()
        fig.savefig(output_path, dpi=dpi, bbox_inches='tight')
        plt.close(fig)
        return output_path

    # ------------------------------------------------------------------
    # 面板实现
    # ------------------------------------------------------------------

    @staticmethod
    def _plot_position_stack_panel(ax, grid_result, styles, names) -> None:
        """
        下方面板: 各资产网格持仓市值堆叠面积图

        - 每个资产的网格持仓市值（份额 × 收盘价）堆叠展示
        - Y 轴按堆叠高度自适应，清晰反映持仓随时间的变化
        - 不绘制买卖标记（由上方走势图体现）
        """
        df = grid_result.daily_values

        # 1. 准备各资产持仓市值序列（按峰值从大到小堆叠，大的在底部）
        items = []
        position_values = {}

        for code in grid_result.config.asset_codes:
            share_col = f'{code}_shares'
            close_col = f'{code}_close'
            if share_col not in df.columns or close_col not in df.columns:
                continue

            shares = df[share_col].fillna(0.0)
            close = df[close_col]
            value = (shares * close).fillna(0.0)

            position_values[code] = value
            if value.abs().sum() == 0:
                continue

            items.append((float(value.max()), code, value))

        items.sort(key=lambda x: x[0], reverse=True)

        if items:
            # 阶梯式堆叠：持仓周期较短时，step='post' 能把每段持仓画成清晰区块
            cumulative = np.zeros(len(df.index))
            for item in items:
                code = item[1]
                value = item[2].values
                lower = cumulative.copy()
                cumulative = cumulative + value
                ax.fill_between(
                    df.index, lower, cumulative,
                    step='post',
                    label=f"{names.get(code, code)}({code})",
                    color=styles.get(code, ('#333333', 'o'))[0],
                    alpha=0.78, edgecolor='white', linewidth=0.5,
                )
        else:
            ax.text(0.5, 0.5, '本次回测无网格持仓', transform=ax.transAxes,
                    ha='center', va='center', fontsize=13, color='#888888')

        # 2. 右轴: 网格交易收益曲线（仅已实现收益，不绘制含浮动的总盈亏）
        #    百分比 = 累计已实现收益(元) / 回测期间网格最大持仓(堆叠图高度)
        ax2 = ax.twinx()
        pnl_series = []

        # 峰值持仓 = 堆叠图的最大高度（各资产持仓市值之和的最大值）
        max_stack = float(df['market_value'].max()) if 'market_value' in df.columns else 0.0

        if 'realized_pnl' in df.columns:
            if max_stack > 0:
                ax2.plot(df.index, df['realized_pnl'] / max_stack * 100, color='#1f77b4',
                         linewidth=2.2, drawstyle='steps-post', label='交易收益（已实现）')
                pnl_series.append(df['realized_pnl'] / max_stack * 100)
            else:
                ax2.plot(df.index, df['realized_pnl'], color='#1f77b4', linewidth=2.2,
                         drawstyle='steps-post', label='交易收益（已实现）')
                pnl_series.append(df['realized_pnl'])
        ax2.axhline(0, color='#999999', linewidth=0.8, alpha=0.6)

        # 将 0 轴锚定在面板底部，使收益曲线读作"收益累积"
        if pnl_series:
            lo = min(0.0, min(float(s.min()) for s in pnl_series))
            hi = max(0.0, max(float(s.max()) for s in pnl_series))
            span = max(hi - lo, 1e-6)
            ax2.set_ylim(lo - span * 0.08, hi + span * 0.18)

        ax2.set_ylabel('收益 / 峰值持仓 (%)', fontsize=10)
        ax2.tick_params(axis='y', labelcolor='#1f77b4')
        if max_stack > 0:
            from matplotlib.ticker import PercentFormatter
            ax2.yaxis.set_major_formatter(PercentFormatter(xmax=100))

        # 3. 坐标轴与图例（不绘制轮次辅助线）
        ax.set_title('各资产网格持仓市值堆叠图 + 网格交易收益')
        ax.set_xlabel('日期')
        ax.set_ylabel('持仓市值 (元)', fontsize=10)
        ax.set_ylim(bottom=0)

        handles1, labels1 = ax.get_legend_handles_labels()
        handles2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(handles1 + handles2, labels1 + labels2,
                  loc='upper left', fontsize=8, ncol=2)

    @staticmethod
    def _plot_price_panel(ax, grid_result, styles, names) -> None:
        """面板3: 各资产价格走势 + 开仓标注（复用图表领域）"""
        df = grid_result.daily_values

        dates = [idx.date() if hasattr(idx, 'date') else idx for idx in df.index]

        series_list: List[ChartSeries] = []
        traded_codes = sorted({t.asset_code for t in grid_result.trades})

        for code in grid_result.config.asset_codes:
            close_col = f'{code}_close'
            if close_col not in df.columns:
                continue

            raw = [float(v) if v == v else None for v in df[close_col].tolist()]
            valid = [v for v in raw if v is not None]
            if not valid:
                continue

            # 归一化到 100，消除各资产价格量级差异，便于对比
            base = valid[0]
            values = [Decimal(str(v / base * 100)) if v is not None else Decimal("0")
                      for v in raw]

            color, _ = styles.get(code, ('#333333', 'o'))
            traded = code in traded_codes

            series_list.append(ChartSeries(
                name=f"{names.get(code, code)}({code})",
                role=SeriesRole.ASSET,
                dates=dates,
                values=values,
                style=ChartStyle(
                    color=color,
                    line_width=1.6 if traded else 1.1,
                    line_style='-' if traded else '--',
                    alpha=0.95 if traded else 0.3,
                ),
            ))

        if not series_list:
            ax.text(0.5, 0.5, '无可用价格数据', transform=ax.transAxes,
                    ha='center', va='center', fontsize=13, color='#888888')
            return

        config = ChartConfig(
            title='各资产价格（起点=100）与网格开仓标注',
            series_list=series_list,
            xlabel='日期',
            ylabel='归一化价格 (起点=100)',
            show_grid=True,
            require_portfolio_series=False,
        )

        ChartRenderer.render_on(ax, config)
        ax.legend(loc='upper left', fontsize=8, ncol=2)

        # 标注：开仓交易（交易日期 + 步长占价格比例）
        GridChartRenderer._mark_openings(ax, grid_result, styles)

    @staticmethod
    def _mark_openings(ax, grid_result, styles) -> None:
        """
        标注网格开仓交易（仅开仓日）

        - 只标注「开仓」类型的成交（本轮首次买入），加仓/止盈不标注
        - 标注内容: 交易日期 + 网格步长占锚点价格的比例
        - 标注框用引线连回锚点，避免偏移后与日期脱节
        - 按资产错层，保证同一资产的标注都在自己曲线同一侧
        """
        df = grid_result.daily_values

        # 轮次查询：(资产, 轮次) -> 轮次事件（取锚点与步长）
        round_map = {
            (event.asset_code, event.round_no): event
            for event in grid_result.round_events
        }

        # 每个资产的标注层级（0,1,2,...），降低跨资产重叠
        asset_level = {
            code: i for i, code in enumerate(grid_result.config.asset_codes)
        }
        # 同一资产的第 n 次开仓（轮转错层，避免同资产标注互相重叠）
        asset_seq: Dict[str, int] = {}

        for trade in grid_result.trades:
            if trade.trade_type != GridTradeType.OPEN:
                continue

            close_col = f'{trade.asset_code}_close'
            if close_col not in df.columns or trade.date not in df.index:
                continue

            raw = df.loc[trade.date, close_col]
            if raw != raw:  # NaN
                continue

            base_series = df[close_col].dropna()
            if base_series.empty:
                continue
            base = float(base_series.iloc[0])
            if not base:
                continue

            y = float(raw) / base * 100

            # 步长占价格比例
            event = round_map.get((trade.asset_code, trade.round_no))
            ratio = None
            if event is not None and event.step is not None and event.anchor_price:
                ratio = event.step / event.anchor_price

            text = f"{trade.date}"
            if trade.round_no:
                text += f" · 第{trade.round_no}轮"
            text += f"\n步长占比 {float(ratio):.2%}" if ratio is not None else "\n步长占比 -"

            color, _ = styles.get(trade.asset_code, ('#333333', 'o'))
            level = asset_level.get(trade.asset_code, 0)

            seq = asset_seq.get(trade.asset_code, 0)
            asset_seq[trade.asset_code] = seq + 1

            # 先在锚点画一个小圆点
            ax.scatter([trade.date], [y], s=26, color=color,
                       edgecolors='white', linewidths=0.8, zorder=9)

            # 标注框用细引线连回锚点；垂直错层 = 资产层 + 该资产第几次开仓
            dy = 14 + level * 9 + (seq % 4) * 15

            ax.annotate(
                text,
                xy=(trade.date, y),
                xytext=(0, dy),
                textcoords='offset points',
                ha='center',
                va='bottom',
                fontsize=7,
                color='white',
                bbox=dict(boxstyle='round,pad=0.25', facecolor=color,
                          alpha=0.92, edgecolor='none'),
                arrowprops=dict(arrowstyle='-', color=color, lw=0.7,
                                shrinkA=0, shrinkB=2),
                zorder=10,
            )

    @staticmethod
    def _asset_styles(asset_codes: List[str]) -> Dict[str, Tuple[str, str]]:
        """为每个资产分配 (颜色, 标记形状)"""
        styles: Dict[str, Tuple[str, str]] = {}
        for i, code in enumerate(asset_codes):
            styles[code] = (
                ASSET_COLORS[i % len(ASSET_COLORS)],
                ASSET_MARKERS[i % len(ASSET_MARKERS)],
            )
        return styles
