"""运行配置 —— 环境变量 → 显式配置对象 (Phase 5)。

⚠️ **领域层不读应用的环境变量** (`domain/grid/config.py` 是纯数据 +
一个 `from_env` 工厂; 领域层的模块级常量都是它的别名)。转换发生在这里
—— 应用层是系统的**边缘**, 环境的知识属于边缘。

用法:

```python
config = AppConfig.from_env()

policy = RebalancePolicy(threshold_pp=config.threshold_pp)
grid   = config.grid                       # GridConfig —— 七参数一个值
```

⚠️ **本模块不改任何行为**: `from_env()` 读的变量与以前**完全一样**
(`THRESHOLD_PP` + 网格那几个)。以前是各个模块自己 `os.environ.get`,
现在读一次, 之后一路当**值**传。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from ..domain.grid.config import GridConfig


@dataclass(frozen=True)
class AppConfig:
    """一次运行用的全部配置。

    Attributes:
        threshold_pp: 再平衡阈值 —— 偏离多少个百分点才触发 (默认 3)
        grid:         网格规则 (七参数一个值)
    """

    threshold_pp: float = 3.0
    grid: GridConfig = field(default_factory=GridConfig)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AppConfig":
        """从环境变量构造。

        Args:
            env: 环境变量映射; `None` = 用 `os.environ`。
        """
        e = os.environ if env is None else env
        return cls(
            threshold_pp=float(e.get("THRESHOLD_PP", 3.0)),
            grid=GridConfig.from_env(e),
        )


__all__ = ["AppConfig"]
