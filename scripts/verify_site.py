"""
站点验收脚本（工具层，非业务代码）

用无头浏览器验证 Page 的三个区块：
  ① 策略全景图  ② 交易记录  ③ 持仓
必须自上而下排列，且数据渲染正常。

用法:
    python scripts/verify_site.py                     # 验证本地 pages/（自动起 http 服务）
    python scripts/verify_site.py --url https://...    # 验证已发布的线上站点
    python scripts/verify_site.py --url ... --shot out.png

依赖: playwright（`pip install playwright && playwright install chromium`）
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SECTION_ORDER = [
    ("#section-overview", "① 策略全景图"),
    ("#section-trades", "② 回测交易记录"),
    ("#section-holdings", "③ 持仓"),
    ("#section-specs", "④ 策略说明"),
]

TRADE_HEADERS = [
    "日期", "策略", "资产", "方向", "类型",
    "价格", "数量", "金额", "步长", "轮次",
]


def serve(directory: Path, port: int = 0):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/pages/index.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 Pages 站点")
    parser.add_argument("--url", default=None, help="站点 URL；留空则本地起服务验证")
    parser.add_argument("--shot", default=None, help="整页截图输出路径")
    parser.add_argument("--timeout", type=int, default=45000, help="页面加载超时(ms)")
    args = parser.parse_args(argv)

    from playwright.sync_api import sync_playwright

    httpd = None
    url = args.url
    if not url:
        httpd, url = serve(ROOT)
        print(f"[local] 服务已启动: {url}")

    failures: list[str] = []
    checks: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str) -> None:
        checks.append((ok, msg))
        print(("  PASS  " if ok else "  FAIL  ") + msg)
        if not ok:
            failures.append(msg)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})

            console_errors: list[str] = []
            page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
            page.on(
                "console",
                lambda m: console_errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None,
            )

            print(f"\n[1] 打开页面: {url}")
            page.goto(url, wait_until="networkidle", timeout=args.timeout)
            page.wait_for_timeout(1200)

            check(not console_errors,
                  f"无 JS 错误" + (f"（实际: {console_errors[:2]}）" if console_errors else ""))

            # ---- 数据加载 ----
            print("\n[2] 数据加载状态")
            badge = page.inner_text("#status-badge")
            check("加载中" not in badge and "失败" not in badge,
                  f"状态徽章已填充（{badge.splitlines()[0][:60]}…）")

            # ---- 布局顺序 ----
            print("\n[3] 三个区块自上而下排列")
            boxes = {}
            for sel, label in SECTION_ORDER:
                loc = page.locator(sel)
                check(loc.count() == 1, f"{label} 区块存在")
                box = loc.bounding_box()
                check(box is not None and box["height"] > 40,
                      f"{label} 有可见高度"
                      + (f"（{box['height']:.0f}px）" if box else ""))
                if box:
                    boxes[sel] = box

            tops = [(sel, label) for sel, label in SECTION_ORDER if sel in boxes]
            for (prev_sel, prev_label), (next_sel, next_label) in zip(tops, tops[1:]):
                prev_box, next_box = boxes[prev_sel], boxes[next_sel]
                ok = prev_box["y"] + prev_box["height"] <= next_box["y"] + 1
                check(ok, f"{prev_label} 在 {next_label} 之上"
                          f"（{prev_box['y']:.0f}+{prev_box['height']:.0f}"
                          f" <= {next_box['y']:.0f}）")

            # ---- ① 全景图 ----
            print("\n[4] 策略全景图")
            chart = page.locator("#grid-chart")
            check(chart.count() == 1, "全景图 <img> 存在")
            loaded = page.evaluate(
                """() => { const i = document.getElementById('grid-chart');
                           return {ok: i.complete && i.naturalWidth > 0,
                                   w: i.naturalWidth, h: i.naturalHeight}; }"""
            )
            check(loaded["ok"], f"全景图已加载（{loaded['w']}×{loaded['h']}）")
            chips = page.locator("#chart-summary .chip")
            check(chips.count() >= 3, f"摘要 chips 渲染（{chips.count()} 个）")

            # ---- ② 交易记录 ----
            print("\n[5] 交易记录")
            rows = page.locator("#trades-table tbody tr")
            n_rows = rows.count()
            check(n_rows > 0, f"交易行渲染（{n_rows} 行）")
            headers = page.locator("#trades-table thead th").all_inner_texts()
            check(
                [h.strip() for h in headers] == TRADE_HEADERS,
                f"表头字段正确（{headers}）",
            )
            first = rows.first.inner_text().split("\t")
            check(len(first) >= len(TRADE_HEADERS) and first[0][:2] == "20",
                  f"首行数据形如日期开头（{first[0]}）")

            # 策略筛选（新增列后的核心交互）
            page.select_option("#trade-strategy-filter", "grid")
            page.wait_for_timeout(300)
            grid_rows = page.locator("#trades-table tbody tr").count()
            page.select_option("#trade-strategy-filter", "rebalance")
            page.wait_for_timeout(300)
            reb_rows = page.locator("#trades-table tbody tr").count()
            check(0 < grid_rows < n_rows and reb_rows > 0,
                  f"策略筛选生效（网格 {grid_rows} / 再平衡 {reb_rows}）")
            page.select_option("#trade-strategy-filter", "")
            page.wait_for_timeout(300)

            # 筛选
            page.select_option("#trade-side-filter", "sell")
            page.wait_for_timeout(300)
            sell_rows = page.locator("#trades-table tbody tr").count()
            check(0 < sell_rows < n_rows,
                  f"方向筛选生效（全部 {n_rows} → 卖出 {sell_rows}）")
            page.select_option("#trade-side-filter", "")
            page.wait_for_timeout(300)

            # ---- ③ 持仓 ----
            print("\n[6] 持仓（再平衡 / 网格 分开）")
            for sel, label in [
                ("#card-rebalance", "再平衡组合卡片"),
                ("#card-grid", "网格组合卡片"),
            ]:
                check(page.locator(sel).count() == 1, f"{label}存在")

            reb = page.inner_text("#rebalance-holdings")
            grid = page.inner_text("#grid-holdings")
            check("总市值" in reb and "现金" in reb, "再平衡：含持仓市值/现金/总市值")
            check("总市值" in grid and "现金" in grid, "网格：含持仓市值/现金/总市值")

            # 持仓隔离：两栏现金不同
            def cash_of(text: str) -> str:
                for line in text.splitlines():
                    if "现金" in line:
                        idx = text.splitlines().index(line)
                        return text.splitlines()[idx + 1].strip()
                return ""

            reb_cash, grid_cash = cash_of(reb), cash_of(grid)
            check(bool(reb_cash) and bool(grid_cash) and reb_cash != grid_cash,
                  f"两策略现金独立（再平衡 {reb_cash} / 网格 {grid_cash}）")

            reb_stats = page.inner_text("#rebalance-stats")
            grid_stats = page.inner_text("#grid-stats")
            check("年化收益" in reb_stats and "夏普比率" in reb_stats,
                  "再平衡：绩效指标渲染")
            check("完成轮次" in grid_stats and "买入/卖出" in grid_stats,
                  "网格：轮次与买卖次数渲染")

            combine = page.inner_text("#combined-holdings")
            check("组合总市值" in combine, "组合合计视图渲染")

            # ---- ④ 策略说明 ----
            print("\n[7] 策略说明（策略名 / 规则描述 / 规则实现）")
            spec_cards = page.locator("#strategy-specs .spec-card")
            check(spec_cards.count() >= 2,
                  f"策略卡片渲染（{spec_cards.count()} 个）")
            rules = page.locator("#strategy-specs .rule")
            check(rules.count() >= 6, f"规则条目渲染（{rules.count()} 条）")
            impls = page.locator("#strategy-specs .rule-impl code")
            check(impls.count() >= 6, f"规则实现指向源码（{impls.count()} 处）")
            grid_card = page.locator("#strategy-specs .spec-grid")
            check(grid_card.count() == 1, "网格策略卡片存在")
            grid_text = grid_card.inner_text()
            check("ATR" in grid_text and "最低价" in grid_text,
                  "网格规则描述含 ATR 步长与前N日最低价")
            rebalance_card = page.locator("#strategy-specs .spec-rebalance")
            check(rebalance_card.count() == 1, "再平衡策略卡片存在")
            check("百分点" in rebalance_card.inner_text(),
                  "再平衡规则描述含阈值百分点")

            # ---- 响应式 ----
            print("\n[8] 移动端布局")
            page.set_viewport_size({"width": 420, "height": 900})
            page.wait_for_timeout(400)
            narrow_boxes = {
                sel: page.locator(sel).bounding_box() for sel, _ in SECTION_ORDER
            }
            order_ok = all(
                narrow_boxes[a]["y"] + narrow_boxes[a]["height"]
                <= narrow_boxes[b]["y"] + 1
                for a, b in zip(
                    [s for s, _ in SECTION_ORDER][:-1],
                    [s for s, _ in SECTION_ORDER][1:],
                )
                if narrow_boxes[a] and narrow_boxes[b]
            )
            check(order_ok, "窄屏下顺序不变（单列堆叠）")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.wait_for_timeout(300)

            if args.shot:
                page.screenshot(path=args.shot, full_page=True)
                print(f"\n截图已保存: {args.shot}")

            browser.close()
    finally:
        if httpd is not None:
            httpd.shutdown()

    passed = sum(1 for ok, _ in checks if ok)
    total = len(checks)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{total} 通过")
    if failures:
        print("失败项:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 60)
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
