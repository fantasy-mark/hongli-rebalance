"""
策略规格（领域层）

表达「策略名 —— 规则描述 —— 规则实现」三者的对应关系。

核心关系:
    StrategySpec (策略名)
        └── StrategyRule (规则)
              ├── description   规则描述（人读）
              ├── parameters    规则参数（值）
              ├── formula       规则公式（可选）
              └── implementation 规则实现（代码位置）

设计约束:
- 规则描述必须由**参数**推导，而非硬编码文案，
  否则描述会与实现漂移（改了参数忘了改文案）。
  各策略的规格由 `src/domain/<strategy>/spec.py` 的 Builder 负责。
- 规则实现用「文件路径::符号」定位，可被工具直接跳转到源码。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Tuple


@dataclass(frozen=True)
class RuleParameter:
    """
    规则参数（值对象）

    参数是规则的「可调旋钮」，描述与实现都应引用它。
    """

    name: str            # 参数名（中文）
    key: str             # 参数标识（对应代码字段名）
    value: str           # 参数值（展示用字符串）
    meaning: str         # 参数含义

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("参数名不能为空")
        if not self.key:
            raise ValueError("参数标识不能为空")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "key": self.key,
            "value": self.value,
            "meaning": self.meaning,
        }


@dataclass(frozen=True)
class StrategyRule:
    """
    策略规则（值对象）

    一条规则 = 描述 + 参数 + 公式 + 实现位置。
    """

    rule_id: str             # 规则标识，如 "grid.trigger"
    title: str               # 规则标题
    description: str         # 规则描述（人读，由参数推导）
    implementation: str      # 规则实现位置（文件::符号）
    formula: str = ""        # 规则公式（可选）
    parameters: Tuple[RuleParameter, ...] = ()

    def __post_init__(self) -> None:
        if not self.rule_id:
            raise ValueError("规则标识不能为空")
        if not self.title:
            raise ValueError("规则标题不能为空")
        if not self.description:
            raise ValueError("规则描述不能为空")
        if not self.implementation:
            raise ValueError("规则实现位置不能为空")

    def parameter(self, key: str) -> RuleParameter | None:
        for p in self.parameters:
            if p.key == key:
                return p
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "formula": self.formula,
            "implementation": self.implementation,
            "parameters": [p.to_dict() for p in self.parameters],
        }


@dataclass(frozen=True)
class StrategySpec:
    """
    策略规格（聚合根）

    一个策略 = 策略名 + 规则集合 + 实现清单。
    规则集合中的每条规则都指向自己的实现，
    从而形成「策略名 → 规则描述 → 规则实现」的完整链路。
    """

    strategy_id: str                 # 策略标识，如 'grid' / 'rebalance'
    name: str                        # 策略名
    summary: str                     # 一句话说明
    rules: Tuple[StrategyRule, ...] = ()
    implementation: Tuple[str, ...] = ()   # 策略整体实现位置清单

    def __post_init__(self) -> None:
        if not self.strategy_id:
            raise ValueError("策略标识不能为空")
        if not self.name:
            raise ValueError("策略名不能为空")

    def rule(self, rule_id: str) -> StrategyRule | None:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "summary": self.summary,
            "rules": [r.to_dict() for r in self.rules],
            "implementation": list(self.implementation),
        }


def build_spec(
    strategy_id: str,
    name: str,
    summary: str,
    rules: Iterable[StrategyRule],
    implementation: Iterable[str] = (),
) -> StrategySpec:
    """构造策略规格（便于 Builder 使用）"""
    return StrategySpec(
        strategy_id=strategy_id,
        name=name,
        summary=summary,
        rules=tuple(rules),
        implementation=tuple(implementation),
    )
