"""One-shot: 把 data/history.json 里**受影响的记录**重算到新的 3pp 口径。

用法:
    python -X utf8 scripts/migrate_history_capital_base.py            # 写盘
    python -X utf8 scripts/migrate_history_capital_base.py --dry-run  # 只打印

⚠️ **为什么要有这个脚本**: 2026-11-02 改了 3pp 的**规则** —— 资本基数从
「ETF 市值 + 真实现金 + 网格虚拟预算」改成「ETF 市值 + **真实现金**」
(见 `src/domain/portfolio/portfolio.py:Portfolio.total`)。

网格那一池是**虚拟预算** (D3), 它进分母 = 稀释每个真实持仓的权重 = 凭空
造出假的「偏离」。2026-11-02 就是这么误报了一次「⚠ 触发调仓」:

    黄金ETF博时  真实偏离 1.09pp (没到 3pp 的阈值)
                含虚拟预算 3.01pp → 触发 → 回正方案要买 ¥15,328
                而真实现金只有 ¥328

历史记录里那几条**已经用旧规则算过**的派生字段因此是错的 —— 页面画的
「总市值」曲线上会有一截 ¥15,000 的假台阶 (2026-10-30 那根), 邮件也会
按旧口径说「触发调仓」。

做什么 (逐条记录):
    total / cash_total / weights / triggered / plan  ← **只有这五个**是派生的
    grid_signals[].layer_yuan                        ← 派生 (层额的池子= total)

⚠️ **事实一个字节都不动**: 持仓、份额、价格、两个现金池、日期 —— 全部原样
保留。改的只是「用哪条规则算出来的那些数」。

⚠️ **重算走领域里的那一份实现** (`RebalancePolicy.evaluate`) —— 这里不抄
任何公式。抄一份 = 迟早长歪。

⚠️ **层额 (`layer_yuan`) 也跟着改**: 层额的池子就是 `Portfolio.total`
(`daily_snapshot.size_grid_layer` 把它当池子) —— 池子口径改了, 层额不改,
页面上的「单层金额」还是按含虚拟预算的旧池子算的。

幂等: 重算一遍跟存储值一样 → 不动 (第二次跑是 no-op)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.domain.grid.config import GridConfig  # noqa: E402
from src.domain.grid.layer_sizing import LayerSizingPolicy  # noqa: E402
from src.domain.portfolio.portfolio import Portfolio  # noqa: E402
from src.domain.portfolio.rebalance_policy import RebalancePolicy  # noqa: E402
from src.domain.shared.money import (  # noqa: E402
    LayerSize, Money, ShareCount, Weight,
)

HISTORY = ROOT / "data" / "history.json"

#: 只重算这五个**派生**字段 —— 事实不在这个清单里。
DERIVED = ("total", "cash_total", "weights", "triggered", "plan")


def needs_migration(record: dict) -> bool:
    """这条记录是不是**被新规则改到**了?

    只有 `cash_grid > 0` 的那些 —— 网格的虚拟预算进了分母, 权重/判阈/方案
    才需要重算。`cash_grid == 0` 的记录本来就是真钱口径, 一个字节都不该动。

    ⚠️ 早期那几条 (2026-10-12 ~ 10-15) **不迁**: 它们的权重跟记录里自己的
    positions 对不上 (红利 40,600 × 1.385 = 56,231, 而 56,231/173,424 =
    32.42%, 存的是 33.38%) —— 那是**前复权重新基准化**之前算的, 属于另一段
    历史。重算它们 = 用今天的规则改写昨天的数, 那是篡改历史, 不是迁移。
    """
    return float(record.get("cash_grid", 0.0) or 0.0) > 0.0


def recent_min_of(series: list[float], window: int) -> float:
    """池子序列的「最近最低」。

    ⚠️ 这是 `daily_snapshot.size_grid_layer` 里 `pool.tail(window).min()` 的
    **等价实现** —— 池子序列 = history 里每天的 total + 今天这个 total, 取
    最近 `floating_window + 1` 天的最小值。

    ⚠️ 抄的是**口径**, 不是公式 —— 层额本身仍然走领域里的
    `LayerSizingPolicy.layer_yuan` (那里才是唯一定义)。
    """
    if not series:
        return 0.0
    return min(series[-window:])


def refresh_layer(record: dict, pool_value: float, prior_series: list[float],
                  config: GridConfig) -> dict:
    """把 `grid_signals[].layer_yuan` 刷成新口径 —— 层额是**派生**的。

    ⚠️ 层额的**池子**就是 `Portfolio.total` (见
    `daily_snapshot.size_grid_layer`: 它把池子传给 `advance_all_grids` →
    `LayerSizingPolicy.layer_yuan`)。池子口径从「含虚拟预算」改成「只算
    真钱」, 层额就得跟着改 —— 否则页面上的「单层金额」还是按旧池子算的
    (2026-11-02: ¥10,931.8 → ¥10,181.8)。

    ⚠️ 池子用的是**未取整的 float** (`Portfolio.total` 的原值), `recent_min`
    用的是 history 里**已存**的 total —— 这跟原来那次 daily 跑的一模一样
    (池子序列 = `[r["total"] for r in history] + [total_pre]`)。
    """
    signals = record.get("grid_signals")
    if not signals:
        return record

    window = config.floating_window + 1
    series = list(prior_series) + [float(pool_value)]
    recent_min = recent_min_of(series, window)
    layer = LayerSizingPolicy.from_config(config).layer_yuan(
        float(pool_value), recent_min)

    new = dict(record)
    new_signals = []
    for s in signals:
        s2 = dict(s)
        s2["layer_yuan"] = LayerSize(layer)
        # 建议金额/份额跟着层额走 —— 只有 open/add 有金额 (见 advisor.py)
        if s2.get("signal") in ("open", "add"):
            price = float(s2.get("price", 0.0) or 0.0)
            if price > 0:
                s2["suggest_amount"] = LayerSize(layer)
                s2["suggest_shares"] = ShareCount(layer / price)
        new_signals.append(s2)
    new["grid_signals"] = new_signals
    return new


def migrate_record(record: dict, prior_series: Optional[list[float]] = None,
                  config: Optional[GridConfig] = None) -> tuple[dict, bool]:
    """按**当前**规则重算一条记录的派生字段。返回 (新记录, 变了没有)。

    ⚠️ 只碰派生字段 —— 持仓 / 份额 / 价格 / 两个现金池原样保留。
    ⚠️ 记录里**没有**的键不会被补上 (老记录用 `cash` 而不是 `cash_rebalance`,
    那几行不该因为一次迁移就长出新键)。

    Args:
        prior_series: 截至**前一天**的 3pp 总市值序列 (history 里每天的
            total)。给了就顺带把层额 (`grid_signals[].layer_yuan`) 也刷成
            新口径; `None` = 只重算那五个字段, 不动层额。
        config: 网格规则 —— 默认读环境变量 (跟生产那次跑的一样)。
    """
    positions = record.get("positions") or []
    cash_rebalance = float(
        record.get("cash_rebalance", record.get("cash", 0.0)) or 0.0)
    grid_budget = float(record.get("cash_grid", 0.0) or 0.0)
    threshold_pp = float(record.get("threshold_pp", 3.0) or 3.0)

    pf = Portfolio.from_records(positions,
                                cash_rebalance=cash_rebalance,
                                grid_budget=grid_budget)
    # ⚠️ 安全网: 重算出来的 ETF 市值必须跟记录里存的一致 —— 不一致说明这条
    #    记录来自**另一段数据管道**, 重算它就是篡改历史, 宁可不迁。
    stored_etf = record.get("etf_value")
    if stored_etf is not None and abs(pf.etf_value - float(stored_etf)) > 0.01:
        raise ValueError(
            f"record {record.get('date')!r}: recomputed etf_value "
            f"{pf.etf_value} != stored {stored_etf} —— 不迁这条")

    outcome = RebalancePolicy(threshold_pp=threshold_pp).evaluate(pf)

    new = dict(record)
    new["total"] = Money(outcome.total)
    new["cash_total"] = Money(cash_rebalance)
    new["weights"] = [Weight(w) for w in outcome.weights]
    new["triggered"] = outcome.triggered
    new["plan"] = [dict(p) for p in outcome.plan]

    changed = False
    for key in DERIVED:
        if key not in record:
            continue
        if _differs(record.get(key), new.get(key)):
            changed = True

    # 层额也是派生的 —— 池子口径改了, 层额跟着改
    if prior_series is not None:
        refreshed = refresh_layer(new, outcome.total, prior_series,
                                  config or GridConfig.from_env())
        if not _same_layers(record.get("grid_signals") or [],
                            refreshed.get("grid_signals") or []):
            changed = True
        new = refreshed
    return new, changed


def _same_layers(old: list[dict], new: list[dict]) -> bool:
    """两串信号的层额/建议金额是不是**逐个一样**。"""
    if len(old) != len(new):
        return False
    for a, b in zip(old, new):
        for key in ("layer_yuan", "suggest_amount", "suggest_shares"):
            if _differs(a.get(key), b.get(key)):
                return False
    return True


def _differs(old, new) -> bool:
    """两个派生值算不算不一样 (列表按元素比, 浮点按值比)。"""
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return True
        return any(abs(float(a) - float(b)) > 1e-9 if isinstance(a, (int, float))
                   and isinstance(b, (int, float)) else a != b
                   for a, b in zip(old, new))
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return abs(float(old) - float(new)) > 1e-9
    return old != new


def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]

    if not HISTORY.exists():
        print(f"ERROR {HISTORY} 不存在", file=sys.stderr)
        sys.exit(1)
    try:
        history = json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception as ex:
        print(f"ERROR history.json 读不出来: {ex}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(history, list):
        print("ERROR history.json 根必须是数组", file=sys.stderr)
        sys.exit(1)

    out: list = []
    touched = 0
    # 池子序列 —— 截至**前一天**的 3pp 总市值 (层额的「最近最低」靠它)
    series: list[float] = []
    config = GridConfig.from_env()
    for record in history:
        if not needs_migration(record):
            series.append(float(record.get("total", 0.0) or 0.0))
            out.append(record)
            continue
        new, changed = migrate_record(record, prior_series=series,
                                      config=config)
        series.append(float(new.get("total", 0.0) or 0.0))
        if changed:
            touched += 1
            old_layer = (record.get("grid_signals") or [{}])[0].get("layer_yuan")
            new_layer = (new.get("grid_signals") or [{}])[0].get("layer_yuan")
            print(f"[{record.get('date')}] "
                  f"总市值 {record.get('total')} → {new['total']}  "
                  f"权重 {[round(w, 4) for w in record.get('weights', [])]} → "
                  f"{[round(w, 4) for w in new['weights']]}  "
                  f"触发 {record.get('triggered')} → {new['triggered']}")
            if old_layer != new_layer:
                print(f"          单层金额 {old_layer} → {new_layer}")
        out.append(new)

    if touched == 0:
        print("没有记录需要迁移 (已经是 3pp 口径)")
        return

    if dry_run:
        print(f"\n[dry-run] 不会写盘。涉及 {touched} 条记录。")
        return

    tmp = HISTORY.with_suffix(HISTORY.suffix + ".tmp")
    # 不加尾随换行 —— src/infrastructure/json_repositories.py:save_history 也是
    # 这个口径, 免得两条写盘路径互相制造「只有一行之差」的假 diff。
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, HISTORY)
    print(f"\nOK 已写回 {HISTORY.relative_to(ROOT)} ({touched} 条记录)")


if __name__ == "__main__":
    main()
