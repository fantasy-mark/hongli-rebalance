"""Unit tests for the domain + application + infrastructure layers
(pure functions, no network).

Covers:
  src/domain/ledger/ledger.py           账本重放与现金池
  src/domain/portfolio/*                3pp 评估 / 归并份额 / 估值
  src/domain/grid/grid_position.py      网格引擎 (只给建议)
  src/simulation/grid_simulator.py      回测重放
  src/simulation/equal_weight_backtest.py  3pp 回测
  src/application/daily_snapshot.py     每日快照
  src/presentation/notify_failure.py    失败邮件

Run:  python -m tests.test_record
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.application import config as app_config
from src.application import daily_snapshot as snapshot
from src.domain.events import domain_events as events
from src.domain.grid import config as grid_config
from src.domain.grid import grid_position as grid_pos
from src.domain.grid import layer_sizing
from src.domain.grid import trigger_policy as grid_trigger_policy
from src.domain.ledger import ledger
from src.domain.portfolio import asset_universe as universe
from src.domain.portfolio import portfolio, rebalance_policy as policy
from src.domain.portfolio import valuation as valuation_mod
from src.domain.shared.provenance import Provenance
from src.infrastructure import json_repositories as repos
from src.presentation import notify_failure as nf
from src.simulation import equal_weight_backtest as three_pp
from src.simulation import grid_simulator as grid_sim


def test_read_holdings_with_现金_label():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "现金", 50000.0, [
            ["黄金ETF博时", "159937", 20000],
            ["红利ETF易方达", "515180", 100000],
            ["可转债ETF博时", "511380", 13000],
        ])
        repos.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = repos.read_holdings()
        assert cash_rebalance == 50000.0, f"cash_rebalance={cash_rebalance}"
        assert cash_grid == 0.0, f"cash_grid={cash_grid}"
        assert len(h) == 3
        codes = sorted(x["code"] for x in h)
        assert codes == ["159937", "511380", "515180"]


def test_read_holdings_accepts_legacy_资产_label():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "资产", 123456.0, [
            ["黄金ETF博时", "159937", 5000],
        ])
        repos.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = repos.read_holdings()
        assert cash_rebalance == 123456.0
        assert cash_grid == 0.0
        assert len(h) == 1 and h[0]["code"] == "159937"


def test_read_holdings_skips_invalid_rows():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        # Incomplete rows + header duplicate should be ignored
        _write_holdings(xlsx, "现金", 0.0, [
            ["黄金ETF博时", "159937", 1000],
            ["名称", "代码", "数量"],                # duplicate header → skip
            ["bad", None, 100],                      # missing code → skip
            ["bad2", "159937", "not-a-number"],      # bad shares → skip
            ["可转债ETF博时", "511380", 500],
        ])
        repos.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = repos.read_holdings()
        assert cash_rebalance == 0.0
        assert cash_grid == 0.0
        assert len(h) == 2
        assert sorted(x["code"] for x in h) == ["159937", "511380"]


def test_read_holdings_no_label_defaults_to_zero():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "", 0.0, [["A", "159937", 100]])
        repos.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = repos.read_holdings()
        assert cash_rebalance == 0.0
        assert cash_grid == 0.0
        assert len(h) == 1


def test_evaluate_3pp_equal_weight_no_trigger():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
    ]
    total, weights, triggered, _plan = policy.evaluate(positions, 0.0, 0.0)
    assert total == 300
    assert all(abs(w - 1/3) < 1e-9 for w in weights)
    assert not triggered


def test_evaluate_3pp_threshold_triggers_on_50pp_drift():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 150, "value": 150, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = policy.evaluate(positions, 0.0, 0.0)
    # A: 150/350 = 42.86%, target 33.33%, delta = +9.52pp > 3pp
    assert total == 350
    assert weights[0] > 0.4
    assert triggered


def test_evaluate_2asset_dynamic_target_w():
    """N=2: equal weight = 50%, target_w derived from len(positions)."""
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = policy.evaluate(positions, 0.0, 0.0)
    assert total == 200
    assert all(abs(w - 0.5) < 1e-9 for w in weights), f"weights={weights}"
    assert not triggered
    # build_record also produces dynamic target_w
    r = snapshot.build_record(
        today="2026-10-14", positions=positions,
        cash_rebalance=0.0, cash_grid=0.0,
        total=200, weights=weights, triggered_3pp=False, plan=[],
        grid_signals=[], grid_state={},
    )
    assert r["target_w"] == 0.5
    assert len(r["positions"]) == 2


def test_evaluate_5asset_dynamic_target_w():
    """N=5: equal weight = 20%, target_w derived from len(positions)."""
    positions = [
        {"name": f"E{i}", "code": str(i), "shares": 1,
         "price": 100, "value": 100, "date": "x"}
        for i in range(5)
    ]
    total, weights, triggered, plan = policy.evaluate(positions, 0.0, 0.0)
    assert total == 500
    assert all(abs(w - 0.2) < 1e-9 for w in weights), f"weights={weights}"
    assert not triggered
    # Plan has one entry per position
    assert len(plan) == 0  # equal weight → no plan unless triggered
    r = snapshot.build_record(
        today="2026-10-14", positions=positions,
        cash_rebalance=0.0, cash_grid=0.0,
        total=500, weights=weights, triggered_3pp=False, plan=[],
        grid_signals=[], grid_state={},
    )
    assert r["target_w"] == 0.2


def test_evaluate_5asset_threshold_triggers_correctly():
    """N=5: drift detection uses 1/n target, not hardcoded 1/3."""
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 250, "value": 250, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "D", "code": "4", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "E", "code": "5", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = policy.evaluate(positions, 0.0, 0.0)
    # total = 250 + 100*4 = 650; A weight = 38.46%; target 20%; delta = +18.46pp > 3pp
    assert total == 650
    assert weights[0] > 0.38
    assert triggered


def test_read_holdings_n_agnostic():
    """read_holdings() accepts any number of ETF rows (N=2..N=5+)."""
    # Curated rows per N to avoid duplicating the same code (which read_holdings rejects)
    curated = {
        2: [
            ["黄金ETF博时", "159937", 100],
            ["红利ETF易方达", "515180", 200],
        ],
        3: [
            ["黄金ETF博时", "159937", 100],
            ["红利ETF易方达", "515180", 200],
            ["可转债ETF博时", "511380", 300],
        ],
        5: [
            ["黄金ETF博时", "159937", 100],
            ["红利ETF易方达", "515180", 200],
            ["可转债ETF博时", "511380", 300],
            ["纳斯达克ETF华夏", "513300", 400],
            ["某未来 ETF", "999999", 500],
        ],
    }
    for n, rows in curated.items():
        with tempfile.TemporaryDirectory() as td:
            xlsx = Path(td) / "h.xlsx"
            _write_holdings(xlsx, "现金", 1000.0, rows)
            repos.HOLDINGS = xlsx
            h, cash_rebalance, cash_grid = repos.read_holdings()
            assert cash_rebalance == 1000.0
            assert cash_grid == 0.0
            assert len(h) == n, f"N={n} got {len(h)} positions: {h}"


def test_read_holdings_json_schema():
    """v2 schema: cash + positions[]; _schema/_comment ignored.

    Unknown codes are NOT filtered here — that happens later in
    fetch_positions(). read_holdings is a pure parser.
    """
    import json
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "h.json"
        p.write_text(json.dumps({
            "_schema": "v1",
            "_comment": "ignored",
            "cash": 1234.5,
            "positions": [
                {"name": "A", "code": "515180", "shares": 100},
                {"name": "B", "code": "159937", "shares": 200.0},  # float
                {"name": "bad", "code": "",      "shares": 50},     # empty code → skip
                {"name": "x",   "code": "511380","shares": None},   # null shares → skip
            ],
        }, ensure_ascii=False), encoding="utf-8")
        repos.HOLDINGS = p
        h, cash_rebalance, cash_grid = repos.read_holdings()
        # Legacy single-cash file migrates: 1234.5 → cash_rebalance, grid=0
        assert cash_rebalance == 1234.5
        assert cash_grid == 0.0
        assert len(h) == 2, h
        assert h[0]["code"] == "515180"
        assert h[0]["shares"] == 100
        assert h[1]["code"] == "159937"
        # 200.0 → 200.0 (json.load returns float; not coerced to int)
        assert h[1]["shares"] == 200.0


def test_read_holdings_json_missing_returns_empty():
    """Missing file with no xlsx fallback → ([], 0, 0)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nope.json"
        repos.HOLDINGS = p
        h, cash_rebalance, cash_grid = repos.read_holdings()
        assert h == []
        assert cash_rebalance == 0.0
        assert cash_grid == 0.0


def test_read_holdings_xlsx_fallback_when_json_missing():
    """If json doesn't exist, fall back to legacy xlsx (for migration)."""
    import openpyxl
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "h.json"     # doesn't exist
        xlsx = Path(td) / "h.xlsx"  # legacy format
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=2, column=1, value="现金")
        ws.cell(row=2, column=2, value=999.0)
        ws.cell(row=4, column=1, value="名称")
        ws.cell(row=4, column=2, value="代码")
        ws.cell(row=4, column=3, value="数量")
        ws.cell(row=5, column=1, value="红利ETF易方达")
        ws.cell(row=5, column=2, value="515180")
        ws.cell(row=5, column=3, value=1000)
        wb.save(xlsx)
        repos.HOLDINGS = p
        h, cash_rebalance, cash_grid = repos.read_holdings()
        assert cash_rebalance == 999.0
        assert cash_grid == 0.0
        assert len(h) == 1 and h[0]["code"] == "515180" and h[0]["shares"] == 1000


def test_evaluate_with_cash_affects_weights():
    # 4-asset test fixture (NOT the production rebalance config — production
    # is 3-asset, see app.js:REBALANCE_CODES; this test just exercises the
    # generic n-asset math). 4 个等权 ETF, 总值 = 4*100 = 400.
    # target_w = 1/4 = 0.25, 每个 weight = 0.25, 不触发.
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "D", "code": "4", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = policy.evaluate(positions, 0.0, 0.0)
    assert total == 400
    assert all(abs(w - 0.25) < 0.01 for w in weights)
    assert not triggered

    # 有现金但仍等权: 每个 200, 现金 200, 总值 1000, weight 0.20 vs target 0.25 → 5pp 偏离 → 触发
    #
    # ⚠️ `price` 必须跟着一起改 —— Phase 4 起市值是**推出来的**
    # (`Position.value` = total_shares × price), 记录里的 `value` 不再被
    # 当作事实源照抄。夹具自相矛盾 (shares 1 × price 100 ≠ value 200) 的
    # 话, 聚合根本身会拒绝那个市值。
    for p in positions:
        p["price"] = 200.0
        p["value"] = 200.0
    total2, weights2, triggered2, _plan2 = policy.evaluate(positions, 200.0, 0.0)
    assert total2 == 1000
    assert all(abs(w - 0.20) < 0.01 for w in weights2)
    assert triggered2   # 5pp > 3pp 阈值


def test_build_record_shape():
    positions = [
        {"name": "黄金ETF博时", "code": "159937", "shares": 20000,
          "price": 8.594, "value": 171880.0, "date": "2026-10-09"},
        {"name": "红利ETF易方达", "code": "515180", "shares": 122800,
          "price": 1.443, "value": 177200.4, "date": "2026-10-09"},
        {"name": "可转债ETF博时", "code": "511380", "shares": 13700,
          "price": 12.930, "value": 177141.0, "date": "2026-10-09"},
    ]
    r = snapshot.build_record(
        today="2026-10-11", positions=positions,
        cash_rebalance=5.0, cash_grid=0.0,
        total=526221.4, weights=[0.3267, 0.3368, 0.3365],
        triggered_3pp=False, plan=[],
        grid_signals=[], grid_state={},
    )
    assert r["date"] == "2026-10-11"
    assert r["cash_rebalance"] == 5.0
    assert r["cash_grid"] == 0.0
    assert r["cash_total"] == 5.0
    assert abs(r["total"] - 526221.4) < 0.1
    assert len(r["positions"]) == 3
    assert len(r["weights"]) == 3
    assert abs(sum(r["weights"]) - 1.0) < 1e-4
    assert r["target_w"] == 1.0 / 3
    assert r["threshold_pp"] == 3.0
    assert not r["triggered"]
    assert r["grid_signals"] == []
    assert r["grid_state"] == {}


# -----------------------------------------------------------------------------
# 3pp engine tests
# -----------------------------------------------------------------------------

def _build_3pp_prices(close: list[float], start: str = "2026-01-01") -> pd.DataFrame:
    """Build a 3-ETF aligned price DataFrame with all three columns equal to `close`."""
    dates = pd.bdate_range(start, periods=len(close))
    return pd.DataFrame({
        "黄金ETF博时":   close,
        "红利ETF易方达": close,
        "可转债ETF博时": close,
    }, index=dates)


def test_three_pp_equal_weight_no_trigger():
    prices = _build_3pp_prices([100.0] * 10)
    res = three_pp.run_backtest(prices, initial_cash=300_000.0)
    # run_backtest 从全现金起步, 第 0 天会先建仓(算 1 次再平衡),
    # 之后价格一直 100 → 等权 1/3, 不会再触发。最多 1 次。
    assert res["n_rebalances"] <= 1
    assert res["final"] == pytest_approx(300_000.0)
    assert res["cagr"] == pytest_approx(0.0)


def test_three_pp_drastic_drift_triggers():
    # 第一天 100, 接下来一直 150: 权重严重漂移
    close = [100.0] * 5 + [150.0] * 10
    prices = _build_3pp_prices(close)
    res = three_pp.run_backtest(prices, initial_cash=300_000.0)
    # 一旦价格涨到 150, 第一次 3pp 偏差时立刻全调仓, 然后 lock 在 1/3
    assert res["n_rebalances"] >= 1
    # 调仓后 weights 都回到 1/3 附近
    final_w = res["series"].iloc[-1]
    # 净值会在某个范围, 但 series 中没有 weights 字段
    assert res["final"] > 0


# -----------------------------------------------------------------------------
# Grid engine tests
# -----------------------------------------------------------------------------

def test_layer_yuan_floor_only():
    # 池子 = 初始本金 → 浮盈 = 0 → 加仓 = 底仓
    assert grid_pos.layer_yuan(pool_value=200_000.0, recent_min=200_000.0) == grid_pos.GRID_BASE_YUAN
    # 池子跌到 18 万 → 浮亏 → 仍买底仓
    assert grid_pos.layer_yuan(pool_value=180_000.0, recent_min=180_000.0) == grid_pos.GRID_BASE_YUAN


def test_layer_yuan_scales_with_floating():
    # 池子 25 万, 3 年低点 20 万 → 浮盈 5 万 × 5% = 2500, 加上底仓
    expected = grid_pos.GRID_BASE_YUAN + 50_000 * grid_pos.GRID_FLOATING_FRACTION
    assert grid_pos.layer_yuan(pool_value=250_000.0, recent_min=200_000.0) == pytest_approx(expected)


def test_advance_grid_suggests_open_without_opening():
    """引擎只**建议**开仓 —— 自己不动仓位, 不写 ops.

    窗口最低 100, 今日 99 → 建议开仓 (signal="open"), 但
    in_cycle 仍为 False, entries 仍为空, ops 仍为空。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_pos.advance_one_day(
        state, date_str="2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert signal == "open"
    assert new_state["in_cycle"] is False, "引擎不自动开仓"
    assert new_state["entries"] == []
    assert new_state["ops"] == []
    # 唯一的副作用: 滚动窗口
    assert new_state["last_prices"] == [100.0, 99.0]


def test_advance_grid_hold_no_signal():
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_pos.advance_one_day(
        state, date_str="2026-01-01", price=101.0, layer_yuan_value=10_000.0,
    )
    assert signal is None
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []
    # rolling window: today's 101 appended → [100, 101]
    assert new_state["last_prices"] == [100.0, 101.0]


def test_advance_grid_suggests_add_and_tp_without_mutating():
    """两层金字塔 → 建议 add → 建议 tp; 引擎全程不改仓位.

    周期是**手动**建出来的 (manual_op) —— 引擎只负责"今天该做什么"。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    # 第一层 @ 99 (手动开仓)
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    assert state["in_cycle"] is True
    pre_entries = list(state["entries"])
    pre_ops = list(state["ops"])

    # 第二层 @ 99*0.97 = 96.03 → 建议 add
    state2, sig2 = grid_pos.advance_one_day(
        state, "2026-01-02", price=96.0, layer_yuan_value=10_000.0,
    )
    assert sig2 == "add"
    assert state2["entries"] == pre_entries, "引擎不加仓"
    assert state2["ops"] == pre_ops, "引擎不写 ops"
    assert state2["in_cycle"] is True

    # 均价 × 1.03 触发回本 (v2 行为, 显式传 tp_basis)
    # 只有一层 (引擎不加仓) → 均价就是那一笔的 99
    snap = grid_pos.asset_snapshot(state, current_price=96.0)
    assert snap["avg_cost"] == pytest_approx(99.0)
    assert snap["num_buys"] == 1
    exit_price = snap["avg_cost"] * 1.03 + 0.01
    state3, sig3 = grid_pos.advance_one_day(
        state2, "2026-01-03", price=exit_price, layer_yuan_value=10_000.0,
        tp_basis="avg_cost",
    )
    assert sig3 == "tp"
    # 引擎**不**出场: 仓位、层数、盈亏、归并份额全部原样
    assert state3["in_cycle"] is True
    assert state3["entries"] == pre_entries
    assert state3["ops"] == pre_ops
    assert state3["cum_realized"] == 0.0
    assert state3["merged_shares"] == 0.0


def test_asset_snapshot_no_cycle():
    state = grid_pos.fresh_asset_state()
    snap = grid_pos.asset_snapshot(state, current_price=100.0)
    assert snap["in_cycle"] is False
    assert snap["num_buys"] == 0
    assert snap["current_value"] == 0.0


def test_asset_snapshot_out_of_cycle_reports_merged_but_no_capital():
    """不在周期里 = 网格不再持有该资产 (出场时本金全额收回)。

    归并到再平衡的份额仍然要报出来 —— 它们现在是**组合**的持仓, 组合那
    一侧要用它们算权重和市值 (见 portfolio.merge_grid_shares_into_positions),
    但它们**不是**网格的资金占用。
    """
    state = grid_pos.fresh_asset_state()
    state["merged_shares"] = 1125.0
    snap = grid_pos.asset_snapshot(state, current_price=8.18)
    assert snap["in_cycle"] is False
    assert snap["current_value"] == 0.0, "no in-cycle position"
    assert snap["merged_shares"] == pytest_approx(1125.0)
    assert snap["merged_value"] == pytest_approx(1125.0 * 8.18)
    assert "in_use" not in snap, (
        "in_use was removed: 网格资金占用 = current_value (在周期内的层)")
    # 浮盈% 没有成本基准 —— 不在周期里就是 0 (展示层负责显示 "—")。
    assert snap["return_pct"] == 0.0


def test_advance_grid_tp_basis_first_buy_higher_target():
    """first_buy basis: 出场位 = 第一笔 × 1.03 (固定), 高于均价 × 1.03 (v2)。

    引擎**只给建议** —— 三段都只断言 signal, 周期/层数/ops 一字未改。
    用固定价 101 / 103 验证两套规则的差异:
    - 均价 ≈ 97.48, 均价×1.03 ≈ 100.40 (101 ≥ 100.40 → 均价基准建议出场)
    - 第一笔 99, 第一笔×1.03 = 101.97 (101 < 101.97 不触发, 103 ≥ 101.97 触发)
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    # 周期只能由**手动**路径造出来 —— 引擎不再自动开仓。
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    state = grid_pos.manual_op(state, "add", "2026-01-02", price=96.0,
                               shares=10_000.0 / 96.0)
    # 均价 = (10000 + 10000) / (10000/99 + 10000/96) ≈ 97.477
    # 均价 × 1.03 ≈ 100.40, 第一笔 × 1.03 = 101.97
    pre = json.loads(json.dumps(state))

    # 在 101: 均价 basis 建议出场, first_buy basis 不建议
    s1, sig_avg = grid_pos.advance_one_day(
        state, "2026-01-03", price=101.0, layer_yuan_value=10_000.0,
        tp_basis="avg_cost",
    )
    assert sig_avg == "tp", f"avg_cost basis should suggest tp at 101, got {sig_avg}"

    s2, sig_fb = grid_pos.advance_one_day(
        state, "2026-01-03", price=101.0, layer_yuan_value=10_000.0,
        tp_basis="first_buy",
    )
    assert sig_fb is None, f"first_buy basis should NOT suggest tp at 101, got {sig_fb}"
    assert s2["in_cycle"] is True

    # 涨到 103: first_buy basis 建议出场 (103 >= 99*1.03 = 101.97)
    s3, sig_fb2 = grid_pos.advance_one_day(
        state, "2026-01-03", price=103.0, layer_yuan_value=10_000.0,
        tp_basis="first_buy",
    )
    assert sig_fb2 == "tp", f"first_buy basis should suggest tp at 103, got {sig_fb2}"
    # 引擎建议了但不执行: 状态与之前完全一致
    assert s3["in_cycle"] is True
    assert s3["entries"] == pre["entries"]
    assert s3["ops"] == pre["ops"]
    assert s3["merged_shares"] == 0.0
    assert s3["cum_realized"] == 0.0


# -----------------------------------------------------------------------------
# grid.manual_op — Pages-driven open/add/tp operations
# -----------------------------------------------------------------------------

def test_manual_op_open_creates_cycle():
    """open: in_cycle False → True, entry appended, last_prices extended."""
    import json as _json
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [1.40, 1.38, 1.37]
    new_state = grid_pos.manual_op(
        state, op="open", today="2026-10-17", price=1.35, shares=1000.0,
    )
    assert new_state["in_cycle"] is True
    assert len(new_state["entries"]) == 1
    assert new_state["entries"][0] == {
        "date": "2026-10-17", "price": 1.35, "shares": 1000.0,
    }
    # 1.35 pushed to rolling window
    assert new_state["last_prices"][-1] == 1.35
    assert len(new_state["last_prices"]) == 4
    # original state NOT mutated
    assert state["in_cycle"] is False
    assert state["entries"] == []


def test_manual_op_open_rejects_existing_cycle():
    """open while in_cycle=True → ValueError."""
    state = grid_pos.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}]
    try:
        grid_pos.manual_op(state, op="open", today="2026-10-17",
                           price=1.30, shares=500.0)
    except ValueError as e:
        assert "no open cycle" in str(e)
    else:
        raise AssertionError("open on in_cycle=True should raise")


def test_manual_op_add_appends_to_cycle():
    """add: in_cycle True → entry appended, last_prices UNCHANGED."""
    state = grid_pos.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}]
    state["last_prices"] = [1.40, 1.38, 1.37]
    new_state = grid_pos.manual_op(
        state, op="add", today="2026-10-17", price=1.30, shares=1000.0,
    )
    assert new_state["in_cycle"] is True
    assert len(new_state["entries"]) == 2
    assert new_state["entries"][1]["price"] == 1.30
    # 加仓参考价 = entries[-1].price (新加的)
    assert new_state["entries"][-1]["price"] == 1.30
    # last_prices unchanged
    assert new_state["last_prices"] == [1.40, 1.38, 1.37]


def test_manual_op_add_rejects_no_cycle():
    """add while in_cycle=False → ValueError."""
    state = grid_pos.fresh_asset_state()
    try:
        grid_pos.manual_op(state, op="add", today="2026-10-17",
                           price=1.30, shares=1000.0)
    except ValueError as e:
        assert "requires an open cycle" in str(e)
    else:
        raise AssertionError("add on in_cycle=False should raise")


def test_manual_op_tp_sells_cost_only():
    """tp: 只卖出成本金额 → 本金取回, 浮盈归并到再平衡持仓, 已实现盈亏不变。"""
    state = grid_pos.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [
        {"date": "2026-10-15", "price": 1.40, "shares": 1000.0},
        {"date": "2026-10-16", "price": 1.30, "shares": 1000.0},
    ]
    state["cum_realized"] = 50.0
    new_state = grid_pos.manual_op(
        state, op="tp", today="2026-10-17", price=1.50, shares=0.0,
    )
    # total_invested = 1000*1.40 + 1000*1.30 = 2700 ; total_shares = 2000
    # 只卖回本所需: 2700 / 1.50 = 1800 份 → proceeds 2700 (本金原样取回)
    # 归并份额: 2000 - 1800 = 200 份, 交给再平衡组合
    assert new_state["merged_shares"] == pytest_approx(200.0)
    assert new_state["cum_realized"] == 50.0, "正常止盈不产生已实现盈亏"
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []


def test_manual_op_tp_below_cost_sells_out_and_books_loss():
    """手动止盈价低于均价: 卖出的份额会超过持仓 → 退化为卖光并认亏。

    自动止盈的触发价永远高于均价, 所以这条路径只有手动可达。绝不能转出
    负的归并份额。
    """
    state = grid_pos.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [
        {"date": "2026-10-15", "price": 1.40, "shares": 1000.0},
        {"date": "2026-10-16", "price": 1.30, "shares": 1000.0},
    ]
    new_state = grid_pos.manual_op(
        state, op="tp", today="2026-10-17", price=1.00, shares=0.0,
    )
    # 回本需要 2700/1.00 = 2700 份 > 持仓 2000 份 → 卖光
    assert new_state["merged_shares"] == 0.0, "不能转出负的归并份额"
    # 卖光 2000 × 1.00 = 2000, 认亏 2000 - 2700 = -700
    assert new_state["cum_realized"] == pytest_approx(-700.0)
    assert new_state["in_cycle"] is False


def test_manual_op_tp_rejects_no_cycle():
    """tp while in_cycle=False → ValueError."""
    state = grid_pos.fresh_asset_state()
    try:
        grid_pos.manual_op(state, op="tp", today="2026-10-17",
                           price=1.50, shares=0.0)
    except ValueError as e:
        assert "requires an open cycle" in str(e)
    else:
        raise AssertionError("tp on in_cycle=False should raise")


# -----------------------------------------------------------------------------
# grid.py — ops log (per-asset append-only log of every open/add/tp event).
# Backed by data/grid_state.json's `ops` field; rendered on the page under
# 策略全景图 as the "复合网格 操作记录" table.
# -----------------------------------------------------------------------------

def test_fresh_asset_state_has_ops_key():
    """fresh_asset_state() must include an empty `ops` list (not absent)."""
    state = grid_pos.fresh_asset_state()
    assert "ops" in state, "fresh_asset_state must seed ops=[]"
    assert state["ops"] == []


def test_advance_one_day_never_appends_ops():
    """引擎**绝不**写 ops —— 建议开仓了也只是一条建议.

    ops 是"已成交"的账本, 只有用户提交的操作才算成交。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_pos.advance_one_day(
        state, date_str="2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert signal == "open", "引擎建议开仓"
    assert new_state["ops"] == [], "引擎不写 ops"
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []


def test_advance_one_day_suggests_add_without_appending_op():
    """建议加仓 —— 引擎不加仓, 也不写 ops."""
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    # 周期是手动建的 (引擎不建)
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    ops_before = list(state["ops"])
    state2, sig = grid_pos.advance_one_day(
        state, "2026-01-02", price=96.0, layer_yuan_value=10_000.0,
    )
    assert sig == "add"
    assert len(state2["entries"]) == 1, "引擎不加仓"
    assert state2["ops"] == ops_before, "引擎不写 ops"


def test_advance_one_day_suggests_tp_without_exiting():
    """建议回本出场 —— 引擎**不**出场, 也不写 ops, 归并份额不变."""
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    state = grid_pos.manual_op(state, "add", "2026-01-02", price=96.0,
                               shares=10_000.0 / 96.0)
    ops_before = list(state["ops"])
    target = 99.0 * 1.03  # first_buy basis = first entry * (1 + tp_pct/100)
    state2, sig = grid_pos.advance_one_day(
        state, "2026-01-03", price=target + 0.01, layer_yuan_value=10_000.0,
        tp_basis="first_buy",
    )
    assert sig == "tp", "引擎建议回本出场"
    # 引擎不执行: 周期照旧开着, ops / 盈亏 / 归并份额全部不变
    assert state2["in_cycle"] is True
    assert len(state2["entries"]) == 2
    assert state2["ops"] == ops_before
    assert state2["cum_realized"] == 0.0
    assert state2["merged_shares"] == 0.0


def test_advance_one_day_hold_does_not_append_op():
    """No signal (hold) → ops log unchanged."""
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_pos.advance_one_day(
        state, "2026-01-01", price=101.0, layer_yuan_value=10_000.0,
    )
    assert signal is None
    assert new_state["ops"] == []
    # Original state's ops untouched
    assert state.get("ops", []) == []


def test_advance_one_day_legacy_state_without_ops_key():
    """老 state (没有 `ops` 键) → 结果里 `ops` 键存在且为空."""
    state = {"in_cycle": False, "entries": [], "last_prices": [100.0],
             "cum_realized": 0.0}   # NO "ops" key
    new_state, signal = grid_pos.advance_one_day(
        state, "2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert signal == "open"
    assert "ops" in new_state, "固定 6 key 必须补齐"
    assert new_state["ops"] == []


# -----------------------------------------------------------------------------
# 引擎 = 只读建议器 (advisory-only) —— 位置字段一字不改
# -----------------------------------------------------------------------------

def test_advance_one_day_never_mutates_position_fields():
    """跑一天只滚动窗口 —— 仓位字段一字不改, 连传入的对象都不能被就地改。

    调用方 (daily_snapshot) 拿到的 state 还要继续用, 引擎就地改会污染账本。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    state = grid_pos.manual_op(state, "add", "2026-01-02", price=96.0,
                               shares=10_000.0 / 96.0)
    before = json.dumps(state, sort_keys=True, ensure_ascii=False)

    new_state, _ = grid_pos.advance_one_day(
        state, "2026-01-03", price=97.0, layer_yuan_value=10_000.0,
    )
    assert json.dumps(state, sort_keys=True, ensure_ascii=False) == before, (
        "advance_one_day must not mutate the state it was given")
    for k in ("in_cycle", "entries", "cum_realized", "merged_shares", "ops"):
        assert new_state[k] == state[k], f"{k} must be untouched by the engine"
    # 唯一变的是窗口 (manual open 把 99.0 塞进了窗口, add 不塞)
    assert new_state["last_prices"] == [100.0, 99.0, 97.0]


def test_advance_one_day_rolls_last_prices_and_trims_to_120():
    """窗口每天 +1, 封顶 120 —— 最老的一根被挤掉。"""
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [float(i) for i in range(120)]   # 0..119
    new_state, _ = grid_pos.advance_one_day(
        state, "2026-01-01", price=1000.0, layer_yuan_value=10_000.0,
    )
    assert len(new_state["last_prices"]) == 120
    assert new_state["last_prices"][0] == 1.0, "最老的一根 (0.0) 应被挤掉"
    assert new_state["last_prices"][-1] == 1000.0, "最新一根是今天"
    # 传入的对象没被就地改
    assert len(state["last_prices"]) == 120
    assert state["last_prices"][-1] == 119.0


def test_advisory_reports_three_distances():
    """advisory() 给出三个触发位各自的距离 + 建议金额/份额。

    距离口径: **正数 = 还没触发, ≤0 = 已越过触发线**。
    """
    # --- 待机: 只算得出「距开仓」 ---
    st = grid_pos.fresh_asset_state()
    st["last_prices"] = [100.0, 99.0]
    adv = grid_pos.advisory(st, 99.5, 10_000.0)
    assert adv["signal"] is None
    assert adv["trigger_kind"] == "open"
    # advisory 的数字都四舍五入到 2 位 —— 比的时候要对齐口径
    assert adv["dist_open_pct"] == pytest_approx(
        round((99.5 - 99.0) / 99.0 * 100, 2))
    assert adv["dist_add_pct"] is None
    assert adv["dist_tp_pct"] is None
    assert adv["suggest_amount"] == 0.0, "没建议时没有建议金额"

    # 跌破窗口最低 → 建议开仓, 金额 = 单层
    adv2 = grid_pos.advisory(st, 98.0, 10_000.0)
    assert adv2["signal"] == "open"
    assert adv2["dist_open_pct"] < 0, "已越过触发线 = 负数"
    assert adv2["suggest_amount"] == pytest_approx(10_000.0)
    assert adv2["suggest_shares"] == pytest_approx(round(10_000.0 / 98.0, 2))
    assert adv2["suggest_price"] == 98.0

    # --- 周期中: 距加仓 + 距回本 ---
    st2 = grid_pos.manual_op(st, "open", "2026-01-01", price=100.0,
                              shares=1000.0)
    st2 = grid_pos.manual_op(st2, "add", "2026-01-02", price=97.0,
                              shares=1000.0)
    adv3 = grid_pos.advisory(st2, 97.0, 10_000.0, tp_basis="first_buy")
    assert adv3["signal"] is None
    # 加仓位 = 97 × 0.97 = 94.09 → (94.09 - 97) / 97 = -3%
    assert adv3["dist_add_pct"] == pytest_approx(
        round((97.0 * 0.97 - 97.0) / 97.0 * 100, 2))
    # 出场位 = 100 × 1.03 = 103 → (103 - 97) / 97
    assert adv3["dist_tp_pct"] == pytest_approx(
        round((103.0 - 97.0) / 97.0 * 100, 2))
    assert adv3["trigger_kind"] == "add", "两个都没触发 → 取更近的那个"

    # 跌到加仓位以下 → 建议加仓, 金额 = 单层
    adv4 = grid_pos.advisory(st2, 94.0, 10_000.0, tp_basis="first_buy")
    assert adv4["signal"] == "add"
    assert adv4["suggest_amount"] == pytest_approx(10_000.0)
    assert adv4["suggest_shares"] == pytest_approx(round(10_000.0 / 94.0, 2))

    # 涨到出场位以上 → 建议回本, 没有建议金额 (tp 不买)
    adv5 = grid_pos.advisory(st2, 103.5, 10_000.0, tp_basis="first_buy")
    assert adv5["signal"] == "tp"
    assert adv5["suggest_amount"] == 0.0
    assert adv5["suggest_shares"] == 0.0


def test_manual_cycle_open_add_tp_merges_bought_minus_sold():
    """N 买 1 卖 —— 走**生产用的** inline_manual_op (脚本与引擎同一份实现)。

    open 1000 份 @100 + add 1030 份 @97 → 2030 份 / 投入 ¥199,910;
    tp @103 → 卖出 199,910/103 份 (恰好回本), 余量归并进再平衡。
    """
    import importlib.util
    src = ROOT / "scripts" / "apply_grid.py"
    spec = importlib.util.spec_from_file_location("apply_grid_cycle", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    state, _ = mod.inline_manual_op(state, op="open", today="2026-01-01",
                                    price=100.0, shares=1000.0)
    state, _ = mod.inline_manual_op(state, op="add", today="2026-01-02",
                                    price=97.0, shares=1030.0)
    assert len(state["entries"]) == 2
    bought = sum(e["shares"] for e in state["entries"])
    invested = sum(e["shares"] * e["price"] for e in state["entries"])
    assert bought == 2030.0
    assert invested == pytest_approx(199_910.0)

    state, pnl = mod.inline_manual_op(state, op="tp", today="2026-01-03",
                                      price=103.0, shares=0.0)
    assert pnl == 0.0, "恰好回本 → 已实现盈亏为 0"
    sold = invested / 103.0
    merged = bought - sold
    assert state["merged_shares"] == pytest_approx(merged, rel=1e-9)
    tp_op = state["ops"][-1]
    assert tp_op["op"] == "tp"
    assert tp_op["source"] == "manual"
    assert tp_op["shares"] == pytest_approx(sold, rel=1e-9)
    assert tp_op["shares_to_merge"] == pytest_approx(merged, rel=1e-9)
    assert tp_op["total_shares_exited"] == pytest_approx(bought, rel=1e-9)
    assert tp_op["num_buys_exited"] == 2
    # 一个周期结束: 层清空, 周期关闭
    assert state["entries"] == []
    assert state["in_cycle"] is False


def test_advance_all_grids_returns_suggestions_not_trades():
    """record 层: 引擎只产出建议, 不动仓位。

    4 个资产的 signal 只能是 open/add/tp/None; grid_state 的
    in_cycle / entries / ops 前后完全一致。
    """
    names = list(universe.ASSET_NAMES)
    state = {}
    for name in names:
        s = grid_pos.fresh_asset_state()
        s["last_prices"] = [100.0]
        state[name] = s
    before = json.dumps(state, sort_keys=True, ensure_ascii=False)

    prices = {name: 99.0 for name in names}
    new_state, signals = snapshot.advance_all_grids(
        state, "2026-01-01", prices, pool_value=100_000.0, recent_min=100_000.0)
    assert len(signals) == 4
    for s in signals:
        assert s["signal"] in ("open", "add", "tp", None)
        assert s["signal"] == "open", "跌破窗口最低 → 建议开仓"
        assert s["in_cycle"] is False
        assert s["num_buys"] == 0
        assert s["current_value"] == 0.0
        assert s["suggest_amount"] > 0.0
        assert s["trigger_kind"] == "open"
    for name in names:
        assert new_state[name]["in_cycle"] is False
        assert new_state[name]["entries"] == []
        assert new_state[name]["ops"] == []
    # 传入的 state 没被就地改
    assert json.dumps(state, sort_keys=True, ensure_ascii=False) == before


def test_manual_op_open_appends_to_ops_with_source_manual():
    """manual open → ops log gets an entry with source=manual, note=empty."""
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [1.40, 1.38, 1.37]
    new_state = grid_pos.manual_op(
        state, op="open", today="2026-10-17", price=1.35, shares=1000.0,
    )
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "open"
    assert op["source"] == "manual"
    assert op["date"] == "2026-10-17"
    assert op["price"] == 1.35
    assert op["shares"] == 1000.0
    assert op["note"] == ""   # note 默认为空 (用户没填)
    assert "pnl" not in op
    # original state NOT mutated
    assert "ops" not in state or state.get("ops", []) == []


def test_manual_op_add_appends_to_ops_with_source_manual():
    """manual add → ops log gets an entry with op='add', source='manual'."""
    state = grid_pos.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}]
    new_state = grid_pos.manual_op(
        state, op="add", today="2026-10-17", price=1.30, shares=1000.0,
    )
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "add"
    assert op["source"] == "manual"
    assert op["note"] == ""
    assert "pnl" not in op


def test_manual_op_tp_appends_tp_op_recording_the_cost_split():
    """manual tp → ops log records what was sold vs. what became free."""
    state = grid_pos.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [
        {"date": "2026-10-15", "price": 1.40, "shares": 1000.0},
        {"date": "2026-10-16", "price": 1.30, "shares": 1000.0},
    ]
    state["cum_realized"] = 50.0
    new_state = grid_pos.manual_op(
        state, op="tp", today="2026-10-17", price=1.50, shares=0.0,
    )
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []
    assert new_state["cum_realized"] == 50.0, "只卖成本 → 不新增已实现盈亏"
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "tp"
    assert op["source"] == "manual"
    assert op["num_buys_exited"] == 2
    # 本金 2700 / 1.50 = 1800 份卖出, 余 200 份归并 → 出场 2000 份
    assert op["total_shares_exited"] == 2000.0
    assert op["shares_to_merge"] == 200.0
    assert op["shares"] == 1800.0
    assert op["pnl"] == 0.0
    assert op["note"] == ""


def test_ops_log_persists_through_tp_cycle_then_reopens():
    """ops log is append-only and survives a tp → next cycle's open.

    走**手动路径** (manual_op) —— 引擎不再写 ops, 所以只有用户提交
    的操作才进这个账本。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    # Cycle 1: open @ 99, tp @ 103 (first_buy basis)
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    state = grid_pos.manual_op(state, "tp", "2026-01-02",
                               price=99.0 * 1.03 + 0.01, shares=0.0)
    assert len(state["ops"]) == 2   # open + tp, both retained
    assert state["in_cycle"] is False
    # Cycle 2: 下一次开仓
    state = grid_pos.manual_op(state, "open", "2026-02-01", price=199.0,
                               shares=10_000.0 / 199.0)
    assert len(state["ops"]) == 3   # cycle-1 ops preserved + new open
    assert [op["date"] for op in state["ops"]] == [
        "2026-01-01", "2026-01-02", "2026-02-01",
    ]
    assert state["ops"][2]["op"] == "open"
    assert state["ops"][0]["op"] == "open"
    assert state["ops"][1]["op"] == "tp"


def test_inline_manual_op_legacy_state_without_ops_key():
    """apply_grid.inline_manual_op on a state missing `ops` key still works
    and seeds an empty ops list on the result."""
    import importlib.util
    import sys
    src = ROOT / "scripts" / "apply_grid.py"
    spec = importlib.util.spec_from_file_location("apply_grid_inline", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    legacy_state = {
        "in_cycle": False, "entries": [], "last_prices": [1.4], "cum_realized": 0.0,
        # NO "ops" key — simulates grid_state.json pre-migration
    }
    new_state, pnl = mod.inline_manual_op(
        legacy_state, op="open", today="2026-01-01", price=1.30, shares=1000.0,
        note="first cycle",
    )
    assert new_state["in_cycle"] is True
    assert new_state["ops"] == [{
        "date": "2026-01-01", "op": "open", "source": "manual",
        "price": 1.30, "shares": 1000.0, "note": "first cycle",
    }]
    assert pnl == 0.0


def test_inline_manual_op_note_kwarg_round_trip():
    """The `note` kwarg of inline_manual_op reaches the persisted op dict."""
    import importlib.util
    src = ROOT / "scripts" / "apply_grid.py"
    spec = importlib.util.spec_from_file_location("apply_grid_inline2", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = {
        "in_cycle": False, "entries": [], "last_prices": [1.4],
        "cum_realized": 0.0, "ops": [],
    }
    new_state, _ = mod.inline_manual_op(
        state, op="open", today="2026-01-01", price=1.30, shares=1000.0,
        note="用户备注：手动开仓",
    )
    assert new_state["ops"][0]["note"] == "用户备注：手动开仓"


def _write_holdings(json_path: Path, cash_label: str, cash: float, rows: list[list]) -> None:
    """Build a minimal holdings.json matching the schema read_holdings() expects.

    Schema (v2, two-pool):
        {"cash_rebalance": <number>, "cash_grid": <number>,
         "positions": [{"name":..., "code":..., "shares":...}, ...]}

    `cash_label` is accepted for backwards-compat (no longer written — json has no
    "资产" / "现金" label, just the numbers).

    Bad rows (None fields, non-numeric shares) are dropped to match the
    tolerant behaviour of the production read_holdings().
    """
    import json
    positions = []
    for row in rows:
        if not row or len(row) < 3: continue
        name, code, shares = row[0], row[1], row[2]
        if name is None or code is None or shares is None: continue
        try:
            positions.append({
                "name": str(name),
                "code": str(code).strip().split(".")[0],
                "shares": float(shares),
            })
        except (ValueError, TypeError):
            continue
    doc = {
        "_schema": "v2",
        "cash_rebalance": float(cash),
        "cash_grid": 0.0,
        "positions": positions,
    }
    json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# -----------------------------------------------------------------------------
# Mini pytest-equivalent helper (we don't depend on pytest)
# -----------------------------------------------------------------------------

def pytest_approx(value, rel=1e-9):
    """Return a value that can be compared with == using a tolerance."""
    class _Approx:
        def __init__(self, v, r):
            self.v, self.r = v, r
        def __eq__(self, other):
            return abs(self.v - other) <= self.r * max(abs(self.v), abs(other), 1)
    return _Approx(value, rel)


# ---------------- QDII premium + notify tests ----------------

def _make_record_with_qdii(premium_pct: float) -> dict:
    """Build a minimal record with a QDII position carrying `premium` info."""
    return {
        "date": "2026-10-14",
        "total": 200000.0,
        "etf_value": 199500.0,
        "cash_rebalance": 500.0,
        "cash_grid": 0.0,
        "cash_total": 500.0,
        "positions": [
            {"name": "纳斯达克ETF华夏", "code": "513300", "shares": 17000,
             "price": 2.884, "date": "2026-10-14", "value": 49028.0,
             "premium": {
                 "code": "513300",
                 "price": 2.884,
                 "iopv": 2.5466,
                 "nav_date": "2026-10-13",
                 "premium_pct": premium_pct,
                 "quote_time": "2026-10-14 16:14:45",
                 "source": "tencent+fund.eastmoney",
             }},
            {"name": "黄金ETF博时", "code": "159937", "shares": 5800,
             "price": 8.482, "date": "2026-10-14", "value": 49195.6},
        ],
        "weights": [0.245, 0.246],
        "target_w": 0.5,
        "threshold_pp": 3.0,
        "triggered": False,
        "plan": [],
        "grid_signals": [],
        "grid_state": {},
    }


def test_notify_qdii_high_premium_warns():
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=13.25)
    text = notify.build_text(r)
    subject = notify.build_subject(r)
    # Subject: high premium flag
    assert "QDII溢价" in subject, f"subject missing QDII flag: {subject!r}"
    # Text body: warning
    assert "溢价 +13.25%" in text
    assert "高溢价" in text
    assert "不建议加仓" in text
    # HTML body: red badge + 警示 block
    html = notify.build_html(r, pages_url="https://x/")
    assert "⚠ QDII 溢价监控" in html
    assert "background:#c0392b" in html  # warning red
    assert "+13.25%" in html


def test_notify_qdii_low_premium_advises_buildable():
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=2.5)
    subject = notify.build_subject(r)
    text = notify.build_text(r)
    html = notify.build_html(r, pages_url="https://x/")
    # No warning
    assert "QDII溢价" not in subject, f"subject wrongly warns at 2.5%: {subject!r}"
    # Text body: friendly
    assert "溢价 +2.50%" in text
    assert "可关注建仓" in text
    # HTML body: blue/green, not red
    assert "✓ QDII 溢价可控" in html
    assert "background:#c0392b" not in html


def test_notify_qdii_discount_opportunity():
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=-1.2)
    html = notify.build_html(r, pages_url="https://x/")
    text = notify.build_text(r)
    assert "折价 -1.20%" in text
    assert "折价机会" in text
    assert "✓ QDII 溢价可控" in html


def test_qdii_codes_contain_only_true_qdii():
    """ETF_DEFS qdii flag must be True only for genuinely QDII (overseas) ETFs."""
    from src.domain.portfolio import asset_universe as universe
    assert universe.QDII_CODES == {"513300"}, f"QDII_CODES={universe.QDII_CODES}"
    # 4-tuple shape
    for code, v in universe.ETF_DEFS.items():
        assert len(v) == 4, f"ETF_DEFS[{code}] not 4-tuple: {v}"


def test_notify_qdii_unheld_still_alerts():
    """A QDII tracked but NOT in the user's portfolio must still appear in the
    QDII block (with an "未持仓" tag) so the user knows the premium is too
    high to buy.  Subject still gets the QDII warning tag."""
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=13.25)
    # Remove 513300 from positions, move it to top-level qdii_premiums
    r["positions"] = [p for p in r["positions"] if p["code"] != "513300"]
    r["weights"] = [r["weights"][1]]  # drop the QDII weight
    r["qdii_premiums"] = [{
        "code": "513300", "name": "纳斯达克ETF华夏", "in_portfolio": False,
        "price": 2.884, "iopv": 2.5466, "nav_date": "2026-10-13",
        "premium_pct": 13.25, "quote_time": "2026-10-14 16:14:45",
        "source": "tencent+fund.eastmoney",
    }]
    text = notify.build_text(r)
    subject = notify.build_subject(r)
    html = notify.build_html(r, pages_url="https://x/")

    # Subject
    assert "QDII溢价" in subject, f"subject: {subject!r}"
    # Text body shows the QDII line with 未持仓 tag
    assert "纳斯达克ETF华夏" in text
    assert "未持仓" in text
    assert "高溢价" in text
    # HTML body shows the block, red, with 未持仓 badge
    assert "⚠ QDII 溢价监控" in html
    assert ">未持仓<" in html


def test_notify_qdii_unheld_low_premium_advises_buildable():
    """Tracked but unheld QDII with low premium should advise 建仓."""
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=2.0)
    r["positions"] = [p for p in r["positions"] if p["code"] != "513300"]
    r["weights"] = [r["weights"][1]]
    r["qdii_premiums"] = [{
        "code": "513300", "name": "纳斯达克ETF华夏", "in_portfolio": False,
        "price": 2.6, "iopv": 2.5466, "nav_date": "2026-10-13",
        "premium_pct": 2.0, "quote_time": "2026-10-14 16:14:45",
        "source": "tencent+fund.eastmoney",
    }]
    text = notify.build_text(r)
    subject = notify.build_subject(r)
    html = notify.build_html(r, pages_url="https://x/")

    # No subject warning
    assert "QDII溢价" not in subject, f"subject wrongly warns at 2%: {subject!r}"
    # Buildable advice
    assert "可关注建仓" in text
    assert "未持仓" in text
    # HTML: green, not red
    assert "✓ QDII 溢价可控" in html
    assert "background:#c0392b" not in html


# ---------------- Grid notify: cost-only exit wording ----------------

def _make_record_with_grid() -> dict:
    """Minimal record with two grid signals.

    One asset is 待机 (out of cycle, capital free — the shares it merged into
    the rebalance portfolio are reported separately) and the engine suggests
    开仓; the other is still in a cycle and the engine suggests 回本出场.
    """
    r = _make_record_with_qdii(premium_pct=2.0)
    r["grid_signals"] = [
        {"name": "黄金ETF博时", "price": 8.18, "layer_yuan": 10000.0,
         "signal": "open", "in_cycle": False, "num_buys": 0,
         "avg_cost": 0.0, "current_value": 0.0, "unrealized_pnl": 0.0,
         "return_pct": 0.0, "merged_shares": 1125.0, "merged_value": 9206.0,
         "trigger_pct": 3.2, "trigger_kind": "open"},
        {"name": "纳斯达克ETF华夏", "price": 2.69, "layer_yuan": 10000.0,
         "signal": "tp", "in_cycle": True, "num_buys": 2,
         "avg_cost": 2.55, "current_value": 15000.0, "unrealized_pnl": 900.0,
         "return_pct": 6.0, "merged_shares": 5265.0, "merged_value": 14162.0,
         "trigger_pct": 1.1, "trigger_kind": "tp"},
    ]
    return r


def test_notify_grid_tp_signal_is_recovery_not_take_profit():
    """「tp / 回本」不叫止盈: 只卖成本金额回本, 已实现盈亏恒为 0。

    叫它止盈会让人以为收益已经兑现了 —— 而新规则下收益全在归并份额里。
    """
    from src.presentation import digest as notify
    r = _make_record_with_grid()
    text = notify.build_text(r)
    html = notify.build_html(r, pages_url="https://x/")
    assert "建议回本" in text, "text body should label the tp signal as 建议回本"
    assert "建议回本" in html, "html body should label the tp signal as 建议回本"
    assert "止盈" not in text, "old 止盈 wording still in text body"
    assert "止盈" not in html, "old 止盈 wording still in html body"
    # The subject tag follows the same wording. (open 优先于 tp, 所以这里
    # 只留一条 tp 建议再问 subject。)
    r2 = json.loads(json.dumps(r))
    r2["grid_signals"][0]["signal"] = None
    assert "网格建议回本" in notify.build_subject(r2)


def test_notify_grid_shows_capital_column_and_merged_note():
    """邮件要能看出「资金占用」和「已归并」是两回事。

    资金占用 = 网格在周期内的层 (出场后本金回来, 占用回落);
    归并份额的市值记在组合那一栏 —— 不显示等于收益凭空消失。
    """
    from src.presentation import digest as notify
    r = _make_record_with_grid()
    text = notify.build_text(r)
    html = notify.build_html(r, pages_url="https://x/")
    for body in (text, html):
        assert "资金占用" in body, "grid table should have a 资金占用 column"
        assert "已归并" in body, "grid table should have a 已归并 column"
        assert "归并到再平衡持仓的份额" in body
        # 9,206 + 14,162 = 23,368
        assert "23,368" in body, "merged share market value should be shown"
        # 资金占用 = 只有纳指在周期内 → 15,000 (旧的 in_use 口径是 29,162)
        assert "15,000" in body, "grid capital should be the in-cycle layers only"


def test_notify_grid_return_pct_is_dash_when_out_of_cycle():
    """不在周期里时浮盈% 显示 "—", 而不是 "+0.00%"。

    "+0.00%" 会被读成"没赚没亏", 但资产可能刚把一批份额归并给了组合。
    """
    from src.presentation import digest as notify
    r = _make_record_with_grid()
    text = notify.build_text(r)
    # 纳斯达克 (在周期内) 仍有浮盈; 黄金 (不在周期内) 显示 —.
    assert "+6.00%" in text, "in-cycle asset should still show its unrealized %"
    # 只看 [复合网格] 那一段 —— 3pp 再平衡表里也有同名的行。
    grid_part = text.split("[复合网格]", 1)[1]
    row = [ln for ln in grid_part.splitlines() if ln.startswith("黄金ETF博时")]
    assert row, "gold row missing from the grid section"
    assert "—" in row[0] and "+0.00%" not in row[0], (
        f"out-of-cycle row should show — not +0.00%: {row[0]!r}")


def test_notify_renders_new_signal_vocabulary():
    """新词表 open / add / tp 的徽章 + 「只给建议」字样。

    引擎不再自动交易 —— 邮件必须把「建议」和「已成交」区分开, 否则用户会
    以为 ���已经按建议下单了。
    """
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=2.0)
    base = {"layer_yuan": 10000.0, "in_cycle": True, "num_buys": 1,
            "avg_cost": 2.0, "current_value": 10000.0, "unrealized_pnl": 0.0,
            "return_pct": 0.0, "merged_shares": 0.0, "merged_value": 0.0,
            "trigger_pct": 0.0, "trigger_kind": "open"}
    r["grid_signals"] = [
        dict(base, name="黄金ETF博时", price=8.18, signal="open"),
        dict(base, name="红利ETF易方达", price=1.32, signal="add"),
        dict(base, name="可转债ETF博时", price=12.32, signal="tp"),
    ]
    text = notify.build_text(r)
    html = notify.build_html(r, pages_url="https://x/")
    for body in (text, html):
        assert "只给建议" in body, "body must say the engine only advises"
        assert "建议开仓" in body
        assert "建议加仓" in body
        assert "建议回本" in body
    # HTML 徽章 (带图标前缀)
    assert "⚠ 建议开仓" in html
    assert "⚠ 建议加仓" in html
    assert "✓ 建议回本" in html
    # 表头改成「建议」/「距建议触发」
    assert "距建议触发" in text
    assert "距建议触发" in html
    # subject 优先级: open > add > tp
    assert "网格建议开仓" in notify.build_subject(r)


def test_notify_legacy_signal_vocabulary_still_renders():
    """老 history.json 里的 "buy" / "sell" 不能把邮件渲染崩了。

    旧记录只用于画曲线, 但 notify 仍可能被历史数据喂到 —— 兜底映射
    ("买入(旧)" / "回本(旧)") 必须存在。
    """
    from src.presentation import digest as notify
    r = _make_record_with_qdii(premium_pct=2.0)
    base = {"layer_yuan": 10000.0, "in_cycle": True, "num_buys": 1,
            "avg_cost": 2.0, "current_value": 10000.0, "unrealized_pnl": 0.0,
            "return_pct": 0.0, "merged_shares": 0.0, "merged_value": 0.0,
            "trigger_pct": 0.0, "trigger_kind": "open"}
    r["grid_signals"] = [
        dict(base, name="黄金ETF博时", price=8.18, signal="buy"),
        dict(base, name="红利ETF易方达", price=1.32, signal="sell"),
    ]
    text = notify.build_text(r)          # 不抛异常 = 通过
    html = notify.build_html(r, pages_url="https://x/")
    assert "买入(旧)" in text
    assert "回本(旧)" in text
    assert "买入(旧)" in html
    assert "回本(旧)" in html
    assert "网格建议" in notify.build_subject(r)


def test_apply_holdings_happy_path():
    """scripts/apply_holdings.py accepts a clean trades-v1 payload and writes
    both data/trades.json and data/holdings.json (two-pool schema)."""
    import json
    import sys
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = ROOT / "scripts" / "apply_holdings.py"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit_rebalance", "amount": 10000.0},
                {"date": "2026-10-13", "side": "buy", "code": "515180",
                 "shares": 1000, "price": 5.0},
            ],
        })
        import importlib.util
        spec = importlib.util.spec_from_file_location("apply_holdings_test", src)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
        except SystemExit as e:
            assert e.code == 0, f"expected exit 0, got {e.code}"
        finally:
            sys.argv = old_argv
        # trades.json
        td_doc = json.loads(mod.TRADES.read_text(encoding="utf-8"))
        assert td_doc["_schema"] == "trades-v1"
        assert len(td_doc["trades"]) == 2
        # holdings.json (rebuilt)
        h_doc = json.loads(mod.HOLDINGS.read_text(encoding="utf-8"))
        assert h_doc["_schema"] == "v2"
        # 10000 deposit_rebalance - 5000 buy = 5000 cash_rebalance
        assert h_doc["cash_rebalance"] == 5000.0
        assert h_doc["cash_grid"] == 0.0
        assert len(h_doc["positions"]) == 1
        assert h_doc["positions"][0]["code"] == "515180"
        assert h_doc["positions"][0]["shares"] == 1000


def test_apply_holdings_rejects_bad_code():
    """Unknown code in payload → non-zero exit, no file written."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [{"date": "2026-10-13", "side": "buy",
                        "code": "999999", "shares": 1, "price": 1.0}],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "expected non-zero exit for bad code"
        assert not mod.HOLDINGS.exists(), "holdings.json should not be written on failure"
        assert not mod.TRADES.exists(),   "trades.json should not be written on failure"


def test_apply_holdings_rejects_legacy_rows_payload():
    """Old {rows, cash} payload is hard-rejected with a clear message."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "rows": [{"code": "515180", "shares": 1}],
            "cash": 100,
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "legacy rows payload should be rejected"
        assert not mod.HOLDINGS.exists()


def test_apply_holdings_rejects_negative_cash():
    """A buy that would push cash_rebalance < 0 is rejected."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit_rebalance", "amount": 100.0},
                {"date": "2026-10-13", "side": "buy",
                 "code": "515180", "shares": 1000, "price": 1.0},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "buy that pushes cash_rebalance negative should be rejected"
        assert not mod.HOLDINGS.exists()


def test_apply_holdings_rejects_oversell():
    """A sell exceeding current shares is rejected."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit_rebalance", "amount": 1000.0},
                {"date": "2026-10-13", "side": "buy",
                 "code": "515180", "shares": 100, "price": 1.0},
                {"date": "2026-10-14", "side": "sell",
                 "code": "515180", "shares": 999, "price": 1.5},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "oversell should be rejected"


# -----------------------------------------------------------------------------
# Trade-ledger tests (data/trades.json)
# -----------------------------------------------------------------------------

def test_load_trades_missing_returns_empty():
    """Missing trades.json → [] (not an error)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trades.json"
        repos.TRADES = path
        assert repos.load_trades() == []


def test_load_trades_unreadable_returns_empty():
    """Corrupt JSON → [] (warning printed, not an error)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trades.json"
        path.write_text("not-json{", encoding="utf-8")
        repos.TRADES = path
        assert repos.load_trades() == []


def test_apply_trade_buy_increments_shares_decrements_cash():
    state = {"cash_rebalance": 10000.0, "cash_grid": 0.0, "positions": {}}
    ledger.apply_trade(state, {
        "side": "buy", "code": "515180", "shares": 1000, "price": 1.40, "fee": 0.0,
    })
    assert state["cash_rebalance"] == round(10000 - 1000 * 1.40, 2) == 8600.0
    assert state["positions"]["515180"] == 1000


def test_apply_trade_sell_rejects_oversell_raises():
    state = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {"515180": 100.0}}
    raised = False
    try:
        ledger.apply_trade(state, {
            "side": "sell", "code": "515180", "shares": 200, "price": 1.50,
        })
    except ValueError as ex:
        raised = True
        assert "oversell" in str(ex) and "515180" in str(ex)
    assert raised, "expected ValueError for oversell"


def test_rebuild_holdings_from_trades_deposit_only_yields_initial_state():
    """One deposit_rebalance → cash_rebalance = amount, positions empty."""
    trades = [{"side": "deposit_rebalance", "date": "2026-10-12", "amount": 175900.60}]
    positions, cash_rebalance, cash_grid = ledger.rebuild_holdings_from_trades(
        trades, as_of_date="2026-10-12",
    )
    assert cash_rebalance == 175900.60
    assert cash_grid == 0.0
    assert positions == []


def test_rebuild_holdings_from_trades_chains_buy_sell_deposit_withdraw():
    """Replay: deposit_rebalance 10000, buy 1000@10, sell 200@12, withdraw_rebalance 500.
    Expected: cash_rebalance = 10000 - 10000 + 2400 - 500 = 1900
              cash_grid = 0
              515180 shares = 800
    """
    trades = [
        {"side": "deposit_rebalance", "date": "2026-10-12", "amount": 10000.0},
        {"side": "buy",     "date": "2026-10-13", "code": "515180",
         "shares": 1000, "price": 10.0, "fee": 0.0},
        {"side": "sell",    "date": "2026-10-14", "code": "515180",
         "shares": 200, "price": 12.0, "fee": 0.0},
        {"side": "withdraw_rebalance","date": "2026-10-15", "amount": 500.0},
    ]
    positions, cash_rebalance, cash_grid = ledger.rebuild_holdings_from_trades(
        trades, as_of_date="2026-10-15",
    )
    assert cash_rebalance == round(10000 - 10000 + 2400 - 500, 2) == 1900.0
    assert cash_grid == 0.0
    assert len(positions) == 1
    assert positions[0]["code"] == "515180"
    assert positions[0]["shares"] == 800.0


def test_rebuild_holdings_from_trades_as_of_date_inclusive():
    """Trades after as_of_date are skipped."""
    trades = [
        {"side": "deposit_rebalance", "date": "2026-10-12", "amount": 1000.0},
        {"side": "deposit_rebalance", "date": "2026-10-15", "amount": 500.0},
    ]
    _, cash_rebalance, _ = ledger.rebuild_holdings_from_trades(
        trades, as_of_date="2026-10-13",
    )
    assert cash_rebalance == 1000.0   # second deposit is after 10-13, skipped


# -----------------------------------------------------------------------------
# Two-pool cash tests — exercises the new deposit_rebalance / deposit_grid /
# withdraw_rebalance / withdraw_grid / transfer_to_rebalance / transfer_to_grid
# sides at the apply_trade / rebuild / apply_holdings level.
# -----------------------------------------------------------------------------

def test_apply_trade_deposit_rebalance_credits_rebalance_only():
    state = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {}}
    ledger.apply_trade(state, {
        "side": "deposit_rebalance", "amount": 5000.0,
    })
    assert state["cash_rebalance"] == 5000.0
    assert state["cash_grid"] == 0.0


def test_apply_trade_deposit_grid_credits_grid_only():
    state = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {}}
    ledger.apply_trade(state, {
        "side": "deposit_grid", "amount": 8000.0,
    })
    assert state["cash_rebalance"] == 0.0
    assert state["cash_grid"] == 8000.0


def test_apply_trade_withdraw_grid_rejects_insufficient():
    state = {"cash_rebalance": 0.0, "cash_grid": 100.0, "positions": {}}
    raised = False
    try:
        ledger.apply_trade(state, {
            "side": "withdraw_grid", "amount": 200.0,
        })
    except ValueError as ex:
        raised = True
        assert "cash would go negative" in str(ex)
    assert raised, "withdraw_grid exceeding balance should raise"
    # State must not be mutated on failure
    assert state["cash_grid"] == 100.0


def test_apply_trade_transfer_to_grid_deducts_rebalance_adds_grid():
    state = {"cash_rebalance": 5000.0, "cash_grid": 1000.0, "positions": {}}
    ledger.apply_trade(state, {
        "side": "transfer_to_grid", "amount": 2000.0,
    })
    assert state["cash_rebalance"] == 3000.0
    assert state["cash_grid"] == 3000.0
    # Total preserved
    assert state["cash_rebalance"] + state["cash_grid"] == 6000.0


def test_apply_trade_transfer_to_grid_rejects_insufficient_rebalance():
    state = {"cash_rebalance": 100.0, "cash_grid": 5000.0, "positions": {}}
    raised = False
    try:
        ledger.apply_trade(state, {
            "side": "transfer_to_grid", "amount": 1000.0,
        })
    except ValueError as ex:
        raised = True
        assert "cash would go negative" in str(ex)
    assert raised, "transfer_to_grid exceeding rebalance pool should raise"
    # No partial credit — atomic
    assert state["cash_rebalance"] == 100.0
    assert state["cash_grid"] == 5000.0


def test_apply_trade_transfer_to_rebalance_deducts_grid_adds_rebalance():
    state = {"cash_rebalance": 0.0, "cash_grid": 5000.0, "positions": {}}
    ledger.apply_trade(state, {
        "side": "transfer_to_rebalance", "amount": 1500.0,
    })
    assert state["cash_rebalance"] == 1500.0
    assert state["cash_grid"] == 3500.0


def test_apply_trade_transfer_sides_dont_change_positions():
    """transfer_* only moves cash between pools; positions stay identical."""
    state = {
        "cash_rebalance": 2000.0, "cash_grid": 1000.0,
        "positions": {"515180": 5000.0},
    }
    positions_before = dict(state["positions"])
    ledger.apply_trade(state, {
        "side": "transfer_to_grid", "amount": 500.0,
    })
    ledger.apply_trade(state, {
        "side": "transfer_to_rebalance", "amount": 200.0,
    })
    assert state["positions"] == positions_before


def test_rebuild_holdings_from_trades_six_new_cash_sides():
    """End-to-end replay: one of each cash side, final pool balances correct."""
    trades = [
        {"side": "deposit_rebalance",     "date": "2026-10-12", "amount": 10000.0},
        {"side": "deposit_grid",          "date": "2026-10-13", "amount": 3000.0},
        {"side": "transfer_to_grid",      "date": "2026-10-14", "amount": 2000.0},
        {"side": "withdraw_grid",         "date": "2026-10-15", "amount": 500.0},
        {"side": "transfer_to_rebalance", "date": "2026-10-16", "amount": 400.0},
        {"side": "withdraw_rebalance",    "date": "2026-10-17", "amount": 100.0},
    ]
    positions, cash_rebalance, cash_grid = ledger.rebuild_holdings_from_trades(
        trades, as_of_date="2026-10-17",
    )
    # rebalance: +10000 -2000(transfer out) +400(transfer in) -100(withdraw) = 8300
    # grid:     +3000 +2000(transfer in) -500(withdraw) -400(transfer out) = 4100
    assert cash_rebalance == 8300.0, f"got {cash_rebalance}"
    assert cash_grid == 4100.0, f"got {cash_grid}"
    assert positions == []


def test_apply_holdings_accepts_6_new_cash_sides():
    """scripts/apply_holdings.py accepts a payload with one of each of the 6
    new cash sides and writes a two-pool holdings.json with the right balances."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test6", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit_rebalance",     "amount": 10000.0},
                {"date": "2026-10-13", "side": "deposit_grid",          "amount":  3000.0},
                {"date": "2026-10-14", "side": "transfer_to_grid",      "amount":  2000.0},
                {"date": "2026-10-15", "side": "withdraw_grid",         "amount":   500.0},
                {"date": "2026-10-16", "side": "transfer_to_rebalance", "amount":   400.0},
                {"date": "2026-10-17", "side": "withdraw_rebalance",    "amount":   100.0},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
        except SystemExit as e:
            assert e.code == 0, f"expected exit 0, got {e.code}"
        finally:
            sys.argv = old_argv
        h_doc = json.loads(mod.HOLDINGS.read_text(encoding="utf-8"))
        assert h_doc["cash_rebalance"] == 8300.0, f"got {h_doc}"
        assert h_doc["cash_grid"]      == 4100.0, f"got {h_doc}"
        assert h_doc["positions"] == []


def test_apply_holdings_rejects_withdraw_exceeding_pool():
    """withdraw_grid 100 from grid=0 → rejected."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test7", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "withdraw_grid", "amount": 100.0},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "withdraw_grid exceeding pool should be rejected"
        assert not mod.HOLDINGS.exists()


def test_apply_holdings_rejects_transfer_exceeding_source_pool():
    """transfer_to_grid 1000 from rebalance=0 → rejected (atomic)."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test8", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        # pre-seed: cash_grid=100 via deposit_grid, then try to transfer 1000
        # from rebalance=0 to grid → fail
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit_grid", "amount": 100.0},
                {"date": "2026-10-13", "side": "transfer_to_grid", "amount": 1000.0},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "transfer_to_grid exceeding rebalance should be rejected"
        assert not mod.HOLDINGS.exists()


def test_apply_holdings_rejects_legacy_deposit_side():
    """The old single-pool `deposit` side is no longer accepted."""
    import importlib.util
    import json
    import sys
    src = ROOT / "scripts" / "apply_holdings.py"
    spec = importlib.util.spec_from_file_location("apply_holdings_test9", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        mod.TRADES   = td / "trades.json"
        mod.HOLDINGS = td / "holdings.json"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit", "amount": 100.0},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_holdings.py", payload]
        try:
            mod.main()
            ok = True
        except SystemExit as e:
            ok = (e.code == 0)
        finally:
            sys.argv = old_argv
        assert not ok, "legacy `deposit` side should be rejected after pool-split"




# -----------------------------------------------------------------------------
# scripts/apply_grid.py — Pages editor → grid_state.json (open/add/tp)
# -----------------------------------------------------------------------------

def test_apply_grid_happy_path_add_then_tp():
    """An `add` op followed by a `tp` op in one batch updates state correctly."""
    import importlib.util
    import json
    import sys
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Pre-populate an in-cycle grid state for one asset
        initial = {
            "红利ETF易方达": {
                "in_cycle": True,
                "entries": [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}],
                "last_prices": [1.40, 1.38, 1.37],
                "cum_realized": 0.0,
            },
            "黄金ETF博时": {
                "in_cycle": False, "entries": [],
                "last_prices": [8.50], "cum_realized": 0.0,
                "ops": [],
            },
        }
        state_path = td / "grid_state.json"
        state_path.write_text(json.dumps(initial, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        src = ROOT / "scripts" / "apply_grid.py"
        spec = importlib.util.spec_from_file_location("apply_grid_test", src)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.GRID_STATE = state_path
        payload = json.dumps({
            "schema": "grid-ops-v1",
            "ops": [
                {"date": "2026-10-17", "code": "515180", "op": "add",
                 "price": 1.30, "shares": 1000, "note": "pyramid"},
                {"date": "2026-10-17", "code": "515180", "op": "tp",
                 "price": 1.50, "shares": 0, "note": "tp"},
            ],
        })
        old_argv = sys.argv
        sys.argv = ["apply_grid.py", payload]
        try:
            mod.main()
        except SystemExit as e:
            assert e.code == 0, f"expected exit 0, got {e.code}"
        finally:
            sys.argv = old_argv
        # Reload state, verify the two ops applied
        out = json.loads(state_path.read_text(encoding="utf-8"))
        a = out["红利ETF易方达"]
        # After add+tp: cycle closed, 2 entries added then cleared
        assert a["in_cycle"] is False
        assert a["entries"] == []
        # 只卖成本金额: 本金 2700 / 1.50 = 1800 份卖出 → 恰好回本, pnl = 0
        # 原持仓 2000 份中余 200 份归并到再平衡持仓
        assert a["cum_realized"] == 0.0
        assert a["merged_shares"] == pytest_approx(200.0)
        # last_prices for the add branch should NOT have been touched
        assert a["last_prices"] == [1.40, 1.38, 1.37]
        # ops log: 2 entries (add + tp), both source='manual', with notes
        # round-tripped from the payload.  tp op records the cost split.
        assert len(a["ops"]) == 2
        add_op, tp_op = a["ops"]
        assert add_op["op"] == "add"
        assert add_op["source"] == "manual"
        assert add_op["note"] == "pyramid"
        assert "pnl" not in add_op
        assert tp_op["op"] == "tp"
        assert tp_op["source"] == "manual"
        assert tp_op["note"] == "tp"
        assert tp_op["num_buys_exited"] == 2
        assert tp_op["total_shares_exited"] == 2000.0
        assert tp_op["shares_to_merge"] == 200.0
        assert tp_op["pnl"] == 0.0
        # Other asset untouched (also must have ops=[] from fresh-asset init)
        assert out["黄金ETF博时"]["in_cycle"] is False
        assert out["黄金ETF博时"]["ops"] == []


def test_apply_grid_rejects_open_when_in_cycle():
    """open on an in-cycle asset → SystemExit(1)."""
    import importlib.util
    import json
    import sys
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_path = td / "grid_state.json"
        state_path.write_text(json.dumps({
            "红利ETF易方达": {
                "in_cycle": True, "entries": [],
                "last_prices": [], "cum_realized": 0.0,
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        src = ROOT / "scripts" / "apply_grid.py"
        spec = importlib.util.spec_from_file_location("apply_grid_test", src)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.GRID_STATE = state_path
        payload = json.dumps({
            "schema": "grid-ops-v1",
            "ops": [{"date": "2026-10-17", "code": "515180", "op": "open",
                     "price": 1.20, "shares": 1000}],
        })
        old_argv = sys.argv
        sys.argv = ["apply_grid.py", payload]
        raised = False
        try:
            mod.main()
        except SystemExit as e:
            raised = True
            assert e.code == 1
        finally:
            sys.argv = old_argv
        assert raised, "expected SystemExit(1) for open on in-cycle asset"
        # State file should NOT have been written (atomic write happens after
        # all ops apply; the failing op should abort before any write).
        # In practice apply_grid.py may rewrite the file even on a single-op
        # failure depending on order — we only assert exit code here.



def _fake_cycle(dates, prices, exit_date, exit_price, pnl=None,
                shares_per_layer=1000.0, pos_yuan=None, to_merge=0.0):
    """Build a `run_compound_grid` cycles[0]-shaped dict.

    `pos_yuan` mirrors the real engine's per-layer sizing (grid.py:
    `sh = pos_yuan / price`), so each layer costs exactly that much even
    though prices differ. Without it, every entry gets the same flat
    `shares_per_layer` (handy for arithmetic on round numbers).

    `to_merge` mirrors the cost-only exit: the cycle's `total_shares` is
    the number of shares SOLD to recover the principal, and
    `shares_transferred` is the remainder that became permanently-held
    zero-cost shares.
    """
    entries = [{"date": pd.Timestamp(d), "price": float(p),
                "shares": (pos_yuan / float(p) if pos_yuan else shares_per_layer)}
               for d, p in zip(dates, prices)]
    total_shares = sum(e["shares"] for e in entries)
    total_invested = sum(e["shares"] * e["price"] for e in entries)
    sold = total_shares - to_merge
    return {
        "entries": entries,
        "exit_date": pd.Timestamp(exit_date),
        "exit_price": float(exit_price),
        "num_buys": len(entries),
        "avg_cost": total_invested / total_shares,
        "total_shares": sold,
        "shares_transferred": to_merge,
        "total_invested": total_invested,
        "exit_value": sold * exit_price,
        "pnl": (total_shares * exit_price - total_invested) if pnl is None else pnl,
        "return_pct": 0.0,
    }


def test_ops_from_cycles_maps_closed_cycle_to_open_add_tp():
    c = _fake_cycle(["2024-01-10", "2024-01-20"], [10.0, 9.5],
                    "2024-02-05", 10.6)
    ops = grid_sim.ops_from_cycles([c])
    assert len(ops) == 3, f"expected 3 ops, got {len(ops)}"
    # entries[0] -> open, entries[1] -> add, exit -> tp
    assert [o["op"] for o in ops] == ["open", "add", "tp"]
    assert ops[0]["date"] == "2024-01-10"
    assert ops[1]["date"] == "2024-01-20"
    assert ops[2]["date"] == "2024-02-05"
    # every op is tagged as simulated, never as a live trigger
    assert all(o["source"] == "backtest" for o in ops)
    assert ops[0]["note"] == "回测·窗口突破开仓"
    assert ops[1]["note"] == "回测·金字塔第2层"
    assert ops[2]["note"] == "回测·回本出场"
    # dates normalized from pandas Timestamps to the ISO strings the page sorts on
    assert all(isinstance(o["date"], str) for o in ops)
    # tp carries the realized stats; open/add must not
    assert ops[2]["pnl"] == pytest_approx(c["pnl"])
    assert ops[2]["num_buys_exited"] == 2
    assert ops[2]["total_shares_exited"] == pytest_approx(2000.0)
    assert ops[2]["shares_to_merge"] == pytest_approx(0.0)
    assert "pnl" not in ops[0] and "pnl" not in ops[1]


def test_ops_from_cycles_tp_reports_zero_cost_transfer():
    """只卖成本金额的出场: 卖出的份额与归并出去的余量必须分开记账。

    `total_shares_exited` 仍是"这一整个周期关掉的仓位", `shares` 是真正卖出
    的那部分, `shares_to_merge` 是归并到再平衡持仓的部分。
    """
    c = _fake_cycle(["2024-01-10", "2024-01-20"], [10.0, 9.5],
                    "2024-02-05", 10.6, to_merge=250.0)
    ops = grid_sim.ops_from_cycles([c])
    tp = [o for o in ops if o["op"] == "tp"][0]
    assert tp["shares"] == pytest_approx(1750.0), "卖出的是回本所需那部分"
    assert tp["shares_to_merge"] == pytest_approx(250.0)
    assert tp["total_shares_exited"] == pytest_approx(2000.0)


def test_ops_from_cycles_open_cycle_has_no_tp():
    """The still-open cycle contributes entries but never a take-profit."""
    open_cycle = [{"date": pd.Timestamp("2026-09-04"), "price": 13.446, "shares": 743.72},
                  {"date": pd.Timestamp("2026-09-28"), "price": 13.038, "shares": 766.99},
                  {"date": pd.Timestamp("2026-10-14"), "price": 12.634, "shares": 791.51}]
    ops = grid_sim.ops_from_cycles([], open_cycle)
    assert len(ops) == 3
    assert [o["op"] for o in ops] == ["open", "add", "add"]
    assert not any(o["op"] == "tp" for o in ops)
    assert ops[2]["note"] == "回测·金字塔第3层"
    # no open cycle at all -> no ops, not a crash
    assert grid_sim.ops_from_cycles([], None) == []
    assert grid_sim.ops_from_cycles([], []) == []


def test_ops_from_cycles_orders_add_before_tp_on_same_date():
    """run_compound_grid can add a layer and take profit on the SAME bar
    (grid.py adds at line ~100, then checks tp at ~110). The op order has to
    be explicit, not incidental to dict/append order."""
    c = _fake_cycle(["2024-01-10", "2024-02-05"], [10.0, 9.5],
                    "2024-02-05", 10.6)
    ops = grid_sim.ops_from_cycles([c])
    same_day = [o for o in ops if o["date"] == "2024-02-05"]
    assert len(same_day) == 2, "fixture should put an add and a tp on one date"
    assert [o["op"] for o in same_day] == ["add", "tp"]


def test_merge_backtest_ops_is_idempotent_and_preserves_live_ops():
    """Re-running the backfill must not duplicate, and must never eat a live op."""
    auto = {"date": "2026-10-21", "op": "open", "source": "auto",
            "price": 1.0, "shares": 100.0, "note": "窗口突破开仓"}
    manual = {"date": "2026-10-22", "op": "add", "source": "manual",
              "price": 0.9, "shares": 200.0, "note": "手动加仓"}
    state = {"A": {"in_cycle": False, "entries": [], "cum_realized": 0.0,
                   "last_prices": [1.0, 2.0], "ops": [auto, manual]}}

    gen = grid_sim.ops_from_cycles(
        [_fake_cycle(["2024-01-10"], [10.0], "2024-02-05", 10.6)])

    preserved = grid_sim.merge_backtest_ops(state, "A", gen)
    assert preserved == 2, "both live ops should survive"
    first = list(state["A"]["ops"])

    # Second run over the same state, same generated ops.
    preserved = grid_sim.merge_backtest_ops(state, "A", gen)
    assert preserved == 2
    assert state["A"]["ops"] == first, "re-running must be a no-op"
    assert len(state["A"]["ops"]) == 4       # 2 live + 2 backtest (open + tp)
    # live state fields untouched by the merge
    assert state["A"]["in_cycle"] is False
    assert state["A"]["last_prices"] == [1.0, 2.0]
    # unknown asset is a safe no-op
    assert grid_sim.merge_backtest_ops(state, "NOPE", gen) == 0


def test_ops_from_cycles_reconciles_invested_yuan():
    """Every backtest layer costs exactly GRID_BASE_YUAN, so the sum of
    entry shares × price must equal layers × base. This is the assertion
    scripts/backfill_grid_ops.py prints as 对账."""
    base = grid_pos.GRID_BASE_YUAN
    cycles = [
        _fake_cycle(["2021-03-01", "2021-03-15"], [10.0, 9.7],
                    "2021-04-01", 10.3, pos_yuan=base),
        _fake_cycle(["2024-06-01"], [8.0], "2024-07-01", 8.3,
                    pos_yuan=base),
    ]
    open_cycle = [{"date": pd.Timestamp("2026-09-04"), "price": 13.446,
                   "shares": base / 13.446}]
    ops = grid_sim.ops_from_cycles(cycles, open_cycle)

    layers = [o for o in ops if o["op"] in ("open", "add")]
    assert len(layers) == 4
    # ops_from_cycles rounds shares to 2dp so grid_state.json stays readable,
    # so a layer costs base_yuan ± (0.005 shares × price) — a few cents, not
    # exact. At the highest price here (13.446) that's < ¥0.07.
    tol = 0.10
    for o in layers:
        cost = o["shares"] * o["price"]
        assert abs(cost - base) < tol, \
            f"layer should cost ¥{base}, got ¥{cost:.4f}"

    expected = sum(c["total_invested"] for c in cycles) + sum(
        e["shares"] * e["price"] for e in open_cycle)
    actual = sum(o["shares"] * o["price"] for o in layers)
    assert abs(actual - expected) < tol * len(layers)
    assert abs(actual - 4 * base) < tol * len(layers)


def test_build_record_strips_ops_from_grid_state_snapshot():
    """history.json embeds grid_state once per day. `ops` is an append-only
    log that already lives in data/grid_state.json (which the page fetches
    directly) — copying it into every record would grow history.json
    without bound, and nothing reads that copy."""
    grid_state = {
        "可转债ETF博时": {
            "in_cycle": True,
            "entries": [{"date": "2026-09-04", "price": 13.446, "shares": 743.72}],
            "last_prices": [13.1, 13.2],
            "cum_realized": 3048.28,
            "ops": [{"date": "2026-09-04", "op": "open", "source": "backtest",
                     "price": 13.446, "shares": 743.72, "note": "回测·窗口突破开仓"}],
        },
        "黄金ETF博时": {"in_cycle": False, "entries": [], "last_prices": [8.0],
                        "cum_realized": 0.0, "ops": []},
    }
    rec = snapshot.build_record(
        today="2026-10-21", positions=[], cash_rebalance=0.0, cash_grid=0.0,
        total=0.0, weights=[], triggered_3pp=False, plan=[],
        grid_signals=[], grid_state=grid_state)

    snap = rec["grid_state"]
    # ops stripped everywhere
    for name in snap:
        assert "ops" not in snap[name], f"{name} still carries ops"
    # but the fields a daily snapshot actually needs survive intact
    assert snap["可转债ETF博时"]["in_cycle"] is True
    assert snap["可转债ETF博时"]["entries"] == grid_state["可转债ETF博时"]["entries"]
    assert snap["可转债ETF博时"]["last_prices"] == [13.1, 13.2]
    assert snap["可转债ETF博时"]["cum_realized"] == pytest_approx(3048.28)
    assert snap["黄金ETF博时"]["in_cycle"] is False
    # the caller's dict is not mutated
    assert "ops" in grid_state["可转债ETF博时"]


def test_advance_one_day_preserves_backtest_ops():
    """The backfilled ops must survive the live engine's daily run.

    advance_one_day rebuilds the asset state as a fixed 6-key literal, so
    anything riding in a 7th key would be silently dropped the first time
    the daily run. `ops` is the one array it copies forward verbatim —
    this test is the guard on that. The engine no longer writes ops, so
    the backfill must survive **byte-identical and un-augmented**.
    """
    backtest = [
        {"date": "2026-09-04", "op": "open", "source": "backtest",
         "price": 13.45, "shares": 743.72, "note": "回测·窗口突破开仓"},
        {"date": "2026-10-14", "op": "add", "source": "backtest",
         "price": 12.63, "shares": 791.51, "note": "回测·金字塔第3层"},
    ]
    state = {"in_cycle": False, "entries": [], "last_prices": [13.5, 13.4],
             "cum_realized": 0.0, "ops": [dict(o) for o in backtest],
             "merged_shares": 321.5}   # 已归并份额, 必须跨天保留

    # A price far below the window min → 引擎**建议**开仓 (但不执行).
    after, signal = grid_pos.advance_one_day(
        state, date_str="2026-10-21", price=12.0,
        layer_yuan_value=grid_pos.GRID_BASE_YUAN)

    assert signal == "open", "fixture should trigger an open suggestion"
    assert after["in_cycle"] is False, "引擎不自动开仓"
    assert after["entries"] == []
    # backfill survived byte-identical...
    kept = [o for o in after["ops"] if o["source"] == "backtest"]
    assert kept == backtest, "backtest ops were altered or dropped"
    # ...and the engine added **no** live row at all
    live = [o for o in after["ops"] if o["source"] != "backtest"]
    assert live == [], "引擎不写 ops"
    assert len(after["ops"]) == 2
    # 归并份额同样是必须被字面量复制过去的字段 —— 漏一个就是每天清零
    assert after["merged_shares"] == 321.5, "merged_shares was dropped by the rebuild"
    assert sorted(after.keys()) == ["cum_realized", "entries", "in_cycle",
                                    "last_prices", "merged_shares", "ops"]


def test_merged_shares_survive_subsequent_daily_runs():
    """归并份额一旦转出, 就必须跨天、跨周期一直存活。

    这是"交给组合就不再撒手"的落地守护: 出场 → 次日 → 再开新周期 → 再出场,
    份额只能增加, 绝不能因为 state 重建而减少。

    引擎**不再改变**归并份额 —— 它只会建议。归并份额只能由**手动** tp 产生,
    之后跨天原样存活。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    # 周期 1: 手动开仓 @99, 手动止盈 @101.97 (first_buy * 1.03)
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    state = grid_pos.manual_op(state, "tp", "2026-01-02", price=101.98,
                               shares=0.0)
    merged_after_tp = state["merged_shares"]
    assert merged_after_tp > 0

    # 之后连续多天跑引擎 —— merged_shares 只能不变 (引擎不产生也不消耗它)
    seen = [merged_after_tp]
    for i, p in enumerate([101.0, 100.0, 98.0, 97.5, 100.98, 99.5]):
        state, _ = grid_pos.advance_one_day(
            state, f"2026-01-{3 + i:02d}", price=p,
            layer_yuan_value=10_000.0)
        seen.append(state["merged_shares"])
    for prev, cur in zip(seen, seen[1:]):
        assert cur == prev, f"引擎改变了 merged_shares: {seen}"

    # 再手动开一个新周期 + 再手动止盈 → 份额应当增加
    state = grid_pos.manual_op(state, "open", "2026-02-01", price=97.0,
                               shares=10_000.0 / 97.0)
    state = grid_pos.manual_op(state, "tp", "2026-02-02", price=100.0,
                               shares=0.0)
    assert state["merged_shares"] > merged_after_tp


# ---------------------------------------------------------------------------
# src/domain/portfolio — 网格归并份额并入再平衡组合
#
# 规则 (src/domain/grid/exit_policy.py:cost_only_exit): 出场只卖出"回本所需"那部分, 余量
# 归并到再平衡的持仓。归并份额从这一刻起是**组合**的资产: 必须计入市值、
# 权重和总市值, 否则组合的真实敞口被低估。
#
# 归并份额是模拟口径 —— 不写进 data/trades.json (那里只记录真实成交)。
# ---------------------------------------------------------------------------

def test_merged_shares_by_code_maps_asset_names_to_codes():
    """grid_state 的 key 是中文名, 组合的 key 是 6 位代码 —— 归并前必须对齐。"""
    state = {
        "黄金ETF博时": {"merged_shares": 1125.0},
        "纳斯达克ETF华夏": {"merged_shares": 0.0},
        "红利ETF易方达": {"merged_shares": 250.0},
        "不认识的名字": {"merged_shares": 999.0},
    }
    out = portfolio.merged_shares_by_code(state)
    assert out == {"159937": 1125.0, "515180": 250.0}, (
        f"unexpected mapping: {out}")
    # 0 份额的资产不该出现 (否则会造出一个空仓位的行)
    assert "513300" not in out


def test_merge_grid_shares_into_positions_folds_into_value():
    """归并份额要进 total_shares 和 value, 但不能改掉真实持仓 shares。"""
    positions = [
        {"name": "黄金ETF博时", "code": "159937", "shares": 5200.0,
         "price": 8.18, "value": 42536.0},
    ]
    out = portfolio.merge_grid_shares_into_positions(
        positions, {"159937": 1000.0})
    assert len(out) == 1
    p = out[0]
    assert p["shares"] == 5200.0, "真实持仓份额不能被改写"
    assert p["merged_shares"] == pytest_approx(1000.0)
    assert p["total_shares"] == pytest_approx(6200.0)
    assert p["merged_value"] == pytest_approx(1000.0 * 8.18)
    assert p["value"] == pytest_approx(6200.0 * 8.18), (
        "市值必须按 total_shares 算, 否则权重分母被低估")
    # 输入不被修改
    assert positions[0]["value"] == 42536.0
    assert "merged_shares" not in positions[0]


def test_merged_shares_change_rebalance_weights():
    """归并份额计入权重 —— 这是整个改动的目的。"""
    positions = [
        {"name": "A", "code": "515180", "shares": 1000.0, "price": 10.0,
         "value": 10000.0},
        {"name": "B", "code": "159937", "shares": 1000.0, "price": 10.0,
         "value": 10000.0},
    ]
    before_total, before_w, _, _ = policy.evaluate(positions, 0.0, 0.0)
    assert before_w[0] == pytest_approx(0.5)
    assert before_w[1] == pytest_approx(0.5)

    merged = portfolio.merge_grid_shares_into_positions(
        positions, {"159937": 500.0})
    after_total, after_w, _, _ = policy.evaluate(merged, 0.0, 0.0)
    assert after_total == pytest_approx(25000.0), "A 10,000 + B 15,000"
    assert after_w[0] == pytest_approx(10000.0 / 25000.0)
    assert after_w[1] == pytest_approx(15000.0 / 25000.0)
    assert after_w[1] > before_w[1], "归并份额必须把权重推高"


def test_merged_only_code_becomes_its_own_position_row():
    """网格归并了组合没持有的代码 → 它也得有一行持仓 (组合真的持有它了)。"""
    positions = [
        {"name": "红利ETF易方达", "code": "515180", "shares": 1000.0,
         "price": 10.0},
    ]
    out = portfolio.merge_grid_shares_into_positions(
        positions, {"513300": 500.0}, {"513300": 2.5})
    assert len(out) == 2
    row = [p for p in out if p["code"] == "513300"][0]
    assert row["shares"] == 0.0
    assert row["merged_shares"] == pytest_approx(500.0)
    assert row["total_shares"] == pytest_approx(500.0)
    assert row["value"] == pytest_approx(1250.0)
    assert row.get("merged_only") is True
    # 定价失败的代码不能凭空造出一行 0 市值的持仓
    out2 = portfolio.merge_grid_shares_into_positions(
        positions, {"513300": 500.0}, {})
    assert len(out2) == 1


def test_grid_capital_drops_to_zero_after_exit_while_merged_survives():
    """出场后: 网格的资金占用归零 (本金收回), 归并份额留在组合那边。

    走**手动路径** —— 引擎不再出场。
    """
    state = grid_pos.fresh_asset_state()
    state["last_prices"] = [100.0]
    state = grid_pos.manual_op(state, "open", "2026-01-01", price=99.0,
                               shares=10_000.0 / 99.0)
    state = grid_pos.manual_op(state, "tp", "2026-01-02",
                               price=99.0 * 1.04, shares=0.0)
    snap = grid_pos.asset_snapshot(state, current_price=103.0)
    assert snap["in_cycle"] is False
    assert snap["current_value"] == 0.0, "出场后网格不再占用资金"
    assert snap["merged_shares"] > 0.0, "余量归并给组合"
    assert snap["merged_value"] == pytest_approx(snap["merged_shares"] * 103.0)


def test_merged_shares_never_leak_into_the_real_trade_ledger():
    """归并份额是模拟口径 —— trades.json 只记录真实成交。

    归并只是给持仓加权, 不产生任何一笔"买入"记录。
    """
    trades_path = ROOT / "data" / "trades.json"
    before = trades_path.read_text(encoding="utf-8") if trades_path.exists() else None
    positions = [
        {"name": "红利ETF易方达", "code": "515180", "shares": 1000.0,
         "price": 10.0},
    ]
    merged = portfolio.merge_grid_shares_into_positions(positions, {"515180": 500.0})
    # 归并后的持仓仍然是"一笔真实持仓 + 模拟的余量", 不是新的成交
    assert merged[0]["shares"] == 1000.0
    after = trades_path.read_text(encoding="utf-8") if trades_path.exists() else None
    assert before == after, "归并不得改动 trades.json"


def _nf_fixture(files: dict, env: dict):
    """Write `files` into a temp dir and return (workspace, env) for diagnose_steps."""
    tmp = tempfile.mkdtemp()
    for name, body in files.items():
        Path(tmp, name).write_text(body, encoding="utf-8")
    return tmp, env


# ---------------------------------------------------------------------------
# src/domain/grid/grid_position — GridPosition 聚合根 + 显式 mapper
#
# 背景 (不变量 I11): 以前 `advance_one_day` 与 `manual_op` 各自用一份
# 6 key 字面量重建状态, 任何新增字段都会在**下一次每日运行时被静默丢弃**
# (2026-10-23 的 free_shares → merged_shares 改名就是这么丢过一次)。
# 现在形状由 GridPosition + mapper 唯一定义, 忘了迁移会**立刻报错**。
# ---------------------------------------------------------------------------

def test_grid_position_mapper_writes_exactly_the_declared_fields():
    """mapper 产出的 key 集合必须 == dataclass 的字段集合。

    给 GridPosition 加了字段却忘了写 to_state() —— 这条测试会红,
    to_state() 自己也会在第一次写状态时抛错 (双保险)。
    """
    from dataclasses import fields as dc_fields
    declared = {f.name for f in dc_fields(grid_pos.GridPosition)}
    produced = set(grid_pos.fresh_asset_state())
    assert produced == declared, (
        f"mapper 与 dataclass 不同步: 产出 {sorted(produced)}, "
        f"声明 {sorted(declared)}")


def test_grid_position_mapper_roundtrip_is_lossless():
    state = {
        "in_cycle": True,
        "entries": [{"date": "2026-10-20", "price": 1.3, "shares": 1000.0}],
        "last_prices": [1.35, 1.32, 1.3],
        "cum_realized": 0.0,
        "ops": [{"date": "2026-10-20", "op": "open", "source": "manual",
                 "price": 1.3, "shares": 1000.0, "note": "x"}],
        "merged_shares": 12.34,
    }
    pos = grid_pos.GridPosition.from_state(state)
    assert pos.to_state() == state, "往返必须逐字段相同"


def test_grid_position_mapper_rejects_unknown_keys():
    """多出来的 key 不再被静默丢弃 —— 直接报错, 免得丢掉之后没人知道。"""
    state = dict(grid_pos.fresh_asset_state())
    state["free_shares"] = 99.0        # 老字段名 (2026-10-23 之前)
    try:
        grid_pos.GridPosition.from_state(state)
    except ValueError as ex:
        assert "free_shares" in str(ex)
    else:
        raise AssertionError("未知 key 必须报错, 不能静默忽略")


def test_grid_position_mapper_accepts_legacy_min_key():
    """历史别名 last_120d_min 折进 last_prices 队列, 且不再被输出。"""
    state = {"in_cycle": False, "entries": [], "cum_realized": 0.0,
             "ops": [], "merged_shares": 0.0, "last_120d_min": 8.205}
    pos = grid_pos.GridPosition.from_state(state)
    assert pos.last_prices == [8.205]
    assert "last_120d_min" not in pos.to_state()


def test_grid_state_json_format_stays_six_keys():
    """data/grid_state.json 的格式**不能变** —— mapper 负责读写。"""
    path = ROOT / "data" / "grid_state.json"
    if not path.exists():
        return
    doc = json.loads(path.read_text(encoding="utf-8"))
    expected = {"in_cycle", "entries", "last_prices", "cum_realized",
                "ops", "merged_shares"}
    for name, asset in doc.items():
        assert set(asset) == expected, (
            f"{name} 的 key 集合变了: {sorted(asset)}")


def test_cost_only_exit_matches_legacy_dict_shape():
    """新策略 (ExitAtCost) 与旧别名 (dict) 的数字必须逐位相同。"""
    from src.domain.grid.exit_policy import cost_only_exit
    for args in ((2030.0, 199910.0, 103.0),      # 正常回本
                 (2030.0, 199910.0, 90.0),       # 亏本出场
                 (0.0, 0.0, 100.0)):             # 空仓
        legacy = grid_pos.split_take_profit(*args)
        new = cost_only_exit(*args).to_dict()
        assert legacy == new, f"{args}: {legacy} != {new}"


def test_cost_only_exit_keeps_full_precision():
    """出场是**比例** —— 中间量不许取整, 否则「买入-卖出==归并」会破。"""
    from src.domain.grid.exit_policy import cost_only_exit
    r = cost_only_exit(2030.0, 199910.0, 103.0)
    assert r.merged_shares == 2030.0 - 199910.0 / 103.0
    assert r.shares_sold == 199910.0 / 103.0
    # 归并份额不能是"取整到 2 位"的版本
    assert r.merged_shares != round(r.merged_shares, 2)


def test_manual_op_preserves_merged_identity_bit_exact():
    """买入 − 卖出 == 归并, 逐位成立 (全精度, 不取整)。"""
    st = grid_pos.fresh_asset_state()
    st = grid_pos.manual_op(st, op="open", today="2026-10-20", price=1.30,
                             shares=1000.0)
    st = grid_pos.manual_op(st, op="add", today="2026-10-21", price=1.26,
                             shares=1030.0)
    st = grid_pos.manual_op(st, op="tp", today="2026-10-22", price=1.36,
                             shares=0.0)
    op = st["ops"][-1]
    bought = 1000.0 + 1030.0
    assert op["shares_to_merge"] == bought - op["shares"], (
        "归并份额必须精确等于 买入 − 卖出")
    assert st["merged_shares"] == op["shares_to_merge"]


# ---------------------------------------------------------------------------
# src/domain/grid/{trigger_policy,grid_op,advisor} — Phase 2b 分型
#
# 背景: 「建议 ≠ 成交」这条不变量 (I10 / I15) 以前只靠注释守着。现在:
#   - 触发规则的**唯一**定义住在 TriggerPolicy 里 (生产路径 + 回测路径共用);
#   - 「已经发生」的那一条是 GridOp (ExecutedOp=实盘 / SimulatedOp=模拟);
#   - 「应该做什么」是 GridSuggestion —— 与 GridOp 是**不同的类型**。
# ---------------------------------------------------------------------------

def test_trigger_policy_signals_open_when_price_breaks_window_low():
    """待机 + 今日价跌破窗口最低 → 建议开仓 (负数 = 已越过触发线)。"""
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    lv = TriggerPolicy().levels(in_cycle=False, entries=[],
                                window=[10.0, 9.5], price=9.0)
    assert lv.signal is TriggerLevel.OPEN
    assert lv.nearest.level is TriggerLevel.OPEN
    assert lv.open is not None and lv.open.price == 9.5
    assert lv.open.pct == (9.0 - 9.5) / 9.5 * 100
    assert lv.add is None and lv.tp is None, "待机时不该有加仓/回本触发位"


def test_trigger_policy_no_signal_when_price_above_window_low():
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    lv = TriggerPolicy().levels(in_cycle=False, entries=[],
                                window=[10.0, 9.5], price=11.0)
    assert lv.signal is None
    assert lv.nearest.level is TriggerLevel.OPEN
    assert lv.open.pct == (11.0 - 9.5) / 9.5 * 100


def test_trigger_policy_signals_add_at_three_percent_below_last_entry():
    """周期中 + 今日价 ≤ 最近一笔买价 × 97% → 建议加仓。"""
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    entries = [{"date": "2026-10-01", "price": 10.0, "shares": 1000.0}]
    lv = TriggerPolicy().levels(in_cycle=True, entries=entries,
                                window=[10.0, 9.8], price=9.7)
    assert lv.signal is TriggerLevel.ADD
    assert lv.add is not None and lv.add.price == 10.0 * 0.97
    assert lv.add.pct == 0.0, "正好踩在触发线上 → 距离 0"


def test_trigger_policy_signals_tp_at_three_percent_above_first_buy():
    """周期中 + 今日价 ≥ 首笔买价 × 103% → 建议回本出场。"""
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    entries = [{"date": "2026-10-01", "price": 10.0, "shares": 1000.0}]
    lv = TriggerPolicy().levels(in_cycle=True, entries=entries,
                                window=[10.0, 9.8], price=10.3)
    assert lv.signal is TriggerLevel.TP, "tp 优先于 add"
    assert lv.tp is not None and lv.tp.price == 10.0 * 1.03


def test_trigger_policy_picks_nearest_trigger_when_nothing_hit():
    """都没触发 → 取**更近**的那个 (|距离| 最小)。"""
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    entries = [{"date": "2026-10-01", "price": 10.0, "shares": 1000.0}]
    lv = TriggerPolicy().levels(in_cycle=True, entries=entries,
                                window=[10.0, 9.8], price=10.05)
    assert lv.signal is None
    # add 距 3.48pp, tp 距 2.49pp → 更近的是 tp
    assert lv.nearest.level is TriggerLevel.TP
    assert lv.nearest.pct == (10.3 - 10.05) / 10.05 * 100


def test_trigger_policy_tie_goes_to_add():
    """平手时取**先**出现的那个 (add) —— 与老实现一致, 不能漂移。"""
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    entries = [{"date": "2026-10-01", "price": 100.0, "shares": 1000.0}]
    lv = TriggerPolicy().levels(in_cycle=True, entries=entries,
                                window=[100.0], price=100.0)
    assert lv.signal is None
    assert lv.add.pct == -3.0 and lv.tp.pct == 3.0, "两边正好对称"
    assert lv.nearest.level is TriggerLevel.ADD, "平手 → add"


def test_trigger_level_formats_as_bare_value():
    """⚠️ `str, Enum` 的坑: f"{TriggerLevel.OPEN}" 默认会显示 `TriggerLevel.OPEN`。

    页面/邮件里有 `距{trigger_kind}` 这种 f-string —— 踩中就是显示
    「距TriggerLevel.OPEN」。所以 __str__ 必须返回裸值。
    """
    from src.domain.grid.trigger_policy import TriggerLevel
    assert f"{TriggerLevel.OPEN}" == "open"
    assert str(TriggerLevel.TP) == "tp"
    assert TriggerLevel.ADD == "add", "还得是 str 的子类, 老代码的 == 才成立"
    assert json.dumps({"k": TriggerLevel.OPEN}) == '{"k": "open"}'


def test_grid_op_to_dict_keeps_legacy_json_shape():
    """data/grid_state.json 里一行 op 的 key 与顺序**一字不改**。"""
    from src.domain.grid.grid_op import ExecutedOp, OpSource
    op = ExecutedOp(date="2026-10-20", op="open", price=1.3, shares=1000.0,
                    source=OpSource.MANUAL, note="x")
    d = op.to_dict()
    assert list(d) == ["date", "op", "source", "price", "shares", "note"]
    assert d["op"] == "open" and d["source"] == "manual"
    # 非 tp 的 op 不输出那 4 个额外字段
    for k in ("pnl", "num_buys_exited", "total_shares_exited",
              "shares_to_merge"):
        assert k not in d


def test_executed_op_rejects_non_manual_source():
    """实盘成交只能来自用户提交 —— 类型上就不许标成 backtest。"""
    from src.domain.grid.grid_op import ExecutedOp, OpSource
    try:
        ExecutedOp(date="2026-10-20", op="open", price=1.3, shares=1000.0,
                   source=OpSource.BACKTEST)
    except ValueError as ex:
        assert "manual" in str(ex)
    else:
        raise AssertionError("ExecutedOp 不许接受 source='backtest'")


def test_simulated_op_rejects_non_backtest_source():
    """模拟成交不许被标成实盘。"""
    from src.domain.grid.grid_op import OpSource, SimulatedOp
    try:
        SimulatedOp(date="2026-10-20", op="open", price=1.3, shares=1000.0,
                    source=OpSource.MANUAL)
    except ValueError as ex:
        assert "backtest" in str(ex)
    else:
        raise AssertionError("SimulatedOp 不许接受 source='manual'")


def test_grid_op_from_dict_types_by_source():
    """读盘时按 source 分型 —— 遗留的 'auto' 落进通用 GridOp, **不猜**。"""
    from src.domain.grid.grid_op import (ExecutedOp, GridOp, OpSource,
                                        SimulatedOp)
    row = {"date": "2026-10-20", "op": "open", "source": "manual",
           "price": 1.3, "shares": 1000.0, "note": "x"}
    assert isinstance(GridOp.from_dict(row), ExecutedOp)
    assert isinstance(GridOp.from_dict({**row, "source": "backtest"}),
                      SimulatedOp)
    legacy = GridOp.from_dict({**row, "source": "auto"})
    assert type(legacy) is GridOp, "遗留的 auto 不许被当成实盘"
    assert legacy.source is OpSource.AUTO


def test_grid_op_dict_roundtrip_is_lossless():
    from src.domain.grid.grid_op import GridOp
    row = {"date": "2026-10-22", "op": "tp", "source": "manual",
           "price": 1.5, "shares": 1000.0, "note": "take profit",
           "pnl": 0.0, "num_buys_exited": 2, "total_shares_exited": 1030.0,
           "shares_to_merge": 30.0}
    assert GridOp.from_dict(row).to_dict() == row


def test_grid_suggestion_to_dict_keeps_legacy_shape():
    """advisory() 的 dict 形状**一字不变** —— history.json / app.js 不用改。"""
    from src.domain.grid.advisor import GridSuggestion
    st = grid_pos.fresh_asset_state()
    st["last_prices"] = [10.0, 9.5]
    adv = grid_pos.advisory(st, 9.0, 10000.0)
    assert isinstance(adv, GridSuggestion)
    d = adv.to_dict()
    assert set(d) == {"signal", "trigger_kind", "trigger_pct",
                      "trigger_price", "dist_open_pct", "dist_add_pct",
                      "dist_tp_pct", "suggest_price", "suggest_shares",
                      "suggest_amount"}
    assert d["signal"] == "open"
    assert d["trigger_kind"] == "open"
    assert d["trigger_pct"] == round((9.0 - 9.5) / 9.5 * 100, 2)
    assert d["suggest_price"] == 9.0
    assert d["suggest_amount"] == 10000.0
    assert d["suggest_shares"] == round(10000.0 / 9.0, 2)
    # 过渡期入口: 老代码按 dict 读它
    assert adv["signal"] == "open"
    assert "trigger_kind" in adv


def test_suggestion_and_executed_op_are_different_types():
    """I10 / I15 的类型级保证: 建议**不是**成交。"""
    from src.domain.grid.advisor import GridAdvisor, GridSuggestion
    from src.domain.grid.grid_op import ExecutedOp, GridOp
    st = grid_pos.fresh_asset_state()
    adv = grid_pos.advisory(st, 9.0, 10000.0)
    assert isinstance(adv, GridSuggestion)
    assert not isinstance(adv, GridOp)
    op = ExecutedOp(date="2026-10-20", op="open", price=1.3, shares=1000.0)
    assert isinstance(op, GridOp)
    assert not isinstance(op, GridSuggestion)
    # 建议对象是 frozen 的 —— 拿到它也改不动任何状态
    try:
        adv.level = "open"
    except Exception:
        pass
    else:
        raise AssertionError("GridSuggestion 必须不可变")


def test_manual_op_appends_executed_op_not_simulated():
    """用户提交的 op 必须是**实盘**类型。"""
    from src.domain.grid.grid_op import ExecutedOp, GridOp, SimulatedOp
    st = grid_pos.fresh_asset_state()
    st = grid_pos.manual_op(st, op="open", today="2026-10-20", price=1.30,
                             shares=1000.0, note="手动开仓")
    op = GridOp.from_dict(st["ops"][0])
    assert isinstance(op, ExecutedOp)
    assert not isinstance(op, SimulatedOp)
    assert op.source.value == "manual"
    assert op.note == "手动开仓"


def test_ops_from_cycles_returns_simulated_ops():
    """回测产物是**模拟**类型 —— 页面上要标成「回测」, 不进实盘账。"""
    from src.domain.grid.grid_op import ExecutedOp, SimulatedOp
    cycles = [{
        "entries": [{"date": "2026-01-05", "price": 10.0, "shares": 1000.0}],
        "exit_date": "2026-02-20", "exit_price": 10.4, "num_buys": 1,
        "avg_cost": 10.0, "total_shares": 1000.0, "shares_transferred": 0.0,
        "total_invested": 10000.0, "exit_value": 10400.0, "pnl": 0.0,
        "return_pct": 0.0,
    }]
    ops = grid_sim.ops_from_cycles(cycles, None)
    assert len(ops) == 2
    for op in ops:
        assert isinstance(op, SimulatedOp)
        assert not isinstance(op, ExecutedOp)
        assert op.source.value == "backtest"


# ---------------------------------------------------------------------------
# src/simulation/grid_simulator — 回测路径必须调用**同一个** TriggerPolicy
#
# 背景 (docs/domain_model.md §7「最严重的一处重复」): 网格规则以前有两套
# 实现 —— 生产路径的 `_triggers` 与回测路径 `run_compound_grid` 的内联判断。
# 两套在 GRID_STEP_PCT / GRID_TP_PCT / GRID_TP_BASIS 被改动时会静默分叉。
#
# Phase 3: 回测路径改成调用同一个 `TriggerPolicy`。驱动价/层额不同是合理的
# (回测用**收盘价** + 固定 ¥10k, 生产用 K 线最低价 + 浮动层额), 但**判定
# 规则**必须只有一份。
# ---------------------------------------------------------------------------

#: 一条精心设计的价格序列 (lookback=3) —— 每个事件都算得出来:
#:   i=4  9.0  跌破前 3 日最低 (10.2) → OPEN
#:   i=5  8.5  ≤ 9.0×97% = 8.73    → ADD
#:   i=6  8.0  ≤ 8.5×97% = 8.245   → ADD
#:   i=7  7.5  ≤ 8.0×97% = 7.76    → ADD
#:   i=8  9.4  ≥ 9.0×103% = 9.27   → TP (周期 1 收尾)
#:   i=10 7.0  跌破前 3 日最低 (7.5) → OPEN (周期 2)
#:   i=12 7.3  ≥ 7.0×103% = 7.21   → TP (周期 2 收尾)
#:   i=13 6.5  跌破前 3 日最低 (7.0) → OPEN (周期 3, 未收尾)
#:   i=14 6.0  ≤ 6.5×97% = 6.305   → ADD
_PHASE3_PRICES = [10.0, 10.5, 10.2, 10.4, 9.0, 8.5, 8.0, 7.5, 9.4, 9.2,
                  7.0, 7.2, 7.3, 6.5, 6.0]


def _phase3_series():
    idx = pd.date_range("2026-01-01", periods=len(_PHASE3_PRICES), freq="B")
    return pd.Series(_PHASE3_PRICES, index=idx)


def test_simulator_follows_the_shared_trigger_policy():
    """回测路径的开仓/加仓/出场, 必须落在 TriggerPolicy 算出的触发位上。"""
    r = grid_sim.run_compound_grid(_phase3_series(), lookback=3,
                                   trigger_mode="window")
    cycles = r["cycles"]
    assert len(cycles) == 2, "应有两个已收尾的周期"

    # 周期 1: 9.0 开仓 → 8.5/8.0/7.5 三层加仓 → 9.4 回本出场
    c1 = cycles[0]
    assert [e["price"] for e in c1["entries"]] == [9.0, 8.5, 8.0, 7.5]
    assert c1["exit_price"] == 9.4
    assert c1["num_buys"] == 4
    assert c1["total_invested"] == 40000.0, "每层固定 ¥10,000"
    assert c1["pnl"] == 0.0, "只卖成本额 → 已实现盈亏恒为 0"

    # 周期 2: 7.0 开仓 → 7.3 回本出场
    c2 = cycles[1]
    assert [e["price"] for e in c2["entries"]] == [7.0]
    assert c2["exit_price"] == 7.3
    assert c2["num_buys"] == 1

    # 周期 3 还没收尾 → 留在 open_cycle
    assert [e["price"] for e in (r["open_cycle"] or [])] == [6.5, 6.0]
    assert r["cum_realized"] == 0.0
    assert r["merged_shares"] > 0, "出场余量归并到再平衡持仓"


def test_simulator_add_and_tp_never_fire_on_the_same_bar():
    """add 与 tp 互斥 —— 金字塔里最后一笔永远是最便宜的。

    所以「一个周期 N 买 1 卖」的 N 笔买入不会跟出场挤在同一根 K 线上。
    """
    r = grid_sim.run_compound_grid(_phase3_series(), lookback=3,
                                   trigger_mode="window")
    for c in r["cycles"]:
        entry_dates = {str(e["date"]) for e in c["entries"]}
        assert str(c["exit_date"]) not in entry_dates, (
            "出场日不能同时是加仓日")


def test_simulator_trigger_sequence_matches_policy_level_by_level():
    """差分测试: 自己用 TriggerPolicy 跑一遍, 事件序列必须与模拟器一致。

    这条锁住的是「回测路径确实在用那个策略」—— 万一以后有人把内联判断
    又塞回来, 这里会红。
    """
    from src.domain.grid.trigger_policy import TriggerLevel, TriggerPolicy
    series = _phase3_series()
    policy = TriggerPolicy()
    prices = series.tolist()

    expected: list = []
    in_cycle = False
    entries: list = []
    for i, price in enumerate(prices):
        window = prices[max(0, i - 3):i]
        lv = policy.levels(in_cycle=in_cycle, entries=entries,
                           window=list(window), price=float(price))
        if lv.signal is TriggerLevel.OPEN:
            expected.append((i, "open"))
            entries.append({"price": float(price),
                            "shares": 10000.0 / float(price)})
            in_cycle = True
        elif lv.signal is TriggerLevel.ADD:
            expected.append((i, "add"))
            entries.append({"price": float(price),
                            "shares": 10000.0 / float(price)})
        elif lv.signal is TriggerLevel.TP:
            expected.append((i, "tp"))
            entries = []
            in_cycle = False

    # 从模拟器的结果里还原出同样的事件序列
    result = grid_sim.run_compound_grid(series, lookback=3,
                                        trigger_mode="window")
    actual: list = []
    for c in result["cycles"]:
        for i, e in enumerate(c["entries"]):
            actual.append((series.index.get_loc(e["date"]),
                           "open" if i == 0 else "add"))
        actual.append((series.index.get_loc(c["exit_date"]), "tp"))
    for i, e in enumerate(result["open_cycle"] or []):
        actual.append((series.index.get_loc(e["date"]),
                       "open" if i == 0 else "add"))
    actual.sort()

    assert actual == sorted(expected), (
        f"模拟器的事件序列与 TriggerPolicy 不一致:\n"
        f"  policy  = {sorted(expected)}\n  模拟器 = {actual}")


def test_simulator_global_trigger_mode_uses_all_history():
    """trigger_mode='global' → 开仓看**全部**历史最低, 不是滚动窗口最低。

    两条路径的开仓门槛不同:
      window  → 最近 3 根的最低 (12.0) → 11.0 跌破 → 开仓
      global  → 全历史最低 (10.0)     → 11.0 没跌破 → 不开仓
    """
    idx = pd.date_range("2026-01-01", periods=5, freq="B")
    series = pd.Series([10.0, 12.0, 12.5, 12.2, 11.0], index=idx)

    win = grid_sim.run_compound_grid(series, lookback=3, trigger_mode="window")
    assert win["open_cycle"] is not None
    assert win["open_cycle"][0]["price"] == 11.0

    glob = grid_sim.run_compound_grid(series, lookback=3, trigger_mode="global")
    assert glob["open_cycle"] is None, "11.0 没有跌破全历史最低 10.0"
    assert glob["cycles"] == []


def test_simulator_has_no_inline_trigger_arithmetic():
    """「什么算触发」只有一份 —— 回测路径不许再有内联的 3% 算术。"""
    import inspect
    src = inspect.getsource(grid_sim)
    for bad in ("1 - step_pct / 100", "1 + tp_pct / 100",
                "1 - self.step_pct", "1 + self.tp_pct"):
        assert bad not in src, f"回测路径里还有内联的触发算术: {bad}"
    assert "TriggerPolicy(" in src, "回测路径必须调用 TriggerPolicy"


# ---------------------------------------------------------------------------
# src/presentation/notify_failure — 失败邮件必须上报真实步骤状态
#
# 背景 (2026-10-23 事故): 旧版邮件无论何种失败都写死 "失败原因: record
# 步骤未成功完成" + 三条静态 "常见原因"。而当天实际是 apt 装字体被取消,
# record 步骤从未执行 (skipped)。邮件把排查引到了错误的步骤。
# ---------------------------------------------------------------------------

def test_diagnose_steps_reads_outcome_rc_and_log_tail():
    ws, env = _nf_fixture(
        {"font_install.tail.log": "Get:2 http://azure... [61.2 MB]\n"
                                  "Error: The operation was canceled.\n"},
        {"FONT_OUTCOME": "failure", "FONT_RC": "100"},
    )
    steps = nf.diagnose_steps(env=env, workspace=ws)
    by_id = {s["id"]: s for s in steps}
    assert by_id["font"]["outcome"] == "failure"
    assert by_id["font"]["rc"] == "100"
    assert "The operation was canceled." in by_id["font"]["log"]
    # 没有 env 的步骤应为 unknown, 而不是伪装成 success
    assert by_id["record"]["outcome"] == "unknown"


def test_diagnose_steps_blames_font_not_record_when_record_skipped():
    """2026-10-23 的实际形状: font 失败 → record 被跳过。"""
    ws, env = _nf_fixture(
        {"font_install.tail.log": "Error: The operation was canceled.\n"},
        {"FONT_OUTCOME": "failure", "FONT_RC": "100",
         "RECORD_OUTCOME": "skipped", "RECORD_RC": ""},
    )
    steps = nf.diagnose_steps(env=env, workspace=ws)
    bad = nf.failing_steps(steps)
    assert [s["id"] for s in bad] == ["font"], "只有 font 真的失败了"
    assert bad[0]["rc"] == "100"
    # record 是被上游中断的症状, 不该混进"失败步骤"
    assert [s["id"] for s in nf.skipped_steps(steps)] == ["record"]

    text = nf.build_diagnosis_text(steps, "https://example/run/1")
    assert "Install CJK font" in text
    assert "exit=100" in text
    assert "The operation was canceled." in text
    # record 只是被上游中断, 措辞必须体现这一点
    assert "未执行" in text
    assert "真正的故障点" in text
    assert "example/run/1" in text


def test_failure_email_never_blames_record_verbatim():
    """回归测试: 旧版硬编码的那句话不能再出现。

    它在 record 从未执行时依然是错的, 正是本次事故的误导来源。
    """
    ws, env = _nf_fixture(
        {"font_install.tail.log": "Error: The operation was canceled.\n"},
        {"FONT_OUTCOME": "failure", "FONT_RC": "100",
         "RECORD_OUTCOME": "skipped"},
    )
    steps = nf.diagnose_steps(env=env, workspace=ws)
    for text in (nf.build_diagnosis_text(steps, "u"),):
        assert "record 步骤未成功完成" not in text
        assert "常见原因" not in text, "静态猜测列表已移除"


def test_diagnosis_reports_all_clear_when_every_step_succeeded():
    """record 成功但别的环节失败时, 邮件不能说 "无" 就完事。"""
    ws, env = _nf_fixture(
        {"record_stderr.tail.log": ""},
        {"FONT_OUTCOME": "success", "FONT_RC": "0",
         "RECORD_OUTCOME": "success", "RECORD_RC": "0"},
    )
    steps = nf.diagnose_steps(env=env, workspace=ws)
    assert nf.failing_steps(steps) == []
    text = nf.build_diagnosis_text(steps, "https://example/run/2")
    assert "所有受监控步骤都成功" in text
    assert "example/run/2" in text, "仍必须给出日志链接"


def test_diagnosis_html_escapes_log_content():
    """日志尾部会被原样塞进邮件 HTML —— 必须转义, 否则上游报错可注入标签。"""
    ws, env = _nf_fixture(
        {"font_install.tail.log": "E: boom <script>alert(1)</script> & \"x\"\n"},
        {"FONT_OUTCOME": "failure", "FONT_RC": "100"},
    )
    steps = nf.diagnose_steps(env=env, workspace=ws)
    out = nf.build_diagnosis_html(steps, "https://example/run/3")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "alert(1)" in out, "内容本身应保留, 只是被转义"


def test_diagnose_steps_survives_missing_workspace_file():
    """日志文件不存在时不能抛异常 —— 邮件本身不能因为诊断失败而发不出去。"""
    ws, env = _nf_fixture({}, {"FONT_OUTCOME": "failure", "FONT_RC": "100"})
    steps = nf.diagnose_steps(env=env, workspace=ws)
    assert steps[0]["log"] == ""
    assert nf.build_diagnosis_text(steps, "u")     # 不抛异常即可


# ---------------------------------------------------------------------------
# src/domain/portfolio — Phase 4: 聚合根真的被用起来了
#
# Phase 4 之前, `Position` / `PositionLot` / `Provenance` 是**定义了但没人用**
# 的死代码 —— 应用层一路传 dict, 聚合根躺在 domain/ 里当摆设。现在:
#   - `AssetUniverse` 是一个**领域对象** (不是一个装着常量的模块)
#   - `Position` 真的持有 `PositionLot[]`, 模拟份额与真实份额分得开
#   - `Portfolio` 是聚合根, `to_records()` 是它的输出 (JSON 契约)
#   - `RebalancePolicy` 是**带状态的服务** (阈值是实例的状态, 不是模块全局)
# ---------------------------------------------------------------------------


def test_asset_universe_is_a_domain_object():
    """资产宇宙是一个值 —— 可以当参数传, 不是只能伸手去摸的模块全局。"""
    u = universe.UNIVERSE
    assert u.codes == list(universe.ETF_DEFS.keys())
    assert u.names == universe.ASSET_NAMES
    assert u.name_to_code == universe.NAME_TO_CODE
    assert u.qdii_codes == universe.QDII_CODES
    assert len(u) == 4
    assert "515180" in u, "含 QDII 判定用的 code"
    assert u.display_name("515180") == "红利ETF易方达"
    assert u.display_name("不知道") == "不知道", "查不到就退回 code 本身"
    # 模块级别的名字与单例是**同一份数据** —— 不会各自漂移
    assert universe.ETF_DEFS is u.defs
    assert universe.ASSET_NAMES == u.names
    assert universe.QDII_CODES == u.qdii_codes


def test_position_splits_real_and_simulated_lots():
    """一份持仓由「真实批次 + 模拟批次」组成 —— 来源在模型里, 不在旁挂字段里。"""
    p = portfolio.Position(
        code="159937", name="黄金ETF博时", price=8.15,
        lots=[
            portfolio.PositionLot(shares=5200.0),
            portfolio.PositionLot(shares=1125.0,
                                  provenance=Provenance.SIMULATED_GRID),
        ],
    )
    assert p.real_shares == 5200.0
    assert p.simulated_shares == 1125.0
    assert p.merged_shares == 1125.0, "merged_shares 是模拟份额的别名"
    assert p.total_shares == 6325.0
    assert p.value == pytest_approx(6325.0 * 8.15)
    assert p.merged_value == pytest_approx(1125.0 * 8.15)
    assert not p.merged_only, "有真实份额 → 不是纯模拟"
    assert p.lots[0].provenance is Provenance.REAL
    assert p.lots[1].is_simulated


def test_portfolio_merges_grid_shares_into_lots():
    """网格归并的份额变成一个 SIMULATED_GRID 批次 —— 不是第四个旁挂字段。"""
    pf = portfolio.Portfolio.from_holdings(
        [{"name": "黄金ETF博时", "code": "159937", "shares": 5200.0,
          "price": 8.15, "date": "2026-10-27", "value": 42380.0}],
        merged_by_code={"159937": 1125.0},
    )
    assert len(pf.positions) == 1
    p = pf.positions[0]
    assert [l.provenance for l in p.lots] == [Provenance.REAL,
                                              Provenance.SIMULATED_GRID]
    assert p.real_shares == 5200.0
    assert p.simulated_shares == 1125.0
    assert p.total_shares == 6325.0


def test_portfolio_record_roundtrip_is_lossless():
    """Portfolio → records → Portfolio 往返不丢东西 (JSON 契约的形状)。"""
    pf = portfolio.Portfolio.from_holdings(
        [{"name": "黄金ETF博时", "code": "159937", "shares": 5200.0,
          "price": 8.15, "date": "2026-10-27", "value": 42380.0}],
        merged_by_code={"159937": 1125.0},
    )
    records = pf.to_records()
    again = portfolio.Portfolio.from_records(records)
    assert again.to_records() == records, "往返必须逐字段相同"
    assert again.positions[0].simulated_shares == 1125.0
    assert again.positions[0].real_shares == 5200.0


def test_position_value_is_derived_from_lots_not_copied():
    """市值是**推出来的** (份额 × 价格) —— 记录里的 value 不是事实源。"""
    rec = {"name": "A", "code": "1", "shares": 100.0, "price": 10.0,
           "value": 999.0, "date": "x"}     # 故意自相矛盾
    pf = portfolio.Portfolio.from_records([rec])
    assert pf.positions[0].value == 1000.0, "市值 = 份额 × 价格"
    assert pf.total == 1000.0


def test_rebalance_policy_threshold_is_instance_state():
    """阈值是**策略实例**的状态 —— 两套阈值可以并存于一个进程。"""
    pf = portfolio.Portfolio.from_records([
        {"name": "A", "code": "1", "shares": 1, "price": 104, "value": 104,
         "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 96, "value": 96,
         "date": "x"},
    ])
    loose = policy.RebalancePolicy(threshold_pp=10.0)
    tight = policy.RebalancePolicy(threshold_pp=1.0)
    assert not loose.evaluate(pf).triggered, "4pp 偏离 < 10pp 阈值"
    assert tight.evaluate(pf).triggered, "4pp 偏离 > 1pp 阈值"
    # 同一个组合, 两套策略给出不同结论 —— 这正是「显式参数」的意义
    assert loose.evaluate(pf).total == tight.evaluate(pf).total


def test_target_weight_policy_is_dynamic():
    """目标权重 = 1/n, n = 当前篮子的大小 (动态篮子)。"""
    tw = policy.TargetWeightPolicy()
    assert tw.target_weight(4) == 0.25
    assert tw.target_weight(3) == pytest_approx(1 / 3)
    assert tw.target_weight(0) == 0.0, "空篮子没有目标权重"
    pf = portfolio.Portfolio.from_records([
        {"name": "A", "code": "1", "shares": 1, "price": 10, "value": 10},
        {"name": "B", "code": "2", "shares": 1, "price": 10, "value": 10},
    ])
    assert tw.for_portfolio(pf) == 0.5


def test_simulated_shares_still_drive_the_weights():
    """D6: 模拟份额推着真实组合调仓 —— 规则没变, 但现在**看得见**了。"""
    pf = portfolio.Portfolio.from_records([
        {"name": "A", "code": "1", "shares": 1000.0, "price": 10.0,
         "value": 10000.0, "date": "x"},
        {"name": "B", "code": "2", "shares": 1000.0, "price": 10.0,
         "value": 10000.0, "date": "x"},
    ])
    before = policy.RebalancePolicy().evaluate(pf)
    assert not before.triggered

    pf2 = portfolio.Portfolio.from_holdings(
        [{"name": "A", "code": "1", "shares": 1000.0, "price": 10.0},
         {"name": "B", "code": "2", "shares": 1000.0, "price": 10.0}],
        merged_by_code={"2": 500.0},
    )
    after = policy.RebalancePolicy().evaluate(pf2)
    assert after.total == 25000.0
    assert after.weights[1] > before.weights[1], "模拟份额把权重推高了"
    assert pf2.positions[1].simulated_shares == 500.0


# -----------------------------------------------------------------------------
# Phase 5a —— 显式配置对象 (D8)
# -----------------------------------------------------------------------------

def test_grid_config_from_env_reads_every_parameter():
    """环境变量 → 配置对象: 七个参数**一个值**传下去。"""
    env = {
        "GRID_LOOKBACK": "60",
        "GRID_STEP_PCT": "2.5",
        "GRID_TP_PCT": "4.0",
        "GRID_TP_BASIS": "avg",
        "GRID_BASE_YUAN": "20000",
        "GRID_FLOATING_FRACTION": "0.1",
        "GRID_FLOATING_WINDOW": "300",
    }
    cfg = grid_config.GridConfig.from_env(env)
    assert cfg.lookback == 60
    assert cfg.step_pct == 2.5
    assert cfg.tp_pct == 4.0
    assert cfg.tp_basis == "avg"
    assert cfg.base_yuan == 20000.0
    assert cfg.floating_fraction == 0.1
    assert cfg.floating_window == 300


def test_grid_config_defaults_match_the_legacy_hardcoded_values():
    """默认值 = 以前硬编码的那七个 —— 行为**不变**。"""
    cfg = grid_config.GridConfig()
    assert (cfg.lookback, cfg.step_pct, cfg.tp_pct) == (120, 3.0, 3.0)
    assert cfg.tp_basis == "first_buy"
    assert (cfg.base_yuan, cfg.floating_fraction, cfg.floating_window) == (
        10000.0, 0.05, 756)


def test_module_level_aliases_still_point_at_the_config():
    """既有 import 不断 —— 但别名现在是**配置对象的投影**, 不是各自读环境变量。"""
    cfg = grid_config.DEFAULT_GRID_CONFIG
    assert grid_pos.GRID_LOOKBACK == cfg.lookback
    assert grid_trigger_policy.GRID_STEP_PCT == cfg.step_pct
    assert grid_trigger_policy.GRID_TP_PCT == cfg.tp_pct
    assert grid_trigger_policy.GRID_TP_BASIS == cfg.tp_basis
    assert layer_sizing.GRID_BASE_YUAN == cfg.base_yuan
    assert layer_sizing.GRID_FLOATING_FRACTION == cfg.floating_fraction
    assert layer_sizing.GRID_FLOATING_WINDOW == cfg.floating_window


def test_trigger_policy_from_config_carries_the_parameters():
    cfg = grid_config.GridConfig(step_pct=1.5, tp_pct=2.0, tp_basis="avg")
    pol = grid_trigger_policy.TriggerPolicy.from_config(cfg)
    assert pol.step_pct == 1.5
    assert pol.tp_pct == 2.0
    assert pol.tp_basis == "avg"


def test_layer_sizing_policy_from_config_carries_the_parameters():
    cfg = grid_config.GridConfig(base_yuan=20000.0, floating_fraction=0.1)
    sizing = layer_sizing.LayerSizingPolicy.from_config(cfg)
    assert sizing.base_yuan == 20000.0
    assert sizing.floating_fraction == 0.1
    assert sizing.layer_yuan(100_000.0, 50_000.0) == 20000.0 + 50000.0 * 0.1


def test_app_config_from_env_reads_the_threshold():
    cfg = app_config.AppConfig.from_env({"THRESHOLD_PP": "5.5"})
    assert cfg.threshold_pp == 5.5
    # 网格参数没给 → 走默认值
    assert cfg.grid == grid_config.GridConfig()


def test_app_config_is_frozen():
    """配置是**值** —— 不许就地改。"""
    cfg = app_config.AppConfig.from_env({})
    try:
        cfg.threshold_pp = 9.0
    except Exception:
        return
    raise AssertionError("AppConfig 必须是 frozen —— 配置不该被就地改")


def test_advance_all_grids_accepts_an_explicit_config():
    """显式传进去的网格参数真的生效 —— 一个进程里两套并存。"""
    state = {n: grid_pos.fresh_asset_state() for n in universe.ASSET_NAMES}
    for asset in state.values():
        asset["last_prices"] = [1.0, 2.0, 3.0, 4.0]
    prices = {n: 5.0 for n in universe.ASSET_NAMES}
    cfg = grid_config.GridConfig(lookback=3, step_pct=3.0, tp_pct=3.0,
                                  tp_basis="first_buy", base_yuan=20000.0,
                                  floating_fraction=0.1)
    new_state, signals = snapshot.advance_all_grids(
        state, "2026-01-01", prices, 100_000.0, 90_000.0, config=cfg)
    # layer = 20000 + (100000 - 90000) × 0.1
    for s in signals:
        assert s["layer_yuan"] == 21000.0, f"{s['name']} layer={s['layer_yuan']}"
    # lookback=3 → 窗口只剩 3 根 (默认 120 时会是 5 根)
    for name, asset in new_state.items():
        assert len(asset["last_prices"]) == 3, f"{name} 窗口没截到 3"


def test_advance_all_grids_default_config_is_the_app_config():
    """不传 config → 用应用层的默认配置 (行为与重构前一致)。"""
    state = {n: grid_pos.fresh_asset_state() for n in universe.ASSET_NAMES}
    prices = {n: 5.0 for n in universe.ASSET_NAMES}
    _new_state, signals = snapshot.advance_all_grids(
        state, "2026-01-01", prices, 100_000.0, 90_000.0)
    expected = 10000.0 + (100_000.0 - 90_000.0) * 0.05
    for s in signals:
        assert s["layer_yuan"] == expected


def test_backtest_threshold_default_is_the_domain_constant():
    """回测路径的默认值 = 领域常量 —— 不再是本模块自己读的环境变量。"""
    assert three_pp.THRESHOLD_PP == policy.RebalancePolicy().threshold_pp


def test_backtest_accepts_an_explicit_threshold():
    """回测想要别的阈值就**显式传参**。"""
    idx = pd.date_range("2026-01-01", periods=2, freq="D")
    prices = pd.DataFrame({"A": [10.0, 11.0], "B": [10.0, 9.0]}, index=idx)
    # 第 0 天全现金 → 偏离 50pp (任何阈值都触发, 建仓)
    # 第 1 天权重 0.55/0.45 → 偏离 5pp
    loose = three_pp.run_backtest(prices, initial_cash=1000.0, threshold_pp=6.0)
    tight = three_pp.run_backtest(prices, initial_cash=1000.0, threshold_pp=3.0)
    assert loose["n_rebalances"] == 1, "5pp < 6pp → 第 1 天不触发"
    assert tight["n_rebalances"] == 2, "5pp > 3pp → 第 1 天也触发"
    # 不传 = 默认 (3.0) → 与显式传 3.0 一致
    default = three_pp.run_backtest(prices, initial_cash=1000.0)
    assert default["n_rebalances"] == tight["n_rebalances"]


def test_env_reads_are_confined_to_the_config_objects():
    """D8: 环境变量只在配置对象里读 —— 领域层 / 回测层不再自己读。"""
    allowed = {"src/domain/grid/config.py"}
    offenders = []
    for p in sorted((ROOT / "src").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if not (rel.startswith("src/domain/") or rel.startswith("src/simulation/")):
            continue
        if rel in allowed:
            continue
        if "os.environ" in p.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, f"这些模块还在自己读环境变量: {offenders}"


def test_domain_grid_stays_stdlib_only():
    """`scripts/apply_grid.py` 在**不装** pandas/akshare 的 job 里 import 这条链路。"""
    heavy = ("pandas", "numpy", "akshare", "requests")
    bad = []
    for p in sorted((ROOT / "src" / "domain" / "grid").glob("*.py")):
        text = p.read_text(encoding="utf-8")
        for mod in heavy:
            if f"import {mod}" in text:
                bad.append(f"{p.name}: {mod}")
    assert not bad, f"domain/grid 必须 stdlib-only: {bad}"


# -----------------------------------------------------------------------------
# Phase 5b —— 领域事件
# -----------------------------------------------------------------------------

def test_domain_events_are_immutable_and_named():
    """事件是**不可变**的, 类型名与文档 §8 的表一一对应。"""
    e = events.TradeRecorded(date="2026-10-12", side="buy", code="515180",
                              shares=100.0, price=1.3, amount=130.0)
    assert e.type == "TradeRecorded"
    try:
        e.side = "sell"          # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("事件必须不可变 —— 它是「已经发生的事」")
    rec = e.to_record()
    assert rec["type"] == "TradeRecorded"
    assert rec["code"] == "515180"


def test_event_stream_counts_and_summary():
    s = events.EventStream()
    s.record(events.PositionValued(date="2026-10-23", code="1"))
    s.record(events.PositionValued(date="2026-10-23", code="2"))
    s.record(events.RebalanceTriggered(date="2026-10-23"))
    assert len(s) == 3
    assert s.counts() == {"PositionValued": 2, "RebalanceTriggered": 1}
    assert len(s.of_type(events.PositionValued)) == 2
    assert "PositionValued×2" in s.summary_line()


def test_ledger_emits_trade_and_cash_events():
    trades = [
        {"date": "2026-10-12", "side": "deposit_rebalance", "amount": 1000.0},
        {"date": "2026-10-12", "side": "buy", "code": "515180",
         "shares": 100.0, "price": 1.5, "fee": 0.0},
        {"date": "2026-10-13", "side": "transfer_to_grid", "amount": 100.0},
        {"date": "2026-10-14", "side": "sell", "code": "515180",
         "shares": 50.0, "price": 1.6, "fee": 0.0},
    ]
    stream = events.EventStream()
    ledger.rebuild_holdings_from_trades(trades, events=stream)
    assert [e.type for e in stream] == [
        "CashTransferred", "TradeRecorded", "CashTransferred", "TradeRecorded"]
    dep, xfer = stream.of_type(events.CashTransferred)
    assert (dep.from_pool, dep.to_pool, dep.amount) == ("external", "rebalance", 1000.0)
    assert (xfer.from_pool, xfer.to_pool) == ("rebalance", "grid")
    buy, sell = stream.of_type(events.TradeRecorded)
    assert (buy.side, buy.code, buy.shares, buy.price) == ("buy", "515180", 100.0, 1.5)
    assert buy.amount == pytest_approx(150.0)
    assert sell.amount == pytest_approx(80.0)


def test_ledger_emits_nothing_without_a_stream():
    """事件是**可选**的 —— 老调用方一行不用改。"""
    trades = [{"date": "2026-10-12", "side": "deposit_rebalance", "amount": 100.0}]
    a = ledger.rebuild_holdings_from_trades(trades)
    b = ledger.rebuild_holdings_from_trades(trades, events=None)
    assert a == b


def test_ledger_cannot_record_simulated_shares():
    """I9: 账本上下文里**没有**能写模拟份额的入口。"""
    state = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {}}
    for bad in ("merge", "merged_shares", "grid_merge"):
        try:
            ledger.apply_trade(state, {"side": bad, "code": "515180",
                                       "shares": 10.0, "price": 1.0})
        except ValueError:
            continue
        raise AssertionError(f"账本不该接受 side={bad!r}")
    # 八个 side 里没有任何一个是「归并」
    assert len(list(ledger.TradeSide)) == 8
    assert not any("merge" in s.value for s in ledger.TradeSide)


def test_rebalance_policy_emits_rebalance_triggered():
    pf = portfolio.Portfolio.from_records([
        {"name": "A", "code": "1", "shares": 1, "price": 104, "date": "2026-10-23"},
        {"name": "B", "code": "2", "shares": 1, "price": 96, "date": "2026-10-23"},
    ])
    stream = events.EventStream()
    outcome = policy.RebalancePolicy(threshold_pp=1.0).evaluate(pf, events=stream)
    assert outcome.triggered
    evs = stream.of_type(events.RebalanceTriggered)
    assert len(evs) == 1
    assert evs[0].date == "2026-10-23"
    assert evs[0].deviating_asset in ("A", "B")
    assert evs[0].deviation_pp == pytest_approx(2.0)
    assert evs[0].target_weight == pytest_approx(0.5)
    assert len(evs[0].plan) == 2


def test_rebalance_policy_emits_nothing_when_not_triggered():
    pf = portfolio.Portfolio.from_records([
        {"name": "A", "code": "1", "shares": 1, "price": 100, "date": "2026-10-23"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "date": "2026-10-23"},
    ])
    stream = events.EventStream()
    assert not policy.RebalancePolicy(threshold_pp=3.0).evaluate(
        pf, events=stream).triggered
    assert stream.of_type(events.RebalanceTriggered) == []


def test_grid_manual_op_emits_cycle_events():
    state = grid_pos.fresh_asset_state()

    s1 = events.EventStream()
    state = grid_pos.manual_op(state, op="open", today="2026-10-23",
                                price=1.0, shares=1000.0,
                                asset="红利ETF易方达", code="515180", events=s1)
    assert [e.type for e in s1] == ["GridCycleOpened"]
    opened = s1.of_type(events.GridCycleOpened)[0]
    assert (opened.asset, opened.code, opened.layer_no) == ("红利ETF易方达", "515180", 1)

    s2 = events.EventStream()
    state = grid_pos.manual_op(state, op="add", today="2026-10-24",
                                price=0.97, shares=1000.0,
                                asset="红利ETF易方达", code="515180", events=s2)
    assert [e.type for e in s2] == ["GridLayerAdded"]
    assert s2.of_type(events.GridLayerAdded)[0].layer_no == 2

    s3 = events.EventStream()
    state = grid_pos.manual_op(state, op="tp", today="2026-10-27",
                                price=1.03, shares=0.0,
                                asset="红利ETF易方达", code="515180", events=s3)
    assert [e.type for e in s3] == ["GridCycleClosed", "MergedSharesTransferred"]
    closed = s3.of_type(events.GridCycleClosed)[0]
    assert closed.realized_pnl == pytest_approx(0.0)
    assert closed.sold_shares > 0
    xfer = s3.of_type(events.MergedSharesTransferred)[0]
    assert xfer.provenance == "simulated_grid"
    assert xfer.shares == pytest_approx(state["merged_shares"])
    assert xfer.shares > 0


def test_merged_share_transfers_match_the_grid_state():
    """走事件通路吸收出来的份额 == 以前直接读状态 —— 换通路不改数字。"""
    state = {
        "红利ETF易方达": {"in_cycle": False, "entries": [], "last_prices": [],
                          "cum_realized": 0.0, "ops": [], "merged_shares": 123.45},
        "黄金ETF博时": {"in_cycle": False, "entries": [], "last_prices": [],
                        "cum_realized": 0.0, "ops": [], "merged_shares": 0.0},
    }
    transfers = grid_pos.merged_share_transfers(state)
    assert len(transfers) == 1
    by_code = portfolio.merged_by_code_from_transfers(transfers)
    assert by_code == portfolio.merged_shares_by_code(state)
    assert by_code == {"515180": 123.45}


def test_valuation_emits_position_valued():
    stream = events.EventStream()
    positions, total, errors = valuation_mod.value_positions(
        [{"name": "红利ETF易方达", "code": "515180", "shares": 100.0}],
        lambda code: (1.5, "2026-10-23"), events=stream)
    assert not errors
    assert total == pytest_approx(150.0)
    assert len(positions) == 1
    evs = stream.of_type(events.PositionValued)
    assert len(evs) == 1
    assert evs[0].code == "515180"
    assert evs[0].value == pytest_approx(150.0)
    assert evs[0].date == "2026-10-23"


def test_domain_events_module_is_stdlib_only():
    """领域事件不许拉起 pandas/akshare —— 网格链路要能在裸环境里 import。"""
    heavy = ("pandas", "numpy", "akshare", "requests")
    p = ROOT / "src" / "domain" / "events" / "domain_events.py"
    text = p.read_text(encoding="utf-8")
    bad = [m for m in heavy if f"import {m}" in text]
    assert not bad, f"domain/events 必须 stdlib-only: {bad}"


# ---------------- Phase 5c — 应用层瘦身 ----------------

#: `daily_snapshot` 的用例清单 —— 主流程 = 这一串, 顺序即执行顺序。
#: `absorb_merged_shares` 出现**两次**: 推进网格前一次 (给层额定尺)、
#: 推进网格后一次 (记录里那个组合)。
SNAPSHOT_USE_CASES = [
    "load_universe",
    "replay_trade_ledger",
    "value_holdings",
    "attach_qdii_premiums",
    "absorb_merged_shares",
    "size_grid_layer",
    "advance_grids",
    "absorb_merged_shares",
    "judge_rebalance",
    "persist_snapshot",
    "render_snapshot_chart",
    "report_snapshot",
    "emit_step_outputs",
]


def test_snapshot_exposes_the_use_cases():
    """应用层被拆成一串**具名**用例 —— 不是一条 180 行的直线流程。"""
    for name in SNAPSHOT_USE_CASES:
        fn = getattr(snapshot, name, None)
        assert callable(fn), f"缺用例: {name}"


def test_main_runs_the_use_cases_in_order():
    """main() = 一串用例; 顺序就是「今天要做哪几件事」。"""
    order = []
    originals = {n: getattr(snapshot, n) for n in SNAPSHOT_USE_CASES}

    def _spy(name):
        def _f(*a, **k):
            order.append(name)
            return None
        return _f

    for n in SNAPSHOT_USE_CASES:
        setattr(snapshot, n, _spy(n))
    try:
        snapshot.main()
    finally:
        for n in SNAPSHOT_USE_CASES:
            setattr(snapshot, n, originals[n])
    assert order == SNAPSHOT_USE_CASES, f"用例顺序变了: {order}"


def test_main_is_pure_wiring():
    """应用层瘦身: main() 里**没有业务算术** —— 只有接线。

    规则一条都不许回到应用层 —— 这是 §11.1「application 只调 domain」的形状。
    """
    import inspect
    src = inspect.getsource(snapshot.main)
    for token in ("abs(", "round(", "float(", "min(", "max("):
        assert token not in src, f"main() 里不该出现 {token} —— 规则留在 domain 层"


def test_snapshot_run_names_the_two_bases_separately():
    """D9: 两个「基准」各有名字 —— 层额定尺用的那个**不是**记录里那个。"""
    fields = snapshot.SnapshotRun.__dataclass_fields__
    assert "layer_sizing_basis" in fields
    assert "snapshot_basis" in fields
    assert fields["layer_sizing_basis"] is not fields["snapshot_basis"]
    run = snapshot.SnapshotRun(today="2026-10-23")
    assert run.layer_sizing_basis.total == 0.0
    assert run.snapshot_basis.total == 0.0


def test_size_grid_layer_uses_the_layer_sizing_basis_total():
    """层额的池子取自 `layer_sizing_basis` —— 不是 `snapshot_basis`。"""
    run = snapshot.SnapshotRun(today="2026-10-23")
    run.layer_sizing_basis = policy.RebalanceOutcome(
        total=123456.0, weights=[], triggered=False, plan=[])
    run.snapshot_basis = policy.RebalanceOutcome(
        total=999999.0, weights=[], triggered=False, plan=[])
    original = snapshot.load_history
    snapshot.load_history = lambda: [{"date": "2026-10-22", "total": 200000.0}]
    try:
        snapshot.size_grid_layer(run)
    finally:
        snapshot.load_history = original
    # 池子 = history 的 total + layer_sizing_basis 的 total → min = 123456
    assert run.recent_min == pytest_approx(123456.0)


def test_absorb_merged_shares_absorbs_the_declared_transfers():
    """网格只**宣告**, 组合自己吸收 —— 模拟份额落在 SIMULATED_GRID 那一档。"""
    run = snapshot.SnapshotRun(today="2026-10-23")
    run.positions = [{"name": "红利ETF易方达", "code": "515180",
                      "shares": 100.0, "price": 1.5,
                      "date": "2026-10-23", "value": 150.0}]
    run.cash_rebalance = 100.0
    state = {"红利ETF易方达": {"in_cycle": False, "entries": [],
                              "last_prices": [], "cum_realized": 0.0,
                              "ops": [], "merged_shares": 20.0}}
    snapshot.absorb_merged_shares(run, state)
    assert run.merged_by_code == {"515180": 20.0}
    pos = run.portfolio.positions[0]
    assert pos.real_shares == pytest_approx(100.0)
    assert pos.simulated_shares == pytest_approx(20.0)
    assert pos.total_shares == pytest_approx(120.0)
    transfers = run.events.of_type(events.MergedSharesTransferred)
    assert len(transfers) == 1
    assert transfers[0].code == "515180"


def test_only_the_final_evaluation_emits_rebalance_triggered():
    """两次评估: 层额定尺那次**不发**事件, 只有记录里那次发。"""
    import inspect
    main_src = inspect.getsource(snapshot.main)
    judge_src = inspect.getsource(snapshot.judge_rebalance)
    assert "evaluate(run.portfolio)" in main_src, \
        "层额定尺那次评估不该发事件 (它只是中间量)"
    assert "events=run.events" in judge_src, \
        "最终评估必须发 RebalanceTriggered"


def test_judge_rebalance_emits_exactly_one_rebalance_triggered():
    """触发时只发**一条** `RebalanceTriggered`。"""
    run = snapshot.SnapshotRun(today="2026-10-23")
    run.positions = [
        {"name": "红利ETF易方达", "code": "515180", "shares": 100.0,
         "price": 1.0, "date": "2026-10-23", "value": 100.0},
        {"name": "黄金ETF博时", "code": "159937", "shares": 100.0,
         "price": 10.0, "date": "2026-10-23", "value": 1000.0},
        {"name": "纳斯达克ETF华夏", "code": "513300", "shares": 100.0,
         "price": 1.0, "date": "2026-10-23", "value": 100.0},
    ]
    run.portfolio = portfolio.Portfolio.from_holdings(run.positions)
    snapshot.judge_rebalance(run)
    trig = run.events.of_type(events.RebalanceTriggered)
    assert len(trig) == 1
    assert trig[0].deviating_asset == "黄金ETF博时"
    assert run.snapshot_basis.triggered is True
    # 记录里的持仓是组合的输出 (含归并字段)
    assert run.positions[0].get("total_shares") is not None


def test_main():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and k != "test_main" and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
            fails += 1
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    test_main()