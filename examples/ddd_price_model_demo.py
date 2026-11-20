"""
DDD价格模型使用示例

展示如何使用新的领域模型进行：
1. 获取价格数据
2. 计算收益率
3. 绘制图表
4. 多资产对比
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from decimal import Decimal

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

from src.data.service import PriceQueryService
from src.data.models import AssetCode, AdjustedPriceSeries
from src.data.indicators import PriceCalculator


def example_1_basic_price_query():
    """示例1: 基本价格查询"""
    print("\n" + "=" * 80)
    print("示例1: 基本价格查询")
    print("=" * 80)
    
    service = PriceQueryService()
    
    # 获取515180的价格数据
    df = service.get_adjusted_prices("515180", start_date="2026-11-01")
    
    print(f"\n515180 近5日价格:")
    print(df.tail())
    
    # 获取最新价格
    latest = service.get_latest_price("515180")
    print(f"\n最新价格信息:")
    print(f"  日期: {latest['date']}")
    print(f"  收盘价: {latest['close']:.3f}")
    print(f"  涨跌幅: {latest['change_pct']:.2%}")
    print(f"  数据来源: {latest['source']}")


def example_2_return_metrics():
    """示例2: 收益率指标计算"""
    print("\n" + "=" * 80)
    print("示例2: 收益率指标计算")
    print("=" * 80)
    
    service = PriceQueryService()
    
    # 计算515180的收益率
    metrics = service.calculate_returns(
        "515180",
        start_date="2020-01-01",
        end_date="2026-11-06"
    )
    
    print(f"\n515180 (2020-2026) 收益率指标:")
    print(metrics.summary())
    
    # 对比不同时间段
    print("\n" + "-" * 80)
    print("不同时间段的年化收益:")
    
    periods = [
        ("2020-2021", "2020-01-01", "2021-12-31"),
        ("2022-2023", "2022-01-01", "2023-12-31"),
        ("2024-2026", "2024-01-01", "2026-11-06"),
    ]
    
    for name, start, end in periods:
        m = service.calculate_returns("515180", start_date=start, end_date=end)
        print(f"  {name}: {m.annualized_return:.2%}")


def example_3_multi_asset_comparison():
    """示例3: 多资产对比"""
    print("\n" + "=" * 80)
    print("示例3: 多资产价格对比")
    print("=" * 80)
    
    service = PriceQueryService()
    
    codes = ["515180", "159937", "511380", "513300"]
    names = {
        "515180": "红利ETF",
        "159937": "黄金ETF",
        "511380": "可转债ETF",
        "513300": "纳指ETF",
    }
    
    # 获取对比数据
    df_compare = service.compare_assets(
        codes,
        start_date="2024-01-01",
        normalize=True
    )
    
    print(f"\n归一化价格对比 (起点=1):")
    print(df_compare.tail())
    
    # 计算每个资产的收益率
    print("\n各资产收益率对比 (2024至今):")
    for code in codes:
        metrics = service.calculate_returns(code, start_date="2024-01-01")
        print(f"  {names[code]}: 累计{metrics.total_return:.2%}, 年化{metrics.annualized_return:.2%}")


def example_4_technical_indicators():
    """示例4: 技术指标"""
    print("\n" + "=" * 80)
    print("示例4: 技术指标分析")
    print("=" * 80)
    
    service = PriceQueryService()
    
    # 获取技术指标
    indicators = service.get_technical_indicators("515180", index=-1)
    
    if indicators:
        print(f"\n515180 最新技术指标:")
        print(f"  MA5:  {indicators['ma_5']:.3f}")
        print(f"  MA10: {indicators['ma_10']:.3f}")
        print(f"  MA20: {indicators['ma_20']:.3f}")
        print(f"  MA60: {indicators['ma_60']:.3f}")
        print(f"  RSI14: {indicators['rsi_14']:.2f}")
        print(f"  布林带上轨: {indicators['bollinger_upper']:.3f}")
        print(f"  布林带中轨: {indicators['bollinger_middle']:.3f}")
        print(f"  布林带下轨: {indicators['bollinger_lower']:.3f}")


def example_5_domain_entities():
    """示例5: 直接使用领域实体"""
    print("\n" + "=" * 80)
    print("示例5: 直接使用领域实体")
    print("=" * 80)
    
    # 创建资产代码
    asset_code = AssetCode.from_string("515180")
    print(f"资产代码: {asset_code}")
    print(f"  交易所: {asset_code.exchange.value}")
    print(f"  是否ETF: {asset_code.is_etf()}")
    print(f"  新浪符号: {asset_code.to_sina_symbol()}")
    
    # 从数据源加载序列
    service = PriceQueryService()
    df = service.get_adjusted_prices("515180", start_date="2026-01-01")
    
    series = AdjustedPriceSeries.from_dataframe(asset_code, df)
    
    print(f"\n价格序列信息:")
    print(f"  {repr(series)}")
    print(f"  起始日期: {series.start_date}")
    print(f"  结束日期: {series.end_date}")
    print(f"  数据点数: {series.count}")
    
    # 验证数据
    errors = series.validate()
    if errors:
        print(f"  验证警告: {errors}")
    else:
        print(f"  验证: ✓ 通过")
    
    # 使用领域服务计算
    calculator = PriceCalculator()
    total_return = calculator.calculate_total_return(series)
    print(f"\n累计收益率: {total_return:.2%}")


def example_6_chart_ready_data():
    """示例6: 准备绘图数据"""
    print("\n" + "=" * 80)
    print("示例6: 准备绘图数据")
    print("=" * 80)
    
    service = PriceQueryService()
    
    # 获取适合绘图的数据
    df = service.get_adjusted_prices("515180", start_date="2024-01-01")
    
    print(f"\n数据结构:")
    print(f"  索引类型: {type(df.index)}")
    print(f"  列: {list(df.columns)}")
    print(f"  行数: {len(df)}")
    print(f"\n前3行:")
    print(df.head(3))
    
    print(f"\n可以直接用于matplotlib绘图:")
    print("  import matplotlib.pyplot as plt")
    print("  plt.plot(df.index, df['close'])")
    print("  plt.show()")


def main():
    """运行所有示例"""
    print("\n" + "=" * 80)
    print("DDD价格模型使用示例")
    print("=" * 80)
    
    examples = [
        ("基本价格查询", example_1_basic_price_query),
        ("收益率指标计算", example_2_return_metrics),
        ("多资产对比", example_3_multi_asset_comparison),
        ("技术指标分析", example_4_technical_indicators),
        ("领域实体使用", example_5_domain_entities),
        ("绘图数据准备", example_6_chart_ready_data),
    ]
    
    for name, func in examples:
        try:
            func()
        except Exception as e:
            print(f"\n❌ 示例 [{name}] 失败: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 80)
    print("所有示例完成！")
    print("=" * 80)


if __name__ == "__main__":
    main()
