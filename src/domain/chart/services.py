"""
图表领域服务

ChartRenderer: 图表渲染器
ChartFactory: 图表工厂
"""

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import date, datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional
from pathlib import Path

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

from src.domain.chart.entities import (
    ChartConfig, ChartSeries, SeriesRole, Annotation, ChartStyle, YAxisScale
)


class ChartRenderer:
    """
    图表渲染器（领域服务）
    
    职责:
    - 根据ChartConfig渲染图表
    - 支持保存为文件
    - 无状态，纯函数式
    """
    
    @staticmethod
    def render(config: ChartConfig, 
               output_path: Optional[str] = None,
               show: bool = False) -> str:
        """
        渲染图表
        
        Args:
            config: 图表配置
            output_path: 输出文件路径
            show: 是否显示窗口
            
        Returns:
            输出文件路径
        """
        # 验证配置
        errors = config.validate()
        if errors:
            raise ValueError(f"图表配置无效: {'; '.join(errors)}")
        
        # 创建画布
        fig, ax = plt.subplots(figsize=config.figsize)
        
        # 绘制所有曲线（复用 render_on）
        ChartRenderer.render_on(ax, config)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
        
        # 图例
        if config.show_legend:
            ax.legend(loc=config.legend_location)
        
        plt.tight_layout()
        
        # 保存或显示
        if output_path:
            fig.savefig(output_path, dpi=config.dpi, bbox_inches='tight')
            plt.close(fig)
            return output_path
        elif show:
            plt.show()
            return ""
        else:
            # 默认保存到当前目录
            default_path = "chart_output.png"
            fig.savefig(default_path, dpi=config.dpi, bbox_inches='tight')
            plt.close(fig)
            return default_path
    
    @staticmethod
    def render_on(ax, config: ChartConfig) -> None:
        """
        在已有坐标轴上渲染图表配置
        
        用于多面板组合图（如网格策略图），复用曲线/标记点绘制逻辑
        
        Args:
            ax: matplotlib 坐标轴
            config: 图表配置
        """
        errors = config.validate()
        if errors:
            raise ValueError(f"图表配置无效: {'; '.join(errors)}")
        
        # 绘制所有曲线
        for series in config.series_list:
            ChartRenderer._plot_series(ax, series)
        
        # 配置坐标轴
        ax.set_xlabel(config.xlabel, fontsize=11)
        ax.set_ylabel(config.ylabel, fontsize=11)
        ax.set_title(config.title, fontsize=12, fontweight='bold')
        
        # 设置Y轴刻度类型
        if config.y_axis_scale == YAxisScale.LOGARITHMIC:
            ax.set_yscale('log')

            # 使用自定义格式化器，直接显示小数（如 1.0, 2.0, 3.0）
            from matplotlib.ticker import FuncFormatter, FixedLocator

            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, p: f"{v:.2f}"))

            # 设置主刻度位置为整数（1, 2, 3, ...），避免科学计数法
            all_values = []
            for series in config.series_list:
                if series.role != SeriesRole.BENCHMARK:
                    all_values.extend([float(v) for v in series.values])

            if all_values:
                min_val = min(all_values)
                max_val = max(all_values)
                ticks = [float(i) for i in range(1, int(max_val) + 2)]
                ticks = [t for t in ticks if min_val * 0.8 <= t <= max_val * 1.2]
                if ticks:
                    ax.yaxis.set_major_locator(FixedLocator(ticks))

            # 对数坐标下显示主次网格
            if config.show_grid:
                ax.grid(True, which='both', alpha=0.2)
        else:
            if config.show_grid:
                ax.grid(True, alpha=0.3)
    
    @staticmethod
    def _plot_series(ax, series: ChartSeries):
        """绘制单条曲线及标记点"""
        ax.plot(
            series.dates,
            [float(v) for v in series.values],
            label=series.name,
            color=series.style.color,
            linewidth=series.style.line_width,
            linestyle=series.style.line_style,
            marker=series.style.marker,
            alpha=series.style.alpha,
        )
        
        # 绘制分组标记点（如网格策略中各资产的买入/卖出点）
        if series.marker_groups:
            for group in series.marker_groups:
                ChartRenderer._plot_marker_group(ax, series, group)
        
        # 绘制单组标记点（如再平衡触发日）
        elif series.marker_dates and len(series.marker_dates) > 0:
            # 找到标记点对应的Y值
            marker_values = []
            marker_xs = []
            date_to_idx = {d: i for i, d in enumerate(series.dates)}
            
            for md in series.marker_dates:
                if md in date_to_idx:
                    idx = date_to_idx[md]
                    marker_xs.append(md)
                    marker_values.append(float(series.values[idx]))
            
            if marker_xs:
                # 默认红色圆点样式
                m_color = 'red'
                m_size = 50
                m_marker = 'o'
                
                if series.marker_style:
                    m_color = series.marker_style.get('color', 'red')
                    m_size = series.marker_style.get('size', 50)
                    m_marker = series.marker_style.get('marker', 'o')
                
                ax.scatter(
                    marker_xs,
                    marker_values,
                    c=m_color,
                    s=m_size,
                    marker=m_marker,
                    zorder=5,  # 确保在曲线上方
                    edgecolors='white',
                    linewidths=0.5,
                )
        
        # 添加标注
        if series.annotation:
            ChartRenderer._add_annotation(ax, series, series.annotation)
    
    @staticmethod
    def _plot_marker_group(ax, series: ChartSeries, group):
        """绘制一组标记点（独立样式与图例）"""
        date_to_idx = {d: i for i, d in enumerate(series.dates)}
        
        marker_xs = []
        marker_values = []
        for md in group.dates:
            if md in date_to_idx:
                idx = date_to_idx[md]
                marker_xs.append(md)
                marker_values.append(float(series.values[idx]))
        
        if not marker_xs:
            return
        
        style = group.style or {}
        ax.scatter(
            marker_xs,
            marker_values,
            c=style.get('color', 'red'),
            s=style.get('size', 50),
            marker=style.get('marker', 'o'),
            label=group.label or None,
            zorder=5,
            edgecolors='white',
            linewidths=0.5,
        )
    
    @staticmethod
    def _add_annotation(ax, series: ChartSeries, annotation: Annotation):
        """添加标注 - ASSET使用数据坐标贴在曲线末端右侧，其他使用轴坐标"""
        import pandas as pd
        
        # 创建文本框样式
        bbox_props = {
            'boxstyle': annotation.bbox_style,
            'facecolor': annotation.bbox_facecolor,
            'alpha': annotation.bbox_alpha,
        }
        
        if series.role == SeriesRole.ASSET:
            # 资产标注：使用数据坐标，贴在曲线最后一个点的右侧
            # x_position 表示相对于最后两点间距的偏移比例
            last_date = series.dates[-1]
            last_value = float(series.values[-1])
            
            # 计算日期偏移（取相邻两点间距的固定比例作为向右偏移量）
            if len(series.dates) >= 2:
                date_range = (series.dates[-1] - series.dates[-2]).days
                offset_days = int(date_range * annotation.x_position)
                annot_x = last_date + pd.Timedelta(days=offset_days)
            else:
                annot_x = last_date
            
            ax.text(
                annot_x,
                last_value,
                annotation.text,
                transform=ax.transData,
                fontsize=annotation.fontsize,
                fontweight=annotation.fontweight,
                verticalalignment='center',
                horizontalalignment='left',
                bbox=bbox_props,
            )
        else:
            # 组合/基准标注：使用轴坐标
            h_align = 'left' if annotation.x_position < 0.5 else 'right'
            v_align = 'bottom' if annotation.y_position < 0.5 else 'top'
            
            ax.text(
                annotation.x_position,
                annotation.y_position,
                annotation.text,
                transform=ax.transAxes,
                fontsize=annotation.fontsize,
                fontweight=annotation.fontweight,
                verticalalignment=v_align,
                horizontalalignment=h_align,
                bbox=bbox_props,
            )


class ChartFactory:
    """
    图表工厂（领域服务）
    
    职责:
    - 从回测结果创建图表配置
    - 提供常用的图表模板
    """
    
    @staticmethod
    def create_equity_curve_config(
        portfolio_dates: List[date],
        portfolio_values: List[Decimal],
        asset_data: Dict[str, Dict[str, Any]],  # {asset_code: {dates, values, name}}
        title: str = "收益曲线对比",
        show_annotations: bool = True,
        annualized_return: Optional[Decimal] = None,
        max_drawdown: Optional[Decimal] = None,
        asset_weights: Optional[Dict[str, Decimal]] = None,  # 各资产最新权重
        y_axis_scale: YAxisScale = YAxisScale.LINEAR,  # Y轴刻度类型
        rebalance_dates: Optional[List[date]] = None,  # 再平衡触发日期列表
    ) -> ChartConfig:
        """
        创建收益曲线对比图配置
        
        Args:
            portfolio_dates: 组合日期序列
            portfolio_values: 组合归一化市值序列
            asset_data: 各资产数据
                {
                    '515180': {
                        'dates': [...],
                        'values': [...],  # 原始市值
                        'name': '红利ETF'
                    },
                    ...
                }
            title: 图表标题
            show_annotations: 是否显示收益率标注
            annualized_return: 年化收益率（可选）
            max_drawdown: 最大回撤（可选，负值）
            asset_weights: 各资产最新权重 {asset_code: weight}（可选）
            y_axis_scale: Y轴刻度类型（LINEAR或LOGARITHMIC）
            
        Returns:
            图表配置
        """
        config = ChartConfig(
            title=title,
            xlabel="日期",
            ylabel="归一化市值",
            y_axis_scale=y_axis_scale,
        )
        
        # 左侧添加资产权重标注（包含年化收益、最大回撤和当前回撤）
        if asset_weights:
            weight_lines = ["当前持仓比例:"]
            for asset_code, weight in asset_weights.items():
                # 从asset_data中获取资产名称
                asset_name = asset_data.get(asset_code, {}).get('name', asset_code)
                weight_lines.append(f"  {asset_name}: {float(weight):.1%}")
            
            # 添加年化收益和最大回撤到左侧
            if annualized_return is not None:
                weight_lines.append("")
                weight_lines.append(f"年化收益: {float(annualized_return):.2%}")
            if max_drawdown is not None:
                weight_lines.append(f"最大回撤: {float(max_drawdown):.2%}")
            
            # 计算当前回撤（从最近峰值到当前的回撤百分比）
            if portfolio_values and len(portfolio_values) > 0:
                current_value = float(portfolio_values[-1])
                peak_value = max(float(v) for v in portfolio_values)
                if peak_value > 0:
                    current_dd = (current_value - peak_value) / peak_value
                    weight_lines.append("")
                    weight_lines.append(f"当前回撤: {current_dd:.2%}")
            
            weight_text = "\n".join(weight_lines)
            
            # 创建一个虚拟系列来承载标注（不绘制曲线）
            weight_series = ChartSeries(
                name="_weight_annotation",
                role=SeriesRole.BENCHMARK,
                dates=portfolio_dates[:1],  # 只需要一个日期
                values=[Decimal("1")],
                style=ChartStyle(color="white", line_width=0.01, alpha=0.001),  # 极细几乎透明线，实际不可见
                annotation=Annotation(
                    text=weight_text,
                    x_position=0.01,
                    y_position=0.82,
                    fontsize=9,
                    fontweight="normal",
                    bbox_style="round,pad=0.3",
                    bbox_facecolor="lightyellow",
                    bbox_alpha=0.9,
                ),
            )
            config.add_series(weight_series)
        
        # 添加组合曲线（实线）+ 再平衡标记点
        portfolio_series = ChartSeries(
            name="组合",
            role=SeriesRole.PORTFOLIO,
            dates=portfolio_dates,
            values=portfolio_values,
            style=ChartStyle.for_portfolio(),
        )
        
        # 添加标注 - 组合曲线标注贴在末端右侧（与资产标注规则一致）
        if show_annotations and portfolio_values:
            current_return = portfolio_values[-1] - Decimal("1")
            
            # 只保留百分比数值，不带前缀
            annotation_text = f"{float(current_return):.2%}"
            
            portfolio_series = ChartSeries(
                name="组合",
                role=SeriesRole.ASSET,  # 改为ASSET角色，使用数据坐标渲染
                dates=portfolio_dates,
                values=portfolio_values,
                style=ChartStyle.for_portfolio(),
                marker_dates=rebalance_dates,  # 再平衡触发日期
                marker_style={'color': 'red', 'size': 20, 'marker': 'o'},  # 红色圆点
                annotation=Annotation(
                    text=annotation_text,
                    x_position=0.5,  # 向右偏移半个日期间隔
                    y_position=0,    # Y值由渲染器自动取最后一个数据点
                    fontsize=10,
                    fontweight="bold",
                ),
            )
        
        config.add_series(portfolio_series)
        
        # 添加各资产曲线（虚线）
        asset_index = 0
        for asset_code, data in asset_data.items():
            asset_name = data.get('name', asset_code)
            asset_dates = data['dates']
            asset_values_raw = data['values']
            
            # 归一化
            if asset_values_raw and asset_values_raw[0] > 0:
                base = asset_values_raw[0]
                asset_values_normalized = [v / base for v in asset_values_raw]
            else:
                asset_values_normalized = asset_values_raw
            
            # 创建资产曲线
            asset_series = ChartSeries(
                name=asset_name,
                role=SeriesRole.ASSET,
                dates=asset_dates,
                values=asset_values_normalized,
                style=ChartStyle.for_asset(asset_index),
            )
            
            # 添加收益率标注（贴在曲线末端右侧）
            if show_annotations and asset_values_normalized:
                asset_return = asset_values_normalized[-1] - Decimal("1")
                asset_series = ChartSeries(
                    name=asset_name,
                    role=SeriesRole.ASSET,
                    dates=asset_dates,
                    values=asset_values_normalized,
                    style=ChartStyle.for_asset(asset_index),
                    annotation=Annotation(
                        text=f"{float(asset_return):.2%}",
                        x_position=0.5,  # 向右偏移半个日期间隔
                        y_position=0,    # Y值由渲染器自动取最后一个数据点
                        fontsize=9,
                    ),
                )
            
            config.add_series(asset_series)
            asset_index += 1
        
        return config
    
    @staticmethod
    def create_from_backtest_result(
        backtest_result: Any,  # BacktestResult from rebalance engine
        asset_price_data: Dict[str, Dict[str, Any]],
        title: Optional[str] = None,
        y_axis_scale: YAxisScale = YAxisScale.LINEAR,  # Y轴刻度类型
    ) -> ChartConfig:
        """
        从回测结果创建图表配置
        
        Args:
            backtest_result: 回测结果对象
            asset_price_data: 各资产价格数据
            title: 自定义标题
            y_axis_scale: Y轴刻度类型（LINEAR或LOGARITHMIC）
            
        Returns:
            图表配置
        """
        # 提取组合数据
        daily_values = backtest_result.daily_values
        portfolio_dates = daily_values.index.tolist()
        portfolio_values_raw = [Decimal(str(v)) for v in daily_values['total_value']]
        
        # 归一化
        if portfolio_values_raw and portfolio_values_raw[0] > 0:
            base = portfolio_values_raw[0]
            portfolio_values = [v / base for v in portfolio_values_raw]
        else:
            portfolio_values = portfolio_values_raw
        
        # 准备资产数据
        asset_data = {}
        for asset_code, price_info in asset_price_data.items():
            asset_dates = price_info['dates']
            asset_values = [Decimal(str(v)) for v in price_info['values']]
            
            # 归一化
            if asset_values and asset_values[0] > 0:
                base = asset_values[0]
                asset_values_normalized = [v / base for v in asset_values]
            else:
                asset_values_normalized = asset_values
            
            asset_data[asset_code] = {
                'dates': asset_dates,
                'values': asset_values_normalized,
                'name': price_info.get('name', asset_code),
            }
        
        # 创建配置
        chart_title = title or f"再平衡策略收益曲线 ({portfolio_dates[0]} ~ {portfolio_dates[-1]})"
        
        # 从回测结果提取绩效指标
        annualized_return = getattr(backtest_result, 'annualized_return', None)
        max_drawdown = None
        if hasattr(backtest_result, 'max_drawdown'):
            md = backtest_result.max_drawdown
            # max_drawdown可能是元组(max_dd, start_date, end_date)或直接是Decimal
            if isinstance(md, tuple):
                max_drawdown = md[0]
            else:
                max_drawdown = md
        
        # 从回测结果提取最新资产权重（如果有）
        asset_weights = None
        if hasattr(backtest_result, 'daily_values'):
            dv = backtest_result.daily_values
            weight_cols = [c for c in dv.columns if c.endswith('_weight')]
            if weight_cols:
                last_row = dv.iloc[-1]
                asset_weights = {}
                for col in weight_cols:
                    asset_code = col.replace('_weight', '')
                    asset_weights[asset_code] = Decimal(str(last_row[col]))
        
        # 从回测结果提取再平衡触发日期（从交易记录中获取唯一日期）
        rebalance_dates = None
        if hasattr(backtest_result, 'all_trades') and backtest_result.all_trades:
            # 去重并按日期排序
            trade_dates = sorted(set(t.date for t in backtest_result.all_trades))
            rebalance_dates = trade_dates
        
        return ChartFactory.create_equity_curve_config(
            portfolio_dates=portfolio_dates,
            portfolio_values=portfolio_values,
            asset_data=asset_data,
            title=chart_title,
            show_annotations=True,
            annualized_return=annualized_return,
            max_drawdown=max_drawdown,
            asset_weights=asset_weights,
            y_axis_scale=y_axis_scale,
            rebalance_dates=rebalance_dates,
        )
