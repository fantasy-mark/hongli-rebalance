"""成本出场策略 —— `CostOnlyExitPolicy` (BC-C)。

网格的出场规则**只有一条**: **只卖出成本金额**。

    卖出份额 = 本金 / 价格
    卖出金额 = 本金            (恰好把本金取回来)
    归并份额 = 原持仓 − 卖出份额   (余量并入再平衡组合)

于是每个周期的**已实现盈亏恒为 0** —— 止盈不兑现浮盈, 只是把它永久化并
交给组合。网格仍然决定加仓的层数和时点, 但没有独立的退出机制。

⚠️ **本模块的中间量必须保持全精度** —— 它们是**比例**, 不是金额:

  - `shares_sold` / `merged_shares` 四舍五入会破坏「买入 − 卖出 == 归并」
    这个恒等式, 也会让 `cum_realized` 的数字变;
  - `pnl` 在亏本出场分支里是 `原持仓 × 价格 − 本金`, 取整会改掉账。

所以这里**不套** `ShareCount` / `Money` 值对象 —— 值对象的价值在于
"在输出边界上取整", 而这些量要一路保持精确直到最后一刻。

`stdlib-only` —— 与 `grid_position.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这条链路。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExitAtCost:
    """一次「只卖成本额」出场的**结果**。

    Attributes:
        shares_sold:   实际卖出的份额 (= 本金 / 价格, 取回全部本金)
        proceeds:      卖出所得 (正常路径 == 本金)
        merged_shares: 归并到再平衡持仓的份额 (网格不再持有)
        pnl:           已实现盈亏; 正常路径恒为 0, 亏本出场时为负
    """

    shares_sold: float
    proceeds: float
    merged_shares: float
    pnl: float

    def to_dict(self) -> dict:
        """The legacy dict shape (`split_take_profit` 兼容别名的返回形状)。"""
        return {
            "shares_sold": self.shares_sold,
            "proceeds": self.proceeds,
            "merged_shares": self.merged_shares,
            "pnl": self.pnl,
        }


def cost_only_exit(total_shares: float, total_invested: float,
                   price: float) -> ExitAtCost:
    """按「只卖成本金额」规则拆分一次止盈。

    当 `price` 低于持仓均价时要卖的份额会超过持仓, 归并出「负的份额」没有
    意义, 此时退化为全部卖出并认亏。引擎**不再自动止盈**, 所以这个分支只能
    由**手动** tp 触发 —— 用户在低于成本处出场时会真的认亏。
    """
    if not price > 0:
        raise ValueError(f"price must be > 0, got {price}")
    if total_shares <= 0:
        return ExitAtCost(shares_sold=0.0, proceeds=0.0,
                          merged_shares=0.0, pnl=0.0)
    want = total_invested / price
    if want >= total_shares:
        # 亏本出场 (只有手动路径可能): 卖光并认亏
        proceeds = total_shares * price
        return ExitAtCost(shares_sold=total_shares, proceeds=proceeds,
                          merged_shares=0.0, pnl=proceeds - total_invested)
    return ExitAtCost(shares_sold=want, proceeds=total_invested,
                      merged_shares=total_shares - want, pnl=0.0)


__all__ = ["ExitAtCost", "cost_only_exit"]
