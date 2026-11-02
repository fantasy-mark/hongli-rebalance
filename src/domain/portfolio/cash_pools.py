"""资金管理表的两池分解 —— 「合计」的口径在这里定死 (D4)。

⚠️ **这块存在的唯一理由**: 页面顶部 badge 的「总市值」与「资金管理」表的
「合计市值」以前**不是同一个口径**, 用户看着自相矛盾:

    badge 总市值   = etf_value + cash_rebalance + cash_grid
    表合计市值     = etf_value + 网格的**虚拟资金占用**   ← 多出来的那一块

网格那一块是**虚拟**的 (网格是虚拟策略, 那不是用户的钱), badge 不算它 ——
对。但表把它算进了「合计」, 于是两张表说的不是同一件事。

本模块把规则写死成**代码**:

    合计市值 = 只含 3pp再平衡 的 ETF 持仓市值 (网格的虚拟占用**不计入**)
    合计余额 = **真实现金** (网格的虚拟现金**不计入**)
    ⇒ 合计市值 + 合计余额 == 顶部 badge 的「总市值」  ← 恒等式

⚠️ 网格那一行的「市值 / 余额」**没有被藏起来** —— 它还在表里, 只是被标成
「虚拟」并且不参与合计。**虚拟不等于该被藏起来, 只等于必须被标出来**
(D3: 领域侧标类型; D4: 呈现侧标「虚拟」)。

⚠️ **规则改过** (2026-11-02): `grid_budget` (虚拟预算) **不再计入 `total`**
—— 3pp 的资本基数只算真钱 (`etf_value + cash_rebalance`)。虚拟预算进分母
= 稀释每个真实持仓的权重 = 制造假的「偏离」, 而且回正方案会要求买入根本
不存在的那笔钱 (2026-11-02: 虚拟 ¥15,000 把黄金的偏离从 1.09pp 顶到
3.01pp, 方案要买 ¥15,328 而真实现金只有 ¥328)。

**stdlib-only** —— 与 `domain/shared/money.py` 同样的理由。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class CashPoolBreakdown:
    """两个资金池的「市值 / 余额」分解 —— 呈现层直接用这个, 不自己算。

    四个格子:
      - `rebalance_market`  3pp再平衡 的 ETF 持仓市值 (含网格归并的份额)
      - `rebalance_balance` 3pp再平衡 的现金余额 (**真钱**)
      - `grid_market`       网格的**虚拟**资金占用 (周期内的层, 当前价估值)
      - `grid_balance`      网格的**虚拟**现金余额 (`GridBudget`)

    ⚠️ `grid_market` **不计入** `total_market` —— 它是网格自己的账, 不是
    用户的钱。规则见模块 docstring。
    """

    rebalance_market: float = 0.0
    rebalance_balance: float = 0.0
    grid_market: float = 0.0
    grid_balance: float = 0.0

    # -- construction -----------------------------------------------------
    @classmethod
    def from_portfolio(cls, portfolio: Any, *,
                       grid_market: float = 0.0) -> "CashPoolBreakdown":
        """From the portfolio aggregate + the grid's virtual occupancy.

        `portfolio` 是 `Portfolio` —— 这里只取它的**只读**属性, 所以不必
        import 它 (避免模块环)。
        """
        return cls(
            rebalance_market=float(portfolio.etf_value),
            rebalance_balance=float(portfolio.cash_rebalance),
            grid_market=float(grid_market),
            grid_balance=float(portfolio.grid_budget),
        )

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CashPoolBreakdown":
        """From a daily record (`data/history.json` 的形状)。

        ⚠️ 记录的 JSON 键**不改名** —— `cash_grid` 仍然是 `cash_grid`
        (committed artifact, D3)。这里读的正是那些键。
        """
        return cls(
            rebalance_market=float(record.get("etf_value", 0.0) or 0.0),
            rebalance_balance=float(record.get("cash_rebalance", 0.0) or 0.0),
            grid_market=grid_occupancy(record.get("grid_signals") or []),
            grid_balance=float(record.get("cash_grid", 0.0) or 0.0),
        )

    # -- the numbers the page shows --------------------------------------
    @property
    def total_market(self) -> float:
        """合计市值 —— **不含**网格的虚拟资金占用。

        ⚠️ 这一条就是 D4 的全部: 顶部 badge 的「总市值」= `etf_value +
        cash_rebalance`, **不含**网格的虚拟占用**与虚拟现金**。所以「合计
        市值」也必须不含, 两者才能对上 (见 `total`)。
        """
        return self.rebalance_market

    @property
    def total_balance(self) -> float:
        """合计余额 —— **真实现金** (3pp 再平衡那一池的余额)。

        ⚠️ 网格的虚拟余额**不计入** —— badge 的「总市值」就是
        `etf_value + cash_rebalance` (虚拟预算不进 3pp 的资本基数, 见
        `Portfolio.total`)。所以「合计」这一行里**每一分钱都是真钱** ——
        虚拟的钱单列在「复合网格」那一行, 标着「虚拟」。

        ⚠️ 加法的**顺序**是 `market + reb` —— float 加法不满足结合律, 而
        `Portfolio.total` 算的就是 `etf + cr`。两边顺序一致, 数字才逐位相同。
        """
        return self.rebalance_balance

    @property
    def total(self) -> float:
        """合计市值 + 合计余额 —— **恒等于**顶部 badge 的「总市值」。

        ⚠️ 加法的**顺序**是 `market + reb` —— float 加法不满足结合律, 而
        `Portfolio.total` 算的就是 `etf + cr`。两边顺序一致, 数字才逐位相同。
        """
        return self.total_market + self.total_balance


def grid_occupancy(grid_signals: Sequence[Mapping[str, Any]]) -> float:
    """网格的**虚拟资金占用**合计 —— 周期内的层, 按当前价估值。

    出场后本金收回, 占用回落 (见 `grid.asset_snapshot`)。归并到再平衡的
    份额**不在这里** —— 它们已经算进 ETF 持仓市值了, 重复计就是双份。
    """
    total = 0.0
    for s in grid_signals or ():
        try:
            value = float(s.get("current_value", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            total += value
    return total


def pools_of(record: Mapping[str, Any]) -> CashPoolBreakdown:
    """记录 → 两池分解 (便捷入口)。"""
    return CashPoolBreakdown.from_record(record)


__all__ = ["CashPoolBreakdown", "grid_occupancy", "pools_of"]
