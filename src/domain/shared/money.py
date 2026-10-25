"""数值值对象 —— Money / ShareCount / Price / Weight / DeviationPP / LayerSize。

`docs/domain_model.md` §6.2 建议用 `Decimal` 实现。**本模块刻意不用
`Decimal`**, 理由有两条, 都不是偏好问题:

1. **`data/*.json` 由 `app.js` 读**。`Decimal` 不是 JSON 原生类型 ——
   `json.dumps` 要么抛错, 要么靠 `default=str` 变成字符串 `"1.5"`,
   页面拿到字符串就没法算数了。
2. **改算术会改数字**。`Decimal` 的加减乘除自带精度规则; 一旦值对象参与
   运算, 舍入的**时机**就变了 —— 今天的 `round(x, 2)` 是在**算完之后**
   做的, 换成 `Decimal` 会变成"每一步都舍入", 账上的数字就全变了。

所以这里用 **`float` 子类**: 构造时按语义取整 (这是值对象的核心 ——
"份额就是 2 位小数"这条规则只写一遍), 但**不重写算术**。于是:

- `isinstance(x, float)` 依然成立, 现有代码一行都不用改;
- `json.dumps` 出来还是数字, `app.js` 不受影响;
- `x * y` 的结果与今天**逐位相同** (float 的乘法没有额外舍入);
- 想要"取整后的值"就显式构造一个值对象 —— 舍入点**由代码结构决定**,
  不再散落在 `round(x, 2)` 里。

⚠️ **舍入点纪律**: 值对象的构造 = 原来那一次 `round()`。在**没有**
`round()` 的地方构造值对象, 就是在**偷偷改数字**。本模块的每个类型都
在 docstring 里写明了「它替掉的是哪一次 round」。

⚠️ **全精度的例外**: `split_take_profit` 的中间量 (卖出份额 / 归并份额 /
盈亏) **必须保持全精度** —— 它们是比例, 四舍五入会破坏「买入 − 卖出 == 归并」
这个恒等式, 也会让 `cum_realized` 的数字变。所以那些量**不**用值对象。

**stdlib-only** —— 与 `identifiers.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这个模块。
"""
from __future__ import annotations


class _RoundedFloat(float):
    """所有数值值对象的基类 —— 构造时按 `DP` 取整, 算术**不**重写。

    取整用 Python 内置的 `round()` (银行家舍入), 与代码里原有的
    `round(x, 2)` **完全同一条路径** —— 所以替过去之后数字逐位不变。
    """

    #: 小数位数
    DP: int = 2
    #: 展示用的单位后缀 (仅供人读, 不参与运算)
    UNIT: str = ""

    def __new__(cls, value):
        try:
            raw = float(value)
        except (TypeError, ValueError) as ex:
            raise ValueError(
                f"{cls.__name__} must be a number, got {value!r}") from ex
        return super().__new__(cls, round(raw, cls.DP))


class Money(_RoundedFloat):
    """人民币金额, 2 位小数。

    替掉: `ledger.apply_trade` 的现金池取整、`Position.value` /
    `merged_value`、再平衡计划的 `current_value` / `target_value` /
    `delta_value`、网格快照的 `total_invested` / `current_value` /
    `unrealized_pnl`。

    ⚠️ 不是所有「金额」都归它管 —— 见 `LayerSize`。
    """

    DP = 2
    UNIT = "¥"


class ShareCount(_RoundedFloat):
    """基金份额, 2 位小数。

    替掉: `ledger.apply_trade` 的持仓份额、`Position.real_shares` /
    `simulated_shares` / `total_shares`、网格快照的 `merged_shares`、
    `advisory` 的 `suggest_shares`。

    ⚠️ 用户的**手动**下单份额 (`manual_op` 写进 `entries` 的那个) 保持
    全精度 —— 它是成本计算的基础, 取整会改掉 `total_invested`, 进而
    改掉出场时的归并份额。
    """

    DP = 2
    UNIT = "份"


class Price(_RoundedFloat):
    """证券价格 (元/份), 4 位小数。

    替掉: `valuation.value_positions` 的 `price`、持仓快照的 `price` /
    `avg_cost`、`advisory` 的 `trigger_price` / `suggest_price`、
    回测 op 行的 `price`、行情适配器的 `price` / `iopv`。
    """

    DP = 4
    UNIT = ""


class Weight(_RoundedFloat):
    """组合权重 (0~1), 4 位小数。

    ⚠️ **只在展示边界上构造**。`rebalance_policy.evaluate` 内部算触发
    判定时用的必须是**未取整**的权重 —— 权重取整到 4 位会移动触发边界,
    让「差 0.001pp 不触发」变成「触发」。所以 `evaluate` 返回的权重是
    裸 float, 只有写进 `history.json` 时才包成 `Weight`。
    """

    DP = 4
    UNIT = ""


class DeviationPP(_RoundedFloat):
    """百分点偏离 (percentage point), 2 位小数。

    替掉: `advisory` 的 `trigger_pct` 与三个 `dist_*_pct`、
    `rebalance_policy.max_deviation_pp`。

    ⚠️ 语义是**百分点** (权重差 × 100), 不是百分比收益率 ——
    `asset_snapshot` 的 `return_pct` 是收益率, 不用这个类型。
    """

    DP = 2
    UNIT = "pp"


class LayerSize(_RoundedFloat):
    """网格单层买入金额 (¥), 2 位小数。

    替掉: 再平衡快照里 `grid_signals[].layer_yuan`、
    `advisory` 的 `suggest_amount`。

    ⚠️ `layer_yuan()` (算层额的纯函数) 返回**未取整**的 float ——
    层额要除以价格算份额, 提前取整会改掉份额。它只在**输出边界**上
    被包成 `LayerSize`。
    """

    DP = 2
    UNIT = "¥"


__all__ = [
    "Money",
    "ShareCount",
    "Price",
    "Weight",
    "DeviationPP",
    "LayerSize",
]
