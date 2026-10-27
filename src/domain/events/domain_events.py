"""领域事件 —— 状态变更的**一等公民** (Phase 5b)。

⚠️ **这不是事件溯源** (docs/domain_model.md §11.3 明确「不要做」):
事实源仍然是 JSON 文件 —— `data/trades.json` (真实成交)、
`data/grid_state.json` (网格状态)、`data/history.json` (每日快照);
Git 提交历史就是最好的事件溯源。引入数据库会是净损失。

事件在这里的角色是**运行时的通知**: 让「谁在什么时候因为什么改变了状态」
变成**可以断言的东西**, 而不是散落在 dict 形状里的巧合。

九个事件 (docs/domain_model.md §8):

    BC-A 账本    TradeRecorded / CashTransferred
    BC-B 组合    PositionValued / RebalanceTriggered
    BC-C 网格    GridCycleOpened / GridLayerAdded / GridCycleClosed /
                 MergedSharesTransferred
    BC-G 编排    DailySnapshotProduced

`MergedSharesTransferred` 是**唯一的跨上下文事件** (BC-C → BC-B): 网格只
**宣告**「我这边累计移交了 N 份零成本模拟份额」, 组合自己决定怎么吸收
(记 `Provenance.SIMULATED_GRID`)。这正是 §10 的不变量 I9 ——
「归并份额不写 trades.json」—— 现在由**类型**保证: 账本上下文里根本没有
能写模拟份额的入口, 组合只从事件里读。

**stdlib-only** —— 领域层不引任何第三方依赖 (与 `domain/grid/*` 同一条规矩)。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Iterator, Optional

from ..shared.provenance import Provenance

#: 现金池的名字 —— `CashTransferred` 的 from/to 用它们。
#: `external` = 用户的外部账户 (钱包), 两个池子是账本里的两笔虚拟余额。
POOL_EXTERNAL = "external"
POOL_REBALANCE = "rebalance"
POOL_GRID = "grid"


@dataclass(frozen=True)
class DomainEvent:
    """所有领域事件的基类。

    **不可变** —— 事件一旦发出就不会再变 (它是「已经发生的事」的记录)。
    载荷是**具名字段**, 不是 dict —— 这样「这个事件带什么」由**类型**回答,
    而不是靠读文档。

    Attributes:
        date: 事件发生的日期 (ISO `yyyy-mm-dd`)。
    """

    date: str = ""

    @property
    def type(self) -> str:
        """事件类型名 —— 与 docs/domain_model.md §8 的表一一对应。"""
        return type(self).__name__

    def to_record(self) -> dict[str, Any]:
        """JSON 形状 (给日志 / 邮件 / 断言用)。"""
        rec: dict[str, Any] = {"type": self.type, "date": self.date}
        for f in fields(self):
            if f.name == "date":
                continue
            rec[f.name] = getattr(self, f.name)
        return rec

    def __str__(self) -> str:
        return f"{self.type}({self.date})"


# -----------------------------------------------------------------------------
# BC-A 账本
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeRecorded(DomainEvent):
    """一笔**真实成交**被记进账本 (`data/trades.json`)。

    只覆盖有成交语义的两侧 —— `buy` / `sell`。现金的进出是
    `CashTransferred`, 不是成交。
    """

    side: str = ""
    code: str = ""
    shares: float = 0.0
    price: float = 0.0
    amount: float = 0.0


@dataclass(frozen=True)
class CashTransferred(DomainEvent):
    """钱在两个池子之间移动 (或从外部进出)。

    Attributes:
        from_pool: 来源池 (`external` / `rebalance` / `grid`)
        to_pool:   去向池
        amount:    金额 (¥, 正数)
    """

    from_pool: str = ""
    to_pool: str = ""
    amount: float = 0.0


# -----------------------------------------------------------------------------
# BC-B 组合
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionValued(DomainEvent):
    """某个持仓在某个价上被重新估了值。

    Attributes:
        code:   资产代码
        name:   显示名
        price:  用的价格 (¥)
        shares: 份额
        value:  市值 (¥)
    """

    code: str = ""
    name: str = ""
    price: float = 0.0
    shares: float = 0.0
    value: float = 0.0


@dataclass(frozen=True)
class RebalanceTriggered(DomainEvent):
    """3pp 再平衡**触发了** —— 篮子偏离超过阈值。

    Attributes:
        deviating_asset: 偏离最大的那个资产的名字
        deviation_pp:    偏离多少个百分点
        target_weight:   目标权重 (1/n)
        plan:            回正方案 (每个资产一行)
    """

    deviating_asset: str = ""
    deviation_pp: float = 0.0
    target_weight: float = 0.0
    plan: list[dict] = field(default_factory=list)


# -----------------------------------------------------------------------------
# BC-C 网格
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class GridCycleOpened(DomainEvent):
    """网格**开了一个周期** (第一层买入)。"""

    asset: str = ""
    code: str = ""
    price: float = 0.0
    shares: float = 0.0
    layer_no: int = 1


@dataclass(frozen=True)
class GridLayerAdded(DomainEvent):
    """网格**加了一层** (金字塔的第 N 层买入)。"""

    asset: str = ""
    code: str = ""
    layer_no: int = 0
    price: float = 0.0
    shares: float = 0.0


@dataclass(frozen=True)
class GridCycleClosed(DomainEvent):
    """网格**周期收尾** (回本出场)。

    Attributes:
        exit_price:    出场价 (¥)
        sold_shares:   卖出的份额 (只卖成本额)
        merged_shares: 归并进再平衡组合的份额 (买 − 卖)
        realized_pnl:  已实现盈亏 (正常出场恒为 0 —— 见 `cost_only_exit`)
    """

    asset: str = ""
    code: str = ""
    exit_price: float = 0.0
    sold_shares: float = 0.0
    merged_shares: float = 0.0
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class MergedSharesTransferred(DomainEvent):
    """网格 → 组合: **模拟份额的移交声明** (唯一的跨上下文事件)。

    ⚠️ 载荷里的 `shares` 是网格**累计**移交的份额 (网格状态里的
    `merged_shares`), 不是增量 —— 组合每次重建时吸收的是**当前持有量**,
    所以同一资产有多条声明时, **最后一条**说了算。

    Attributes:
        asset:      资产显示名
        code:       资产代码
        shares:     累计移交的份额
        provenance: 恒为 `SIMULATED_GRID` —— 这批份额**never**进账本
    """

    asset: str = ""
    code: str = ""
    shares: float = 0.0
    provenance: str = Provenance.SIMULATED_GRID.value


# -----------------------------------------------------------------------------
# BC-G 编排
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class DailySnapshotProduced(DomainEvent):
    """一天的快照产出了。

    Attributes:
        total:    组合总市值 (¥)
        weights:  各资产权重
        triggered: 3pp 是否触发
        signals:   网格建议 (`open` / `add` / `tp` / None)
    """

    total: float = 0.0
    weights: list[float] = field(default_factory=list)
    triggered: bool = False
    signals: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# 收集器
# -----------------------------------------------------------------------------

@dataclass
class EventStream:
    """有序的事件列表 —— 一条运行里发生的所有领域事件。

    它是**可选**的: 每个会发事件的领域函数都接受一个 `events: EventStream
    | None = None` 参数 —— 传 `None` 就什么都不发 (老调用方一行不用改)。
    """

    events: list[DomainEvent] = field(default_factory=list)

    def record(self, event: DomainEvent) -> DomainEvent:
        """记下一个事件并返回它 (方便链式)。"""
        self.events.append(event)
        return event

    def extend(self, events: Iterable[DomainEvent]) -> None:
        """批量记下。"""
        self.events.extend(events)

    def of_type(self, event_type: type) -> list[DomainEvent]:
        """某一类的全部事件 (按发生顺序)。"""
        return [e for e in self.events if isinstance(e, event_type)]

    def counts(self) -> dict[str, int]:
        """每类事件各多少个 —— 用来断言「今天发生了什么」。"""
        out: dict[str, int] = {}
        for e in self.events:
            out[e.type] = out.get(e.type, 0) + 1
        return out

    def summary_line(self) -> str:
        """一行摘要 —— 「今天发生了什么」。"""
        if not self.events:
            return "(无事件)"
        return " · ".join(f"{k}×{v}" for k, v in self.counts().items())

    def to_records(self) -> list[dict[str, Any]]:
        """全部事件的 JSON 形状。"""
        return [e.to_record() for e in self.events]

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[DomainEvent]:
        return iter(self.events)


__all__ = [
    "CashTransferred", "DailySnapshotProduced", "DomainEvent", "EventStream",
    "GridCycleClosed", "GridCycleOpened", "GridLayerAdded",
    "MergedSharesTransferred", "PositionValued", "RebalanceTriggered",
    "TradeRecorded",
    "POOL_EXTERNAL", "POOL_GRID", "POOL_REBALANCE",
]
