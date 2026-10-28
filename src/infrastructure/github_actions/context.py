"""Actions 的**这次运行**是谁 —— 仓库 / run id / 工作区 (D12)。

`GITHUB_SERVER_URL` / `GITHUB_REPOSITORY` / `GITHUB_RUN_ID` /
`GITHUB_WORKSPACE` 这四个环境变量**只应该**在这个包里出现 —— 别的层
想知道「这次 run 的链接是什么」, 就问 `current_context().run_url`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class RunContext:
    """一次 GitHub Actions 运行的上下文。

    Attributes:
        server_url:  GitHub 实例地址 (自建实例会是别的域名)。
        repository:  `owner/repo`。
        run_id:      本次 run 的 id。
        workspace:   checkout 出来的工作区目录 (不在 Actions 里 = `None`)。
        api_url:     REST API 的根 (自建实例会不一样)。
    """

    server_url: str = ""
    repository: str = ""
    run_id: str = ""
    workspace: Optional[Path] = None
    api_url: str = "https://api.github.com"

    @property
    def is_actions(self) -> bool:
        """我们现在**是不是**跑在 GitHub Actions 里。"""
        return bool(self.repository and self.run_id)

    @property
    def run_url(self) -> str:
        """这次 run 的网页地址 —— 不在 Actions 里 = 空串。"""
        if not (self.server_url and self.repository and self.run_id):
            return ""
        return f"{self.server_url}/{self.repository}/actions/runs/{self.run_id}"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "RunContext":
        """从环境变量读 —— 可以显式传 `env` (测试用)。"""
        e = os.environ if env is None else env
        workspace = (e.get("GITHUB_WORKSPACE") or "").strip()
        api_url = (e.get("GITHUB_API_URL") or "").strip().rstrip("/")
        return cls(
            server_url=(e.get("GITHUB_SERVER_URL") or "").strip().rstrip("/"),
            repository=(e.get("GITHUB_REPOSITORY") or "").strip(),
            run_id=(e.get("GITHUB_RUN_ID") or "").strip(),
            workspace=Path(workspace) if workspace else None,
            api_url=api_url or "https://api.github.com",
        )


def current_context(env: Optional[Mapping[str, str]] = None) -> RunContext:
    """当前这次 Actions 运行的上下文。"""
    return RunContext.from_env(env)


def workflow_token(env: Optional[Mapping[str, str]] = None) -> str:
    """用来派发 workflow 事件的 Bearer 令牌 (`GITHUB_TOKEN`)。

    ⚠️ Actions **不会**自动注入它 —— workflow 要自己写
    `env: GITHUB_TOKEN: ${{{{ github.token }}}}`。空串 = 没有令牌。
    """
    e = os.environ if env is None else env
    return (e.get("GITHUB_TOKEN") or "").strip()


__all__ = ["RunContext", "current_context", "workflow_token"]
