"""
账本统计一致性校验（离线）

对比两套实现，对同一份账本算出的数字必须一致：
  - Python: src/strategy/ledger/stats.py
  - JS:     ledger-stats.js（页面实际用的那份）

用法:
    python scripts/verify_ledger_stats.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.strategy.ledger.book import TradeLedger  # noqa: E402
from src.strategy.ledger.capital import DEFAULT_INITIAL_CAPITALS  # noqa: E402
from src.strategy.ledger.stats import compute_pool_stats  # noqa: E402
from src.strategy.ledger.trade import CashFlowType  # noqa: E402

LEDGER_FILE = ROOT / "data" / "trades.json"
PRICES_FILE = ROOT / "data" / "site" / "prices.json"

# 数值比对容差（浮点误差）
TOLERANCE = 1e-6


def _js_stats(pool: str, trades: list, prices: dict, capital: float) -> dict:
    """调用 Node 跑 ledger-stats.js，取回统计结果"""
    payload = json.dumps({
        "pool": pool,
        "trades": trades,
        "prices": prices,
        "initial_capital": capital,
    }, ensure_ascii=False)

    script = f"""
const LedgerStats = require({str(ROOT / 'ledger-stats.js')!r});
const input = JSON.parse(process.argv[1]);
const out = LedgerStats.computePoolStats(
  input.pool, input.trades, input.prices, input.initial_capital);
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "-e", script, payload],
        capture_output=True, text=True, cwd=str(ROOT), check=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    with open(LEDGER_FILE, encoding="utf-8") as handle:
        raw_trades = json.load(handle).get("trades", [])

    prices = {}
    if PRICES_FILE.exists():
        with open(PRICES_FILE, encoding="utf-8") as handle:
            prices = json.load(handle).get("prices", {})

    ledger = TradeLedger.from_dict({"trades": raw_trades})
    ledger.initial_capitals = dict(DEFAULT_INITIAL_CAPITALS)

    print("=" * 64)
    print("账本统计一致性校验（Python vs JavaScript）")
    print("=" * 64)

    mismatches = []

    for pool, pool_name in ((CashFlowType.REAL, "real"), (CashFlowType.GRID, "grid")):
        py_trades = ledger.trades_of(pool=pool)
        js_trades = [t for t in raw_trades
                     if (t.get("pool") or "real") == pool_name]

        capital = DEFAULT_INITIAL_CAPITALS[pool]
        py = compute_pool_stats(pool, py_trades, prices, capital)
        js = _js_stats(pool_name, js_trades, prices, float(capital))

        print(f"\n[{pool.label}]  笔数 {py.trade_count} "
              f"(买入 {py.buy_count} / 卖出 {py.sell_count})")

        checks = [
            ("初始资金", py.initial_capital, js["initial_capital"]),
            ("现金余额", py.cash, js["cash"]),
            ("持仓市值", py.market_value, js["market_value"]),
            ("总资产", py.total_value, js["total_value"]),
            ("累计收益率", py.total_return, js["total_return"]),
        ]
        for label, py_value, js_value in checks:
            py_float = float(py_value)
            ok = abs(py_float - float(js_value)) < TOLERANCE
            flag = "OK " if ok else "!! "
            print(f"  {flag}{label:10s} Python={py_float:>15,.4f}  "
                  f"JS={float(js_value):>15,.4f}")
            if not ok:
                mismatches.append(f"{pool.label} {label}")

        # 持仓明细逐项比对
        py_positions = {p.code: p for p in py.positions}
        js_positions = {p["code"]: p for p in js["positions"]}
        if set(py_positions) != set(js_positions):
            mismatches.append(f"{pool.label} 持仓集合不一致")
            print(f"  !! 持仓集合不一致: {set(py_positions)} vs {set(js_positions)}")
        else:
            for code, py_pos in py_positions.items():
                js_pos = js_positions[code]
                for label, a, b in (
                    ("数量", float(py_pos.shares), js_pos["shares"]),
                    ("最新价", float(py_pos.price), js_pos["price"]),
                    ("市值", float(py_pos.market_value), js_pos["market_value"]),
                    ("权重", float(py_pos.weight), js_pos["weight"]),
                ):
                    if abs(a - float(b)) > TOLERANCE:
                        mismatches.append(f"{pool.label} {code} {label}")
                        print(f"  !! {code} {label}: {a} vs {b}")
            print(f"  OK 持仓明细 {len(py_positions)} 项一致")

    print("\n" + "=" * 64)
    if mismatches:
        print("校验失败，以下项不一致：")
        for item in mismatches:
            print(f"  - {item}")
        return 1

    print("校验通过：Python 与 JavaScript 算出的账本统计完全一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
