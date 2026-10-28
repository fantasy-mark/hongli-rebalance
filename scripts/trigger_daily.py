"""Re-trigger the daily workflow —— 一份实现, 两个调用者 (D12)。

用法 (在 GitHub Actions 里):
    python -X utf8 scripts/trigger_daily.py

环境变量:
    GITHUB_TOKEN     必填 —— 派发需要 Bearer 令牌 (workflow 里传
                     `${{ github.token }}`)
    GITHUB_REPOSITORY  必填 —— `owner/repo` (Actions 自动注入)

---
**为什么要有这个脚本**

`apply-grid.yml` 与 `apply-holdings.yml` 里**各抄了一份**同样的 curl:

    curl -fsS -X POST ... /repos/{repo}/dispatches -d '{"event_type":"trigger"}'

抄两份的代价:

1. 改一处漏一处 —— 两边的 header 已经不一样了 (User-Agent)。
2. 出错时只有一行 curl 的 stderr。`curl -fsS` 在 401 上会**静默**失败
   (HTTP 错误码不是传输错误), 而令牌没注入是最常见的原因 —— 那时候看到
   的是「没反应」, 不是「401」。

这里把它变成**一段可测的 Python**: 令牌从环境变量读, HTTP 错误码被翻译
成一句人话 + 一条 `::error::` annotation (直接出现在 Actions UI 上)。

**stdlib-only** —— 这两个 job 不装任何依赖。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.infrastructure.github_actions import (  # noqa: E402
    current_context,
    error as annotate_error,
    workflow_token,
)

#: `daily.yml` 监听的 repository_dispatch 事件名。
EVENT_TYPE = "trigger"


def dispatch(repo: str, event_type: str, token: str,
             api_url: str = "https://api.github.com",
             timeout: float = 30.0) -> int:
    """POST `/repos/{repo}/dispatches` —— 返回 HTTP 状态码 (204 = 受理)。

    Raises:
        urllib.error.HTTPError: 服务端回了 4xx/5xx。
        urllib.error.URLError: 连不上。
    """
    url = f"{api_url.rstrip('/')}/repos/{repo}/dispatches"
    body = json.dumps({"event_type": event_type}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "hongli-rebalance-bot",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(resp.status)


def main() -> None:
    ctx = current_context()
    repo = ctx.repository
    token = workflow_token()

    if not repo:
        raise SystemExit("ERROR GITHUB_REPOSITORY 是空的 —— 不知道往哪个仓库派发")
    if not token:
        raise SystemExit("ERROR GITHUB_TOKEN 是空的 —— 派发需要 Bearer 令牌 "
                         "(workflow 里传 ${{ github.token }})")

    try:
        status = dispatch(repo, EVENT_TYPE, token, ctx.api_url)
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode("utf-8", "replace")[:500]
        annotate_error(f"派发 {EVENT_TYPE} 失败: HTTP {ex.code} — {detail}")
        raise SystemExit(f"ERROR HTTP {ex.code}: {detail}")
    except urllib.error.URLError as ex:
        annotate_error(f"派发 {EVENT_TYPE} 失败: {ex.reason}")
        raise SystemExit(f"ERROR {ex.reason}")

    print(f"dispatched {EVENT_TYPE} -> {repo} (HTTP {status})")


if __name__ == "__main__":
    main()
