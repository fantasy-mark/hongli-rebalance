"""
Pages 数据落盘（图表模块）

负责把视图模型落盘成站点可 fetch 的 JSON / PNG 文件。
不做任何业务计算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.chart.site import PagesSiteData
from src.chart.overview import StrategyOverview
from src.chart.overview_renderer import (
    StrategyOverviewRenderer,
)


class PagesSiteRepository:
    """把 PagesSiteData 写入站点目录"""

    CHART_FILENAME = "grid_chart.png"
    SUMMARY_FILENAME = "summary.json"
    BACKTEST_TRADES_FILENAME = "backtest_trades.json"  # 回测产生的交易记录
    TRADES_FILENAME = "trades.json"  # 用户提交的真实交易记录
    DATA_SUBDIR = "site"   # 站点数据存放于 <pages_dir>/data/site/

    def __init__(
        self,
        pages_dir: Path | str,
        renderer: StrategyOverviewRenderer | None = None,
        data_subdir: str | None = None,
    ) -> None:
        self.pages_dir = Path(pages_dir)
        subdir = data_subdir if data_subdir is not None else self.DATA_SUBDIR
        self.data_dir = self.pages_dir / "data" / subdir
        self.renderer = renderer or StrategyOverviewRenderer()

    # ------------------------------------------------------------------ 对外
    def save_all(self, site: PagesSiteData) -> Dict[str, Path]:
        """一次性写完全部产物，返回 {名称: 路径}"""
        self.data_dir.mkdir(parents=True, exist_ok=True)

        return {
            "chart": self.save_chart(site.overview),
            "summary": self.save_summary(site.summary.to_dict()),
            "backtest_trades": self.save_trades([t.to_dict() for t in site.trades]),
            "trades": self.save_real_trades(site.real_trades),
        }

    def save_real_trades(self, trades: List[Dict[str, Any]]) -> Path:
        """
        写真实交易记录（站点投影）

        真相源是交易账本 `data/trades.json`，
        这里只是投影，**不覆盖账本**。
        """
        payload = {"trades": list(trades)}
        return self._write_json(self.data_dir / self.TRADES_FILENAME, payload)

    def save_chart(self, overview: StrategyOverview) -> Path:
        return self.renderer.render(overview, self.data_dir / self.CHART_FILENAME)

    def save_summary(self, payload: Dict[str, Any]) -> Path:
        return self._write_json(self.data_dir / self.SUMMARY_FILENAME, payload)

    def save_trades(self, trades: Iterable[Dict[str, Any]]) -> Path:
        payload = {"trades": list(trades)}
        return self._write_json(self.data_dir / self.BACKTEST_TRADES_FILENAME, payload)

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        return path


class PagesStaticRepository:
    """页面静态资源（index.html / app.js / style.css）定位"""

    def __init__(self, pages_dir: Path | str) -> None:
        self.pages_dir = Path(pages_dir)

    def list_static_files(self) -> List[Path]:
        return sorted(
            p for p in self.pages_dir.iterdir()
            if p.is_file() and p.suffix in {".html", ".js", ".css"}
        )
