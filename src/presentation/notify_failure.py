"""Send a failure notice email when the daily workflow fails.

Called by daily.yml's `if: always()` email step: record step succeeded ->
normal digest (src.presentation.digest), otherwise -> this module.

WHY THIS FILE REPORTS STEP STATE INSTEAD OF GUESSING
---------------------------------------------------
The first version hardcoded three "常见原因" (akshare 网络 / holdings.json
格式错 / 非交易日) and asserted "失败原因: record 步骤未成功完成".

On 2026-10-23 both claims were false. The job had died five steps EARLIER,
at `sudo apt-get install -y fonts-noto-cjk`, which aborted with
"Error: The operation was canceled." — so `python -m src.application.daily_snapshot` never
ran at all, and none of the three listed causes had anything to do with
it. The email did not merely fail to help; it pointed the debugging at
the wrong step.

So this version reports what actually happened:

  * the outcome + exit code of every instrumented step, handed in by
    daily.yml as `${{ steps.<id>.outcome }}` / `${{ steps.<id>.outputs.rc }}`
    (env `FONT_OUTCOME` / `FONT_RC` / `RECORD_OUTCOME` / `RECORD_RC`);
  * the tail of the failing step's captured log, read from the workspace.

A step is only diagnosable here if daily.yml wraps it and captures its
log. Adding a step to the workflow without adding it to
INSTRUMENTED_STEPS yields "unknown" for that step — which is honest, but
means the email stays vague for it. Keeping the two lists in sync is the
whole contract.
"""
from __future__ import annotations

import html as _html
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..infrastructure.github_actions import current_context
from .digest import build_html, build_text, send

# 北京时间 (UTC+8) 用于邮件显示
CST = timezone(timedelta(hours=8))

# (env 前缀, workflow step id, 显示名, 该步骤捕获的日志尾部文件名)
# 必须与 .github/workflows/daily.yml 里包装过的步骤一一对应。
INSTRUMENTED_STEPS = [
    ("FONT", "font", "Install CJK font (Linux runner)", "font_install.tail.log"),
    ("RECORD", "record", "Record today's snapshot", "record_stderr.tail.log"),
]

# 判定「这一步出了问题」: Actions 的 outcome 取值 success / failure /
# cancelled / skipped。空字符串 = 该步骤没跑 (env 没传进来)。
_BAD_OUTCOMES = ("failure", "cancelled", "skipped")


def diagnose_steps(env: dict | None = None, workspace: str | Path | None = None) -> list[dict]:
    """Collect per-step outcome / exit code / log tail.

    Pure w.r.t. the filesystem beyond reading two small files, and takes
    `env` + `workspace` explicitly so tests can drive it without Actions.
    """
    env = os.environ if env is None else env
    # The workspace path comes from the GitHub Actions adapter (D12) --
    # this module no longer knows the GITHUB_* variable names.
    ws = (Path(workspace) if workspace
          else (current_context(env).workspace or Path(".")))
    steps = []
    for prefix, sid, label, tail_file in INSTRUMENTED_STEPS:
        outcome = (env.get(f"{prefix}_OUTCOME") or "").strip() or "unknown"
        rc = (env.get(f"{prefix}_RC") or "").strip()
        tail = ""
        try:
            p = ws / tail_file
            if p.is_file():
                tail = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            tail = ""
        steps.append({"id": sid, "label": label, "outcome": outcome,
                      "rc": rc, "log": tail})
    return steps


def failing_steps(steps: list[dict]) -> list[dict]:
    """Steps that actually broke (hard failure / cancelled).

    `skipped` is deliberately NOT here: a step skipped because an earlier
    one died is a *symptom*, not a cause. Listing it as a failure is how
    the 2026-10-23 email ended up blaming the record step for an apt
    failure. Use `skipped_steps()` for the downstream fallout.
    """
    return [s for s in steps if s["outcome"] in ("failure", "cancelled")]


def skipped_steps(steps: list[dict]) -> list[dict]:
    """Steps that never ran because something upstream failed."""
    return [s for s in steps if s["outcome"] == "skipped"]


def _rc_suffix(step: dict) -> str:
    return f"  exit={step['rc']}" if step["rc"] else ""


def build_diagnosis_text(steps: list[dict], run_url: str) -> str:
    bad = failing_steps(steps)
    skipped = skipped_steps(steps)
    lines = ["", "失败的步骤:"]
    if not bad:
        lines.append("  (无 —— 所有受监控步骤都成功。失败发生在本邮件未覆盖的步骤,")
        lines.append("   见下方日志链接。)")
        for s in steps:
            lines.append(f"    ✓ {s['label']}")
    else:
        for s in bad:
            if s["outcome"] == "cancelled":
                lines.append(f"  ✗ {s['label']}: 被取消{_rc_suffix(s)}")
            else:
                lines.append(f"  ✗ {s['label']}: 失败{_rc_suffix(s)}")
            if s["log"]:
                lines.append("")
                lines.extend(f"    {ln}" for ln in s["log"].splitlines()[-25:])
                lines.append("")
    if skipped:
        lines.append("  因上游失败而未执行的步骤:")
        for s in skipped:
            lines.append(f"    – {s['label']} (未执行, 上面才是真正的故障点)")
    if run_url and run_url != "(unavailable)":
        lines.append(f"查看完整日志: {run_url}")
    return "\n".join(lines)


def build_diagnosis_html(steps: list[dict], run_url: str) -> str:
    bad = failing_steps(steps)
    skipped = skipped_steps(steps)
    rows = []
    for s in bad:
        why = (f"被取消{_rc_suffix(s)}" if s["outcome"] == "cancelled"
               else f"失败{_rc_suffix(s)}")
        rows.append(f"<div style='margin:6px 0'><strong>✗ {_html.escape(s['label'])}</strong>"
                    f" <span style='color:#c0392b'>{_html.escape(why)}</span></div>")
        if s["log"]:
            body = "\n".join(s["log"].splitlines()[-25:])
            rows.append(
                "<pre style='background:#fff;border:1px solid #e0e0e0;border-left:3px solid #c0392b;"
                "border-radius:3px;padding:10px;margin:4px 0 10px;overflow-x:auto;"
                "font-size:12px;line-height:1.45;white-space:pre-wrap'>"
                f"{_html.escape(body)}</pre>")
    if not rows:
        rows.append("<div style='color:#666'>受监控步骤均成功; "
                    "失败发生在本邮件未覆盖的步骤, 见下方日志链接。</div>")
    if skipped:
        names = ", ".join(_html.escape(s["label"]) for s in skipped)
        rows.append(f"<div style='margin:8px 0 0;color:#666'>因上游失败而未执行: "
                    f"<em>{names}</em> (上面才是真正的故障点)</div>")
    link = (f"<div style='margin-top:10px'><strong>查看完整日志:</strong> "
            f"<a href='{_html.escape(run_url, quote=True)}' "
            f"style='color:#0d6efd'>{_html.escape(run_url)}</a></div>"
            if run_url and run_url != "(unavailable)" else "")
    return (f"<div style='padding:14px 24px;background:#fff8e1;font-size:13px;"
            f"color:#5d4037;border-top:1px solid #ffe082'>"
            f"<strong>实际失败原因</strong>{''.join(rows)}{link}</div>")


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


def append_failure_extras(text: str, html: str, run_url: str,
                          steps: list[dict]) -> tuple[str, str]:
    """Insert the real diagnosis before the email footer."""
    extra_text = build_diagnosis_text(steps, run_url)
    extra_html = build_diagnosis_html(steps, run_url)
    # Insert before the footer ("— 自动化 GitHub Actions 发送")
    text = text.replace("\n— 自动化 GitHub Actions 发送",
                        extra_text + "\n— 自动化 GitHub Actions 发送")
    # Insert HTML extra block before </div>\n</body>
    html = html.replace('</div>\n</body>', extra_html + '</div>\n</body>')
    return text, html


def main() -> None:
    # The run link is GitHub Actions' business (D12) -- ask the adapter.
    run_url = current_context().run_url or "(unavailable)"

    steps = diagnose_steps()
    bad_desc = ", ".join(f"{s['id']}={s['outcome']}" for s in failing_steps(steps)) or "无"
    print(f"[notify_failure] 失败的步骤: {bad_desc}")

    r = build_failure_record()
    text = build_text(r)
    html = build_html(r, os.environ.get("PAGES_URL", ""))
    text, html = append_failure_extras(text, html, run_url, steps)

    subject = "[⚠ FAIL] 稳健再平衡 任务失败"
    send(subject, html, text)
    print(f"failure notice sent → {os.environ.get('MAIL_TO', '?')}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)