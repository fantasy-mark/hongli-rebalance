"""Unit tests for src.record (pure functions, no network).

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

from src import record


def _write_holdings(path: Path, label: str, cash: float, rows: list[list]) -> None:
    """Write a holdings.xlsx with given label/cash and (name, code, shares) rows."""
    data = [
        [None, None, None],
        [label, cash, None],
        [None, None, None],
        ["名称", "代码", "数量"],
        *rows,
    ]
    pd.DataFrame(data).to_excel(path, header=False, index=False)


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
    total, weights, triggered = record.evaluate(positions, 0.0)
    assert total == 300
    assert all(abs(w - 1/3) < 1e-9 for w in weights)
    assert not triggered


def test_evaluate_3pp_threshold_triggers_on_50pp_drift():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 150, "value": 150, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "C", "code": "3", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered = record.evaluate(positions, 0.0)
    # A: 150/350 = 42.86%, target 33.33%, delta = +9.52pp > 3pp
    assert total == 350
    assert weights[0] > 0.4
    assert triggered


def test_evaluate_with_cash_affects_weights():
    positions = [
        {"name": "A", "code": "1", "shares": 1, "price": 100, "value": 100, "date": "x"},
        {"name": "B", "code": "2", "shares": 1, "price": 100, "value": 100, "date": "x"},
    ]
    total, weights, triggered = record.evaluate(positions, 100.0)
    # Total = 300, A=B=33.33%, no trigger (target=1/3 here but assets are 2)
    # Note: with 2 positions, TARGET_W = 1/3, so A=B=33% means (33-33.33)pp ≈ 0,
    # well within 3pp.
    assert total == 300
    assert all(abs(w - 1/3) < 0.01 for w in weights)
    assert not triggered


def test_build_record_shape():
    positions = [
        {"name": "黄金ETF博时", "code": "159937", "shares": 20000,
          "price": 8.594, "value": 171880.0, "date": "2026-10-09"},
        {"name": "红利ETF易方达", "code": "515180", "shares": 122800,
          "price": 1.443, "value": 177200.4, "date": "2026-10-09"},
        {"name": "可转债ETF博时", "code": "511380", "shares": 13700,
          "price": 12.930, "value": 177141.0, "date": "2026-10-09"},
    ]
    r = record.build_record(positions, 5.0, "2026-10-11")
    assert r["date"] == "2026-10-11"
    assert r["cash"] == 5.0
    assert abs(r["total"] - (171880.0 + 177200.4 + 177141.0 + 5.0)) < 0.1
    assert len(r["positions"]) == 3
    assert len(r["weights"]) == 3
    assert abs(sum(r["weights"]) - 1.0) < 1e-4
    assert r["target_w"] == 1.0 / 3
    assert r["threshold_pp"] == 3.0
    # 三个权重都在 33% 附近, 不应触发
    assert not r["triggered"]


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
    main()