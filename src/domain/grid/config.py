"""网格规则的配置 —— 一个值, 到处传 (Phase 5)。

以前网格的七个参数散在**三个模块**的模块级常量里:

    trigger_policy.py   GRID_STEP_PCT / GRID_TP_PCT / GRID_TP_BASIS
    layer_sizing.py     GRID_BASE_YUAN / GRID_FLOATING_FRACTION /
                        GRID_FLOATING_WINDOW
    grid_position.py    GRID_LOOKBACK

三个模块各自 `import os` 读环境变量 —— 改一处要记得改另一处, 而且
「这套策略用哪组参数」这件事**没法当值传** (D8: 「无法做多策略并存、
测试需改环境」)。

`GridConfig` 把七个参数收成一个**不可变值**: 构造好, 到处传, 一个进程里
可以并存多套 (「生产用 3%」与「回测用 2%」各走各的, 互不影响)。

⚠️ **本模块不 import 任何其他层**, 只依赖 stdlib —— 与 `grid_position.py`
同样的理由 (`scripts/apply_grid.py` 在一个**不装 pandas/akshare** 的
GitHub Actions job 里 import 这条链路)。

⚠️ **舍入点纪律**: 这里的每个数字都是**参数**, 不是**算出来的值** ——
没有 `round()` 的地方, 也就没有值对象 (§11.1.2)。

⚠️ **本模块是整个代码库唯一读网格环境变量的地方** —— `trigger_policy.py` /
`layer_sizing.py` / `grid_position.py` 的模块级常量全部是这里的别名,
读一次, 到处用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class GridConfig:
    """网格规则的**全部**参数 —— 一个不可变值。

    Attributes:
        lookback:          滚动窗口的深度 (K 线根数) —— 「距开仓」的唯一依据
        step_pct:          加仓触发百分比 (比最近一笔买价低 step_pct% 加一层)
        tp_pct:            回本触发百分比 (比基准价高 tp_pct% 出场)
        tp_basis:          回本基准 —— "first_buy" (第一笔买价) 或 "avg_cost"
        base_yuan:         每层的固定底数 (¥)
        floating_fraction: 浮动部分的比例 (池子超出最近最低的那部分)
        floating_window:   「最近最低」的回看窗口 (交易日)
    """

    lookback: int = 120
    step_pct: float = 3.0
    tp_pct: float = 3.0
    tp_basis: str = "first_buy"
    base_yuan: float = 10000.0
    floating_fraction: float = 0.05
    floating_window: int = 756

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "GridConfig":
        """从环境变量构造 —— **唯一**读网格环境变量的地方。

        Args:
            env: 环境变量映射; `None` = 用 `os.environ`。

        ⚠️ `GRID_STEP_PCT` / `GRID_TP_PCT` / `GRID_LOOKBACK` 以前是**写死**
        的 (只有 `GRID_TP_BASIS` / `GRID_BASE_YUAN` / `GRID_FLOATING_*`
        读环境变量)。现在三个也能用环境变量覆盖 —— 这是**新增的能力**,
        不是行为变更: 仓库里没有任何地方设置这三个变量 (CI 的 `daily.yml` /
        `apply-grid.yml` 都没设), 所以实际取值完全一样。
        """
        e = os.environ if env is None else env
        d = cls()
        return cls(
            lookback=int(e.get("GRID_LOOKBACK", d.lookback)),
            step_pct=float(e.get("GRID_STEP_PCT", d.step_pct)),
            tp_pct=float(e.get("GRID_TP_PCT", d.tp_pct)),
            tp_basis=e.get("GRID_TP_BASIS", d.tp_basis),
            base_yuan=float(e.get("GRID_BASE_YUAN", d.base_yuan)),
            floating_fraction=float(e.get("GRID_FLOATING_FRACTION",
                                         d.floating_fraction)),
            floating_window=int(e.get("GRID_FLOATING_WINDOW",
                                      d.floating_window)),
        )


#: 默认网格规则 —— 从环境变量读**一次** (import 时)。
#: 各模块的模块级常量都是它的别名, 既有 import 不断。
DEFAULT_GRID_CONFIG: GridConfig = GridConfig.from_env()


__all__ = ["GridConfig", "DEFAULT_GRID_CONFIG"]
