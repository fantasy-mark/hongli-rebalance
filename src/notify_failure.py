"""Send a failure notice email when the record step fails.

Called by the daily workflow in the else-branch of the email step.
Uses the same SMTP env vars as src.notify.
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
import traceback
from email.mime.text import MIMEText


def send(subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())


def main() -> None:
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if (server and repo and run_id) else "(unavailable)"

    body = (
        "稳健再平衡 record 步骤失败。\n\n"
        f"查看日志: {run_url}\n\n"
        "常见原因:\n"
        "  - akshare 网络问题 (重试通常能恢复)\n"
        "  - data/holdings.xlsx 格式错 (确认 \"现金\" + 名称/代码/数量列)\n"
        "  - 当天非交易日 + akshare 历史数据缺失\n"
    )
    subject = "[⚠ FAIL] 稳健再平衡 任务失败"
    send(subject, body)
    print(f"failure notice sent → {os.environ.get('MAIL_TO', '?')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)