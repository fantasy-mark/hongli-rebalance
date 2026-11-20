"""
网格策略规格（策略模块）

把 `GridTrigger` 的真实参数翻译成「策略名 —— 规则描述 —— 规则实现」。

关键点：描述**由参数推导**，参数改了描述自动跟着变，
不会出现「文案说 2ATR、代码跑 0.5ATR」这种漂移。
"""

from __future__ import annotations

from decimal import Decimal
from typing import List

from src.strategy.grid.models import GridAnchor, GridPriceBasis, GridTrigger
from src.strategy.spec import (
    RuleParameter,
    StrategyRule,
    StrategySpec,
    build_spec,
)

STRATEGY_ID = "grid"
STRATEGY_NAME = "复合网格"

# 规则实现位置（可跳转源码）
IMPL_TRIGGER = "src/strategy/grid/engine.py::GridEngine.evaluate"
IMPL_LEVELS = "src/strategy/grid/engine.py::GridEngine._process_levels"
IMPL_TAKE_PROFIT = "src/strategy/grid/engine.py::GridEngine._process_take_profit"
IMPL_STATE = "src/strategy/grid/models.py::GridState"
IMPL_TRIGGER_VO = "src/strategy/grid/models.py::GridTrigger"


def _ratio_text(ratio: Decimal | None) -> str:
    if ratio is None:
        return "无"
    text = format(ratio.normalize(), "f")
    return text


def _basis_text(basis: GridPriceBasis) -> str:
    return {
        GridPriceBasis.CLOSE: "收盘价",
        GridPriceBasis.LOW: "当日最低价",
    }.get(basis, basis.value)


def _anchor_text(anchor: GridAnchor) -> str:
    return {
        GridAnchor.TRIGGER_PRICE: "触发日价格",
        GridAnchor.PRIOR_LOW: "前N日最低价",
    }.get(anchor, anchor.value)


def build_grid_spec(trigger: GridTrigger) -> StrategySpec:
    """由网格触发参数构建策略规格"""
    step_ratio = _ratio_text(trigger.step_ratio)
    tp_ratio = _ratio_text(trigger.take_profit_ratio)

    # ---- 规则 1：触发条件 ----
    trigger_rule = StrategyRule(
        rule_id="grid.trigger",
        title="触发条件",
        description=(
            f"当{_basis_text(trigger.price_basis)}跌破前 {trigger.lookback_days} "
            f"个交易日的最低价时，网格被激活，开启新一轮；"
            f"激活瞬间计算 ATR({trigger.atr_period}) 与网格步长，"
            f"并以「{_anchor_text(trigger.anchor)}」作为锚点。"
        ),
        formula=(
            f"触发: price <= min(close[-{trigger.lookback_days}:-1])\n"
            f"步长: step = ATR({trigger.atr_period}) × {step_ratio}"
        ),
        implementation=IMPL_TRIGGER,
        parameters=(
            RuleParameter("回看天数", "lookback_days", str(trigger.lookback_days),
                          "跌破前 N 个交易日的最低价即触发"),
            RuleParameter("ATR 周期", "atr_period", str(trigger.atr_period),
                          "步长所用的 ATR 周期（Wilder 平滑）"),
            RuleParameter("步长倍数", "step_ratio", step_ratio,
                          "网格步长 = ATR × 该倍数"),
            RuleParameter("价格口径", "price_basis", trigger.price_basis.value,
                          f"触发判定使用{_basis_text(trigger.price_basis)}"),
            RuleParameter("锚点", "anchor", trigger.anchor.value,
                          f"第一档价格基准：{_anchor_text(trigger.anchor)}"),
        ),
    )

    # ---- 规则 2：加仓 ----
    level_rule = StrategyRule(
        rule_id="grid.add",
        title="加仓规则",
        description=(
            f"网格激活后，每再下跌 {step_ratio}×ATR({trigger.atr_period}) "
            f"加一档仓位；每档买入 {trigger.buy_amount:,.0f} 元，"
            f"股数向下取整到 {trigger.lot_size} 股的整数倍；"
            f"档位价格 = 锚点 − 步长 × 档位序号。"
        ),
        formula=(
            f"档位N价格 = 锚点 − N × step\n"
            f"单档金额 = {trigger.buy_amount:,.0f} 元（向下取整到 {trigger.lot_size} 股）"
        ),
        implementation=IMPL_LEVELS,
        parameters=(
            RuleParameter("单档金额", "buy_amount", f"{trigger.buy_amount:,.0f}",
                          "每档买入金额"),
            RuleParameter("最小交易单位", "lot_size", str(trigger.lot_size),
                          "股数向下取整的整倍数"),
            RuleParameter("最大档位", "max_levels",
                          str(trigger.max_levels) if trigger.max_levels else "不限",
                          "单轮最多加仓层数"),
        ),
    )

    # ---- 规则 3：止盈 ----
    if trigger.take_profit_ratio is None:
        tp_description = "本策略不设止盈，网格持仓一直持有。"
        tp_formula = ""
    else:
        tp_description = (
            f"当价格回升至「首次建仓价 + {tp_ratio}×步长」时触发止盈，"
            f"整轮一次性卖出全部持仓（合并为一笔成交），本轮结束；"
            f"必须再次跌破前 {trigger.lookback_days} 日最低价才能开启下一轮。"
        )
        tp_formula = f"止盈价 = 首次建仓价 + {tp_ratio} × step"

    tp_rule = StrategyRule(
        rule_id="grid.take_profit",
        title="止盈规则",
        description=tp_description,
        formula=tp_formula,
        implementation=IMPL_TAKE_PROFIT,
        parameters=(
            RuleParameter("止盈倍数", "take_profit_ratio", tp_ratio,
                          "相对一个网格步长的止盈幅度"),
        ),
    )

    # ---- 规则 4：资金隔离 ----
    isolation_rule = StrategyRule(
        rule_id="grid.isolation",
        title="资金隔离",
        description=(
            "网格使用独立资金池，只动网格自己的现金与持仓，"
            "与再平衡组合完全隔离；单轮止盈后网格现金回到池中，等待下一轮。"
        ),
        implementation="src/strategy/portfolio.py::Portfolio",
        parameters=(),
    )

    rules: List[StrategyRule] = [trigger_rule, level_rule, tp_rule, isolation_rule]

    return build_spec(
        strategy_id=STRATEGY_ID,
        name=STRATEGY_NAME,
        summary=(
            f"跌破前 {trigger.lookback_days} 日最低价开轮，"
            f"每跌 {step_ratio}×ATR({trigger.atr_period}) 加一档，"
            f"回升至建仓价 +{tp_ratio}×步长 整轮止盈"
        ),
        rules=rules,
        implementation=(
            IMPL_TRIGGER,
            IMPL_LEVELS,
            IMPL_TAKE_PROFIT,
            IMPL_STATE,
            IMPL_TRIGGER_VO,
        ),
    )
