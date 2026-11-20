"""
再平衡策略回测示例

展示：
1. 等权重再平衡策略
2. 自定义权重策略
3. 收益曲线绘制
4. 性能指标对比
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import date

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

from src.strategy.rebalance.backtest import (
    RebalanceConfig, 
    AssetConfig, 
    RebalanceBacktester
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)


def create_equal_weight_config() -> RebalanceConfig:
    """创建等权重配置（各25%）"""
    return RebalanceConfig(
        assets=[
            AssetConfig(code="515180", name="红利ETF", target_weight=0.25),
            AssetConfig(code="159937", name="黄金ETF", target_weight=0.25),
            AssetConfig(code="511380", name="可转债ETF", target_weight=0.25),
            AssetConfig(code="513300", name="纳指ETF", target_weight=0.25),
        ],
        threshold_pp=3,  # 偏离3个百分点触发
        initial_capital=1000000,  # 100万初始资金
        transaction_cost_bps=5,  # 0.05%交易成本
    )


def create_custom_weight_config() -> RebalanceConfig:
    """创建自定义权重配置"""
    return RebalanceConfig(
        assets=[
            AssetConfig(code="515180", name="红利ETF", target_weight=0.20),
            AssetConfig(code="159937", name="黄金ETF", target_weight=0.30),
            AssetConfig(code="511380", name="可转债ETF", target_weight=0.20),
            AssetConfig(code="513300", name="纳指ETF", target_weight=0.30),
        ],
        threshold_pp=3,
        initial_capital=1000000,
        transaction_cost_bps=5,
    )


def plot_equity_curve(result, title="再平衡策略收益曲线"):
    """绘制收益曲线"""
    df = result.daily_values
    
    fig, ax1 = plt.subplots(figsize=(14, 7))
    
    # 主图：总市值
    ax1.plot(df.index, df['total_value'], 'b-', linewidth=2, label='组合市值')
    ax1.set_xlabel('日期', fontsize=12)
    ax1.set_ylabel('市值 (元)', fontsize=12, color='b')
    ax1.tick_params(axis='y', labelcolor='b')
    ax1.grid(True, alpha=0.3)
    
    # 格式化x轴日期
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
    
    # 添加标题和图例
    ax1.set_title(title, fontsize=14, fontweight='bold')
    ax1.legend(loc='upper left')
    
    # 添加收益率标注
    total_return = result.total_return
    ann_return = result.annualized_return
    max_dd, _, _ = result.max_drawdown
    
    textstr = (
        f'累计收益: {total_return:.2%}\n'
        f'年化收益: {ann_return:.2%}\n'
        f'最大回撤: {max_dd:.2%}'
    )
    props = dict(boxstyle='round,pad=0.5', facecolor='wheat', alpha=0.8)
    ax1.text(0.02, 0.98, textstr, transform=ax1.transAxes, fontsize=10,
            verticalalignment='top', bbox=props)
    
    plt.tight_layout()
    plt.savefig('equity_curve.png', dpi=150, bbox_inches='tight')
    print("[OK] 收益曲线已保存为 equity_curve.png")
    # plt.show()  # 注释掉避免阻塞


def plot_asset_weights(result):
    """绘制资产权重变化"""
    df = result.daily_values
    
    # 提取权重列
    weight_cols = [col for col in df.columns if col.endswith('_weight')]
    
    fig, ax = plt.subplots(figsize=(14, 7))
    
    for col in weight_cols:
        asset_code = col.replace('_weight', '')
        asset_name = result.config.asset_names.get(asset_code, asset_code)
        ax.plot(df.index, df[col], label=f'{asset_name} ({asset_code})', linewidth=1.5)
    
    # 添加目标权重线
    colors = ['red', 'green', 'blue', 'orange']
    for i, asset in enumerate(result.config.assets):
        ax.axhline(y=float(asset.target_weight), color=colors[i % len(colors)], 
                  linestyle='--', alpha=0.5, linewidth=1)
    
    ax.set_xlabel('日期', fontsize=12)
    ax.set_ylabel('权重', fontsize=12)
    ax.set_title('资产权重变化', fontsize=14, fontweight='bold')
    ax.legend(loc='upper left')
    ax.grid(True, alpha=0.3)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.0%}'))
    
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
    
    plt.tight_layout()
    plt.savefig('asset_weights.png', dpi=150, bbox_inches='tight')
    print("[OK] 权重变化图已保存为 asset_weights.png")
    # plt.show()


def compare_strategies():
    """对比不同策略"""
    print("\n" + "="*80)
    print("策略对比测试")
    print("="*80)
    
    # 策略1: 等权重
    print("\n" + "="*80)
    print("【策略1】等权重再平衡 (各25%)")
    print("="*80)
    config1 = create_equal_weight_config()
    backtester1 = RebalanceBacktester(config1)
    result1 = backtester1.run()
    print(f"累计收益: {float(result1.total_return):.2%}")
    print(f"年化收益: {float(result1.annualized_return):.2%}")
    print(f"夏普比率: {float(result1.sharpe_ratio):.2f}")
    max_dd1, _, _ = result1.max_drawdown
    print(f"最大回撤: {float(max_dd1):.2%}")
    print(f"交易次数: {result1.total_trades}")
    
    # 策略2: 自定义权重
    print("\n" + "="*80)
    print("【策略2】自定义权重 (红利20%, 黄金30%, 可转债20%, 纳指30%)")
    print("="*80)
    config2 = create_custom_weight_config()
    backtester2 = RebalanceBacktester(config2)
    result2 = backtester2.run()
    print(f"累计收益: {float(result2.total_return):.2%}")
    print(f"年化收益: {float(result2.annualized_return):.2%}")
    print(f"夏普比率: {float(result2.sharpe_ratio):.2f}")
    max_dd2, _, _ = result2.max_drawdown
    print(f"最大回撤: {float(max_dd2):.2%}")
    print(f"交易次数: {result2.total_trades}")
    
    # 对比图表
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))
    
    # 收益曲线对比
    df1 = result1.daily_values
    df2 = result2.daily_values
    
    # 归一化到起点
    df1_norm = df1['total_value'] / df1['total_value'].iloc[0]
    df2_norm = df2['total_value'] / df2['total_value'].iloc[0]
    
    ax1.plot(df1_norm.index, df1_norm, 'b-', linewidth=2, label='等权重策略')
    ax1.plot(df2_norm.index, df2_norm, 'r-', linewidth=2, label='自定义权重策略')
    ax1.set_title('策略收益对比 (归一化)', fontsize=14, fontweight='bold')
    ax1.set_ylabel('归一化市值', fontsize=12)
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    ax1.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
    
    # 关键指标对比柱状图
    metrics = ['累计收益', '年化收益', '夏普比率', '最大回撤']
    values1 = [
        float(result1.total_return),
        float(result1.annualized_return),
        float(result1.sharpe_ratio),
        float(result1.max_drawdown[0]),
    ]
    values2 = [
        float(result2.total_return),
        float(result2.annualized_return),
        float(result2.sharpe_ratio),
        float(result2.max_drawdown[0]),
    ]
    
    x = range(len(metrics))
    width = 0.35
    
    bars1 = ax2.bar([i - width/2 for i in x], values1, width, label='等权重', color='steelblue')
    bars2 = ax2.bar([i + width/2 for i in x], values2, width, label='自定义权重', color='coral')
    
    ax2.set_ylabel('值', fontsize=12)
    ax2.set_title('关键指标对比', fontsize=14, fontweight='bold')
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(metrics)
    ax2.legend()
    ax2.grid(True, alpha=0.3, axis='y')
    
    # 在柱子上添加数值标签
    for bar in bars1:
        height = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.2%}' if abs(height) < 1 else f'{height:.2f}',
                ha='center', va='bottom', fontsize=8)
    for bar in bars2:
        height = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2., height,
                f'{height:.2%}' if abs(height) < 1 else f'{height:.2f}',
                ha='center', va='bottom', fontsize=8)
    
    plt.tight_layout()
    plt.savefig('strategy_comparison.png', dpi=150, bbox_inches='tight')
    print("\n[OK] 策略对比图已保存为 strategy_comparison.png")
    # plt.show()


def main():
    """主函数"""
    print("\n" + "="*80)
    print("再平衡策略回测系统")
    print("="*80)
    
    # 运行等权重策略
    print("\n【运行】等权重再平衡策略...")
    config = create_equal_weight_config()
    backtester = RebalanceBacktester(config)
    result = backtester.run()
    
    # 打印摘要（避免Unicode问题）
    print("\n" + "="*80)
    print("回测结果摘要")
    print("="*80)
    print(f"回测期间: {result.snapshots[0].date} ~ {result.snapshots[-1].date}")
    print(f"初始资金: {result.config.initial_capital:,.0f}")
    print(f"最终市值: {result.snapshots[-1].total_value:,.0f}")
    print(f"\n收益指标:")
    print(f"  累计收益: {float(result.total_return):.2%}")
    print(f"  年化收益: {float(result.annualized_return):.2%}")
    print(f"  波动率:   {float(result.volatility):.2%}")
    print(f"  夏普比率: {float(result.sharpe_ratio):.2f}")
    max_dd, dd_start, dd_end = result.max_drawdown
    print(f"  最大回撤: {float(max_dd):.2%} ({dd_start} ~ {dd_end})")
    print(f"\n交易统计:")
    print(f"  总交易次数: {result.total_trades}")
    print(f"  总交易成本: {float(result.total_transaction_cost):,.2f}")
    
    # 绘制收益曲线
    plot_equity_curve(result, title="等权重再平衡策略收益曲线 (2020-2026)")
    
    # 绘制权重变化
    plot_asset_weights(result)
    
    # 对比不同策略
    compare_strategies()
    
    print("\n" + "="*80)
    print("回测完成！所有图表已保存。")
    print("="*80)


if __name__ == "__main__":
    main()
