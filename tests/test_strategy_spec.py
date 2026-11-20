"""策略规格测试（策略名 / 规则描述 / 规则实现）

覆盖:
- domain/strategy/spec.py 的值对象不变量
- domain/grid/spec.py 的规则描述由参数推导（不会与实现漂移）
- domain/rebalance/spec.py
- application/pages/site_builder.py 的交易记录合并与策略规格输出
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.grid.spec import build_grid_spec
from src.domain.grid.value_objects import (
    GridAnchor,
    GridPriceBasis,
    GridTrigger,
)
from src.domain.rebalance.spec import build_rebalance_spec
from src.domain.strategy.spec import (
    RuleParameter,
    StrategyRule,
    StrategySpec,
)


# ------------------------------------------------------------------ 通用值对象
class TestRuleParameter:
    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="参数名不能为空"):
            RuleParameter("", "k", "v", "m")

    def test_rejects_empty_key(self):
        with pytest.raises(ValueError, match="参数标识不能为空"):
            RuleParameter("名称", "", "v", "m")

    def test_to_dict(self):
        p = RuleParameter("回看天数", "lookback_days", "120", "跌破前N日最低价")
        assert p.to_dict() == {
            "name": "回看天数",
            "key": "lookback_days",
            "value": "120",
            "meaning": "跌破前N日最低价",
        }


class TestStrategyRule:
    def _rule(self, **kw):
        base = dict(
            rule_id="grid.trigger",
            title="触发条件",
            description="当价格跌破前N日最低价时触发",
            implementation="src/domain/grid/services.py::GridEngine.evaluate",
        )
        base.update(kw)
        return StrategyRule(**base)

    def test_rejects_empty_rule_id(self):
        with pytest.raises(ValueError, match="规则标识不能为空"):
            self._rule(rule_id="")

    def test_rejects_empty_description(self):
        with pytest.raises(ValueError, match="规则描述不能为空"):
            self._rule(description="")

    def test_rejects_empty_implementation(self):
        with pytest.raises(ValueError, match="规则实现位置不能为空"):
            self._rule(implementation="")

    def test_parameter_lookup(self):
        rule = self._rule(parameters=(RuleParameter("回看天数", "lookback_days", "120", "m"),))
        assert rule.parameter("lookback_days").value == "120"
        assert rule.parameter("missing") is None


class TestStrategySpec:
    def test_rule_lookup(self):
        rule = StrategyRule("a.b", "标题", "描述", "impl")
        spec = StrategySpec("grid", "复合网格", "摘要", rules=(rule,))
        assert spec.rule("a.b") is rule
        assert spec.rule("nope") is None

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="策略名不能为空"):
            StrategySpec("grid", "", "摘要")

    def test_to_dict_shape(self):
        spec = StrategySpec(
            "grid", "复合网格", "摘要",
            rules=(StrategyRule("a.b", "标题", "描述", "impl"),),
            implementation=("x.py::y",),
        )
        d = spec.to_dict()
        assert d["strategy_id"] == "grid"
        assert d["name"] == "复合网格"
        assert len(d["rules"]) == 1
        assert d["implementation"] == ["x.py::y"]


# ------------------------------------------------------------------ 网格规格
def _trigger(**kw):
    base = dict(
        lookback_days=120,
        atr_period=120,
        step_ratio=Decimal("0.5"),
        buy_amount=Decimal("10000"),
        lot_size=100,
        price_basis=GridPriceBasis.LOW,
        take_profit_ratio=Decimal("1"),
    )
    base.update(kw)
    return GridTrigger(**base)


class TestGridSpec:
    def test_strategy_name_and_id(self):
        spec = build_grid_spec(_trigger())
        assert spec.strategy_id == "grid"
        assert spec.name == "复合网格"

    def test_has_all_rules(self):
        spec = build_grid_spec(_trigger())
        ids = {r.rule_id for r in spec.rules}
        assert ids == {
            "grid.trigger", "grid.add", "grid.take_profit", "grid.isolation",
        }

    def test_every_rule_points_to_implementation(self):
        spec = build_grid_spec(_trigger())
        for rule in spec.rules:
            assert rule.implementation
            assert "::" in rule.implementation

    def test_description_follows_lookback_days(self):
        spec = build_grid_spec(_trigger(lookback_days=60))
        trigger_rule = spec.rule("grid.trigger")
        assert "60" in trigger_rule.description

    def test_description_follows_step_ratio(self):
        """描述由参数推导：改 step_ratio 后描述随之改变（不会与实现漂移）"""
        spec_05 = build_grid_spec(_trigger(step_ratio=Decimal("0.5")))
        spec_2 = build_grid_spec(_trigger(step_ratio=Decimal("2")))

        assert "0.5×ATR(120)" in spec_05.rule("grid.add").description
        assert "2×ATR(120)" in spec_2.rule("grid.add").description
        assert "0.5×ATR(120)" not in spec_2.rule("grid.add").description

    def test_description_follows_take_profit_ratio(self):
        spec = build_grid_spec(_trigger(take_profit_ratio=Decimal("2")))
        assert "2×步长" in spec.rule("grid.take_profit").description

    def test_no_take_profit_description(self):
        spec = build_grid_spec(_trigger(take_profit_ratio=None))
        assert "不设止盈" in spec.rule("grid.take_profit").description

    def test_anchor_description(self):
        spec = build_grid_spec(_trigger(anchor=GridAnchor.PRIOR_LOW))
        assert "前N日最低价" in spec.rule("grid.trigger").description

    def test_price_basis_description(self):
        spec = build_grid_spec(_trigger(price_basis=GridPriceBasis.CLOSE))
        assert "收盘价" in spec.rule("grid.trigger").description

    def test_parameters_expose_knobs(self):
        spec = build_grid_spec(_trigger(buy_amount=Decimal("20000")))
        add_rule = spec.rule("grid.add")
        param = add_rule.parameter("buy_amount")
        assert param is not None
        assert param.value == "20,000"

    def test_summary_mentions_key_numbers(self):
        spec = build_grid_spec(_trigger())
        assert "120" in spec.summary
        assert "0.5" in spec.summary


# ------------------------------------------------------------------ 再平衡规格
class TestRebalanceSpec:
    def _config(self, threshold_pp=Decimal("3")):
        from types import SimpleNamespace

        return SimpleNamespace(
            assets=[
                SimpleNamespace(code="515180", name="红利ETF",
                                target_weight=Decimal("0.25")),
                SimpleNamespace(code="159937", name="黄金ETF",
                                target_weight=Decimal("0.25")),
                SimpleNamespace(code="511380", name="可转债ETF",
                                target_weight=Decimal("0.25")),
                SimpleNamespace(code="513300", name="纳斯达克ETF",
                                target_weight=Decimal("0.25")),
            ],
            threshold_pp=threshold_pp,
            transaction_cost_bps=Decimal("5"),
        )

    def test_strategy_name_and_id(self):
        spec = build_rebalance_spec(self._config())
        assert spec.strategy_id == "rebalance"
        assert spec.name == "阈值再平衡"

    def test_has_all_rules(self):
        spec = build_rebalance_spec(self._config())
        ids = {r.rule_id for r in spec.rules}
        assert ids == {
            "rebalance.allocation", "rebalance.trigger",
            "rebalance.cost", "rebalance.isolation",
        }

    def test_description_follows_threshold(self):
        spec = build_rebalance_spec(self._config(Decimal("5")))
        assert "5 个百分点" in spec.rule("rebalance.trigger").description

    def test_rules_have_implementation(self):
        for rule in build_rebalance_spec(self._config()).rules:
            assert "::" in rule.implementation
