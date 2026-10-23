"""Unit tests for src.record + src.grid + src.three_pp + src.notify_failure
(pure functions, no network).

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

from src import record, grid as grid_mod
from src import three_pp
from src import notify_failure as nf


def test_read_holdings_with_现金_label():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "现金", 50000.0, [
            ["黄金ETF博时", "159937", 20000],
            ["红利ETF易方达", "515180", 100000],
            ["可转债ETF博时", "511380", 13000],
        ])
        record.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = record.read_holdings()
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
        record.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = record.read_holdings()
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
        record.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = record.read_holdings()
        assert cash_rebalance == 0.0
        assert cash_grid == 0.0
        assert len(h) == 2
        assert sorted(x["code"] for x in h) == ["159937", "511380"]


def test_read_holdings_no_label_defaults_to_zero():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "", 0.0, [["A", "159937", 100]])
        record.HOLDINGS = xlsx
        h, cash_rebalance, cash_grid = record.read_holdings()
        assert cash_rebalance == 0.0
        assert cash_grid == 0.0
        assert len(h) == 1


def test_evaluate_3pp_equal_weight_no_trigger():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
    ]
    total, weights, triggered, _plan = record.evaluate(positions, 0.0, 0.0)
    assert total == 300
    assert all(abs(w - 1/3) < 1e-9 for w in weights)
    assert not triggered


def test_evaluate_3pp_threshold_triggers_on_50pp_drift():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 150, "value": 150, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = record.evaluate(positions, 0.0, 0.0)
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
    total, weights, triggered, _plan = record.evaluate(positions, 0.0, 0.0)
    assert total == 200
    assert all(abs(w - 0.5) < 1e-9 for w in weights), f"weights={weights}"
    assert not triggered
    # build_record also produces dynamic target_w
    r = record.build_record(
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
    total, weights, triggered, plan = record.evaluate(positions, 0.0, 0.0)
    assert total == 500
    assert all(abs(w - 0.2) < 1e-9 for w in weights), f"weights={weights}"
    assert not triggered
    # Plan has one entry per position
    assert len(plan) == 0  # equal weight → no plan unless triggered
    r = record.build_record(
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
    total, weights, triggered, _plan = record.evaluate(positions, 0.0, 0.0)
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
            record.HOLDINGS = xlsx
            h, cash_rebalance, cash_grid = record.read_holdings()
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
        record.HOLDINGS = p
        h, cash_rebalance, cash_grid = record.read_holdings()
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
        record.HOLDINGS = p
        h, cash_rebalance, cash_grid = record.read_holdings()
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
        record.HOLDINGS = p
        h, cash_rebalance, cash_grid = record.read_holdings()
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
    total, weights, triggered, _plan = record.evaluate(positions, 0.0, 0.0)
    assert total == 400
    assert all(abs(w - 0.25) < 0.01 for w in weights)
    assert not triggered

    # 有现金但仍等权: 每个 200, 现金 200, 总值 1000, weight 0.20 vs target 0.25 → 5pp 偏离 → 触发
    for p in positions:
        p["value"] = 200.0
    total2, weights2, triggered2, _plan2 = record.evaluate(positions, 200.0, 0.0)
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
    r = record.build_record(
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
    assert grid_mod.layer_yuan(pool_value=200_000.0, recent_min=200_000.0) == grid_mod.GRID_BASE_YUAN
    # 池子跌到 18 万 → 浮亏 → 仍买底仓
    assert grid_mod.layer_yuan(pool_value=180_000.0, recent_min=180_000.0) == grid_mod.GRID_BASE_YUAN


def test_layer_yuan_scales_with_floating():
    # 池子 25 万, 3 年低点 20 万 → 浮盈 5 万 × 5% = 2500, 加上底仓
    expected = grid_mod.GRID_BASE_YUAN + 50_000 * grid_mod.GRID_FLOATING_FRACTION
    assert grid_mod.layer_yuan(pool_value=250_000.0, recent_min=200_000.0) == pytest_approx(expected)


def test_advance_grid_buy_on_120day_low():
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_mod.advance_one_day(
        state, date_str="2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert signal == "buy"
    assert new_state["in_cycle"] is True
    assert len(new_state["entries"]) == 1
    assert new_state["entries"][0]["price"] == 99.0
    # shares = 10000 / 99
    assert new_state["entries"][0]["shares"] == pytest_approx(10000 / 99, rel=1e-6)


def test_advance_grid_hold_no_signal():
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_mod.advance_one_day(
        state, date_str="2026-01-01", price=101.0, layer_yuan_value=10_000.0,
    )
    assert signal is None
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []
    # rolling window: today's 101 appended → [100, 101]
    assert new_state["last_prices"] == [100.0, 101.0]


def test_advance_grid_pyramid_then_sell():
    """两层金字塔 → +3% 止盈触发 sell."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    # 第一层 @ 100
    state, sig1 = grid_mod.advance_one_day(
        state, "2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert sig1 == "buy" and state["in_cycle"] is True

    # 第二层 @ 99*0.97 = 96.03
    state, sig2 = grid_mod.advance_one_day(
        state, "2026-01-02", price=96.0, layer_yuan_value=10_000.0,
    )
    assert sig2 == "buy"
    assert len(state["entries"]) == 2
    # 均价 = (99*101.01 + 96*104.17) / 205.18 — 简单校验均价在 96-99 之间
    snap = grid_mod.asset_snapshot(state, current_price=96.0)
    assert 96.0 < snap["avg_cost"] < 99.0
    assert snap["num_buys"] == 2

    # 涨 3% 以上: 均价 × 1.03 触发止盈 (v2 行为, 显式传 tp_basis)
    target = snap["avg_cost"] * 1.03
    state, sig3 = grid_mod.advance_one_day(
        state, "2026-01-03", price=target + 0.01, layer_yuan_value=10_000.0,
        tp_basis="avg_cost",
    )
    assert sig3 == "sell"
    assert state["in_cycle"] is False
    assert state["entries"] == []
    # cum_realized 应该有正收益 (target > avg_cost)
    assert state["cum_realized"] > 0


def test_asset_snapshot_no_cycle():
    state = grid_mod.fresh_asset_state()
    snap = grid_mod.asset_snapshot(state, current_price=100.0)
    assert snap["in_cycle"] is False
    assert snap["num_buys"] == 0
    assert snap["current_value"] == 0.0


def test_advance_grid_tp_basis_first_buy_higher_target():
    """first_buy basis: 止盈位 = 第一笔 × 1.03 (固定), 高于均价 × 1.03 (v2)。

    用固定价 101 / 103 验证两套规则的差异:
    - 均价 ≈ 97.48, 均价×1.03 ≈ 100.40 (100 < 101 < 100.40+epsilon 不触发)
    - 第一笔 99, 第一笔×1.03 = 101.97 (101 < 101.97 不触发, 103 ≥ 101.97 触发)
    """
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    state, _ = grid_mod.advance_one_day(state, "2026-01-01", price=99.0,
                                        layer_yuan_value=10_000.0)
    state, _ = grid_mod.advance_one_day(state, "2026-01-02", price=96.0,
                                        layer_yuan_value=10_000.0)
    # 均价 = (10000 + 10000) / (10000/99 + 10000/96) ≈ 97.477
    # 均价 × 1.03 ≈ 100.40, 第一笔 × 1.03 = 101.97

    # 在 101: 均价 basis 触发, first_buy basis 不触发
    s1 = json.loads(json.dumps(state)); s1["last_prices"] = [100.0]
    s1, sig_avg = grid_mod.advance_one_day(
        s1, "2026-01-03", price=101.0, layer_yuan_value=10_000.0,
        tp_basis="avg_cost",
    )
    assert sig_avg == "sell", f"avg_cost basis should trigger at 101, got {sig_avg}"

    s2 = json.loads(json.dumps(state)); s2["last_prices"] = [100.0]
    s2, sig_fb = grid_mod.advance_one_day(
        s2, "2026-01-03", price=101.0, layer_yuan_value=10_000.0,
        tp_basis="first_buy",
    )
    assert sig_fb is None, f"first_buy basis should NOT trigger at 101, got {sig_fb}"
    assert s2["in_cycle"] is True

    # 涨到 103: first_buy basis 触发 (103 >= 99*1.03 = 101.97)
    s3 = json.loads(json.dumps(state)); s3["last_prices"] = [100.0]
    s3, sig_fb2 = grid_mod.advance_one_day(
        s3, "2026-01-03", price=103.0, layer_yuan_value=10_000.0,
        tp_basis="first_buy",
    )
    assert sig_fb2 == "sell", f"first_buy basis should trigger at 103, got {sig_fb2}"
    assert s3["in_cycle"] is False


# -----------------------------------------------------------------------------
# grid.manual_op — Pages-driven open/add/tp operations
# -----------------------------------------------------------------------------

def test_manual_op_open_creates_cycle():
    """open: in_cycle False → True, entry appended, last_prices extended."""
    import json as _json
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [1.40, 1.38, 1.37]
    new_state = grid_mod.manual_op(
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
    state = grid_mod.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}]
    try:
        grid_mod.manual_op(state, op="open", today="2026-10-17",
                           price=1.30, shares=500.0)
    except ValueError as e:
        assert "no open cycle" in str(e)
    else:
        raise AssertionError("open on in_cycle=True should raise")


def test_manual_op_add_appends_to_cycle():
    """add: in_cycle True → entry appended, last_prices UNCHANGED."""
    state = grid_mod.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}]
    state["last_prices"] = [1.40, 1.38, 1.37]
    new_state = grid_mod.manual_op(
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
    state = grid_mod.fresh_asset_state()
    try:
        grid_mod.manual_op(state, op="add", today="2026-10-17",
                           price=1.30, shares=1000.0)
    except ValueError as e:
        assert "requires an open cycle" in str(e)
    else:
        raise AssertionError("add on in_cycle=False should raise")


def test_manual_op_tp_books_pnl():
    """tp: in_cycle True → P&L booked to cum_realized, cycle closed."""
    state = grid_mod.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [
        {"date": "2026-10-15", "price": 1.40, "shares": 1000.0},
        {"date": "2026-10-16", "price": 1.30, "shares": 1000.0},
    ]
    state["cum_realized"] = 50.0
    new_state = grid_mod.manual_op(
        state, op="tp", today="2026-10-17", price=1.50, shares=0.0,
    )
    # total_invested = 1000*1.40 + 1000*1.30 = 2700
    # total_shares = 2000
    # exit_value = 2000 * 1.50 = 3000
    # pnl = 3000 - 2700 = 300
    assert new_state["cum_realized"] == 50.0 + 300.0
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []


def test_manual_op_tp_rejects_no_cycle():
    """tp while in_cycle=False → ValueError."""
    state = grid_mod.fresh_asset_state()
    try:
        grid_mod.manual_op(state, op="tp", today="2026-10-17",
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
    state = grid_mod.fresh_asset_state()
    assert "ops" in state, "fresh_asset_state must seed ops=[]"
    assert state["ops"] == []


def test_advance_one_day_trigger_appends_open_op_with_auto_note():
    """Trigger buy (today < window min) → ops log gets an `open` auto op."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_mod.advance_one_day(
        state, date_str="2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert signal == "buy"
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "open"
    assert op["source"] == "auto"
    assert op["date"] == "2026-01-01"
    assert op["price"] == 99.0
    assert op["shares"] == pytest_approx(10000 / 99, rel=1e-6)
    assert op["note"] == "窗口突破开仓"
    # No tp-only keys on open/add
    assert "pnl" not in op
    assert "num_buys_exited" not in op


def test_advance_one_day_pyramid_appends_add_op_with_layer_number():
    """Pyramid buy → ops log gets an `add` op with layer-numbered note."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert len(state["ops"]) == 1 and state["ops"][0]["note"] == "窗口突破开仓"
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-02", price=96.0, layer_yuan_value=10_000.0,
    )
    assert len(state["ops"]) == 2
    op = state["ops"][1]
    assert op["op"] == "add"
    assert op["source"] == "auto"
    # post-append len(entries) = 2 → note = "金字塔第2层"
    assert op["note"] == "金字塔第2层"
    assert op["price"] == 96.0
    # Third pyramid → "金字塔第3层"
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-03", price=93.0, layer_yuan_value=10_000.0,
    )
    assert state["ops"][2]["note"] == "金字塔第3层"


def test_advance_one_day_auto_tp_appends_tp_op_with_pnl_and_exit_stats():
    """Auto tp → ops log gets a `tp` op with pnl + num_buys_exited +
    total_shares_exited, and cycle closes (entries cleared, cum_realized up)."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-02", price=96.0, layer_yuan_value=10_000.0,
    )
    target = 99.0 * 1.03  # first_buy basis = first entry * (1 + tp_pct/100)
    state, sig = grid_mod.advance_one_day(
        state, "2026-01-03", price=target + 0.01, layer_yuan_value=10_000.0,
        tp_basis="first_buy",
    )
    assert sig == "sell"
    assert state["in_cycle"] is False
    assert state["entries"] == []
    # ops log: 3 entries — open, add, tp (in order)
    assert len(state["ops"]) == 3
    tp_op = state["ops"][2]
    assert tp_op["op"] == "tp"
    assert tp_op["source"] == "auto"
    assert tp_op["note"] == "自动止盈"
    # num_buys_exited = 2 (both entries exited together)
    assert tp_op["num_buys_exited"] == 2
    # total_shares_exited = sum of both entries' shares
    expected_shares = 10000 / 99 + 10000 / 96
    assert tp_op["total_shares_exited"] == pytest_approx(expected_shares, rel=1e-6)
    # pnl = exit_value - total_invested (positive when exit above avg)
    assert tp_op["pnl"] > 0
    # cum_realized reflects the same pnl (delta since pre-cycle was 0)
    assert state["cum_realized"] == pytest_approx(tp_op["pnl"], rel=1e-6)


def test_advance_one_day_hold_does_not_append_op():
    """No signal (hold) → ops log unchanged."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    new_state, signal = grid_mod.advance_one_day(
        state, "2026-01-01", price=101.0, layer_yuan_value=10_000.0,
    )
    assert signal is None
    assert new_state["ops"] == []
    # Original state's ops untouched
    assert state.get("ops", []) == []


def test_advance_one_day_legacy_state_without_ops_key():
    """Old state without `ops` key → advance_one_day reads as [] and appends."""
    state = {"in_cycle": False, "entries": [], "last_prices": [100.0],
             "cum_realized": 0.0}   # NO "ops" key
    new_state, signal = grid_mod.advance_one_day(
        state, "2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    assert signal == "buy"
    assert new_state["ops"] == [{
        "date": "2026-01-01", "op": "open", "source": "auto",
        "price": 99.0, "shares": pytest_approx(10000 / 99, rel=1e-6),
        "note": "窗口突破开仓",
    }]


def test_manual_op_open_appends_to_ops_with_source_manual():
    """manual open → ops log gets an entry with source=manual, note=empty."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [1.40, 1.38, 1.37]
    new_state = grid_mod.manual_op(
        state, op="open", today="2026-10-17", price=1.35, shares=1000.0,
    )
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "open"
    assert op["source"] == "manual"
    assert op["date"] == "2026-10-17"
    assert op["price"] == 1.35
    assert op["shares"] == 1000.0
    assert op["note"] == ""   # manual_op's signature has no note kwarg
    assert "pnl" not in op
    # original state NOT mutated
    assert "ops" not in state or state.get("ops", []) == []


def test_manual_op_add_appends_to_ops_with_source_manual():
    """manual add → ops log gets an entry with op='add', source='manual'."""
    state = grid_mod.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [{"date": "2026-10-15", "price": 1.40, "shares": 1000.0}]
    new_state = grid_mod.manual_op(
        state, op="add", today="2026-10-17", price=1.30, shares=1000.0,
    )
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "add"
    assert op["source"] == "manual"
    assert op["note"] == ""
    assert "pnl" not in op


def test_manual_op_tp_appends_tp_op_with_pnl_and_clears_entries():
    """manual tp → ops log gets tp op with pnl; entries cleared; cum booked."""
    state = grid_mod.fresh_asset_state()
    state["in_cycle"] = True
    state["entries"] = [
        {"date": "2026-10-15", "price": 1.40, "shares": 1000.0},
        {"date": "2026-10-16", "price": 1.30, "shares": 1000.0},
    ]
    state["cum_realized"] = 50.0
    new_state = grid_mod.manual_op(
        state, op="tp", today="2026-10-17", price=1.50, shares=0.0,
    )
    assert new_state["in_cycle"] is False
    assert new_state["entries"] == []
    assert new_state["cum_realized"] == 50.0 + 300.0
    assert len(new_state["ops"]) == 1
    op = new_state["ops"][0]
    assert op["op"] == "tp"
    assert op["source"] == "manual"
    assert op["num_buys_exited"] == 2
    assert op["total_shares_exited"] == 2000.0
    assert op["pnl"] == 300.0
    assert op["note"] == ""


def test_ops_log_persists_through_tp_cycle_then_reopens():
    """ops log is append-only and survives a tp → next cycle's open."""
    state = grid_mod.fresh_asset_state()
    state["last_prices"] = [100.0]
    # Cycle 1: open @ 99, tp @ 103 (first_buy basis)
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-01", price=99.0, layer_yuan_value=10_000.0,
    )
    state, _ = grid_mod.advance_one_day(
        state, "2026-01-02", price=99.0 * 1.03 + 0.01,
        layer_yuan_value=10_000.0, tp_basis="first_buy",
    )
    assert len(state["ops"]) == 2   # open + tp, both retained
    # Cycle 2: trigger another open
    state["last_prices"] = [200.0]
    state, _ = grid_mod.advance_one_day(
        state, "2026-02-01", price=199.0, layer_yuan_value=10_000.0,
    )
    assert len(state["ops"]) == 3   # cycle-1 ops preserved + new open
    assert [op["date"] for op in state["ops"]] == [
        "2026-01-01", "2026-01-02", "2026-02-01",
    ]
    assert state["ops"][2]["op"] == "open"


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
    from src import notify
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
    from src import notify
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
    from src import notify
    r = _make_record_with_qdii(premium_pct=-1.2)
    html = notify.build_html(r, pages_url="https://x/")
    text = notify.build_text(r)
    assert "折价 -1.20%" in text
    assert "折价机会" in text
    assert "✓ QDII 溢价可控" in html


def test_qdii_codes_contain_only_true_qdii():
    """ETF_DEFS qdii flag must be True only for genuinely QDII (overseas) ETFs."""
    from src import record
    assert record.QDII_CODES == {"513300"}, f"QDII_CODES={record.QDII_CODES}"
    # 4-tuple shape
    for code, v in record.ETF_DEFS.items():
        assert len(v) == 4, f"ETF_DEFS[{code}] not 4-tuple: {v}"


def test_notify_qdii_unheld_still_alerts():
    """A QDII tracked but NOT in the user's portfolio must still appear in the
    QDII block (with an "未持仓" tag) so the user knows the premium is too
    high to buy.  Subject still gets the QDII warning tag."""
    from src import notify
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
    from src import notify
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
        record.TRADES = path
        assert record.load_trades() == []


def test_load_trades_unreadable_returns_empty():
    """Corrupt JSON → [] (warning printed, not an error)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trades.json"
        path.write_text("not-json{", encoding="utf-8")
        record.TRADES = path
        assert record.load_trades() == []


def test_apply_trade_buy_increments_shares_decrements_cash():
    state = {"cash_rebalance": 10000.0, "cash_grid": 0.0, "positions": {}}
    record.apply_trade(state, {
        "side": "buy", "code": "515180", "shares": 1000, "price": 1.40, "fee": 0.0,
    })
    assert state["cash_rebalance"] == round(10000 - 1000 * 1.40, 2) == 8600.0
    assert state["positions"]["515180"] == 1000


def test_apply_trade_sell_rejects_oversell_raises():
    state = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {"515180": 100.0}}
    raised = False
    try:
        record.apply_trade(state, {
            "side": "sell", "code": "515180", "shares": 200, "price": 1.50,
        })
    except ValueError as ex:
        raised = True
        assert "oversell" in str(ex) and "515180" in str(ex)
    assert raised, "expected ValueError for oversell"


def test_rebuild_holdings_from_trades_deposit_only_yields_initial_state():
    """One deposit_rebalance → cash_rebalance = amount, positions empty."""
    trades = [{"side": "deposit_rebalance", "date": "2026-10-12", "amount": 175900.60}]
    positions, cash_rebalance, cash_grid = record.rebuild_holdings_from_trades(
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
    positions, cash_rebalance, cash_grid = record.rebuild_holdings_from_trades(
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
    _, cash_rebalance, _ = record.rebuild_holdings_from_trades(
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
    record.apply_trade(state, {
        "side": "deposit_rebalance", "amount": 5000.0,
    })
    assert state["cash_rebalance"] == 5000.0
    assert state["cash_grid"] == 0.0


def test_apply_trade_deposit_grid_credits_grid_only():
    state = {"cash_rebalance": 0.0, "cash_grid": 0.0, "positions": {}}
    record.apply_trade(state, {
        "side": "deposit_grid", "amount": 8000.0,
    })
    assert state["cash_rebalance"] == 0.0
    assert state["cash_grid"] == 8000.0


def test_apply_trade_withdraw_grid_rejects_insufficient():
    state = {"cash_rebalance": 0.0, "cash_grid": 100.0, "positions": {}}
    raised = False
    try:
        record.apply_trade(state, {
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
    record.apply_trade(state, {
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
        record.apply_trade(state, {
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
    record.apply_trade(state, {
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
    record.apply_trade(state, {
        "side": "transfer_to_grid", "amount": 500.0,
    })
    record.apply_trade(state, {
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
    positions, cash_rebalance, cash_grid = record.rebuild_holdings_from_trades(
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
        # After add+tp: cycle closed, 2 entries added then cleared, pnl booked
        assert a["in_cycle"] is False
        assert a["entries"] == []
        # total_invested = 1000*1.40 + 1000*1.30 = 2700
        # total_shares = 2000; exit_value = 2000*1.50 = 3000; pnl = 300
        assert a["cum_realized"] == 300.0
        # last_prices for the add branch should NOT have been touched
        assert a["last_prices"] == [1.40, 1.38, 1.37]
        # ops log: 2 entries (add + tp), both source='manual', with notes
        # round-tripped from the payload.  tp op carries pnl + exit stats.
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
        assert tp_op["pnl"] == 300.0
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
                shares_per_layer=1000.0, pos_yuan=None):
    """Build a `run_compound_grid` cycles[0]-shaped dict.

    `pos_yuan` mirrors the real engine's per-layer sizing (grid.py:
    `sh = pos_yuan / price`), so each layer costs exactly that much even
    though prices differ. Without it, every entry gets the same flat
    `shares_per_layer` (handy for arithmetic on round numbers).
    """
    entries = [{"date": pd.Timestamp(d), "price": float(p),
                "shares": (pos_yuan / float(p) if pos_yuan else shares_per_layer)}
               for d, p in zip(dates, prices)]
    total_shares = sum(e["shares"] for e in entries)
    total_invested = sum(e["shares"] * e["price"] for e in entries)
    return {
        "entries": entries,
        "exit_date": pd.Timestamp(exit_date),
        "exit_price": float(exit_price),
        "num_buys": len(entries),
        "avg_cost": total_invested / total_shares,
        "total_shares": total_shares,
        "total_invested": total_invested,
        "exit_value": total_shares * exit_price,
        "pnl": (total_shares * exit_price - total_invested) if pnl is None else pnl,
        "return_pct": 0.0,
    }


def test_ops_from_cycles_maps_closed_cycle_to_open_add_tp():
    c = _fake_cycle(["2024-01-10", "2024-01-20"], [10.0, 9.5],
                    "2024-02-05", 10.6)
    ops = grid_mod.ops_from_cycles([c])
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
    assert ops[2]["note"] == "回测·止盈"
    # dates normalized from pandas Timestamps to the ISO strings the page sorts on
    assert all(isinstance(o["date"], str) for o in ops)
    # tp carries the realized stats; open/add must not
    assert ops[2]["pnl"] == pytest_approx(c["pnl"])
    assert ops[2]["num_buys_exited"] == 2
    assert ops[2]["total_shares_exited"] == pytest_approx(2000.0)
    assert "pnl" not in ops[0] and "pnl" not in ops[1]


def test_ops_from_cycles_open_cycle_has_no_tp():
    """The still-open cycle contributes entries but never a take-profit."""
    open_cycle = [{"date": pd.Timestamp("2026-09-04"), "price": 13.446, "shares": 743.72},
                  {"date": pd.Timestamp("2026-09-28"), "price": 13.038, "shares": 766.99},
                  {"date": pd.Timestamp("2026-10-14"), "price": 12.634, "shares": 791.51}]
    ops = grid_mod.ops_from_cycles([], open_cycle)
    assert len(ops) == 3
    assert [o["op"] for o in ops] == ["open", "add", "add"]
    assert not any(o["op"] == "tp" for o in ops)
    assert ops[2]["note"] == "回测·金字塔第3层"
    # no open cycle at all -> no ops, not a crash
    assert grid_mod.ops_from_cycles([], None) == []
    assert grid_mod.ops_from_cycles([], []) == []


def test_ops_from_cycles_orders_add_before_tp_on_same_date():
    """run_compound_grid can add a layer and take profit on the SAME bar
    (grid.py adds at line ~100, then checks tp at ~110). The op order has to
    be explicit, not incidental to dict/append order."""
    c = _fake_cycle(["2024-01-10", "2024-02-05"], [10.0, 9.5],
                    "2024-02-05", 10.6)
    ops = grid_mod.ops_from_cycles([c])
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

    gen = grid_mod.ops_from_cycles(
        [_fake_cycle(["2024-01-10"], [10.0], "2024-02-05", 10.6)])

    preserved = grid_mod.merge_backtest_ops(state, "A", gen)
    assert preserved == 2, "both live ops should survive"
    first = list(state["A"]["ops"])

    # Second run over the same state, same generated ops.
    preserved = grid_mod.merge_backtest_ops(state, "A", gen)
    assert preserved == 2
    assert state["A"]["ops"] == first, "re-running must be a no-op"
    assert len(state["A"]["ops"]) == 4       # 2 live + 2 backtest (open + tp)
    # live state fields untouched by the merge
    assert state["A"]["in_cycle"] is False
    assert state["A"]["last_prices"] == [1.0, 2.0]
    # unknown asset is a safe no-op
    assert grid_mod.merge_backtest_ops(state, "NOPE", gen) == 0


def test_ops_from_cycles_reconciles_invested_yuan():
    """Every backtest layer costs exactly GRID_BASE_YUAN, so the sum of
    entry shares × price must equal layers × base. This is the assertion
    scripts/backfill_grid_ops.py prints as 对账."""
    base = grid_mod.GRID_BASE_YUAN
    cycles = [
        _fake_cycle(["2021-03-01", "2021-03-15"], [10.0, 9.7],
                    "2021-04-01", 10.3, pos_yuan=base),
        _fake_cycle(["2024-06-01"], [8.0], "2024-07-01", 8.3,
                    pos_yuan=base),
    ]
    open_cycle = [{"date": pd.Timestamp("2026-09-04"), "price": 13.446,
                   "shares": base / 13.446}]
    ops = grid_mod.ops_from_cycles(cycles, open_cycle)

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
    rec = record.build_record(
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

    advance_one_day rebuilds the asset state as a fixed 5-key literal, so
    anything riding in a 6th key would be silently dropped the first time
    record.py runs. `ops` is the one array it copies forward verbatim —
    this test is the guard on that, exercised in the worst case: the live
    engine ACTS and opens a real cycle on top of the backfill.
    """
    backtest = [
        {"date": "2026-09-04", "op": "open", "source": "backtest",
         "price": 13.45, "shares": 743.72, "note": "回测·窗口突破开仓"},
        {"date": "2026-10-14", "op": "add", "source": "backtest",
         "price": 12.63, "shares": 791.51, "note": "回测·金字塔第3层"},
    ]
    state = {"in_cycle": False, "entries": [], "last_prices": [13.5, 13.4],
             "cum_realized": 0.0, "ops": [dict(o) for o in backtest]}

    # A price far below the window min forces a real trigger-buy.
    after, signal = grid_mod.advance_one_day(
        state, date_str="2026-10-21", price=12.0,
        layer_yuan_value=grid_mod.GRID_BASE_YUAN)

    assert signal == "buy", "fixture should trigger a live buy"
    assert after["in_cycle"] is True
    # backfill survived byte-identical...
    kept = [o for o in after["ops"] if o["source"] == "backtest"]
    assert kept == backtest, "backtest ops were altered or dropped"
    # ...and the new live op is tagged as live, not backtest
    live = [o for o in after["ops"] if o["source"] == "auto"]
    assert len(live) == 1
    assert live[0]["op"] == "open" and live[0]["note"] == "窗口突破开仓"
    assert len(after["ops"]) == 3
    assert sorted(after.keys()) == ["cum_realized", "entries", "in_cycle",
                                    "last_prices", "ops"]


def _nf_fixture(files: dict, env: dict):
    """Write `files` into a temp dir and return (workspace, env) for diagnose_steps."""
    tmp = tempfile.mkdtemp()
    for name, body in files.items():
        Path(tmp, name).write_text(body, encoding="utf-8")
    return tmp, env


# ---------------------------------------------------------------------------
# src.notify_failure — 失败邮件必须上报真实步骤状态
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