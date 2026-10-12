"""Daily email digest via SMTP (multipart text + HTML).

Required env vars: SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_TO
Optional env vars: PAGES_URL (e.g. https://user.github.io/repo, used in HTML footer)
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import sys
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"

# ---- shared style constants (also used by notify_failure.py) ----
COLOR_OK = "#2c7a2c"   # green
COLOR_WARN = "#c0392b"  # red
COLOR_BG = "#f4f6f8"
COLOR_BORDER = "#e0e0e0"
COLOR_MUTED = "#888888"


def latest_record() -> dict | None:
    if not HISTORY.exists():
        return None
    h = json.loads(HISTORY.read_text(encoding="utf-8"))
    return h[-1] if h else None


def fmt_money(v: float) -> str:
    return f"¥{v:,.0f}"


def fmt_pct(v: float, digits: int = 2) -> str:
    return f"{v * 100:.{digits}f}%"


def fmt_delta_pp(d: float) -> str:
    return f"{d:+.2f}pp"


# ---- plain text body ----

def build_text(r: dict) -> str:
    lines = [
        f"稳健再平衡日报 - {r['date']}",
        "=" * 60,
        "",
        f"总市值:    {fmt_money(r['total']):>16}",
        f"ETF 合计:  {fmt_money(r['etf_value']):>16}",
        f"现金:      {fmt_money(r['cash']):>16}",
        f"目标权重:  {fmt_pct(r['target_w'])}    阈值: ±{r['threshold_pp']:.1f}pp",
        "",
        f"{'名称':<14}{'权重':>8}{'偏离':>10}{'当前价':>10}{'数量':>14}{'触发':>6}",
        "-" * 62,
    ]
    for p, w in zip(r["positions"], r["weights"]):
        delta = (w - r["target_w"]) * 100
        over = abs(delta) > r["threshold_pp"]
        flag = " ⚠" if over else "  "
        lines.append(
            f"{p['name']:<14}{fmt_pct(w):>7}{fmt_delta_pp(delta):>9}"
            f"{p['price']:>10.3f}{int(p['shares']):>14,}{flag}"
        )
    lines += [
        "-" * 62,
        "",
        "结论: " + ("⚠ 触发调仓" if r["triggered"] else "✓ 无需调仓"),
        "",
        "— 自动化 GitHub Actions 发送",
    ]
    return "\n".join(lines)


# ---- HTML body ----

def _position_row(p: dict, w: float, target_w: float, threshold_pp: float) -> str:
    delta = (w - target_w) * 100
    over = abs(delta) > threshold_pp
    delta_color = COLOR_WARN if over else "#222"
    delta_str = fmt_delta_pp(delta)
    shares_int = int(p["shares"])
    return (
        f'<tr>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};font-weight:600;">{p["name"]}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;">{fmt_pct(w)}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:{delta_color};font-weight:{700 if over else 400};">{delta_str}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:#555;">¥{p["price"]:.3f}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;">{shares_int:,}</td>'
        f'</tr>'
    )


def build_html(r: dict, pages_url: str = "") -> str:
    is_warn = r["triggered"]
    accent = COLOR_WARN if is_warn else COLOR_OK
    status_text = "⚠ 需要调仓" if is_warn else "✓ 无需调仓"
    target_pct = fmt_pct(r["target_w"], digits=1)
    threshold = f"{r['threshold_pp']:.1f}"
    price_date = r["positions"][0]["date"] if r["positions"] else "?"
    rows = "".join(
        _position_row(p, w, r["target_w"], r["threshold_pp"])
        for p, w in zip(r["positions"], r["weights"])
    )
    pages_link = (
        f'<a href="{pages_url}" style="color:#0d6efd;text-decoration:none;">Pages →</a>'
        if pages_url else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:{COLOR_BG};font-family:-apple-system,'PingFang SC','Microsoft YaHei',Helvetica,Arial,sans-serif;color:#222;">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

    <div style="background:{accent};padding:20px 24px;color:#fff;">
      <div style="font-size:13px;opacity:0.9;letter-spacing:0.5px;">稳健再平衡日报</div>
      <div style="font-size:24px;font-weight:bold;margin-top:4px;">{status_text}</div>
      <div style="font-size:13px;opacity:0.85;margin-top:4px;">{r['date']} · 价格日期 {price_date}</div>
    </div>

    <div style="padding:20px 24px;border-bottom:1px solid #eee;">
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="padding:6px 0;width:33%;vertical-align:top;">
            <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">总市值</div>
            <div style="font-size:22px;font-weight:bold;margin-top:2px;">{fmt_money(r['total'])}</div>
          </td>
          <td style="padding:6px 0;width:33%;vertical-align:top;">
            <div style="font-size:11px;color:#888;text-transform:uppercase;">ETF</div>
            <div style="font-size:22px;font-weight:bold;margin-top:2px;">{fmt_money(r['etf_value'])}</div>
          </td>
          <td style="padding:6px 0;width:33%;vertical-align:top;">
            <div style="font-size:11px;color:#888;text-transform:uppercase;">现金</div>
            <div style="font-size:22px;font-weight:bold;margin-top:2px;">{fmt_money(r['cash'])}</div>
          </td>
        </tr>
      </table>
    </div>

    <div style="padding:20px 24px;">
      <div style="font-size:12px;color:#666;margin-bottom:10px;">
        目标权重 <strong>{target_pct}</strong> · 阈值 ±{threshold}pp · 3 资产等权
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f8f9fa;">
            <th style="text-align:left;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">名称</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">权重</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">偏离</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">价格</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">数量</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>

    <div style="padding:14px 24px;background:#f8f9fa;border-top:1px solid #eee;font-size:12px;color:{COLOR_MUTED};">
      由 GitHub Actions 自动发送 · 数据 akshare · {pages_link}
    </div>

  </div>
</body></html>"""


def build_subject(r: dict) -> str:
    tag = "⚠ 触发" if r["triggered"] else "✓ OK"
    return f"[{tag}] 稳健再平衡 {r['date']} 总市值 {fmt_money(r['total'])}"


def send(subject: str, html: str, text: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    # RFC 2046: text/plain first, then text/html
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())


def main() -> None:
    r = latest_record()
    if not r:
        print("WARN no history.json; skipping email", file=sys.stderr)
        return
    pages_url = os.environ.get("PAGES_URL", "")
    subject = build_subject(r)
    text = build_text(r)
    html = build_html(r, pages_url)
    send(subject, html, text)
    print(f"sent: {subject!r} → {os.environ.get('MAIL_TO', '?')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)