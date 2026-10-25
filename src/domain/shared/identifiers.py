"""AssetCode & TradingDate — 标识符值对象（domain/shared）。

`scripts/apply_grid.py` 与 `scripts/apply_holdings.py` 曾经**各自**带一份

    CODE_RE = re.compile(r"^\\d{6}$")
    DATE_RE = re.compile(r"^\\d{4}-\\d{2}-\\d{2}$")

改一处漏一处 —— 两个脚本的校验逻辑会悄悄分叉。`app.js` 里还有第三份
（前端没法 import Python，只能各写各的；但服务端至少要有**唯一**实现）。

`AssetCode` 与 `TradingDate` 就是那唯一实现。

⚠️ **stdlib-only** —— `apply_holdings.py` / `apply_grid.py` 在**不装
任何依赖**的 GitHub Actions job 里跑，这个模块只 import `re` / `datetime`。
"""
from __future__ import annotations

import datetime
import re


class AssetCode(str):
    """6 位证券代码 —— 构造时校验格式。

    是 `str` 的子类：可以直接当 dict key、跟普通字符串比较、`json.dumps`
    出来还是字符串。**引入它不会改变任何现有行为**，只是把「代码长什么样」
    这条规则收到一个地方。

    接受带交易所后缀的写法（`"515180.SH"` → `"515180"`）—— 券商导出的
    持仓表经常带后缀。
    """

    PATTERN = re.compile(r"^\d{6}$")

    def __new__(cls, value) -> "AssetCode":
        raw = str(value).strip()
        code = raw.split(".")[0]
        if not cls.PATTERN.match(code):
            raise ValueError(f"asset code must be 6 digits, got {code!r}")
        return super().__new__(cls, code)

    @property
    def is_valid(self) -> bool:
        return bool(self.PATTERN.match(self))


class TradingDate(datetime.date):
    """交易日 —— ISO `yyyy-mm-dd`。

    是 `datetime.date` 的子类：比较、算术、`.isoformat()` 全部照旧，
    `isinstance(d, datetime.date)` 依然成立。

    格式校验与 `datetime.date.fromisoformat` **都**要做：
      - 只用正则 → `"2026-02-30"`（不存在的日期）会被放行
      - 只用 fromisoformat → Python 3.11+ 接受 `"20261026"` / `"2026-W01-1"`
        等宽格式，比原来 `^\\d{4}-\\d{2}-\\d{2}$` 宽松

    两个都做 = 保持原来的严格度，同时多挡掉「格式对但日期不存在」。
    """

    PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def __new__(cls, value) -> "TradingDate":
        if isinstance(value, TradingDate):
            return value
        if isinstance(value, datetime.datetime):
            raw = value.date().isoformat()
        elif isinstance(value, datetime.date):
            raw = value.isoformat()
        else:
            raw = str(value).strip()
        if not cls.PATTERN.match(raw):
            raise ValueError(f"trading date must be YYYY-MM-DD, got {raw!r}")
        try:
            parsed = datetime.date.fromisoformat(raw)
        except ValueError as ex:
            raise ValueError(f"not a real date: {raw!r} ({ex})") from ex
        return super().__new__(cls, parsed.year, parsed.month, parsed.day)

    @classmethod
    def parse(cls, value) -> "TradingDate":
        """Construct from anything date-ish; raises ValueError on bad input."""
        return cls(value)

    def __repr__(self) -> str:
        return f"TradingDate({self.isoformat()})"


__all__ = ["AssetCode", "TradingDate"]
