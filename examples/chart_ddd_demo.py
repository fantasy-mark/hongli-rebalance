"""
图表DDD模型使用示例

展示:
1. 创建图表配置
2. 渲染收益曲线对比图（组合+单资产）
3. 从回测结果自动生成图表
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date
from decimal import Decimal

# 导入领域实体和服务
from src.domain.chart.entities import (
    ChartConfig, ChartSeries, ChartStyle, Annotation,
    SeriesRole, SeriesType
)
from src.domain.chart.services import ChartRenderer, ChartFactory


def example_1_create_chart_config():
    """示例1: 手动创建图表配置"""
    print("\n" + "="*80)
    print("示例1: 手动创建图表配置")
    print("="*80)
    
    # 生成模拟数据
    dates = [date(2024, 1, i+1) for i in range(30)]
    
    # 组合数据（实线）
    portfolio_values = [Decimal("1") + Decimal(str(i * 0.02)) for i in range(30)]
    
    # 资产数据（虚线）
    asset1_values = [Decimal("1") + Decimal(str(i * 0.015)) for i in range(30)]
    asset2_values = [Decimal("1") + Decimal(str(i * 0.025)) for i in range(30)]
    
    # 创建配置
    config = ChartConfig(
        title="收益曲线对比（手动创建）",
        xlabel="日期",
        ylabel="归一化市值",
    )
    
    # 添加组合曲线
    portfolio_series = ChartSeries(
        name="组合",
        role=SeriesRole.PORTFOLIO,
        dates=dates,
        values=portfolio_values,
        style=ChartStyle.for_portfolio(),
        annotation=Annotation(
            text=f"累计收益: {float(portfolio_values[-1] - 1):.2%}",
            x_position=0.02,
            y_position=0.95,
            fontsize=10,
            fontweight="bold",
        ),
    )
    config.add_series(portfolio_series)
    
    # 添加资产1曲线
    asset1_series = ChartSeries(
        name="资产A",
        role=SeriesRole.ASSET,
        dates=dates,
        values=asset1_values,
        style=ChartStyle.for_asset(0),
        annotation=Annotation(
            text=f"{float(asset1_values[-1] - 1):.2%}",
            x_position=0.95,
            y_position=0.1,
            fontsize=9,
        ),
    )
    config.add_series(asset1_series)
    
    # 添加资产2曲线
    asset2_series = ChartSeries(
        name="资产B",
        role=SeriesRole.ASSET,
        dates=dates,
        values=asset2_values,
        style=ChartStyle.for_asset(1),
        annotation=Annotation(
            text=f"{float(asset2_values[-1] - 1):.2%}",
            x_position=0.95,
            y_position=0.2,
            fontsize=9,
        ),
    )
    config.add_series(asset2_series)
    
    # 验证配置
    errors = config.validate()
    if errors:
        print(f"配置验证失败: {errors}")
        return None
    
    print(f"[OK] 图表配置创建成功: {config}")
    print(f"   曲线数量: {len(config.series_list)}")
    
    # 渲染图表
    output_path = "chart_manual.png"
    ChartRenderer.render(config, output_path=output_path)
    print(f"[OK] 图表已保存为 {output_path}")
    
    return config


def example_2_render_from_backtest():
    """示例2: 从回测结果渲染图表"""
    print("\n" + "="*80)
    print("示例2: 从回测结果渲染图表")
    print("="*80)
    
    # 先运行回测获取结果
    from src.application.rebalance.backtest_engine import (
        RebalanceConfig, AssetConfig, RebalanceBacktester
    )
    from src.application.price.query_service import PriceQueryService
    
    # 创建再平衡配置（将可转债ETF改为511260）
    config = RebalanceConfig(
        assets=[
            AssetConfig(code="515180", name="红利ETF", target_weight=0.25),
            AssetConfig(code="159937", name="黄金ETF", target_weight=0.25),
            AssetConfig(code="511260", name="十年国债国泰", target_weight=0.25),
            AssetConfig(code="513300", name="纳指ETF", target_weight=0.25),
        ],
        threshold_pp=3,
        initial_capital=1000000,
        transaction_cost_bps=5,
    )
    
    # 运行回测
    backtester = RebalanceBacktester(config)
    result = backtester.run()
    
    print(f"[OK] 回测完成: 累计收益{float(result.total_return):.2%}")
    
    # 获取各资产价格数据
    price_service = PriceQueryService()
    asset_price_data = {}
    
    for asset in config.assets:
        df = price_service.get_adjusted_prices(asset.code, use_cache=True)
        if not df.empty:
            # 对齐到回测日期范围
            mask = (df.index >= result.daily_values.index[0]) & \
                   (df.index <= result.daily_values.index[-1])
            df_filtered = df[mask]
            
            asset_price_data[asset.code] = {
                'dates': df_filtered.index.tolist(),
                'values': [float(v) for v in df_filtered['close']],
                'name': asset.name,
            }
    
    # 使用ChartFactory创建配置（索引已经是date对象）
    start_d = result.daily_values.index[0]
    end_d = result.daily_values.index[-1]
    
    # 导入YAxisScale枚举
    from src.domain.chart.entities import YAxisScale
    
    chart_config = ChartFactory.create_from_backtest_result(
        backtest_result=result,
        asset_price_data=asset_price_data,
        title=f"等权重再平衡策略收益曲线 ({start_d} ~ {end_d})",
        y_axis_scale=YAxisScale.LOGARITHMIC,  # 使用对数坐标
    )
    
    print(f"[OK] 图表配置创建成功: {chart_config}")
    print(f"   曲线数量: {len(chart_config.series_list)}")
    
    # 渲染图表
    output_path = "equity_curve_ddd.png"
    ChartRenderer.render(chart_config, output_path=output_path)
    print(f"[OK] 图表已保存为 {output_path}")
    
    return chart_config


def example_3_custom_styling():
    """示例3: 自定义样式"""
    print("\n" + "="*80)
    print("示例3: 自定义样式")
    print("="*80)
    
    # 生成有效日期（避免31号问题）
    import calendar
    dates = []
    for month in range(1, 7):
        max_day = calendar.monthrange(2024, month)[1]
        for day in range(1, min(max_day + 1, 11)):  # 每月取前10天
            dates.append(date(2024, month, day))
    
    config = ChartConfig(
        title="自定义样式示例",
        figsize=(16, 9),
        dpi=200,
    )
    
    # 组合曲线 - 粗实线
    portfolio_values = [Decimal("1") + Decimal(str(i * 0.01)) for i in range(60)]
    config.add_series(ChartSeries(
        name="组合",
        role=SeriesRole.PORTFOLIO,
        dates=dates,
        values=portfolio_values,
        style=ChartStyle(
            color="#2196F3",  # Material Blue
            line_width=3.0,
            line_style="-",
        ),
        annotation=Annotation(
            text=f"收益: {float(portfolio_values[-1]-1):.2%}",
            x_position=0.02,
            y_position=0.95,
            fontsize=11,
            fontweight="bold",
            bbox_facecolor="#E3F2FD",
        ),
    ))
    
    # 资产曲线 - 细虚线
    for i, (name, growth_rate) in enumerate([
        ("资产A", 0.008),
        ("资产B", 0.012),
        ("资产C", 0.015),
    ]):
        values = [Decimal("1") + Decimal(str(i * growth_rate)) for i in range(60)]
        config.add_series(ChartSeries(
            name=name,
            role=SeriesRole.ASSET,
            dates=dates,
            values=values,
            style=ChartStyle.for_asset(i),
            annotation=Annotation(
                text=f"{float(values[-1]-1):.2%}",
                x_position=0.95,
                y_position=0.05 + i * 0.08,
                fontsize=9,
            ),
        ))
    
    # 渲染
    output_path = "chart_custom_style.png"
    ChartRenderer.render(config, output_path=output_path)
    print(f"[OK] 自定义样式图表已保存为 {output_path}")
    
    return config


def main():
    """运行所有示例"""
    print("\n" + "="*80)
    print("图表DDD模型使用示例")
    print("="*80)
    
    examples = [
        ("手动创建图表配置", example_1_create_chart_config),
        ("从回测结果渲染", example_2_render_from_backtest),
        ("自定义样式", example_3_custom_styling),
    ]
    
    for name, func in examples:
        try:
            func()
        except Exception as e:
            print(f"\n[FAIL] 示例 [{name}] 失败: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "="*80)
    print("所有示例完成！")
    print("="*80)


if __name__ == "__main__":
    main()
