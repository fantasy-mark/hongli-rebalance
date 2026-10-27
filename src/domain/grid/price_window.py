"""120 日滚动窗口 —— `PriceWindow` (BC-C)。

⚠️ **为什么窗口要带日期** (D1):

`apply-grid.yml` 提交后会 re-trigger `daily.yml`, **同一天** `record.py`
可能跑两次 → 原来那个「纯数字队列」把同一天 append 两次 → 窗口深度少一天。

窗口是「建议开仓」的**唯一**依据 (条件 = `price < min(window)`), 少一天就
少一天的历史 —— 一天不多, 但**每天**都少, 而且**看不出来**: 队列尾部有两根
一模一样的 K 线, 谁知道那是两天还是同一天跑了两遍。

带日期之后, 同一天**覆盖**而不是追加 —— 窗口深度只由**交易日**决定。

---

**窗口里存的是每天的 K 线最低价** (生产路径传当日 K 线的 low, 取不到就退回
实时收盘价) —— 所以同一天被记录两次时, 要留**更低**的那个: 那才是「那天的
最低价」。留高的会把窗口最低价抬上去, 「今天是否创新低」的判定就会失真。

---

**老数据怎么办** (D1 迁移):

`data/grid_state.json` 里存的是一串裸数字 `[8.35, 8.29, ...]`, **没有日期**。
那些日期**不可恢复** —— 窗口里的历史最低价是**前复权**之后的 K 线低点, 而
分红除权会让历史价格**重新基准化**, 同一个「历史低点」在不同日子算出来是
不同的数。所以老数据只能迁成**无日期**的 bar (`day = ""`), 日期靠之后的每
日运行**自己长出来** (约 120 个交易日后, 窗口全是带日期的)。

⚠️ 无日期的 bar 排在**最前面** —— 它们是窗口里最老的那批。

**stdlib-only** —— 与 `grid_position.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这条链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

#: 「日期不可恢复」的哨兵 —— 老数据迁移上来时用它。
UNDATED = ""


@dataclass(frozen=True)
class WindowBar:
    """窗口里的一根 K 线 —— **日期 + 最低价**。

    Attributes:
        day:   交易日 (`YYYY-MM-DD`)。`""` = 日期不可恢复 (老数据迁移上来
               的那些 —— 见模块 docstring)。
        price: 那一天的最低价 (生产路径: 当日 K 线 low)。
    """

    day: str
    price: float

    def to_pair(self) -> list:
        """JSON 形状 —— `[日期, 价格]`。"""
        return [self.day, float(self.price)]

    @classmethod
    def from_raw(cls, raw: Any) -> "WindowBar":
        """从 JSON 的一格读出 bar —— 新格式 `[日期, 价格]` 或旧格式 `价格`。"""
        if isinstance(raw, (list, tuple)):
            day = str(raw[0]) if len(raw) > 0 else UNDATED
            price = float(raw[1]) if len(raw) > 1 else 0.0
            return cls(day=day, price=price)
        # 旧格式: 裸数字 —— 日期不可恢复
        return cls(day=UNDATED, price=float(raw))


@dataclass(frozen=True)
class PriceWindow:
    """120 日滚动窗口 —— 「建议开仓」的**唯一**依据。

    ⚠️ **不可变**: `record()` 返回**新**的窗口, 不改旧的 —— 于是「谁动了
    窗口」永远是显式的 (跟 `GridPosition` 的风格一致)。
    """

    bars: tuple[WindowBar, ...] = ()

    # -- construction -----------------------------------------------------
    @classmethod
    def from_state(cls, raw: Any) -> "PriceWindow":
        """从 JSON 读窗口 —— **同时接受**新旧两种格式:

        - 新: `[["2026-10-27", 8.12], ["2026-10-28", 8.05], ...]`
        - 旧: `[8.12, 8.05, ...]`  ← 迁成无日期的 bar (`day = ""`)
        """
        if not raw:
            return cls()
        if isinstance(raw, (str, bytes)):
            raise ValueError(f"grid window must be a list, got {type(raw).__name__}")
        return cls(tuple(WindowBar.from_raw(item) for item in raw))

    def to_state(self) -> list:
        """JSON 形状 —— `[[日期, 价格], ...]`。"""
        return [bar.to_pair() for bar in self.bars]

    # -- read side --------------------------------------------------------
    @property
    def prices(self) -> list[float]:
        """窗口里的价格序列 (旧接口的形状 —— `TriggerPolicy` 吃这个)。"""
        return [bar.price for bar in self.bars]

    @property
    def minimum(self) -> Optional[float]:
        """窗口最低价 —— 「建议开仓」的触发位。空窗口 = `None`。"""
        return min((bar.price for bar in self.bars), default=None)

    @property
    def days(self) -> list[str]:
        """窗口里的日期序列。"""
        return [bar.day for bar in self.bars]

    def __len__(self) -> int:
        return len(self.bars)

    def __bool__(self) -> bool:
        return bool(self.bars)

    # -- write side -------------------------------------------------------
    def record(self, day: str, price: float, *, lookback: int) -> "PriceWindow":
        """记一根 K 线 —— 返回**新**的窗口。

        - **同一天** → 覆盖, 留**更低**的那个 (那才是那天的最低价)。
        - **新的一天** → 按日期插到正确位置 (无日期的旧 bar 排在最前)。
        - 超过 `lookback` → 砍掉最老的。

        ⚠️ 留低不留高: 窗口最低价是「今天是否创新低」的门槛, 把它抬高会
        让已经该触发的建议**不再触发** —— 那是假的否定信号, 比深度少一天
        严重得多。
        """
        day = str(day or UNDATED)
        price = float(price)
        bars = list(self.bars)

        if day != UNDATED:
            for i, bar in enumerate(bars):
                if bar.day == day:
                    if price < bar.price:
                        bars[i] = WindowBar(day=day, price=price)
                    return PriceWindow(tuple(self._trim(bars, lookback)))
        else:
            # 没有日期可比 —— 只能追加 (引擎每天都有日期, 走不到这条)。
            bars.append(WindowBar(day=day, price=price))
            return PriceWindow(tuple(self._trim(bars, lookback)))

        # 新的一天 → 按日期插到正确的位置 (无日期的旧 bar 排在最前)。
        idx = len(bars)
        while idx > 0 and bars[idx - 1].day and bars[idx - 1].day > day:
            idx -= 1
        bars.insert(idx, WindowBar(day=day, price=price))
        return PriceWindow(tuple(self._trim(bars, lookback)))

    @staticmethod
    def _trim(bars: list[WindowBar], lookback: int) -> list[WindowBar]:
        """砍掉超出窗口深度的**最老**的几根 (窗口按时间排, 所以是尾部之前)。"""
        if lookback > 0 and len(bars) > lookback:
            return bars[-lookback:]
        return bars


def window_from_state(raw: Any) -> PriceWindow:
    """便捷入口 —— JSON → 窗口。"""
    return PriceWindow.from_state(raw)


__all__ = ["UNDATED", "PriceWindow", "WindowBar", "window_from_state"]
