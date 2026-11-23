"""
资金配置（策略模块 · 回测与实盘账本共用）

**单一真相源**：各资金池的初始资金只在这里定义一次，
站点回测（`src/chart/site.py`）与实盘账本（`src/strategy/ledger`）
都从这里取，避免同一个数字散落在多处、各改各的。

| 池 | 含义 | 约束 |
|---|---|---|
| `REAL` | 真实现金池 —— 实盘账户的**总金额** | 建仓/加仓不得超过剩余现金（余额不足即拒） |
| `GRID` | 网格虚拟预算池 —— 网格策略的记账额度 | 同样按预算校验，但**不代表真实现金** |

两个池是**平行的账本视图**，不是同一笔钱：网格预算是网格策略自己
记账用的额度，与真实现金互不挤占。
"""

from decimal import Decimal
from typing import Dict

from src.strategy.ledger.trade import CashFlowType

# 真实现金池：实盘账户总金额（用户实盘一共就这么多）
REAL_INITIAL_CAPITAL = Decimal("200000")

# 网格虚拟预算池：网格策略的记账额度（虚拟，不代表真实现金）
GRID_INITIAL_CAPITAL = Decimal("200000")

# 各池默认初始资金
DEFAULT_INITIAL_CAPITALS: Dict[CashFlowType, Decimal] = {
    CashFlowType.REAL: REAL_INITIAL_CAPITAL,
    CashFlowType.GRID: GRID_INITIAL_CAPITAL,
}
