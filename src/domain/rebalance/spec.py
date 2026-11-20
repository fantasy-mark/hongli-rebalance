"""
再平衡策略规格（领域层）

把再平衡配置的真实参数翻译成「策略名 —— 规则描述 —— 规则实现」。
描述由参数推导，参数改了描述自动跟着变。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, List

from src.domain.strategy.spec import (
    RuleParameter,
    StrategyRule,
    StrategySpec,
    build_spec,
)

STRATEGY_ID = "rebalance"
STRATEGY_NAME = "阈值再平衡"

IMPL_TRIGGER = "src/application/rebalance/backtest_engine.py::RebalanceBacktester._check_rebalance"
IMPL_REBALANCE = "src/application/rebalance/backtest_engine.py::RebalanceBacktester._rebalance"
IMPL_PORTFOLIO = "src/domain/portfolio/entities.py::Portfolio"
IMPL_ALLOCATION = "src/domain/allocation/entities.py::AllocationPlan"


def build_rebalance_spec(config: Any) -> StrategySpec:
    """由再平衡配置构建策略规格"""
    threshold_pp = Decimal(str(config.threshold_pp))
    cost_bps = Decimal(str(config.transaction_cost_bps))

    assets = list(config.assets)
    weight_text = "、".join(
        f"{a.name} {float(a.target_weight):.0%}" for a in assets
    )

    # ---- 规则 1：目标配置 ----
    allocation_rule = StrategyRule(
        rule_id="rebalance.allocation",
        title="目标配置",
        description=(
            f"组合按固定权重持有 {len(assets)} 个标的：{weight_text}；"
            f"网格与再平衡共用同一份总资产配置，保证监控标的一致。"
        ),
        formula="目标权重之和 = 1",
        implementation=IMPL_ALLOCATION,
        parameters=tuple(
            RuleParameter(f"{a.name} 目标权重", f"weight_{a.code}",
                          f"{float(a.target_weight):.1%}", "该标的的目标仓位")
            for a in assets
        ),
    )

    # ---- 规则 2：触发条件 ----
    trigger_rule = StrategyRule(
        rule_id="rebalance.trigger",
        title="触发条件",
        description=(
            f"每日收盘后检查各标的实际权重，"
            f"当任一标的权重偏离目标权重超过 {threshold_pp} 个百分点时触发再平衡。"
        ),
        formula=(
            f"|实际权重 − 目标权重| > {threshold_pp}pp\n"
            f"→ 卖出超配部分，买入低配部分，恢复目标权重"
        ),
        implementation=IMPL_TRIGGER,
        parameters=(
            RuleParameter("再平衡阈值", "threshold_pp", f"{threshold_pp}pp",
                          "权重偏离超过该百分点即触发"),
        ),
    )

    # ---- 规则 3：交易成本 ----
    cost_rule = StrategyRule(
        rule_id="rebalance.cost",
        title="交易成本",
        description=(
            f"每笔成交按 {cost_bps} 个基点（{float(cost_bps) / 100:.2%}）计费，"
            f"成本从组合现金中扣除，已计入净值曲线。"
        ),
        formula=f"cost = amount × {cost_bps} / 10000",
        implementation=IMPL_REBALANCE,
        parameters=(
            RuleParameter("交易成本", "transaction_cost_bps", f"{cost_bps}bp",
                          "单边交易费率（基点）"),
        ),
    )

    # ---- 规则 4：资金隔离 ----
    isolation_rule = StrategyRule(
        rule_id="rebalance.isolation",
        title="资金隔离",
        description=(
            "再平衡组合拥有独立资金池，只动自己的现金与持仓；"
            "与网格组合互不读取、互不修改对方的资金。"
        ),
        implementation=IMPL_PORTFOLIO,
        parameters=(),
    )

    rules: List[StrategyRule] = [
        allocation_rule, trigger_rule, cost_rule, isolation_rule,
    ]

    return build_spec(
        strategy_id=STRATEGY_ID,
        name=STRATEGY_NAME,
        summary=(
            f"{len(assets)} 资产固定权重，"
            f"权重偏离超 {threshold_pp}pp 时再平衡回目标"
        ),
        rules=rules,
        implementation=(
            IMPL_TRIGGER,
            IMPL_REBALANCE,
            IMPL_PORTFOLIO,
            IMPL_ALLOCATION,
        ),
    )
