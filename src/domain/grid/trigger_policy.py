"""触发规则 —— `TriggerPolicy` (BC-C)。

网格的三个触发位是**纯规则**, 与状态无关:

    open — 今日价 < 过去 120 日窗口最低            → 建议开仓
    add  — 今日价 ≤ 最近一笔买入价 × (1 - 3%)      → 建议加仓
    tp   — 今日价 ≥ 第一笔买入价 × (1 + 3%)        → 建议回本出场

⚠️ 本模块**只算**, 不改状态, 更不下单 —— 它回答的是「离触发还差多少」。
真正的动作由用户在「交易记录 → 复合网格」提交 (scripts/apply_grid.py)。

距离口径 (正数 = 还没触发, ≤0 = 已越过触发线):

    open:     (今日价 - 窗口最低) / 窗口最低 × 100
    add / tp: (触发位 - 今日价) / 今日价 × 100

⚠️ 窗口是**追加今天之前**的 —— 今天的低价不能自己当自己的门槛, 否则
「今天是否创新低」永远等于「没创新低」。

⚠️ **中间量保持全精度**: `TriggerDistance.pct` / `.price` 是**原始 float**,
不套 `DeviationPP` / `Price`。取整发生在**输出边界** (`advisor.GridSuggestion`
构造时), 这跟 `shared/money.py` 的舍入点纪律是同一条 —— 在没有 `round()`
的地方构造值对象, 就是在偷偷改数字。

**stdlib-only** —— 与 `grid_position.py` 同样的理由: `scripts/apply_grid.py`
在不装任何依赖的 GitHub Actions job 里 import 这条链路。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

# 触发参数 (env-overridable for parameter sweeps)
GRID_STEP_PCT: float = 3.0
GRID_TP_PCT: float = 3.0
# "avg_cost"   — exit when price >= avg_cost * (1 + tp_pct/100)             [v2]
# "first_buy"  — exit when price >= first_buy_price * (1 + tp_pct/100)       [v3]
GRID_TP_BASIS: str = os.environ.get("GRID_TP_BASIS", "first_buy")


class TriggerLevel(str, Enum):
    """网格动作的词表 —— open / add / tp。

    ⚠️ 是 `str` 的子类, 所以 `TriggerLevel.OPEN == "open"` 成立, `json.dumps`
    也直接输出 `"open"`。但 `Enum` 覆盖了 `str.__str__`, f-string 里
    `f"{TriggerLevel.OPEN}"` 会显示成 `TriggerLevel.OPEN` —— 所以下面
    显式定义了 `__str__`, 让格式化也拿到裸值。
    """

    OPEN = "open"
    ADD = "add"
    TP = "tp"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TriggerDistance:
    """距某个触发位的距离。

    Attributes:
        level:  哪个触发位
        pct:    距触发位的百分比 (正数 = 还没触发, ≤0 = 已越过触发线)。
                `None` = 这个触发位**还不存在** (没有窗口 / 没有持仓 / 价格为 0)
        price:  触发位的绝对价格。`None` = 同上
    """

    level: TriggerLevel
    pct: float | None
    price: float | None


@dataclass(frozen=True)
class TriggerLevels:
    """三个触发位与今日价的关系 —— **纯计算结果**。

    Attributes:
        signal:  已触发的建议 (至多一个); `None` = 今天什么都不用做
        nearest: **最近**的那个触发位 (可能是已触发的那个, 也可能两个都没触发)
        open:    开仓触发位的距离 (窗口为空时为 `None`)
        add:     加仓触发位的距离 (不在周期内 / 没有买入层时为 `None`)
        tp:      回本触发位的距离 (不在周期内 / 没有持仓时为 `None`)
    """

    signal: TriggerLevel | None
    nearest: TriggerDistance
    open: TriggerDistance | None
    add: TriggerDistance | None
    tp: TriggerDistance | None

    def to_dict(self) -> dict:
        """老 `grid_position._triggers` 的 dict 形状 —— 过渡期用。"""
        return {
            "signal": self.signal.value if self.signal is not None else None,
            "kind": self.nearest.level.value,
            "pct": self.nearest.pct,
            "price": self.nearest.price,
            "dist_open_pct": self.open.pct if self.open is not None else None,
            "dist_add_pct": self.add.pct if self.add is not None else None,
            "dist_tp_pct": self.tp.pct if self.tp is not None else None,
            "open_price": self.open.price if self.open is not None else None,
            "add_price": self.add.price if self.add is not None else None,
            "tp_price": self.tp.price if self.tp is not None else None,
        }


@dataclass(frozen=True)
class TriggerPolicy:
    """「什么算触发」的**唯一**定义。纯函数 —— 不改状态, 不下单。

    以前这段逻辑散在两处: 生产路径的 `grid_position._triggers`, 回测路径
    `simulation/grid_simulator.run_compound_grid` 的内联判断。两套实现
    慢慢会长歪 —— 这正是 `docs/domain_model.md` §7 点名的「最严重的一处
    重复」。本类把它们收到**一个**地方。
    """

    step_pct: float = GRID_STEP_PCT
    tp_pct: float = GRID_TP_PCT
    tp_basis: str = GRID_TP_BASIS

    def levels(self, *, in_cycle: bool, entries: list[dict],
               window: list[float], price: float) -> TriggerLevels:
        """算出三个触发位与今日价的关系。**纯函数**, 不改任何状态。

        Args:
            in_cycle: 当前是否处于一个进行中的周期
            entries:  周期内的买入层 [{date, price, shares}]
            window:   滚动窗口 (最多 GRID_LOOKBACK 根 K 线最低价)
            price:    今日价 (生产路径传**当日 K 线最低价**)

        ⚠️ 窗口必须是**追加今天之前**的 —— 今天的低价不能自己当自己的门槛。
        """
        price = float(price)
        entries = list(entries)
        window = list(window)

        # --- open 的触发位: 窗口最低 (待机时才用得到) ---
        wmin = min(window) if window else None
        open_dist: TriggerDistance | None = None
        if wmin and wmin > 0:
            open_dist = TriggerDistance(TriggerLevel.OPEN,
                                        (price - wmin) / wmin * 100,
                                        float(wmin))

        if not in_cycle:
            # 待机: 只可能建议开仓
            if wmin and price < wmin:
                signal = TriggerLevel.OPEN
                nearest = TriggerDistance(TriggerLevel.OPEN,
                                          open_dist.pct if open_dist is not None else 0.0,
                                          float(wmin))
            else:
                signal = None
                nearest = TriggerDistance(
                    TriggerLevel.OPEN,
                    open_dist.pct if open_dist is not None else 0.0,
                    float(wmin) if wmin else 0.0)
            return TriggerLevels(signal=signal, nearest=nearest,
                                  open=open_dist, add=None, tp=None)

        # --- 周期中: 加仓位 + 出场位 ---
        total_shares = sum(e["shares"] for e in entries)
        total_invested = sum(e["shares"] * e["price"] for e in entries)
        avg_cost = total_invested / total_shares if total_shares else 0.0

        add_dist: TriggerDistance | None = None
        if entries:
            add_target = float(entries[-1]["price"]) * (1 - self.step_pct / 100)
            add_dist = TriggerDistance(
                TriggerLevel.ADD,
                ((add_target - price) / price * 100) if price > 0 else None,
                add_target)

        tp_dist: TriggerDistance | None = None
        if total_shares > 0 and avg_cost > 0:
            if self.tp_basis == "first_buy" and entries:
                tp_target = float(entries[0]["price"]) * (1 + self.tp_pct / 100)
            else:
                tp_target = avg_cost * (1 + self.tp_pct / 100)
            tp_dist = TriggerDistance(
                TriggerLevel.TP,
                ((tp_target - price) / price * 100) if price > 0 else None,
                tp_target)

        # --- 判定 ---
        # 顺序: **先 tp 后 add**。正常金字塔里后一笔买价更低 (entries[-1] ≤
        # entries[0]), 所以 "再跌 3%" 与 "涨回 3%" 不可能同时成立; 只有用户手动
        # 在比首笔高 6.2% 以上加仓时才会撞上, 此时报 tp —— 一个周期以 1 笔出场
        # 收尾, 更贴合 "N 买 1 卖" 的口径。
        tp_hit = tp_dist is not None and price >= tp_dist.price
        add_hit = add_dist is not None and price <= add_dist.price

        signal: TriggerLevel | None = None
        if tp_hit:
            signal = TriggerLevel.TP
            nearest = TriggerDistance(TriggerLevel.TP,
                                      tp_dist.pct if tp_dist.pct is not None else 0.0,
                                      tp_dist.price)
        elif add_hit:
            signal = TriggerLevel.ADD
            nearest = TriggerDistance(TriggerLevel.ADD,
                                      add_dist.pct if add_dist.pct is not None else 0.0,
                                      add_dist.price)
        else:
            # 都没触发 → 取**更近**的那个
            # (add 在前 —— 平手时 `min` 取第一个, 与老实现一致)
            cands = [(d.level, d.pct) for d in (add_dist, tp_dist)
                     if d is not None and d.pct is not None]
            if cands:
                level, pct = min(cands, key=lambda kv: abs(kv[1]))
                src = add_dist if level is TriggerLevel.ADD else tp_dist
                nearest = TriggerDistance(level, pct, src.price)
            else:
                nearest = TriggerDistance(
                    TriggerLevel.TP, 0.0,
                    (tp_dist.price if tp_dist is not None else 0.0) or 0.0)
        return TriggerLevels(signal=signal, nearest=nearest,
                              open=open_dist, add=add_dist, tp=tp_dist)


__all__ = [
    "GRID_STEP_PCT", "GRID_TP_BASIS", "GRID_TP_PCT",
    "TriggerDistance", "TriggerLevel", "TriggerLevels", "TriggerPolicy",
]
