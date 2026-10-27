"""Daily snapshot use case — 3pp rebalance + compound grid (BC-G 编排层).

Flow (each Action run):
  1. Read holdings.json (the **universe** of codes the user tracks)
  2. Replay the trade ledger → real positions + the two cash pools
  3. Price every position (values come from an injected price provider)
  4. Evaluate the 3pp rebalance: any |weight − 1/N| > 3pp → trigger
  5. Fold the grid's merged shares into the portfolio, then size today's
     grid layer from the recent pool minimum
  6. Advance every grid state by one day (**advice only** — the engine
     never trades)
  7. Save: data/history.json (the snapshot) + data/grid_state.json
  8. Render data/grid_chart.png (consumed by GitHub Pages <img>)
  9. Emit $GITHUB_OUTPUT for downstream steps

This module is **orchestration**: it wires the domain services to the
infrastructure adapters and owns the *order* of the steps.  The rules
themselves live in `src/domain/`.

**Phase 5c (2026-10-27)** — 应用层瘦身。`main()` 从一条 180 行的直线流程
拆成一串**用例**:

    load_universe            读 holdings.json (只取代码清单) + grid_state.json
    replay_trade_ledger      重放 trades.json → 真实持仓 + 两个现金池
    value_holdings           给每个持仓定价 (价格来源注入)
    attach_qdii_premiums     挂上 QDII 溢价 (best-effort)
    absorb_merged_shares     网格宣告的归并份额 → 组合吸收
    size_grid_layer          算网格层额
    advance_grids            推进网格状态 + 生成建议 (**不下单**)
    judge_rebalance          评估 3pp —— 记录里那个 total 的来源
    persist_snapshot         写 data/history.json
    render_snapshot_chart    画 data/grid_chart.png (best-effort)
    report_snapshot          打印人看的摘要
    emit_step_outputs        写 $GITHUB_OUTPUT

每个用例是一个**具名函数**: 读 `SnapshotRun`、写 `SnapshotRun`。应用层因此
**只剩接线** —— 规则一条都不在这里 (规则全在 `src/domain/`)。

⚠️ **两个基准** (D9): `layer_sizing_basis` 与 `snapshot_basis` 是**两次**
评估 —— 前者给网格层额定尺, 后者才是记录里 `total` / `weights` /
`triggered` 的来源。以前这两段是 `main()` 里相隔几十行的两行代码, 读的人
要数行数才知道「哪个 total 是记录里的那个」; 现在它们各有名字, 各自的
职责写在名字里。
"""
from __future__ import annotations

import datetime
import os
import sys
import traceback
from dataclasses import dataclass, field

import pandas as pd

from ..domain.events.domain_events import (
    DailySnapshotProduced,
    EventStream,
)
from ..domain.grid import grid_position as grid_mod
from ..domain.grid.config import GridConfig
from ..domain.grid.layer_sizing import LayerSizingPolicy
from ..domain.ledger.projection import project_ledger
from ..domain.portfolio.asset_universe import ASSET_NAMES, ETF_DEFS, QDII_CODES
from ..domain.portfolio.cash_pools import CashPoolBreakdown, grid_occupancy
from ..domain.portfolio.portfolio import (
    Portfolio,
    merged_by_code_from_transfers,
)
from ..domain.portfolio.rebalance_policy import (
    RebalanceOutcome,
    RebalancePolicy,
    TargetWeightPolicy,
)
from ..domain.portfolio.valuation import value_extra_codes, value_positions
from ..domain.shared.money import (
    LayerSize,
    Money,
    Price,
    ShareCount,
    Weight,
)
from ..infrastructure.json_repositories import (
    GRID_CHART,
    load_grid_state,
    load_history,
    load_trades,
    read_holdings,
    save_grid_state,
    save_history,
)
from ..infrastructure.quote_providers import (
    fetch_kline_history,
    fetch_qdii_premium,
    latest_for_code,
)
from .config import AppConfig

#: 运行配置 —— 环境变量在这里读**一次** (Phase 5a), 之后一路当**值**传。
#: 领域层 (`domain/grid/config.py`) 只有纯数据 + 一个 `from_env` 工厂。
APP_CONFIG = AppConfig.from_env()

# 3pp threshold — 从配置对象取, 不再由本模块自己读环境变量。
# (兼容别名: 旧的模块级名字还在, 既有 import 不断 —— 但它的**本体**
#  已经是 `AppConfig.threshold_pp` 这个显式的配置字段。)
THRESHOLD_PP = APP_CONFIG.threshold_pp

#: 目标权重规则 (等权 1/n) —— 页面显示的「目标 ±x%」也是它算的。
TARGET_WEIGHT_POLICY = TargetWeightPolicy()


def rebalance_policy() -> RebalancePolicy:
    """当前生效的 3pp 再平衡策略（阈值取模块级的 `THRESHOLD_PP`）。

    刻意做成**函数**而不是一个模块级常量实例 —— 旧的
    `evaluate(..., threshold_pp=THRESHOLD_PP)` 是在**调用时**读这个模块
    全局的，绑定时机必须保持一致（有人在 import 之后改这个全局，行为也要
    跟着变）。

    阈值仍然是**实例状态**：想用别的阈值就
    `RebalancePolicy(threshold_pp=1.0)`，一个进程里可以并存多套。
    """
    return RebalancePolicy(threshold_pp=THRESHOLD_PP)


# -----------------------------------------------------------------------------
# Grid state → daily signals
# -----------------------------------------------------------------------------

def advance_all_grids(state: dict, today: str, prices_by_name: dict[str, float],
                      pool_value: float, recent_min: float,
                      config: GridConfig | None = None) -> tuple[dict, list[dict]]:
    """Advance grid state for all assets, returning (new_state, signals).

    ⚠️ 引擎**不再自动交易** —— 这里只是滚动 120 日窗口 + 算出「建议」。
    `signal` 是**建议提交哪种 op** (open/add/tp), 不是"已成交"。

    Args:
        config: 网格规则 —— `None` = 用应用层的默认配置 (`APP_CONFIG.grid`)。
                显式传进来就能跑另一套参数, 一个进程里两套并存。
    """
    cfg = config or APP_CONFIG.grid
    layer = LayerSizingPolicy.from_config(cfg).layer_yuan(pool_value, recent_min)
    new_state: dict = {}
    signals: list[dict] = []
    for name in ASSET_NAMES:
        asset_state = state.get(name) or grid_mod.fresh_asset_state()
        price = prices_by_name.get(name)
        if price is None:
            new_state[name] = asset_state
            continue
        # 建议基于**追加今天之前**的状态算 —— 今天的低价不能自己当门槛
        adv = grid_mod.advisory(asset_state, float(price), layer,
                                step_pct=cfg.step_pct, tp_pct=cfg.tp_pct,
                                tp_basis=cfg.tp_basis)
        new_asset_state, signal = grid_mod.advance_one_day(
            asset_state, today, float(price), layer,
            lookback=cfg.lookback, step_pct=cfg.step_pct,
            tp_pct=cfg.tp_pct, tp_basis=cfg.tp_basis,
        )
        new_state[name] = new_asset_state
        snap = grid_mod.asset_snapshot(new_asset_state, float(price))
        signals.append({
            "name": name,
            "price": Price(price),
            "layer_yuan": LayerSize(layer),
            # "open" | "add" | "tp" | None —— 建议提交哪种 op, 不是"已成交"
            "signal": signal,
            "in_cycle": snap["in_cycle"],
            "num_buys": snap["num_buys"],
            "avg_cost": snap["avg_cost"],
            # 网格自己的资金占用 = 只算在周期内的层 (出场后本金回来, 占用回落)
            "current_value": snap["current_value"],
            "unrealized_pnl": snap["unrealized_pnl"],
            "return_pct": snap["return_pct"],
            # 已归并到再平衡持仓的份额 (不再是网格的资产) —— 页面在再平衡
            # 那一栏显示, 网格这一栏不重复计入资金占用。
            "merged_shares": snap["merged_shares"],
            "merged_value": snap["merged_value"],
            # 距「最近的那个」触发位还有多远 (正数 = 还没触发, ≤0 = 已越过)
            "trigger_pct": adv["trigger_pct"],
            "trigger_kind": adv["trigger_kind"],
            "trigger_price": adv["trigger_price"],
            # 三个触发位各自的距离 (None = 当前状态下不适用)
            "dist_open_pct": adv["dist_open_pct"],
            "dist_add_pct": adv["dist_add_pct"],
            "dist_tp_pct": adv["dist_tp_pct"],
            # 建议成交价 / 份额 / 金额 (只有 open/add 有金额)
            "suggest_price": adv["suggest_price"],
            "suggest_shares": adv["suggest_shares"],
            "suggest_amount": adv["suggest_amount"],
        })
    return new_state, signals


# -----------------------------------------------------------------------------
# Build & emit
# -----------------------------------------------------------------------------

def write_github_output(date_str: str, total: float, triggered: bool) -> None:
    """Write the `date` / `total` / `trigger` step outputs.

    ⚠️ `trigger=yes` now means "需人工关注" —— either the 3pp rebalance
    fired, or the grid engine has a **suggestion** pending (the engine
    never trades by itself). Only `outputs.date` is consumed downstream.
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"date={date_str}\n")
        f.write(f"total={total:.2f}\n")
        f.write(f"trigger={'yes' if triggered else 'no'}\n")


def build_record(today: str, positions: list[dict],
                 cash_rebalance: float, grid_budget: float,
                 total: float, weights: list[float],
                 triggered_3pp: bool, plan: list[dict],
                 grid_signals: list[dict], grid_state: dict,
                 qdii_premiums: list[dict] | None = None,
                 threshold_pp: float = THRESHOLD_PP) -> dict:
    etf_total = sum(p["value"] for p in positions)
    n = len(positions)
    target_w = TARGET_WEIGHT_POLICY.target_weight(n)
    # `ops` is an append-only log whose canonical home is
    # data/grid_state.json (the page fetches that file directly for the
    # 复合网格 操作记录 table). Embedding it in every daily record would
    # re-copy the whole log once per day forever, growing history.json
    # without bound. Strip it — nothing reads this copy.
    slim_grid_state = {
        name: ({k: v for k, v in asset.items() if k != "ops"}
               if isinstance(asset, dict) else asset)
        for name, asset in (grid_state or {}).items()
    }
    # 网格归并到再平衡的份额合计 —— 页面/邮件在再平衡那一栏显示它。
    merged_shares_total = sum(float(p.get("merged_shares", 0.0) or 0.0)
                              for p in positions)
    merged_value_total = sum(float(p.get("merged_value", 0.0) or 0.0)
                             for p in positions)
    # ⚠️ D4: 「合计」的口径由 `CashPoolBreakdown` 定死 —— 合计市值**不含**
    # 网格的虚拟资金占用, 合计余额 = 真实现金 + 虚拟现金。记录里的
    # `cash_total` 从这里出, 于是页面/邮件/记录说的是同一个数。
    pools = CashPoolBreakdown(
        rebalance_market=etf_total,
        rebalance_balance=cash_rebalance,
        grid_market=grid_occupancy(grid_signals),
        grid_balance=grid_budget,
    )
    return {
        "date": today,
        "total": Money(total),
        "etf_value": Money(etf_total),
        "cash_rebalance": Money(cash_rebalance),
        # ⚠️ JSON 键仍然叫 `cash_grid` (committed artifact, 不改键名);
        #    领域里它是**虚拟预算** `grid_budget` (D3)。
        "cash_grid": Money(grid_budget),
        "cash_total": Money(pools.total_balance),
        "positions": positions,
        # ⚠️ `Weight` 只在**展示边界**构造 —— `evaluate` 内部算触发判定用的
        # 必须是未取整的权重: 取整到 4 位会移动触发边界, 让「差 0.0001pp
        # 不触发」变成「触发」。
        "weights": [Weight(w) for w in weights],
        "target_w": target_w,
        "threshold_pp": threshold_pp,
        "triggered": triggered_3pp,
        "plan": plan,
        "grid_signals": grid_signals,
        "grid_state": slim_grid_state,
        "merged_shares_total": ShareCount(merged_shares_total),
        "merged_value_total": Money(merged_value_total),
        "qdii_premiums": qdii_premiums or [],
    }


# -----------------------------------------------------------------------------
# 运行上下文 —— 一趟快照的「工作记忆」
# -----------------------------------------------------------------------------

def _empty_outcome() -> RebalanceOutcome:
    return RebalanceOutcome(total=0.0, weights=[], triggered=False, plan=[])


@dataclass
class SnapshotRun:
    """一次快照运行的上下文 —— BC-G 编排层的「工作记忆」。

    每个用例函数读它、写它。这**不是**「上帝对象」: 里面的字段全是这一步
    的输入或输出, 没有行为 (行为全在用例函数里), 也没有跨层依赖。

    ⚠️ 两个「基准」是**分开命名**的 (D9):
      - `layer_sizing_basis` — 给网格层额定尺的那次评估。它**不是**记录里
        的那个 total。
      - `snapshot_basis`    — 记录里 `total` / `weights` / `triggered` 的
        来源。

    以前这两段评估是 `main()` 里相隔几十行的两行代码 —— 「哪个 total 是
    记录里的那个」要靠数行数才答得上来。现在两个名字把答案写出来了。
    """

    today: str
    events: EventStream = field(default_factory=EventStream)

    # -- 输入 --
    raw_holdings: list[dict] = field(default_factory=list)
    grid_state: dict = field(default_factory=dict)

    # -- 中间结果 --
    positions: list[dict] = field(default_factory=list)
    cash_rebalance: float = 0.0
    #: 网格那一池的**虚拟预算** (D3) —— 领域里叫 grid_budget;
    #: 写进记录时 JSON 键仍是 `cash_grid`。
    grid_budget: float = 0.0
    qdii_top: list[dict] = field(default_factory=list)
    portfolio: Portfolio = field(default_factory=Portfolio)
    merged_by_code: dict[str, float] = field(default_factory=dict)
    extra_prices: dict[str, float] = field(default_factory=dict)
    layer_sizing_basis: RebalanceOutcome = field(default_factory=_empty_outcome)
    recent_min: float = 0.0
    new_grid_state: dict = field(default_factory=dict)
    grid_signals: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    # -- 输出 --
    snapshot_basis: RebalanceOutcome = field(default_factory=_empty_outcome)
    record: dict = field(default_factory=dict)
    any_signal: bool = False


# -----------------------------------------------------------------------------
# 用例 —— 每个函数做一件事: 读 SnapshotRun, 写 SnapshotRun
# -----------------------------------------------------------------------------

def load_universe(run: SnapshotRun) -> None:
    """读两份输入: holdings.json (代码清单) + grid_state.json (网格状态)。

    ⚠️ holdings.json 里的 shares 是**陈旧的** —— 这里只取**代码清单**;
    份额与现金的真相在账本 (trades.json) 里。
    """
    raw_holdings, _legacy_cash_rebalance, _legacy_grid_budget = read_holdings()
    run.raw_holdings = list(raw_holdings)
    run.grid_state = load_grid_state()


def replay_trade_ledger(run: SnapshotRun) -> None:
    """重放 trades.json → 真实持仓 + 两个现金池。

    账本是**份额与现金的唯一真相** (data/holdings.json 只是派生缓存)。
    """
    trades = load_trades()
    positions, cash_rebalance, grid_budget = project_ledger(
        trades, as_of_date=run.today, events=run.events,
    )
    run.positions = positions
    run.cash_rebalance = cash_rebalance
    run.grid_budget = grid_budget

    # 防御性兜底: 账本还空着 (scripts/init_trades_ledger.py 还没跑过)。
    # 没有这一步, 新用户第一次跑会直接退出 —— 那时还没有任何持仓。
    if not positions:
        if not run.raw_holdings:
            print("ERROR no positions (trades.json empty and holdings.json empty)",
                  file=sys.stderr)
            sys.exit(1)
        run.positions = [
            {"name": h["name"], "code": h["code"], "shares": h["shares"]}
            for h in run.raw_holdings
        ]
        print("WARN trades.json empty; falling back to holdings.json shares",
              file=sys.stderr)


def value_holdings(run: SnapshotRun) -> None:
    """给每个持仓定价 —— 价格来源是**注入**的 (领域层不认识 sina/eastmoney)。

    全部取不到价 → 直接退出: 没有价的组合算不出权重, 记出来的快照没有意义。
    """
    positions, _etf_total, errors = value_positions(
        run.positions, latest_for_code, events=run.events)
    for code, msg in errors:
        print(f"WARN {code}: {msg}", file=sys.stderr)
    if not positions:
        print("ERROR all fetches failed", file=sys.stderr)
        sys.exit(1)
    run.positions = positions
    _report_prices(positions)


def attach_qdii_premiums(run: SnapshotRun) -> None:
    """挂上 QDII 溢价 —— best-effort, 失败不中断这一趟。"""
    run.qdii_top = _enrich_qdii_premiums(run.positions)


def absorb_merged_shares(run: SnapshotRun, grid_state: dict) -> None:
    """把网格宣告的归并份额**吸收进组合** (BC-C → BC-B)。

    网格只**宣告** (`MergedSharesTransferred`), 组合自己决定怎么吸收 ——
    记 `Provenance.SIMULATED_GRID`。这些份额从此刻起是组合的持仓, 必须进
    市值、进 3pp 权重。

    ⚠️ 「组合没持有、但有归并份额」的代码也要定价 —— 它是真的持有, 只是
    没有持仓行可以挂价格。
    """
    transfers = grid_mod.merged_share_transfers(grid_state)
    run.events.extend(transfers)
    merged = merged_by_code_from_transfers(transfers)
    run.merged_by_code = merged
    held_codes = {p["code"] for p in run.positions}
    extra_codes = [c for c in merged if c not in held_codes]
    extra_prices, extra_errors = value_extra_codes(extra_codes, latest_for_code)
    for code, msg in extra_errors:
        print(f"WARN merged-share price {code}: {msg}", file=sys.stderr)
    run.extra_prices = extra_prices
    run.portfolio = Portfolio.from_holdings(
        run.positions, merged_by_code=merged, extra_prices=extra_prices,
        cash_rebalance=run.cash_rebalance, grid_budget=run.grid_budget,
    )


def size_grid_layer(run: SnapshotRun) -> None:
    """算网格层额 —— 用 `layer_sizing_basis` 那次评估的 total 当池子。

    池子 = history 里每天的 total + 今天这个 total; 取最近
    `floating_window` 天的**最小值**当 `recent_min`。层额由
    `LayerSizingPolicy` 算 (BASE + max(pool − recent_min, 0) × 5%)。
    """
    history = load_history()
    pool = pd.Series(
        [r["total"] for r in history if "total" in r]
        + [run.layer_sizing_basis.total],
        index=pd.to_datetime(
            [r["date"] for r in history if "total" in r] + [run.today]
        ),
    ).sort_index()
    window = APP_CONFIG.grid.floating_window + 1
    run.recent_min = float(pool.tail(window).min()) if len(pool) else 0.0
    run.history = history


def advance_grids(run: SnapshotRun) -> None:
    """推进网格状态 + 生成建议 —— **引擎不下单**。

    今日用 **K 线最低价** (盘中低点) 当触发输入, 所以盘中跌破也能触发;
    取不到就退回实时收盘价。

    ⚠️ 源是 `portfolio.to_records()` 而不是 `run.positions` —— 它还带着
    「网格移交了归并份额、但组合没持有」的那些行, 网格照样要在它们上面推进。
    """
    positions_eff = run.portfolio.to_records()
    prices_by_name = {p["name"]: p["price"] for p in positions_eff
                      if p["name"] in ASSET_NAMES}
    lows_by_name = _intraday_lows(positions_eff, run.today)
    # If K-line fetch failed for an asset, fall back to the realtime close
    grid_prices = {n: lows_by_name.get(n, prices_by_name.get(n))
                   for n in ASSET_NAMES}
    new_state, grid_signals = advance_all_grids(
        run.grid_state, run.today, grid_prices,
        run.layer_sizing_basis.total, run.recent_min,
        config=APP_CONFIG.grid,
    )
    # Annotate signals with the (intraday) low that drove the decision so
    # the daily record shows the actual trigger price, not the close.
    for s in grid_signals:
        if s["name"] in lows_by_name:
            s["intraday_low"] = Price(lows_by_name[s["name"]])
    run.new_grid_state = new_state
    run.grid_signals = grid_signals
    save_grid_state(new_state)


def judge_rebalance(run: SnapshotRun) -> None:
    """评估 3pp —— `snapshot_basis` 是记录里 total/weights/triggered 的来源。

    ⚠️ 只给**这一次**发事件 —— 前面那次 (`layer_sizing_basis`) 只是给层额
    定尺用的中间量, 它触发了也不该报「要调仓」。
    """
    outcome = rebalance_policy().evaluate(run.portfolio, events=run.events)
    run.snapshot_basis = outcome
    run.positions = run.portfolio.to_records()


def persist_snapshot(run: SnapshotRun) -> None:
    """写 data/history.json —— 今天那条记录。

    同一天重跑会**替换**当天那条 (一天只有一条记录), 不是追加。
    """
    record = build_record(
        today=run.today, positions=run.positions,
        cash_rebalance=run.cash_rebalance, grid_budget=run.grid_budget,
        total=run.snapshot_basis.total, weights=run.snapshot_basis.weights,
        triggered_3pp=run.snapshot_basis.triggered,
        plan=run.snapshot_basis.plan,
        grid_signals=run.grid_signals, grid_state=run.new_grid_state,
        qdii_premiums=run.qdii_top,
    )
    run.record = record
    history = [r for r in run.history if r["date"] != run.today]
    history.append(record)
    run.history = history
    save_history(history)


def render_snapshot_chart(run: SnapshotRun) -> None:
    """画 data/grid_chart.png —— best-effort。

    画图失败不能把已经存好的记录搞丢, 所以只警告不退出。
    """
    try:
        from ..infrastructure.chart_renderer import render_chart
        render_chart(run.history, GRID_CHART)
    except Exception as ex:
        print(f"WARN chart render failed: {ex}", file=sys.stderr)


def report_snapshot(run: SnapshotRun) -> None:
    """打印人看的摘要 (stdout)。"""
    total = run.snapshot_basis.total
    weights = run.snapshot_basis.weights
    triggered_3pp = run.snapshot_basis.triggered
    positions = run.positions
    any_signal = triggered_3pp or any(s["signal"] for s in run.grid_signals)
    run.any_signal = any_signal
    dyn_target = 1.0 / len(positions) if positions else 1.0 / 3
    print(f"DATE={run.today} TOTAL={total:.2f} STATUS={'TRIGGER' if any_signal else 'ok'}")
    print(f"  3pp: {'⚠ TRIGGERED' if triggered_3pp else 'ok'} (max Δ {max(abs(w - dyn_target) * 100 for w in weights):.2f}pp)")
    for p, w in zip(positions, weights):
        delta = (w - dyn_target) * 100
        flag = " ⚠" if abs(delta) > THRESHOLD_PP else ""
        # D6: 真实持仓与归并**分开报** —— 页面/邮件也是这个口径 (见
        # src/presentation/digest.py)。日志里能直接看出哪部分是虚拟的。
        shares_tag = f"  真实 {float(p.get('shares', 0.0)):,.0f}份"
        if p.get("merged_shares"):
            shares_tag += f" + 归并 {p['merged_shares']:,.0f}份"
        print(f"    {p['name']:<14} {w:>6.2%}  Δ={delta:+.2f}pp{flag}{shares_tag}")
    print(f"  grid layer: ¥{LayerSizingPolicy.from_config(APP_CONFIG.grid).layer_yuan(total, run.recent_min):,.0f} "
          f"(pool ¥{total:,.0f}, recent_min ¥{run.recent_min:,.0f})")
    for s in run.grid_signals:
        # ⚠️ 引擎只给建议, 不会自动下单 —— tag 是"建议提交哪种 op"
        tag = f"建议{s['signal']}" if s["signal"] else "-"
        print(f"    {s['name']:<14} {tag:<8}  layer=¥{s['layer_yuan']:,.0f}  "
              f"in_cycle={s['in_cycle']}  buys={s['num_buys']}  "
              f"unreal={s['unrealized_pnl']:+,.0f}  "
              f"占用=¥{s['current_value']:,.0f}  "
              f"已归并={s['merged_shares']:,.0f}份(¥{s['merged_value']:,.0f})  "
              f"距{s['trigger_kind']}={s['trigger_pct']:+.2f}%")


def emit_step_outputs(run: SnapshotRun) -> None:
    """写 $GITHUB_OUTPUT —— date / total / trigger 给下游 step 用。"""
    write_github_output(run.today, run.snapshot_basis.total, run.any_signal)


# -----------------------------------------------------------------------------
# Internal helpers (报告 / 取价 —— 不是用例)
# -----------------------------------------------------------------------------

def _report_prices(positions: list[dict]) -> None:
    """Print one line per priced position (stderr) — the run's audit trail."""
    today = datetime.date.today()
    for p in positions:
        try:
            as_of = datetime.date.fromisoformat(p["date"])
        except (ValueError, TypeError):
            as_of = today
        age_days = (today - as_of).days
        print(f"  {p['name']:<14} ({p['code']}) ¥{p['price']:>9.4f}  "
              f"as_of {as_of} (Δ {age_days:+d}d)", file=sys.stderr)


def _enrich_qdii_premiums(positions: list[dict]) -> list[dict]:
    """Attach the QDII premium to held positions; return the watchlist rows.

    Best-effort: a failing premium fetch never aborts the run.
    """
    for p in positions:
        if p["code"] in QDII_CODES:
            try:
                prem = fetch_qdii_premium(p["code"])
                if prem:
                    p["premium"] = prem   # {price, iopv, premium_pct, nav_date, ...}
                    flag = "⚠" if abs(prem["premium_pct"]) > 5 else "·"
                    print(
                        f"  QDII {p['name']:<14} premium={prem['premium_pct']:+.2f}%  "
                        f"(市价 {prem['price']:.4f} / IOPV {prem['iopv']:.4f}@{prem['nav_date']})  {flag}",
                        file=sys.stderr,
                    )
            except Exception as ex:
                print(f"WARN QDII premium {p['code']} failed: {ex}", file=sys.stderr)

    # Also fetch premium for QDII codes known to ETF_DEFS but NOT held today —
    # so the email can still warn ("don't buy, premium too high").
    qdii_top: list[dict] = []
    held_codes = {p["code"] for p in positions}
    for code, (_sina, _em, name, is_qdii) in ETF_DEFS.items():
        if not is_qdii or code in held_codes:
            continue
        try:
            prem = fetch_qdii_premium(code)
            if prem:
                qdii_top.append({
                    "code": code, "name": name, "in_portfolio": False,
                    **prem,
                })
                flag = "⚠" if abs(prem["premium_pct"]) > 5 else "·"
                print(
                    f"  QDII {name:<14} (未持仓) premium={prem['premium_pct']:+.2f}%  "
                    f"(市价 {prem['price']:.4f} / IOPV {prem['iopv']:.4f}@{prem['nav_date']})  {flag}",
                    file=sys.stderr,
                )
        except Exception as ex:
            print(f"WARN QDII unheld premium {code} failed: {ex}", file=sys.stderr)
    return qdii_top


def _intraday_lows(positions: list[dict], today: str) -> dict[str, float]:
    """Today's K-line low per asset — the grid's trigger input.

    The low (not the close) is what lets the trigger fire on an intraday
    dip.  If the K-line fetch failed, the caller falls back to the
    realtime close.
    """
    lows_by_name: dict[str, float] = {}
    for p in positions:
        if p["name"] not in ASSET_NAMES:
            continue
        kf = fetch_kline_history(p["code"], start=today, end=today)
        if kf is not None and not kf.empty:
            lows_by_name[p["name"]] = float(kf["low"].iloc[-1])
    return lows_by_name


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    """跑一趟每日快照 —— 一串用例, 按顺序。

    每一行都是**一个具名用例**; 规则一条都不在这里 (全在 `src/domain/`)。
    """
    run = SnapshotRun(today=datetime.date.today().isoformat())

    # 1) 读输入: 代码清单 (holdings.json) + 网格状态 (grid_state.json)
    load_universe(run)

    # 2) 账本重放 → 真实持仓 + 两个现金池
    replay_trade_ledger(run)

    # 3) 定价 + QDII 溢价
    value_holdings(run)
    attach_qdii_premiums(run)

    # 4) 网格 → 组合: 吸收归并份额 (推进网格**之前**的口径)
    absorb_merged_shares(run, run.grid_state)
    #    这第一次评估只用来给层额定尺 —— 它**不是**记录里的那个 total。
    run.layer_sizing_basis = rebalance_policy().evaluate(run.portfolio)

    # 5) 网格: 层额定尺 → 推进状态 + 出建议 (**不下单**)
    size_grid_layer(run)
    advance_grids(run)

    # 6) 再吸收一次 (推进**之后**的口径)。
    #    ⚠️ 引擎不再自动交易, 所以 `merged_shares` 在这一趟里不会变 ——
    #    两次吸收必然得到同一个组合。结构留着是因为手动路径
    #    (scripts/apply_grid.py) 确实会改归并份额, 而每日记录必须看到
    #    用户**将要持有**的那个组合。
    absorb_merged_shares(run, run.new_grid_state)

    # 7) 评估 3pp —— 这次才是记录里 total / weights / triggered 的来源
    judge_rebalance(run)

    # 8) 落盘 + 画图 + 报告 + step outputs
    persist_snapshot(run)
    render_snapshot_chart(run)
    report_snapshot(run)
    emit_step_outputs(run)

    # 收尾事件 —— 「今天这一趟到底发生了什么」。
    run.events.record(DailySnapshotProduced(
        date=run.today, total=run.snapshot_basis.total,
        weights=list(run.snapshot_basis.weights),
        triggered=run.snapshot_basis.triggered,
        signals=[str(s["signal"] or "") for s in run.grid_signals],
    ))
    print(f"  events: {run.events.summary_line()}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
