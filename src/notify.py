"""Daily email digest via SMTP.

Reads data/history.json (last entry), formats a plain-text report, sends via
SSL SMTP. Works with QQ Mail (smtp.qq.com), 163 Mail (smtp.163.com),
Gmail (smtp.gmail.com), and any other TLS-capable SMTP provider.

Required environment variables (set via GitHub Secrets):
    SMTP_HOST  - e.g. smtp.qq.com
    SMTP_PORT  - default 465
    SMTP_USER  - full email address (e.g. xxx@qq.com)
    SMTP_PASS  - authorization code (NOT the login password)
    MAIL_TO    - recipient(s), comma-separated
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import traceback
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"


def latest() -> dict | None:
    if not HISTORY.exists():
        return None
    h = json.loads(HISTORY.read_text(encoding="utf-8"))
    return h[-1] if h else None


def build_body(r: dict) -> str:
    lines = [
        f"稳健再平衡日报 - {r['date']}",
        "=" * 60,
        "",
        f"总市值:    ¥{r['total']:>14,.2f}",
        f"ETF 合计:  ¥{r['etf_value']:>14,.2f}",
        f"现金:      ¥{r['cash']:>14,.2f}",
        f"目标权重:  {r['target_w']:.2%}    阈值: ±{r['threshold_pp']:.1f}pp",
        "",
        f"{'名称':<14}{'权重':>8}{'偏离':>10}{'当前价':>10}{'市值':>14}{'触发':>6}",
        "-" * 62,
    ]
    for p, w in zip(r["positions"], r["weights"]):
        delta = (w - r["target_w"]) * 100
        over = abs(delta) > r["threshold_pp"]
        flag = " ⚠" if over else "  "
        lines.append(
            f"{p['name']:<14}{w:>7.2%}{delta:>+9.2f}pp"
            f"¥{p['price']:>9.3f}¥{p['value']:>13,.0f}{flag}"
        )
    lines += [
        "-" * 62,
        "",
        "结论: " + ("⚠ 触发调仓" if r["triggered"] else "✓ 无需调仓"),
        "",
        "— 自动化 GitHub Actions 发送",
    ]
    return "\n".join(lines)


def build_subject(r: dict) -> str:
    tag = "⚠ 触发" if r["triggered"] else "✓ OK"
    return f"[{tag}] 稳健再平衡 {r['date']} 总市值 ¥{r['total']:,.0f}"


def send(body: str, subject: str) -> None:
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
    r = latest()
    if not r:
        print("WARN no history.json; skipping email", file=sys.stderr)
        return
    subject = build_subject(r)
    body = build_body(r)
    send(body, subject)
    print(f"sent: {subject!r} → {os.environ.get('MAIL_TO', '?')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)