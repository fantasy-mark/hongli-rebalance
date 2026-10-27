"""层额规则 —— `LayerSizingPolicy` (BC-C)。

网格每一层买多少钱:

    layer_yuan = GRID_BASE_YUAN
                 + max(pool_value - recent_min, 0) * GRID_FLOATING_FRACTION

即: 固定 ¥10,000 打底, 再加上「组合池子比最近 `GRID_FLOATING_WINDOW` 个
交易日的最低还多出多少」的 5% —— 池子越大, 每层买得越多。

⚠️ **返回未取整的 float** —— 层额要除以价格算份额 (`amount / price`),
提前取整会改掉份额。它只在**输出边界**上被包成 `LayerSize`
(见 `advisor.GridSuggestion.suggest_amount` 与快照里的 `layer_yuan`)。
这跟 `shared/money.py` 的舍入点纪律是同一条。

⚠️ **回测口径不同**: `simulation/grid_simulator.py` 每层**固定** ¥10,000
(或者由调用方给一个回调) —— 那是**另一种场景**的口径, 不是本规则的
重复实现。`docs/domain_model.md` §9.2 ① 列的就是这条防腐层。

**stdlib-only** —— 与 `grid_position.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这条链路。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: 每层的固定底数 (¥)
GRID_BASE_YUAN: float = float(os.environ.get("GRID_BASE_YUAN", "10000"))

#: 浮动部分的比例 —— 池子超出最近最低的部分, 按这个比例加码
GRID_FLOATING_FRACTION: float = float(os.environ.get("GRID_FLOATING_FRACTION",
                                                      "0.05"))

#: 「最近最低」的回看窗口 (交易日)
GRID_FLOATING_WINDOW: int = int(os.environ.get("GRID_FLOATING_WINDOW", "756"))


@dataclass(frozen=True)
class LayerSizingPolicy:
    """「每层买多少」的**唯一**定义。纯函数 —— 不改任何状态。

    以前这段逻辑是 `grid_position.layer_yuan()` 里的三行, 依赖模块级
    常量 —— 没法做多策略并存, 测试也只能改环境变量。现在参数是显式的。

    Attributes:
        base_yuan:         每层的固定底数 (¥)
        floating_fraction: 浮动部分的比例 (池子超出最近最低的部分)
    """

    base_yuan: float = GRID_BASE_YUAN
    floating_fraction: float = GRID_FLOATING_FRACTION

    def layer_yuan(self, pool_value: float, recent_min: float) -> float:
        """今日的单层买入金额 (¥)。**纯函数**。

        Args:
            pool_value: 当前 3pp 组合总市值 (¥)
            recent_min: 过去 `GRID_FLOATING_WINDOW` 个交易日的池子最低值 (¥)

        ⚠️ 返回**未取整**的 float —— 提前取整会改掉份额。
        """
        floating = max(pool_value - recent_min, 0.0)
        return self.base_yuan + floating * self.floating_fraction


def layer_yuan(pool_value: float, recent_min: float) -> float:
    """便捷入口 —— 用**模块级常量**建一个默认策略。

    ⚠️ 常量在**调用时**读取 (不是在 import 时冻结), 这样改了
    `GRID_BASE_YUAN` 之后立即生效 —— 与以前模块级函数的行为一致。
    """
    return LayerSizingPolicy(
        base_yuan=GRID_BASE_YUAN,
        floating_fraction=GRID_FLOATING_FRACTION,
    ).layer_yuan(pool_value, recent_min)


__all__ = [
    "GRID_BASE_YUAN", "GRID_FLOATING_FRACTION", "GRID_FLOATING_WINDOW",
    "LayerSizingPolicy", "layer_yuan",
]
