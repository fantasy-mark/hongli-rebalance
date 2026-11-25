"""
本地开发服务器（静态站点 + 真实交易 API）

用法:
    python scripts/local_serve_with_api.py
    浏览器打开 http://localhost:8000

数据流（真相源唯一）:

    页面交易表单
        │  POST /api/trades
        ▼
    data/trades.json      ← 交易账本（唯一真相源，追加式）
        │  重演
        └──> 持仓快照（现算，不落盘）

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

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.strategy.ledger.trade import (  # noqa: E402
    CashFlowType,
    Trade,
    TradeAction,
    TradeId,
    TradeSide,
)
from src.strategy.ledger.capital import DEFAULT_INITIAL_CAPITALS  # noqa: E402
from src.strategy.ledger.store import JsonFileTradeLedgerRepository  # noqa: E402

PORT = 8000
LEDGER_DIR = "data"

# 页面策略 -> 账本资金池
STRATEGY_TO_POOL = {
    "rebalance": CashFlowType.REAL,
    "grid": CashFlowType.GRID,
}


class TradeAPIHandler(http.server.SimpleHTTPRequestHandler):
    """静态文件 + 真实交易 API"""

    def do_POST(self):  # noqa: N802
        if self.path == "/api/trades":
            self.handle_add_trade()
        else:
            self.send_error(404, "Not Found")

    def handle_add_trade(self):
        """把表单提交的交易追加到账本（真相源）"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))

            trade = self._to_ledger_trade(payload)

            repo = JsonFileTradeLedgerRepository(LEDGER_DIR)
            ledger = repo.load()
            ledger.record(trade)
            repo.save(ledger)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "id": trade.id.value,
                "trades": len(ledger),
            }).encode("utf-8"))
        except ValueError as error:
            # 账本不自洽（如建仓超过总金额）—— 明确告知，不写盘
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": False,
                "error": str(error),
            }, ensure_ascii=False).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.send_error(500, str(exc))

    # ------------------------------------------------------------------
    @staticmethod
    def _to_ledger_trade(payload: dict) -> Trade:
        """表单载荷 -> 账本流水"""
        strategy = payload.get("strategy", "rebalance")
        pool = STRATEGY_TO_POOL.get(strategy, CashFlowType.REAL)

        on_date = date.fromisoformat(payload["date"])
        action = TradeAction.from_label(payload.get("action", "open"))

        code = payload["asset_code"]
        shares = Decimal(str(payload["shares"]))
        price = Decimal(str(payload["price"]))

        return Trade(
            id=TradeId.build(on_date, action.side, _next_sequence(on_date, action.side)),
            on_date=on_date,
            side=action.side,
            pool=pool,
            asset_code=code,
            shares=shares,
            price=price,
            amount=shares * price,
            action=action.value,
            note=payload.get("note", ""),
        )

    def log_message(self, fmt, *args):
        pass


def _next_sequence(on_date: date, side: TradeSide) -> int:
    """同一天同方向的流水序号（供 TradeId.build 使用）"""
    ledger = JsonFileTradeLedgerRepository(LEDGER_DIR).load()
    same_day = [t for t in ledger.trades if t.on_date == on_date and t.side is side]
    return len(same_day) + 1


def refresh_prices():
    """刷新最新资产价格（不跑回测）"""
    import logging
    from src.data.service import PriceQueryService
    
    logging.basicConfig(level=logging.INFO, format='%(levelname)s|%(message)s')
    
    # 默认监控的资产
    ASSETS = ['515180', '159937', '511380', '513300']
    prices_path = ROOT / 'data' / 'site' / 'prices.json'
    
    service = PriceQueryService()
    prices = {}
    price_dates = {}
    raw_prices = {}
    raw_price_dates = {}
    
    for code in ASSETS:
        try:
            # 后复权价（回测用）
            hfq_info = service.get_latest_price(code)
            prices[code] = float(hfq_info['close'])
            price_dates[code] = hfq_info['date'].isoformat() if hasattr(hfq_info['date'], 'isoformat') else str(hfq_info['date'])
            
            # 不复权实际价（真实持仓用）
            raw_info = service.get_latest_raw_price(code)
            raw_prices[code] = float(raw_info['close'])
            raw_price_dates[code] = raw_info['date'].isoformat() if hasattr(raw_info['date'], 'isoformat') else str(raw_info['date'])
        except Exception as e:
            print(f"警告: 获取 {code} 价格失败: {e}")
    
    if not prices:
        print("错误: 未能获取任何资产价格")
        return False
    
    # 读取现有 prices.json（保留其他字段）
    existing = {}
    if prices_path.exists():
        try:
            with open(prices_path, encoding='utf-8') as f:
                existing = json.load(f)
        except Exception:
            pass
    
    # 更新价格相关字段
    existing.update({
        'prices': prices,
        'price_dates': price_dates,
        'raw_prices': raw_prices,
        'raw_price_dates': raw_price_dates,
    })
    
    # 写入
    prices_path.parent.mkdir(parents=True, exist_ok=True)
    with open(prices_path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    
    print(f"已更新 {prices_path.relative_to(ROOT)}")
    return True


if __name__ == "__main__":
    # 启动前自动刷新价格（确保显示最新价）
    print("正在刷新资产价格...")
    if not refresh_prices():
        print("价格刷新失败，但服务仍会启动（使用缓存价格）")
    print()
    
    with socketserver.TCPServer(("", PORT), TradeAPIHandler) as httpd:
        print(f"本地站点已启动: http://localhost:{PORT}")
        print("真实交易 API: POST /api/trades  ->  data/trades.json (账本)")
        print("按 Ctrl+C 停止")
        httpd.serve_forever()
