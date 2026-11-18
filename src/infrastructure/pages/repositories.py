"""
Pages 数据仓储（基础设施层）

负责把应用层的视图模型落盘成站点可 fetch 的 JSON / PNG 文件。
不做任何业务计算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.application.pages.site_builder import PagesSiteData
from src.domain.chart.overview import StrategyOverview
from src.infrastructure.chart.strategy_overview_renderer import (
    StrategyOverviewRenderer,
)


class PagesSiteRepository:
    """把 PagesSiteData 写入 pages/ 目录"""

    CHART_FILENAME = "grid_chart.png"
    SUMMARY_FILENAME = "summary.json"
    TRADES_FILENAME = "trades.json"

    def __init__(
        self,
        pages_dir: Path | str,
        renderer: StrategyOverviewRenderer | None = None,
    ) -> None:
        self.pages_dir = Path(pages_dir)
        self.data_dir = self.pages_dir / "data"
        self.renderer = renderer or StrategyOverviewRenderer()

    # ------------------------------------------------------------------ 对外
    def save_all(self, site: PagesSiteData) -> Dict[str, Path]:
        """一次性写完全部产物，返回 {名称: 路径}"""
        self.data_dir.mkdir(parents=True, exist_ok=True)

        return {
            "chart": self.save_chart(site.overview),
            "summary": self.save_summary(site.summary.to_dict()),
            "trades": self.save_trades([t.to_dict() for t in site.trades]),
        }

    def save_chart(self, overview: StrategyOverview) -> Path:
        return self.renderer.render(overview, self.data_dir / self.CHART_FILENAME)

    def save_summary(self, payload: Dict[str, Any]) -> Path:
        return self._write_json(self.data_dir / self.SUMMARY_FILENAME, payload)

    def save_trades(self, trades: Iterable[Dict[str, Any]]) -> Path:
        payload = {"trades": list(trades)}
        return self._write_json(self.data_dir / self.TRADES_FILENAME, payload)

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
