"""本地预览服务器

启动后在浏览器打开 http://localhost:8000
"""
import http.server
import socketserver
import os

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 8000

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

with socketserver.TCPServer(("", PORT), QuietHandler) as httpd:
    print(f"本地站点已启动: http://localhost:{PORT}")
    print("按 Ctrl+C 停止")
    httpd.serve_forever()
