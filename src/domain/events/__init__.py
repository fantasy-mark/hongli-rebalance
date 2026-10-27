"""领域事件 (Phase 5b) —— 见 `domain_events.py`。

⚠️ **不是事件溯源** —— 事实源仍然是 JSON 文件; 事件是运行时的通知,
让「谁在什么时候因为什么改变了状态」变成可以断言的东西。
"""
from .domain_events import (
    POOL_EXTERNAL,
    POOL_GRID,
    POOL_REBALANCE,
    CashTransferred,
    DailySnapshotProduced,
    DomainEvent,
    EventStream,
    GridCycleClosed,
    GridCycleOpened,
    GridLayerAdded,
    MergedSharesTransferred,
    PositionValued,
    RebalanceTriggered,
    TradeRecorded,
)

__all__ = [
    "CashTransferred", "DailySnapshotProduced", "DomainEvent", "EventStream",
    "GridCycleClosed", "GridCycleOpened", "GridLayerAdded",
    "MergedSharesTransferred", "PositionValued", "RebalanceTriggered",
    "TradeRecorded",
    "POOL_EXTERNAL", "POOL_GRID", "POOL_REBALANCE",
]
