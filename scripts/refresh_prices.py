"""
只刷新最新资产价格（不跑回测）

场景：
- 只想让页面显示今天的最新价/市值/收益
- 不想重跑整个 build_pages.py（回测要几分钟）

输出：data/site/prices.json（覆盖原有价格部分，其他字段保留）
"""

import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

# 默认监控的资产
DEFAULT_ASSETS = ['515180', '159937', '511380', '513300']
PRICES_PATH = ROOT / 'data' / 'site' / 'prices.json'


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s|%(message)s')

    from scripts.fetch_latest_prices import (
        fetch_latest_prices_with_date,
        fetch_raw_prices_with_date,
    )

    print("正在获取最新价格...")
    results = fetch_latest_prices_with_date(DEFAULT_ASSETS)
    raw_results = fetch_raw_prices_with_date(DEFAULT_ASSETS)

    if not results:
        print("未能获取任何资产的最新价格")
        return 1

    # 合并写入：保留原有字段（如有），覆盖价格部分
    existing = {}
    if PRICES_PATH.exists():
        try:
            existing = json.loads(PRICES_PATH.read_text(encoding='utf-8'))
        except Exception as exc:
            print(f"读取旧 prices.json 失败（会覆盖）: {exc}")

    payload = {
        **existing,
        'prices': {code: value for code, (value, _) in results.items()},
        'price_dates': {code: date for code, (_, date) in results.items()},
        'raw_prices': {code: value for code, (value, _) in raw_results.items()},
        'raw_price_dates': {code: date for code, (_, date) in raw_results.items()},
        'as_of': max(date for _, date in results.values()),
    }

    PRICES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PRICES_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"已保存到 {PRICES_PATH.relative_to(ROOT)}")
    print(f"最新价日期: {payload['as_of']}")
    for code, (value, date) in results.items():
        raw = raw_results.get(code)
        raw_text = f"  实际 {raw[0]:.4f}" if raw else "  实际价缺失"
        print(f"  {code}: 后复权 {value:.4f}  @{date}{raw_text}")

    # 顺便同步 summary.json 里的 latest_prices（让没走 prices.json 的前端路径也能拿到）
    summary_path = ROOT / 'data' / 'site' / 'summary.json'
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
            summary['latest_prices'] = payload['prices']
            summary['latest_price_dates'] = payload['price_dates']
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            print(f"已同步 {summary_path.relative_to(ROOT)}")
        except Exception as exc:
            print(f"同步 summary.json 失败（不影响主流程）: {exc}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
