"""
交易策略与再平衡策略 DDD 模型使用示例

展示:
1. 创建自定义交易策略
2. 创建再平衡策略
3. 信号生成和回测
4. 订单执行模拟
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date, datetime
from decimal import Decimal

# 导入领域实体
from src.domain.strategy.entities import (
    TradingStrategy, TradingRule, Condition, Action,
    ConditionType, ComparisonOperator, ActionType, OrderType, StrategyStatus
)
from src.domain.rebalance.entities import (
    RebalancePolicy, RebalanceTrigger, RebalanceConstraints,
    TransactionCostModel, TriggerType
)
from src.domain.execution.entities import TradeOrder, OrderSide, ExecutionContext

# 导入领域服务
from src.domain.strategy.services import SignalGenerator, StrategyBacktester
from src.domain.rebalance.services import RebalanceEngine
from src.domain.execution.services import OrderExecutor


def example_1_create_trading_strategy():
    """示例1: 创建自定义交易策略"""
    print("\n" + "="*80)
    print("示例1: 创建自定义交易策略")
    print("="*80)
    
    # 创建均线交叉策略
    strategy = TradingStrategy(
        name="双均线交叉策略",
        description="当5日均线上穿20日均线时买入，下穿时卖出",
        rules=[
            TradingRule(
                condition=Condition(
                    type=ConditionType.INDICATOR_CROSS,
                    operator=ComparisonOperator.CROSS_ABOVE,
                    parameters={"fast_ma": 5, "slow_ma": 20}
                ),
                action=Action(
                    type=ActionType.BUY,
                    target_weight=Decimal("0.25"),
                    order_type=OrderType.MARKET
                ),
                priority=1,
                enabled=True
            ),
            TradingRule(
                condition=Condition(
                    type=ConditionType.INDICATOR_CROSS,
                    operator=ComparisonOperator.CROSS_BELOW,
                    parameters={"fast_ma": 5, "slow_ma": 20}
                ),
                action=Action(
                    type=ActionType.SELL,
                    target_weight=Decimal("0"),
                    order_type=OrderType.MARKET
                ),
                priority=2,
                enabled=True
            )
        ]
    )
    
    # 验证策略
    errors = strategy.validate()
    if errors:
        print(f"策略验证失败: {errors}")
    else:
        print(f"[OK] 策略创建成功: {strategy}")
        print(f"   规则数量: {len(strategy.rules)}")
        print(f"   状态: {strategy.status.value}")
    
    return strategy


def example_2_create_rebalance_policy():
    """示例2: 创建再平衡策略"""
    print("\n" + "="*80)
    print("示例2: 创建再平衡策略")
    print("="*80)
    
    # 创建阈值触发再平衡策略
    policy = RebalancePolicy(
        name="等权重阈值再平衡",
        trigger=RebalanceTrigger(
            type=TriggerType.THRESHOLD,
            parameters={"threshold_pp": 3}  # 偏离3个百分点触发
        ),
        target_allocation={
            "515180": Decimal("0.25"),
            "159937": Decimal("0.25"),
            "511380": Decimal("0.25"),
            "513300": Decimal("0.25"),
        },
        constraints=RebalanceConstraints(
            max_turnover=Decimal("0.5"),
            min_trade_size=Decimal("1000"),
            max_single_asset_change=Decimal("0.1")
        ),
        cost_model=TransactionCostModel(
            commission_rate=Decimal("0.0003"),
            slippage_rate=Decimal("0.001"),
            min_commission=Decimal("5")
        )
    )
    
    # 验证策略
    errors = policy.validate()
    if errors:
        print(f"策略验证失败: {errors}")
    else:
        print(f"[OK] 再平衡策略创建成功: {policy}")
        print(f"   目标配置: {len(policy.target_allocation)}个资产")
        print(f"   触发阈值: 3个百分点")
    
    return policy


def example_3_signal_generation():
    """示例3: 信号生成"""
    print("\n" + "="*80)
    print("示例3: 信号生成")
    print("="*80)
    
    strategy = example_1_create_trading_strategy()
    
    # 模拟市场数据
    market_data = {
        'current_price': Decimal("1.762"),
        'prev_close': Decimal("1.758"),
        'open': Decimal("1.760"),
        'high': Decimal("1.770"),
        'low': Decimal("1.755"),
        'volume': 1000000,
        'date': date(2026, 11, 6),
    }
    
    # 生成信号
    actions = SignalGenerator.generate_signals(strategy, market_data)
    
    print(f"\n市场数据:")
    print(f"  当前价格: {market_data['current_price']}")
    print(f"  前日收盘: {market_data['prev_close']}")
    print(f"\n生成的信号:")
    
    if actions:
        for i, action in enumerate(actions, 1):
            print(f"  {i}. {action.type.value.upper()} - 目标权重: {action.target_weight}")
    else:
        print("  无信号触发")
    
    return actions


def example_4_backtest_strategy():
    """示例4: 策略回测"""
    print("\n" + "="*80)
    print("示例4: 策略回测（简化版）")
    print("="*80)
    
    strategy = example_1_create_trading_strategy()
    
    # 创建模拟价格序列
    from src.domain.price.entities import AdjustedPriceSeries, AssetCode, PricePoint, AssetType, DataSource
    
    asset_code = AssetCode.from_string("515180")
    
    # 生成模拟价格数据
    prices = []
    base_price = Decimal("1.0")
    for i in range(100):
        price = base_price * (1 + Decimal(str(i * 0.01)))
        pp = PricePoint(
            date=date(2026, 1, 1).replace(day=min(i+1, 28)),
            open=price,
            high=price * Decimal("1.02"),
            low=price * Decimal("0.98"),
            close=price,
            volume=1000000,
        )
        prices.append(pp)
    
    series = AdjustedPriceSeries(
        asset_code=asset_code,
        asset_type=AssetType.ETF,
        prices=prices,
        source=DataSource.EASTMONEY_HFQ,
    )
    
    # 回测
    result = StrategyBacktester.backtest(
        strategy=strategy,
        price_series=series,
        initial_capital=Decimal("1000000"),
    )
    
    print(f"\n{result.summary()}")
    
    return result


def example_5_rebalance_check():
    """示例5: 再平衡检查"""
    print("\n" + "="*80)
    print("示例5: 再平衡检查")
    print("="*80)
    
    policy = example_2_create_rebalance_policy()
    
    # 模拟当前状态（权重已偏离）
    current_state = {
        'current_weights': {
            "515180": Decimal("0.30"),  # 超配5%
            "159937": Decimal("0.20"),  # 低配5%
            "511380": Decimal("0.25"),
            "513300": Decimal("0.25"),
        },
        'target_weights': policy.target_allocation,
        'portfolio_value': Decimal("1000000"),
        'prices': {
            "515180": Decimal("1.762"),
            "159937": Decimal("8.232"),
            "511380": Decimal("12.664"),
            "513300": Decimal("2.577"),
        },
    }
    
    # 检查是否触发
    triggered = RebalanceEngine.check_trigger(policy, current_state)
    print(f"\n是否触发再平衡: {'是' if triggered else '否'}")
    
    if triggered:
        # 计算订单
        orders = RebalanceEngine.calculate_orders(policy, current_state)
        
        print(f"\n需要执行的订单:")
        for i, order in enumerate(orders, 1):
            print(f"  {i}. {order.side.value.upper()} {order.quantity:.0f} {order.asset_code}")
        
        # 模拟再平衡
        sim_result = RebalanceEngine.simulate_rebalance(policy, current_state)
        print(f"\n{sim_result.summary()}")
    
    return triggered


def example_6_order_execution():
    """示例6: 订单执行模拟"""
    print("\n" + "="*80)
    print("示例6: 订单执行模拟")
    print("="*80)
    
    # 创建订单
    order = TradeOrder(
        asset_code="515180",
        side=OrderSide.BUY,
        quantity=Decimal("1000"),
        order_type=OrderType.MARKET,
    )
    
    # 创建执行上下文
    context = OrderExecutor.create_market_context(
        timestamp=datetime.now(),
        cash=Decimal("100000"),
        positions={"515180": Decimal("0")},
        prices={"515180": Decimal("1.762")},
        commission_rate=Decimal("0.0003"),
        slippage_rate=Decimal("0.001"),
    )
    
    # 执行订单
    result = OrderExecutor.execute_order(order, context)
    
    print(f"\n订单: {order}")
    print(f"\n执行结果:")
    print(f"  成功: {result.success}")
    print(f"  成交数量: {result.filled_quantity}")
    print(f"  成交价格: {result.average_price}")
    print(f"  总成本: {result.total_cost:.2f}")
    print(f"  佣金: {result.commission:.2f}")
    print(f"  滑点: {result.slippage:.2f}")
    
    return result


def main():
    """运行所有示例"""
    print("\n" + "="*80)
    print("交易策略与再平衡策略 DDD 模型演示")
    print("="*80)
    
    examples = [
        ("创建交易策略", example_1_create_trading_strategy),
        ("创建再平衡策略", example_2_create_rebalance_policy),
        ("信号生成", example_3_signal_generation),
        ("策略回测", example_4_backtest_strategy),
        ("再平衡检查", example_5_rebalance_check),
        ("订单执行", example_6_order_execution),
    ]
    
    for name, func in examples:
        try:
            func()
        except Exception as e:
            print(f"[FAIL] 示例 [{name}] 失败: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "="*80)
    print("所有示例完成！")
    print("="*80)


if __name__ == "__main__":
    main()
