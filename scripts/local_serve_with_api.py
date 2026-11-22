"""
本地开发服务器（静态站点 + 真实交易 API）

用法:
    python scripts/local_serve_with_api.py
    浏览器打开 http://localhost:8000

数据流（真相源唯一）:

    页面交易表单
        │  POST /api/trades
        ▼
    data/trades.json        ← 交易账本（唯一真相源，追加式）
        │  重演
        ├──> data/holdings.json      （派生视图：持仓快照）
        │  投影
        └──> data/site/trades.json   （站点展示：真实交易记录）

回测交易走另一条线：data/site/backtest_trades.json（由 build_pages.py 生成）。
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from src.strategy.ledger.trade import CashFlowType, Trade, TradeId, TradeSide  # noqa: E402
from src.strategy.ledger.store import JsonFileTradeLedgerRepository  # noqa: E402
from src.strategy.ledger.projection import project_ledger  # noqa: E402

PORT = 8000
LEDGER_DIR = "data"
SITE_TRADES = Path("data/site/trades.json")

# 页面策略 -> 账本资金池
STRATEGY_TO_POOL = {
    "rebalance": CashFlowType.REAL,
    "grid": CashFlowType.GRID,
}

# 交易类型 -> 账本方向
ACTION_TO_SIDE = {
    "open": TradeSide.BUY,
    "add": TradeSide.BUY,
    "reduce": TradeSide.SELL,
    "take_profit": TradeSide.SELL,
}


class TradeAPIHandler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 真实交易 API"""

    def do_POST(self):  # noqa: N802
        if self.path == "/api/trades":
            self.handle_add_trade()
        else:
            self.send_error(404, "Not Found")

    def handle_add_trade(self):
        """把表单提交的交易追加到账本（真相源），再刷新派生视图"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))

            trade = self._to_ledger_trade(payload)

            repo = JsonFileTradeLedgerRepository(LEDGER_DIR)
            ledger = repo.load()
            ledger.record(trade)
            repo.save(ledger)          # 写回账本 + 重建 holdings.json

            self._refresh_site_projection()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "id": trade.id.value,
                "trades": len(ledger),
            }).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))

    # ------------------------------------------------------------------
    @staticmethod
    def _to_ledger_trade(payload: dict) -> Trade:
        """表单载荷 -> 账本流水"""
        strategy = payload.get("strategy", "rebalance")
        action = payload.get("action", "open")
        side = ACTION_TO_SIDE.get(action, TradeSide.BUY)
        pool = STRATEGY_TO_POOL.get(strategy, CashFlowType.REAL)

        on_date = date.fromisoformat(payload["date"])
        code = payload["asset_code"]
        shares = Decimal(str(payload["shares"]))
        price = Decimal(str(payload["price"]))

        sequence = len([
            t for t in JsonFileTradeLedgerRepository(LEDGER_DIR).load().trades
            if t.on_date == on_date and t.side is side
        ]) + 1

        return Trade(
            id=TradeId.build(on_date, side, sequence),
            on_date=on_date,
            side=side,
            pool=pool,
            asset_code=code,
            shares=shares,
            price=price,
            amount=shares * price,
            note=payload.get("note", ""),
        )

    @staticmethod
    def _refresh_site_projection() -> None:
        """账本 -> 站点真实交易投影"""
        repo = JsonFileTradeLedgerRepository(LEDGER_DIR)
        rows = project_ledger(repo.load())
        SITE_TRADES.parent.mkdir(parents=True, exist_ok=True)
        with open(SITE_TRADES, "w", encoding="utf-8") as fh:
            json.dump({"trades": rows}, fh, ensure_ascii=False, indent=2)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), TradeAPIHandler) as httpd:
        print(f"本地站点已启动: http://localhost:{PORT}")
        print("真实交易 API: POST /api/trades  ->  data/trades.json (账本)")
        print("按 Ctrl+C 停止")
        httpd.serve_forever()
