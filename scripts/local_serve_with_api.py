"""
带交易记录 API 的本地服务器

启动后在浏览器打开 http://localhost:8000
支持静态文件服务和 POST /api/trades 接口
"""
import http.server
import socketserver
import os
import json
from datetime import datetime
from pathlib import Path

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 8000
TRADES_FILE = Path('data/site/trades.json')


class TradeAPIHandler(http.server.SimpleHTTPRequestHandler):
    """支持交易记录 API 的 HTTP 处理器"""
    
    def do_POST(self):
        """处理 POST 请求"""
        if self.path == '/api/trades':
            self.handle_add_trade()
        else:
            self.send_error(404, "Not Found")
    
    def handle_add_trade(self):
        """处理添加交易记录"""
        try:
            # 读取请求体
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            trade = json.loads(post_data.decode('utf-8'))
            
            # 读取现有交易记录
            if TRADES_FILE.exists():
                with open(TRADES_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {'trades': []}
            
            # 添加新交易记录
            data['trades'].append(trade)
            
            # 保存回文件
            with open(TRADES_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 返回成功响应
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True}).encode('utf-8'))
            
        except Exception as e:
            print(f"Error handling trade: {e}")
            self.send_error(500, str(e))
    
    def log_message(self, format, *args):
        """简化日志输出"""
        pass


if __name__ == '__main__':
    with socketserver.TCPServer(("", PORT), TradeAPIHandler) as httpd:
        print(f"本地站点已启动: http://localhost:{PORT}")
        print("支持 API: POST /api/trades")
        print("按 Ctrl+C 停止")
        httpd.serve_forever()
