"""建议层 —— `GridAdvisor` → `GridSuggestion` (BC-C)。

⚠️ **建议 ≠ 成交**: 本模块产出的 `GridSuggestion` 只回答「今天该做什么」,
**不代表已经成交**。真正的成交是 `grid_op.GridOp` —— 两个**不同的类型**,
「引擎永远不下单」这条不变量因此由类型保证, 而不是靠注释和 code review。

引擎降级为只读建议器 (2026-10-25) 之后, 这里的输出是**唯一**的引擎产物:
它不改 `entries` / `in_cycle` / `cum_realized` / `merged_shares`, 也不写
`ops`。真正的开仓/加仓/回本只能来自用户在「交易记录 → 复合网格」提交的 op
(scripts/apply_grid.py → `grid_position.manual_op`)。

⚠️ **取整只发生在输出边界**: `TriggerDistance` 里的 `pct` / `price` 是原始
精度, `GridSuggestion` 构造时才套 `DeviationPP` / `Price` / `ShareCount` /
`LayerSize`。这跟 `shared/money.py` 的舍入点纪律是同一条。

**stdlib-only** —— 与 `grid_position.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这条链路。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..shared.money import DeviationPP, LayerSize, Price, ShareCount
from .trigger_policy import (
    GRID_STEP_PCT,
    GRID_TP_BASIS,
    GRID_TP_PCT,
    TriggerDistance,
    TriggerLevel,
    TriggerPolicy,
)


def _opt_dev(d: TriggerDistance | None) -> DeviationPP | None:
    """距离 → 百分点值对象。触发位不存在时是 `None` (不是 0)。"""
    if d is None or d.pct is None:
        return None
    return DeviationPP(d.pct)


@dataclass(frozen=True)
class GridSuggestion:
    """引擎给出的**建议** —— 还没成交。

    Attributes:
        level:            建议提交哪种 op (`None` = 今天什么都不用做)
        nearest:          **最近**的那个触发位 (已触发的那个, 或两个都没触发时更近的那个)
        open_distance:    距「开仓触发位」的距离 (`None` = 窗口还没灌满)
        add_distance:     距「加仓触发位」的距离 (`None` = 不在周期内)
        tp_distance:      距「回本触发位」的距离 (`None` = 不在周期内)
        suggest_price:    建议价 (= 当日 K 线最低价)
        suggest_shares:   建议买多少份 (只有 open/add 有值, 否则 0)
        suggest_amount:   建议买多少钱 (只有 open/add 有值, 否则 0)

    ⚠️ 这**不是**成交。成交是 `grid_op.GridOp`。
    """

    level: TriggerLevel | None
    nearest: TriggerDistance
    open_distance: TriggerDistance | None
    add_distance: TriggerDistance | None
    tp_distance: TriggerDistance | None
    suggest_price: Price
    suggest_shares: ShareCount
    suggest_amount: LayerSize

    def to_dict(self) -> dict:
        """老 `grid_position.advisory()` 的 dict 形状 —— 一字不差。

        过渡期用: `history.json` 里 `grid_signals` 的形状、`app.js` 读的
        字段名都靠它。⚠️ `signal` / `trigger_kind` 必须是**裸字符串** ——
        f-string 会把 Enum 显示成 `TriggerLevel.OPEN`。
        """
        nearest = self.nearest
        pct = nearest.pct if nearest.pct is not None else 0.0
        price = nearest.price if nearest.price is not None else 0.0
        return {
            "signal": self.level.value if self.level is not None else None,
            "trigger_kind": nearest.level.value,
            "trigger_pct": DeviationPP(pct),
            "trigger_price": Price(price),
            "dist_open_pct": _opt_dev(self.open_distance),
            "dist_add_pct": _opt_dev(self.add_distance),
            "dist_tp_pct": _opt_dev(self.tp_distance),
            "suggest_price": self.suggest_price,
            "suggest_shares": self.suggest_shares,
            "suggest_amount": self.suggest_amount,
        }

    # -- 过渡期: 允许按 dict 的方式读 ---------------------------------------
    # 老代码拿 `advisory()` 的返回值是当 dict 用的 (`adv["suggest_shares"]`)。
    # 保留这个入口, 它们就不必跟着改。
    def __getitem__(self, key: str):
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()


class GridAdvisor:
    """把「状态 + 当日价」翻译成一条**建议**。纯函数 (不改 state)。

    真正的规则在 `trigger_policy.TriggerPolicy` —— 这里只负责**加一层
    建议语义** (建议买多少) 并把结果包成 `GridSuggestion`。
    """

    def __init__(self, policy: TriggerPolicy | None = None,
                 step_pct: float = GRID_STEP_PCT,
                 tp_pct: float = GRID_TP_PCT,
                 tp_basis: str = GRID_TP_BASIS) -> None:
        self.policy = policy or TriggerPolicy(step_pct=step_pct, tp_pct=tp_pct,
                                              tp_basis=tp_basis)

    def suggest(self, *, in_cycle: bool, entries: list[dict],
                window: list[float], price: float,
                layer_yuan_value: float) -> GridSuggestion:
        """算出今天的建议。**纯函数** —— 不改任何状态, 更不写 ops。

        Args:
            in_cycle:         当前是否处于一个进行中的周期
            entries:          周期内的买入层 [{date, price, shares}]
            window:           滚动窗口 (追加今天之前的)
            price:            今日价 (生产路径传**当日 K 线最低价**)
            layer_yuan_value: 今日的单层金额 (¥)
        """
        levels = self.policy.levels(in_cycle=in_cycle, entries=entries,
                                    window=window, price=price)
        sig = levels.signal
        amount = float(layer_yuan_value) if sig in (TriggerLevel.OPEN,
                                                    TriggerLevel.ADD) else 0.0
        shares = (amount / float(price)) if (amount and price > 0) else 0.0
        return GridSuggestion(
            level=sig,
            nearest=levels.nearest,
            open_distance=levels.open,
            add_distance=levels.add,
            tp_distance=levels.tp,
            suggest_price=Price(price),
            suggest_shares=ShareCount(shares),
            suggest_amount=LayerSize(amount),
        )


__all__ = ["GridAdvisor", "GridSuggestion"]
