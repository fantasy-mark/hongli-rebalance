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
"""
from __future__ import annotations

import datetime
import os
import sys
import traceback

import pandas as pd

from ..domain.grid import grid_position as grid_mod
from ..domain.ledger.ledger import rebuild_holdings_from_trades
from ..domain.portfolio.asset_universe import ASSET_NAMES, ETF_DEFS, QDII_CODES
from ..domain.portfolio.portfolio import (
    Portfolio,
    merged_shares_by_code,
)
from ..domain.portfolio.rebalance_policy import (
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
    GRID_STATE,
    HISTORY,
    HOLDINGS,
    TRADES,
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

# 3pp threshold — explicit here, passed down to the policy.
# (The env var is the legacy way to override it; the domain no longer
#  reads the environment itself.)
THRESHOLD_PP = float(os.environ.get("THRESHOLD_PP", "3.0"))

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
                      pool_value: float, recent_min: float) -> tuple[dict, list[dict]]:
    """Advance grid state for all assets, returning (new_state, signals).

    ⚠️ 引擎**不再自动交易** —— 这里只是滚动 120 日窗口 + 算出「建议」。
    `signal` 是**建议提交哪种 op** (open/add/tp), 不是"已成交"。
    """
    layer = grid_mod.layer_yuan(pool_value, recent_min)
    new_state: dict = {}
    signals: list[dict] = []
    for name in ASSET_NAMES:
        asset_state = state.get(name) or grid_mod.fresh_asset_state()
        price = prices_by_name.get(name)
        if price is None:
            new_state[name] = asset_state
            continue
        # 建议基于**追加今天之前**的状态算 —— 今天的低价不能自己当门槛
        adv = grid_mod.advisory(asset_state, float(price), layer)
        new_asset_state, signal = grid_mod.advance_one_day(
            asset_state, today, float(price), layer,
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
                 cash_rebalance: float, cash_grid: float,
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
    return {
        "date": today,
        "total": Money(total),
        "etf_value": Money(etf_total),
        "cash_rebalance": Money(cash_rebalance),
        "cash_grid": Money(cash_grid),
        "cash_total": Money(cash_rebalance + cash_grid),
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
# Use-case steps
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
    today = datetime.date.today().isoformat()

    # 1) Universe of codes = holdings.json (cheap registry of what the user
    #    wants tracked).  Shares here are STALE — we only use the code list.
    raw_holdings, _legacy_cash_rebalance, _legacy_cash_grid = read_holdings()

    # 2) Real positions + cash from the trade ledger.  This is the
    #    canonical source for shares/cash in the daily snapshot.
    trades = load_trades()
    positions, cash_rebalance, cash_grid = rebuild_holdings_from_trades(
        trades, as_of_date=today,
    )

    # 3) Defensive fallback: empty trades (the bootstrap moment before
    #    scripts/init_trades_ledger.py has been run).  In that case fall
    #    back to whatever shares are in holdings.json so the snapshot
    #    still produces a meaningful daily record.
    if not positions:
        if not raw_holdings:
            print("ERROR no positions (trades.json empty and holdings.json empty)",
                  file=sys.stderr)
            sys.exit(1)
        positions = [
            {"name": h["name"], "code": h["code"], "shares": h["shares"]}
            for h in raw_holdings
        ]
        print("WARN trades.json empty; falling back to holdings.json shares",
              file=sys.stderr)

    positions, etf_total, errors = value_positions(positions, latest_for_code)
    for code, msg in errors:
        print(f"WARN {code}: {msg}", file=sys.stderr)
    if not positions:
        print("ERROR all fetches failed", file=sys.stderr)
        sys.exit(1)
    _report_prices(positions)

    # QDII premium rate: enrich position dicts with premium info
    # (best-effort: failure does not abort the run)
    qdii_top = _enrich_qdii_premiums(positions)

    # --- Grid-merged shares → portfolio -------------------------------
    # The grid's take-profit hands its leftover shares to the rebalance
    # portfolio.  Those shares are portfolio holdings from then on, so they
    # have to be in the value the 3pp weights are computed on.
    #
    # Two passes: the pre-merge value sizes today's grid layer, the
    # post-merge value is what the record reports. Since the engine no
    # longer trades, `merged_shares` can't change during a run — so the
    # two totals are now always equal. The structure is kept because the
    # manual path (apply_grid.py) *does* change merged shares, and the
    # daily record must see the portfolio the user will actually hold.
    state = load_grid_state()
    merged_before = merged_shares_by_code(state)
    held_codes = {p["code"] for p in positions}
    extra_codes = [c for c in merged_before if c not in held_codes]
    extra_prices, extra_errors = value_extra_codes(extra_codes, latest_for_code)
    for code, msg in extra_errors:
        print(f"WARN merged-share price {code}: {msg}", file=sys.stderr)
    portfolio = Portfolio.from_holdings(
        positions, merged_by_code=merged_before, extra_prices=extra_prices,
        cash_rebalance=cash_rebalance, cash_grid=cash_grid,
    )
    total_pre, _w_pre, _t_pre, _p_pre = rebalance_policy().evaluate(
        portfolio,
    ).as_tuple()
    positions_eff = portfolio.to_records()

    # 3pp pool for grid layer sizing (history.totals + today)
    history = load_history()
    pool = pd.Series(
        [r["total"] for r in history if "total" in r] + [total_pre],
        index=pd.to_datetime(
            [r["date"] for r in history if "total" in r] + [today]
        ),
    ).sort_index()
    window = grid_mod.GRID_FLOATING_WINDOW + 1
    recent_min = float(pool.tail(window).min()) if len(pool) else 0.0

    # Advance grid
    # Today uses the K-line LOW (intraday low) so the trigger can fire on
    # intraday dips even if the close is above yesterday's close.
    #
    # `positions_eff` (not `positions`) is the source: it also carries the
    # codes the grid tracks but the portfolio doesn't hold — those rows exist
    # only because the grid merged shares into them, and the grid still has
    # to advance on them.
    prices_by_name = {p["name"]: p["price"] for p in positions_eff
                      if p["name"] in ASSET_NAMES}
    lows_by_name = _intraday_lows(positions_eff, today)
    # If K-line fetch failed for an asset, fall back to the realtime close
    grid_prices = {n: lows_by_name.get(n, prices_by_name.get(n)) for n in ASSET_NAMES}
    new_state, grid_signals = advance_all_grids(
        state, today, grid_prices, total_pre, recent_min,
    )
    # Annotate signals with the (intraday) low that drove the decision so
    # the daily record shows the actual trigger price, not the close.
    for s in grid_signals:
        if s["name"] in lows_by_name:
            s["intraday_low"] = Price(lows_by_name[s["name"]])
    save_grid_state(new_state)

    # Re-merge: today's take-profit may have added new merged shares.
    merged_after = merged_shares_by_code(new_state)
    extra_codes = [c for c in merged_after if c not in held_codes]
    if extra_codes:
        extra_prices, extra_errors = value_extra_codes(extra_codes, latest_for_code)
        for code, msg in extra_errors:
            print(f"WARN merged-share price {code}: {msg}", file=sys.stderr)
    portfolio = Portfolio.from_holdings(
        positions, merged_by_code=merged_after, extra_prices=extra_prices,
        cash_rebalance=cash_rebalance, cash_grid=cash_grid,
    )
    outcome = rebalance_policy().evaluate(portfolio)
    total, weights, triggered_3pp, plan = outcome.as_tuple()
    positions_eff = portfolio.to_records()
    positions = positions_eff

    # Persist record
    record = build_record(
        today=today, positions=positions,
        cash_rebalance=cash_rebalance, cash_grid=cash_grid,
        total=total, weights=weights,
        triggered_3pp=triggered_3pp, plan=plan,
        grid_signals=grid_signals, grid_state=new_state,
        qdii_premiums=qdii_top,
    )
    history = [r for r in history if r["date"] != today]
    history.append(record)
    save_history(history)

    # Render chart (best-effort: chart may fail if network blocked; record is already saved)
    try:
        from ..infrastructure.chart_renderer import render_chart
        render_chart(history, GRID_CHART)
    except Exception as ex:
        print(f"WARN chart render failed: {ex}", file=sys.stderr)

    any_signal = triggered_3pp or any(s["signal"] for s in grid_signals)
    dyn_target = 1.0 / len(positions) if positions else 1.0 / 3
    print(f"DATE={today} TOTAL={total:.2f} STATUS={'TRIGGER' if any_signal else 'ok'}")
    print(f"  3pp: {'⚠ TRIGGERED' if triggered_3pp else 'ok'} (max Δ {max(abs(w - dyn_target) * 100 for w in weights):.2f}pp)")
    for p, w in zip(positions, weights):
        delta = (w - dyn_target) * 100
        flag = " ⚠" if abs(delta) > THRESHOLD_PP else ""
        merged_tag = (f"  (含归并 {p['merged_shares']:,.0f}份)"
                      if p.get("merged_shares") else "")
        print(f"    {p['name']:<14} {w:>6.2%}  Δ={delta:+.2f}pp{flag}{merged_tag}")
    print(f"  grid layer: ¥{grid_mod.layer_yuan(total, recent_min):,.0f} "
          f"(pool ¥{total:,.0f}, recent_min ¥{recent_min:,.0f})")
    for s in grid_signals:
        # ⚠️ 引擎只给建议, 不会自动下单 —— tag 是"建议提交哪种 op"
        tag = f"建议{s['signal']}" if s["signal"] else "-"
        print(f"    {s['name']:<14} {tag:<8}  layer=¥{s['layer_yuan']:,.0f}  "
              f"in_cycle={s['in_cycle']}  buys={s['num_buys']}  "
              f"unreal={s['unrealized_pnl']:+,.0f}  "
              f"占用=¥{s['current_value']:,.0f}  "
              f"已归并={s['merged_shares']:,.0f}份(¥{s['merged_value']:,.0f})  "
              f"距{s['trigger_kind']}={s['trigger_pct']:+.2f}%")

    write_github_output(today, total, any_signal)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
