"""Send a failure notice email when the record step fails.

Called by the daily workflow in the else-branch. Uses the same SMTP env vars
as src.notify. The body reuses build_text/build_html from src.notify for
consistent styling.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone, timedelta

from .notify import build_html, build_text, send

# 北京时间 (UTC+8) 用于邮件显示
CST = timezone(timedelta(hours=8))


def build_failure_record() -> dict:
    """Synthesize a record-like dict so we can reuse build_text/build_html."""
    now = datetime.now(CST).date().isoformat()
    return {
        "date": now,
        "total": 0,
        "etf_value": 0,
        "cash_rebalance": 0,
        "cash_grid": 0,
        "cash_total": 0,
        "positions": [],
        "weights": [],
        "target_w": 1.0 / 3,
        "threshold_pp": 3.0,
        "triggered": True,  # 触发样式 (warning red header)
    }


def append_failure_extras(text: str, html: str, run_url: str) -> tuple[str, str]:
    extra_text = (
        f"\n失败原因: record 步骤未成功完成。\n"
        f"查看日志: {run_url}\n\n"
        "常见原因:\n"
        "  - akshare 网络问题 (重试通常能恢复)\n"
        "  - data/holdings.json 格式错 (确认 _schema/cash_rebalance/cash_grid/positions)\n"
        "  - 当天非交易日 + akshare 历史数据缺失\n"
    )
    extra_html = (
        f'<div style="padding:14px 24px;background:#fff8e1;font-size:13px;color:#5d4037;border-top:1px solid #ffe082;">'
        f'<strong>查看日志:</strong> <a href="{run_url}" style="color:#0d6efd;">{run_url}</a><br>'
        f'<span style="color:#888;">常见原因: akshare 网络 · holdings.json 格式错 · 非交易日</span>'
        f'</div>'
    )
    # Insert before the footer ("— 自动化 GitHub Actions 发送")
    text = text.replace("\n— 自动化 GitHub Actions 发送", extra_text + "\n— 自动化 GitHub Actions 发送")
    # Insert HTML extra block before </div></body></html>
    html = html.replace('</div>\n</body>', extra_html + '</div>\n</body>')
    return text, html


def main() -> None:
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = (
        f"{server}/{repo}/actions/runs/{run_id}"
        if (server and repo and run_id) else "(unavailable)"
    )

    r = build_failure_record()
    text = build_text(r)
    html = build_html(r, os.environ.get("PAGES_URL", ""))
    text, html = append_failure_extras(text, html, run_url)

    subject = "[⚠ FAIL] 稳健再平衡 任务失败"
    send(subject, html, text)
    print(f"failure notice sent → {os.environ.get('MAIL_TO', '?')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)