"""
网格策略使用示例

展示:
1. 网格策略回测（跌破前120日最低价启动监控，每跌一个ATR比例买入10000元）
2. 参数敏感性对比（ATR比例 / 单格买入金额）
3. 网格 + 再平衡组合回测（持仓隔离）
4. 净值曲线与网格买入点绘图
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Windows 控制台默认 GBK 编码，这里统一为 UTF-8 以正常输出中文
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # pragma: no cover
    pass

import logging
from datetime import date
from decimal import Decimal

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

from src.strategy.grid.backtest import (
    GridAssetConfig,
    GridBacktester,
    GridConfig,
)
from src.chart.grid_chart import GridChartRenderer
from src.chart.tables import grid_trigger_log
from src.strategy.combined import (
    CombinedBacktester,
    CombinedConfig,
)
from src.strategy.rebalance.backtest import (
    AssetConfig,
    RebalanceConfig,
)
from src.strategy.allocation import AllocationPlan
from src.strategy.grid.models import GridPriceBasis, GridTrigger

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

ASSETS = [
    ("515180", "红利ETF"),
    ("159937", "黄金ETF"),
    ("511380", "可转债ETF"),
    ("513300", "纳指ETF"),
]

# 回测开始日期（与再平衡组合的共同区间起点保持一致）
BACKTEST_START = date(2020, 11, 5)


def build_allocation() -> AllocationPlan:
    """构造共享的总资产配置（再平衡与网格共用）"""
    return AllocationPlan.equal_weight(ASSETS, name="四资产等权组合")


def build_grid_config(step_ratio: str = "2.0",
                      buy_amount: str = "10000",
                      capital: str = "200000",
                      take_profit_ratio: str = "1",
                      start_date: date = BACKTEST_START) -> GridConfig:
    """构造网格配置（资产来自共享的总资产配置）"""
    return GridConfig.from_allocation(
        plan=build_allocation(),
        trigger=GridTrigger(
            lookback_days=120,        # 前120日最低价（不含当日）
            atr_period=120,           # 120日ATR
            step_ratio=Decimal(step_ratio),   # 网格间距 = ATR * 比例
            buy_amount=Decimal(buy_amount),   # 每格买入金额
            lot_size=100,
            price_basis=GridPriceBasis.CLOSE,   # 触发价 + 限价成交
            take_profit_ratio=(Decimal(take_profit_ratio)
                               if take_profit_ratio else None),
        ),
        initial_capital=Decimal(capital),
        start_date=start_date,
    )


def example_1_grid_backtest():
    """示例1: 网格策略回测"""
    print("\n" + "=" * 80)
    print("示例1: 网格策略回测（默认参数）")
    print("=" * 80)

    result = GridBacktester(build_grid_config()).run()
    print(result.summary())

    print("\n网格触发日志:")
    print(grid_trigger_log(result))

    return result


def example_2_parameter_sensitivity():
    """示例2: 参数敏感性对比"""
    print("\n" + "=" * 80)
    print("示例2: 参数敏感性对比")
    print("=" * 80)

    configs = [
        ("ATR比例0.5 / 每格1万", build_grid_config("0.5", "10000")),
        ("ATR比例1.0 / 每格1万", build_grid_config("1.0", "10000")),
        ("ATR比例0.5 / 每格2万", build_grid_config("0.5", "20000")),
    ]

    print(f"{'参数组合':<24}{'买入次数':>10}{'累计收益':>12}{'年化收益':>12}{'最大回撤':>12}")
    print("-" * 70)

    results = []
    for label, config in configs:
        result = GridBacktester(config).run()
        results.append((label, result))
        max_dd = result.max_drawdown[0]
        print(f"{label:<24}{result.buy_count:>10}"
              f"{result.total_return:>12.2%}{result.annualized_return:>12.2%}"
              f"{max_dd:>12.2%}")

    return results


def example_3_combined_isolation():
    """示例3: 网格 + 再平衡组合回测（共享总资产配置，持仓隔离）"""
    print("\n" + "=" * 80)
    print("示例3: 组合回测（共享总资产配置，持仓隔离）")
    print("=" * 80)

    combined = CombinedBacktester(
        config=CombinedConfig(
            allocation=build_allocation(),   # ★ 两个策略共用同一份总资产配置
            threshold_pp=Decimal("3"),
            rebalance_initial_capital=Decimal("1000000"),
            grid_initial_capital=Decimal("200000"),
            grid_trigger=GridTrigger(
                lookback_days=120,
                atr_period=120,
                step_ratio=Decimal("0.5"),
                buy_amount=Decimal("10000"),
                price_basis=GridPriceBasis.CLOSE,
                take_profit_ratio=Decimal("1"),
            ),
            start_date=BACKTEST_START,
        )
    ).run()

    print(combined.summary())

    rebalance_result = combined.rebalance_result
    grid_result = combined.grid_result

    print("\n隔离校验:")
    print(f"  再平衡末现金: ¥{float(rebalance_result.snapshots[-1].cash):,.2f}")
    print(f"  网格末现金:   ¥{float(grid_result.snapshots[-1].cash):,.2f}")
    print(f"  再平衡末持仓: {rebalance_result.snapshots[-1].asset_shares}")
    print(f"  网格末持仓:   {grid_result.snapshots[-1].positions}")

    return combined


def example_4_with_take_profit():
    """示例4: 网格轮次（从跌破前低到完全止盈）"""
    print("\n" + "=" * 80)
    print("示例4: 网格轮次生命周期（跌破前低 -> 完全止盈 -> 重新跌破才开新轮）")
    print("=" * 80)

    result = GridBacktester(build_grid_config(take_profit_ratio="1")).run()

    print(f"买入次数: {result.buy_count}, 卖出次数: {result.sell_count}")
    print(f"网格轮次: {result.completed_rounds}/{result.total_rounds} 轮已完全止盈")
    print(f"累计收益: {result.total_return:.2%}, 年化收益: {result.annualized_return:.2%}")

    if result.trades:
        print("\n网格触发日志:")
        print(grid_trigger_log(result))

    # 输出带买卖点的图表
    output = GridChartRenderer.render(
        result,
        output_path='grid_strategy_take_profit.png',
    )
    print(f"\n图表已保存: {output}")
    return result


def plot_results(grid_result, combined):
    """绘制网格图表（上方资产走势 + 下方持仓堆叠）与组合对比图"""
    output = GridChartRenderer.render(
        grid_result,
        output_path='grid_strategy_result.png',
    )
    print(f"\n图表已保存: {output}")

    if combined is not None:
        comparison = GridChartRenderer.render_comparison(
            combined,
            output_path='grid_strategy_comparison.png',
        )
        if comparison:
            print(f"组合对比图已保存: {comparison}")


def main():
    """主流程"""
    grid_result = example_1_grid_backtest()
    example_2_parameter_sensitivity()
    combined = example_3_combined_isolation()
    plot_results(grid_result, combined)
    example_4_with_take_profit()


if __name__ == '__main__':
    main()
