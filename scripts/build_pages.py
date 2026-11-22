"""
构建 GitHub Pages 站点（CLI 入口）

用法:
    python scripts/build_pages.py            # 用缓存/远端价格跑回测并生成站点数据
    python scripts/build_pages.py --offline  # 只用本地缓存

产物:
    data/site/grid_chart.png        策略全景图
    data/site/summary.json          策略汇总（组合 / 再平衡 / 网格）
    data/site/backtest_trades.json  回测交易记录

真实交易**不在这里**——真相源是 data/trades.json（交易账本），
页面直接读它，不另存副本。

分层:
    application/pages/site_builder.py      用例（编排 + 领域翻译）
    infrastructure/chart/...               全景图渲染（matplotlib）
    infrastructure/pages/repositories.py   落盘（JSON / PNG）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，统一改 UTF-8 以免中文/¥ 报错
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.chart.site import BuildPagesSite, default_config
from src.chart.site_store import PagesSiteRepository


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 GitHub Pages 站点数据")
    parser.add_argument(
        "--pages-dir",
        default=str(ROOT),
        help="站点根目录（默认仓库根目录）",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="回测起始日 YYYY-MM-DD（默认 2020-11-05）",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="回测结束日 YYYY-MM-DD（默认取最新可用交易日）",
    )
    parser.add_argument(
        "--dpi", type=int, default=150, help="策略全景图 DPI（默认 150）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from datetime import date as _date

    start = _date.fromisoformat(args.start) if args.start else None
    end = _date.fromisoformat(args.end) if args.end else None

    config = default_config(start_date=start, end_date=end)

    print("=" * 64)
    print("构建 GitHub Pages 站点")
    print(f"  资产: {', '.join(config.allocation.asset_codes)}")
    print(f"  再平衡阈值: {config.threshold_pp}pp")
    print(f"  再平衡资金: ¥{float(config.rebalance_initial_capital):,.0f}")
    print(f"  网格资金:   ¥{float(config.grid_initial_capital):,.0f}")
    print("=" * 64)

    site = BuildPagesSite(config).execute()

    summary = site.summary
    print("\n回测完成:")
    print(f"  区间: {summary.window_start} → {summary.window_end}")
    print(f"  组合: {float(summary.combined.total_return):+.2%}  "
          f"(¥{float(summary.combined.total_value):,.0f})")
    print(f"  再平衡: {float(summary.rebalance.total_return):+.2%}")
    print(f"  网格:   {float(summary.grid.total_return):+.2%}  "
          f"({summary.grid.extra['completed_rounds']}/"
          f"{summary.grid.extra['total_rounds']} 轮完成)")
    print(f"  占用峰值: ¥{float(site.overview.peak_occupation):,.0f}")

    from src.chart.overview_renderer import (
        StrategyOverviewRenderer,
    )

    repo = PagesSiteRepository(
        args.pages_dir,
        renderer=StrategyOverviewRenderer(dpi=args.dpi),
    )
    paths = repo.save_all(site)

    # 获取并保存最新资产价格
    print("\n获取最新资产价格...")
    try:
        from scripts.fetch_latest_prices import fetch_latest_prices
        latest_prices = fetch_latest_prices(config.allocation.asset_codes)
        if latest_prices:
            prices_path = Path(args.pages_dir) / 'data' / 'site' / 'prices.json'
            with open(prices_path, 'w', encoding='utf-8') as f:
                import json
                json.dump({'prices': latest_prices}, f, ensure_ascii=False, indent=2)
            paths['prices'] = prices_path
            print(f"最新价格已保存")
        else:
            print("未能获取最新价格")
    except Exception as e:
        print(f"获取最新价格失败: {e}")

    print("\n产物:")
    for name, path in paths.items():
        print(f"  {name:8s} {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
