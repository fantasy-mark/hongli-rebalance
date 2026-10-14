"""Daily email digest via SMTP (multipart text + HTML).

Required env vars: SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_TO
Optional env vars: PAGES_URL (e.g. https://user.github.io/repo, used in HTML footer)

Body covers TWO strategies:
  1. 3pp equal-weight rebalance  (weight-based ±3pp)
  2. Compound grid               (120-day low + 3% pyramid + 3% TP)
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
COLOR_OK = "#2c7a2c"
COLOR_WARN = "#c0392b"
COLOR_BG = "#f4f6f8"
COLOR_BORDER = "#e0e0e0"
COLOR_MUTED = "#888888"
COLOR_GRID = "#2c5f8d"


def latest_record() -> dict | None:
    if not HISTORY.exists():
        return None
    h = json.loads(HISTORY.read_text(encoding="utf-8"))
    return h[-1] if h else None


def fmt_money(v: float) -> str:
    return f"¥{v:,.0f}"


def fmt_money_yuan(v: float) -> str:
    return f"¥{v:,.2f}"


def fmt_pct(v: float, digits: int = 2) -> str:
    return f"{v * 100:.{digits}f}%"


def fmt_delta_pp(d: float) -> str:
    return f"{d:+.2f}pp"


def fmt_signal(sig: str | None) -> str:
    return {"buy": "⚠ 买入", "sell": "✓ 止盈"}.get(sig or "", "持有")


def _qdii_premium_view(p: dict) -> tuple[str, str, str]:
    """Return (badge_text, badge_color, advice) for a QDII position.

    Premium thresholds:
      |premium| > 5%  → 警告 (red)
      |premium| ≤ 5%  → 关注可建仓 (blue)
      折价 (negative) → 机会 (green)
    """
    prem = p.get("premium")
    if not prem:
        return ("", "#999", "")
    pct = prem["premium_pct"]
    if pct > 5:
        return (f"溢价 +{pct:.2f}%", COLOR_WARN, "⚠ 高溢价, 不建议加仓")
    if pct > 0:
        return (f"溢价 +{pct:.2f}%", COLOR_GRID, "· 溢价可控, 可关注建仓")
    return (f"折价 {pct:.2f}%", COLOR_OK, "✓ 折价机会")


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
        "[3pp 再平衡]",
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
        f"3pp 结论: {'⚠ 触发调仓' if r['triggered'] else '✓ 无需调仓'}",
        "",
    ]

    # QDII premium section (text)
    qdii_lines = []
    for p, w in zip(r["positions"], r["weights"]):
        if p.get("premium"):
            prem = p["premium"]
            badge, _, advice = _qdii_premium_view(p)
            qdii_lines.append(
                f"  {p['name']:<14} {badge:<14} "
                f"市价 ¥{prem['price']:.4f}  IOPV ¥{prem['iopv']:.4f}@{prem['nav_date']}  "
                f"{advice}"
            )
    if qdii_lines:
        lines += [
            "[QDII 溢价监控]  阈值 ±5% ( > 5% 警示 / ≤ 5% 可关注建仓 )",
            *qdii_lines,
            "",
        ]

    # Grid section
    grid_signals = r.get("grid_signals", [])
    if grid_signals:
        lines += [
            "[复合网格]  120 日新低 + 3% 加仓 + 3% 止盈",
            f"{'名称':<14}{'信号':>8}{'层数':>5}{'均价':>10}{'现价':>10}{'浮盈%':>8}",
            "-" * 62,
        ]
        for s in grid_signals:
            sig_label = fmt_signal(s.get("signal"))
            lines.append(
                f"{s['name']:<14}{sig_label:>8}{s['num_buys']:>5}"
                f"{s['avg_cost']:>10.3f}{s['price']:>10.3f}{s['return_pct']:>+7.2f}%"
            )
        lines += [
            "-" * 62,
            f"加仓额(本笔): ¥{grid_signals[0]['layer_yuan']:,.0f}  "
            f"= ¥10,000 底仓 + 3pp 近 3 年浮盈 × 5%",
            "",
        ]

    lines += [
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
    weight_bold = "bold" if over else "normal"
    return (
        f'<tr>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};font-weight:600;">{p["name"]}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;">{fmt_pct(w)}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:{delta_color};font-weight:{weight_bold};">{delta_str}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:#555;">¥{p["price"]:.3f}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;">{shares_int:,}</td>'
        f'</tr>'
    )


def _grid_row(s: dict) -> str:
    sig = s.get("signal")
    if sig == "buy":
        badge_color = COLOR_WARN
        badge_text = "⚠ 买入"
    elif sig == "sell":
        badge_color = COLOR_OK
        badge_text = "✓ 止盈"
    else:
        badge_color = "#999"
        badge_text = "— 持有"
    badge = (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'background:{badge_color};color:#fff;font-size:12px;font-weight:600;">{badge_text}</span>'
    )
    ret_color = COLOR_OK if s["unrealized_pnl"] >= 0 else COLOR_WARN
    return (
        f'<tr>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};font-weight:600;">{s["name"]}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:center;">{badge}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;">{s["num_buys"]}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:#555;">¥{s["avg_cost"]:.3f}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;">¥{s["price"]:.3f}</td>'
        f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:{ret_color};font-weight:600;">{s["return_pct"]:+.2f}%</td>'
        f'</tr>'
    )


def build_html(r: dict, pages_url: str = "") -> str:
    is_warn = r["triggered"]
    accent = COLOR_WARN if is_warn else COLOR_OK
    status_text = "⚠ 需要调仓" if is_warn else "✓ 无需调仓"
    target_pct = fmt_pct(r["target_w"], digits=1)
    threshold = f"{r['threshold_pp']:.1f}"
    price_date = r["positions"][0]["date"] if r["positions"] else "?"
    n_assets = len(r["positions"])
    rows = "".join(
        _position_row(p, w, r["target_w"], r["threshold_pp"])
        for p, w in zip(r["positions"], r["weights"])
    )

    grid_signals = r.get("grid_signals", [])
    grid_section = ""

    # QDII premium section (HTML)
    qdii_positions = [p for p in r["positions"] if p.get("premium")]
    qdii_section = ""
    if qdii_positions:
        qdii_rows = []
        any_warn = any((p["premium"]["premium_pct"]) > 5 for p in qdii_positions)
        accent = COLOR_WARN if any_warn else COLOR_OK
        title = "⚠ QDII 溢价监控" if any_warn else "✓ QDII 溢价可控"
        for p in qdii_positions:
            prem = p["premium"]
            badge_text, badge_color, advice = _qdii_premium_view(p)
            badge = (
                f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
                f'background:{badge_color};color:#fff;font-size:12px;font-weight:600;">{badge_text}</span>'
            )
            qdii_rows.append(
                f'<tr>'
                f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};font-weight:600;">{p["name"]}</td>'
                f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:center;">{badge}</td>'
                f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:#555;">¥{prem["price"]:.4f}</td>'
                f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:#555;">¥{prem["iopv"]:.4f} <span style="color:#999;font-size:11px;">@{prem["nav_date"]}</span></td>'
                f'<td style="padding:8px 6px;border-bottom:1px solid {COLOR_BORDER};text-align:right;color:{badge_color};font-weight:600;">{advice}</td>'
                f'</tr>'
            )
        qdii_section = f"""
    <div style="padding:20px 24px;border-top:1px solid #eee;">
      <div style="background:{accent};padding:10px 16px;color:#fff;border-radius:6px;margin-bottom:12px;font-weight:bold;font-size:14px;">{title}</div>
      <div style="font-size:12px;color:#666;margin-bottom:10px;">
        QDII 溢价率 = (市价 − IOPV) / IOPV · 阈值 ±5% · &gt; 5% 警示暂停加仓 · ≤ 5% 可关注建仓
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f8f9fa;">
            <th style="text-align:left;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">名称</th>
            <th style="text-align:center;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">溢价率</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">市价</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">IOPV</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">建议</th>
          </tr>
        </thead>
        <tbody>{"".join(qdii_rows)}</tbody>
      </table>
    </div>
"""
    if grid_signals:
        grid_rows = "".join(_grid_row(s) for s in grid_signals)
        layer = grid_signals[0].get("layer_yuan", 10000)
        any_grid_action = any(s.get("signal") for s in grid_signals)
        grid_accent = COLOR_WARN if any_grid_action else COLOR_GRID
        grid_title = "⚠ 网格信号" if any_grid_action else "✓ 网格无操作"
        grid_section = f"""
    <div style="padding:20px 24px;border-top:1px solid #eee;">
      <div style="background:{grid_accent};padding:10px 16px;color:#fff;border-radius:6px;margin-bottom:12px;font-weight:bold;font-size:14px;">{grid_title}</div>
      <div style="font-size:12px;color:#666;margin-bottom:10px;">
        复合网格 · 120 日新低 + 3% 加仓 + 3% 止盈 · 加仓额 <strong>¥{layer:,.0f}</strong>
        (¥10,000 底仓 + 3pp 近 3 年浮盈 × 5%)
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#f8f9fa;">
            <th style="text-align:left;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">名称</th>
            <th style="text-align:center;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">信号</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">层数</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">均价</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">现价</th>
            <th style="text-align:right;padding:10px 6px;color:#666;font-weight:600;border-bottom:2px solid #dee2e6;">浮盈%</th>
          </tr>
        </thead>
        <tbody>{grid_rows}</tbody>
      </table>
    </div>
"""

    pages_link = (
        f'<a href="{pages_url}" style="color:#0d6efd;text-decoration:none;">Pages →</a>'
        if pages_url else ""
    )
    chart_link = (
        f' · <a href="{pages_url}data/grid_chart.png" style="color:#0d6efd;text-decoration:none;">图表 →</a>'
        if pages_url else ""
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:{COLOR_BG};font-family:-apple-system,'PingFang SC','Microsoft YaHei',Helvetica,Arial,sans-serif;color:#222;">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">

    <div style="background:{accent};padding:20px 24px;color:#fff;">
      <div style="font-size:13px;opacity:0.9;letter-spacing:0.5px;">3pp 再平衡 · 日报</div>
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
        目标权重 <strong>{target_pct}</strong> · 阈值 ±{threshold}pp · {n_assets} 资产等权
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
{grid_section}
{qdii_section}
    <div style="padding:14px 24px;background:#f8f9fa;border-top:1px solid #eee;font-size:12px;color:{COLOR_MUTED};">
      由 GitHub Actions 自动发送 · 数据 akshare · {pages_link}{chart_link}
    </div>

  </div>
</body></html>"""


def build_subject(r: dict) -> str:
    triggered_3pp = r.get("triggered", False)
    grid_signals = r.get("grid_signals", [])
    any_buy = any(s.get("signal") == "buy" for s in grid_signals)
    any_sell = any(s.get("signal") == "sell" for s in grid_signals)
    qdii_warn = any(
        p.get("premium") and p["premium"]["premium_pct"] > 5
        for p in r.get("positions", [])
    )

    if triggered_3pp and qdii_warn:
        tag = "⚠ 再平衡+QDII溢价"
    elif triggered_3pp and any_buy:
        tag = "⚠ 再平衡+买入"
    elif triggered_3pp:
        tag = "⚠ 触发"
    elif qdii_warn:
        tag = "⚠ QDII溢价"
    elif any_buy:
        tag = "⚠ 网格买入"
    elif any_sell:
        tag = "✓ 网格止盈"
    else:
        tag = "✓ OK"
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
