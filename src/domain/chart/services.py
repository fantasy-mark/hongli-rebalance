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
    ChartConfig, ChartSeries, SeriesRole, Annotation, ChartStyle
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
        
        # 绘制所有曲线
        for series in config.series_list:
            ChartRenderer._plot_series(ax, series)
        
        # 配置坐标轴
        ax.set_xlabel(config.xlabel, fontsize=12)
        ax.set_ylabel(config.ylabel, fontsize=12)
        ax.set_title(config.title, fontsize=14, fontweight='bold')
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
        
        # 网格和图例
        if config.show_grid:
            ax.grid(True, alpha=0.3)
        
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
    def _plot_series(ax, series: ChartSeries):
        """绘制单条曲线"""
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
        
        # 添加标注
        if series.annotation:
            ChartRenderer._add_annotation(ax, series, series.annotation)
    
    @staticmethod
    def _add_annotation(ax, series: ChartSeries, annotation: Annotation):
        """添加标注"""
        # 计算标注位置（使用数据坐标）
        x_idx = int(annotation.x_position * (len(series.dates) - 1))
        y_value = float(series.values[x_idx])
        
        # 创建文本框样式
        bbox_props = {
            'boxstyle': annotation.bbox_style,
            'facecolor': annotation.bbox_facecolor,
            'alpha': annotation.bbox_alpha,
        }
        
        ax.text(
            series.dates[x_idx],
            y_value,
            annotation.text,
            transform=ax.transData,
            fontsize=annotation.fontsize,
            fontweight=annotation.fontweight,
            verticalalignment='bottom',
            horizontalalignment='left',
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
            
        Returns:
            图表配置
        """
        config = ChartConfig(
            title=title,
            xlabel="日期",
            ylabel="归一化市值",
        )
        
        # 添加组合曲线（实线）
        portfolio_series = ChartSeries(
            name="组合",
            role=SeriesRole.PORTFOLIO,
            dates=portfolio_dates,
            values=portfolio_values,
            style=ChartStyle.for_portfolio(),
        )
        
        # 添加标注 - 包含累计收益、年化收益、最大回撤
        # 移到右侧，避免遮挡曲线
        if show_annotations and portfolio_values:
            current_return = portfolio_values[-1] - Decimal("1")
            
            # 构建多行标注文本
            annotation_lines = [f"累计收益: {float(current_return):.2%}"]
            if annualized_return is not None:
                annotation_lines.append(f"年化收益: {float(annualized_return):.2%}")
            if max_drawdown is not None:
                annotation_lines.append(f"最大回撤: {float(max_drawdown):.2%}")
            
            annotation_text = "\n".join(annotation_lines)
            
            portfolio_series = ChartSeries(
                name="组合",
                role=SeriesRole.PORTFOLIO,
                dates=portfolio_dates,
                values=portfolio_values,
                style=ChartStyle.for_portfolio(),
                annotation=Annotation(
                    text=annotation_text,
                    x_position=0.98,  # 右侧（数据坐标比例）
                    y_position=0.98,  # 顶部
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
            
            # 添加收益率标注
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
                        x_position=0.98,
                        y_position=0.02 + asset_index * 0.08,
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
    ) -> ChartConfig:
        """
        从回测结果创建图表配置
        
        Args:
            backtest_result: 回测结果对象
            asset_price_data: 各资产价格数据
            title: 自定义标题
            
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
        
        return ChartFactory.create_equity_curve_config(
            portfolio_dates=portfolio_dates,
            portfolio_values=portfolio_values,
            asset_data=asset_data,
            title=chart_title,
            show_annotations=True,
            annualized_return=annualized_return,
            max_drawdown=max_drawdown,
        )
