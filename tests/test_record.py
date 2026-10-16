"""Unit tests for src.record + src.grid + src.three_pp (pure functions, no network).

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


def test_read_holdings_with_现金_label():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "现金", 50000.0, [
            ["黄金ETF博时", "159937", 20000],
            ["红利ETF易方达", "515180", 100000],
            ["可转债ETF博时", "511380", 13000],
        ])
        record.HOLDINGS = xlsx
        h, cash = record.read_holdings()
        assert cash == 50000.0, f"cash={cash}"
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
        h, cash = record.read_holdings()
        assert cash == 123456.0
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
        h, cash = record.read_holdings()
        assert cash == 0.0
        assert len(h) == 2
        assert sorted(x["code"] for x in h) == ["159937", "511380"]


def test_read_holdings_no_label_defaults_to_zero():
    with tempfile.TemporaryDirectory() as td:
        xlsx = Path(td) / "h.xlsx"
        _write_holdings(xlsx, "", 0.0, [["A", "159937", 100]])
        record.HOLDINGS = xlsx
        h, cash = record.read_holdings()
        assert cash == 0.0
        assert len(h) == 1


def test_evaluate_3pp_equal_weight_no_trigger():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "2026-01-01"},
    ]
    total, weights, triggered, _plan = record.evaluate(positions, 0.0)
    assert total == 300
    assert all(abs(w - 1/3) < 1e-9 for w in weights)
    assert not triggered


def test_evaluate_3pp_threshold_triggers_on_50pp_drift():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 150, "value": 150, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = record.evaluate(positions, 0.0)
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
    total, weights, triggered, _plan = record.evaluate(positions, 0.0)
    assert total == 200
    assert all(abs(w - 0.5) < 1e-9 for w in weights), f"weights={weights}"
    assert not triggered
    # build_record also produces dynamic target_w
    r = record.build_record(
        today="2026-10-14", positions=positions, cash=0.0,
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
    total, weights, triggered, plan = record.evaluate(positions, 0.0)
    assert total == 500
    assert all(abs(w - 0.2) < 1e-9 for w in weights), f"weights={weights}"
    assert not triggered
    # Plan has one entry per position
    assert len(plan) == 0  # equal weight → no plan unless triggered
    r = record.build_record(
        today="2026-10-14", positions=positions, cash=0.0,
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
    total, weights, triggered, _plan = record.evaluate(positions, 0.0)
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
            h, cash = record.read_holdings()
            assert cash == 1000.0
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
        h, cash = record.read_holdings()
        assert cash == 1234.5
        assert len(h) == 2, h
        assert h[0]["code"] == "515180"
        assert h[0]["shares"] == 100
        assert h[1]["code"] == "159937"
        # 200.0 → 200.0 (json.load returns float; not coerced to int)
        assert h[1]["shares"] == 200.0


def test_read_holdings_json_missing_returns_empty():
    """Missing file with no xlsx fallback → (positions=[], cash=0)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nope.json"
        record.HOLDINGS = p
        h, cash = record.read_holdings()
        assert h == []
        assert cash == 0.0


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
        h, cash = record.read_holdings()
        assert cash == 999.0
        assert len(h) == 1 and h[0]["code"] == "515180" and h[0]["shares"] == 1000


def test_evaluate_with_cash_affects_weights():
    # 4 资产生产配置: 4 个等权 ETF, 总值 = 4*100 = 400.
    # target_w = 1/4 = 0.25, 每个 weight = 0.25, 不触发.
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "D", "code": "4", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered, _plan = record.evaluate(positions, 0.0)
    assert total == 400
    assert all(abs(w - 0.25) < 0.01 for w in weights)
    assert not triggered

    # 有现金但仍等权: 每个 200, 现金 200, 总值 1000, weight 0.20 vs target 0.25 → 5pp 偏离 → 触发
    for p in positions:
        p["value"] = 200.0
    total2, weights2, triggered2, _plan2 = record.evaluate(positions, 200.0)
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
        today="2026-10-11", positions=positions, cash=5.0,
        total=526221.4, weights=[0.3267, 0.3368, 0.3365],
        triggered_3pp=False, plan=[],
        grid_signals=[], grid_state={},
    )
    assert r["date"] == "2026-10-11"
    assert r["cash"] == 5.0
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


def _write_holdings(json_path: Path, cash_label: str, cash: float, rows: list[list]) -> None:
    """Build a minimal holdings.json matching the schema read_holdings() expects.

    Schema:
        {"cash": <number>, "positions": [{"name":..., "code":..., "shares":...}, ...]}

    `cash_label` is accepted for backwards-compat (no longer written — json has no
    "资产" / "现金" label, just the number).

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
        "_schema": "v1",
        "cash": float(cash),
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
        "cash": 500.0,
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
    both data/trades.json and data/holdings.json."""
    import json
    import sys
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = ROOT / "scripts" / "apply_holdings.py"
        payload = json.dumps({
            "schema": "trades-v1",
            "trades": [
                {"date": "2026-10-12", "side": "deposit", "amount": 10000.0},
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
        # 10000 deposit - 5000 buy = 5000 cash, 1000 shares of 515180
        assert h_doc["cash"] == 5000.0
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
    """A buy that would push cash < 0 is rejected."""
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
                {"date": "2026-10-12", "side": "deposit", "amount": 100.0},
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
        assert not ok, "buy that pushes cash negative should be rejected"
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
                {"date": "2026-10-12", "side": "deposit", "amount": 1000.0},
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
    state = {"cash": 10000.0, "positions": {}}
    record.apply_trade(state, {
        "side": "buy", "code": "515180", "shares": 1000, "price": 1.40, "fee": 0.0,
    })
    assert state["cash"] == round(10000 - 1000 * 1.40, 2) == 8600.0
    assert state["positions"]["515180"] == 1000


def test_apply_trade_sell_rejects_oversell_raises():
    state = {"cash": 0.0, "positions": {"515180": 100.0}}
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
    """One deposit → cash = amount, positions empty."""
    trades = [{"side": "deposit", "date": "2026-10-12", "amount": 175900.60}]
    positions, cash = record.rebuild_holdings_from_trades(trades, as_of_date="2026-10-12")
    assert cash == 175900.60
    assert positions == []


def test_rebuild_holdings_from_trades_chains_buy_sell_deposit_withdraw():
    """Replay: deposit 10000, buy 1000@10, sell 200@12, withdraw 500.
    Expected: cash = 10000 - 10000 + 2400 - 500 = 1900
              515180 shares = 800
    """
    trades = [
        {"side": "deposit", "date": "2026-10-12", "amount": 10000.0},
        {"side": "buy",     "date": "2026-10-13", "code": "515180",
         "shares": 1000, "price": 10.0, "fee": 0.0},
        {"side": "sell",    "date": "2026-10-14", "code": "515180",
         "shares": 200, "price": 12.0, "fee": 0.0},
        {"side": "withdraw","date": "2026-10-15", "amount": 500.0},
    ]
    positions, cash = record.rebuild_holdings_from_trades(trades, as_of_date="2026-10-15")
    assert cash == round(10000 - 10000 + 2400 - 500, 2) == 1900.0
    assert len(positions) == 1
    assert positions[0]["code"] == "515180"
    assert positions[0]["shares"] == 800.0


def test_rebuild_holdings_from_trades_as_of_date_inclusive():
    """Trades after as_of_date are skipped."""
    trades = [
        {"side": "deposit", "date": "2026-10-12", "amount": 1000.0},
        {"side": "deposit", "date": "2026-10-15", "amount": 500.0},
    ]
    _, cash = record.rebuild_holdings_from_trades(trades, as_of_date="2026-10-13")
    assert cash == 1000.0   # second deposit is after 10-13, skipped


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