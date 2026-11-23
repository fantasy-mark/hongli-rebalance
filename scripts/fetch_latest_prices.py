"""
生成最新资产价格数据

获取所有监控资产的最新价格，保存为 JSON 文件供前端使用。

**最新价必须来自最近交易日**：同步时已确保缓存追到最近交易日，
并用实时报价补齐历史接口拿不到的那一根；这里同时把价格对应的
**日期**一并写出，前端可据此显示"最新价（2026-11-23）"，
价格过期时也能一眼看出来（而不是拿旧价当最新价用）。
"""

import json
import logging
from pathlib import Path
from typing import Dict, Tuple

from src.data.service import PriceQueryService

logger = logging.getLogger(__name__)

# 默认监控的资产
DEFAULT_ASSETS = ['515180', '159937', '511380', '513300']


def fetch_latest_prices(asset_codes: list[str]) -> Dict[str, float]:
    """
    获取指定资产的最新价格

    Args:
        asset_codes: 资产代码列表

    Returns:
        {asset_code: latest_price}
    """
    return {code: price for code, (price, _) in fetch_latest_prices_with_date(asset_codes).items()}


def fetch_latest_prices_with_date(asset_codes: list[str]) -> Dict[str, Tuple[float, str]]:
    """
    获取指定资产的最新价格及其日期

    Returns:
        {asset_code: (price, 'YYYY-MM-DD')}
    """
    service = PriceQueryService()
    latest = {}

    for code in asset_codes:
        try:
            info = service.get_latest_price(code)
            date = info['date']
            date_str = date.isoformat() if hasattr(date, 'isoformat') else str(date)
            latest[code] = (float(info['close']), date_str)
        except Exception as e:
            print(f"获取 {code} 最新价格失败: {e}")
            continue

    return latest


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s|%(message)s')

    print("正在获取最新资产价格...")
    results = fetch_latest_prices_with_date(DEFAULT_ASSETS)

    if results:
        prices = {code: value for code, (value, _) in results.items()}
        price_dates = {code: date for code, (_, date) in results.items()}

        output_path = Path('data/site/prices.json')
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(
                {
                    'prices': prices,
                    'price_dates': price_dates,
                    'as_of': max(price_dates.values()) if price_dates else None,
                },
                f, ensure_ascii=False, indent=2,
            )

        print(f"已保存最新价格到 {output_path}")
        for code, (price, date) in results.items():
            print(f"  {code}: {price:.4f}  ({date})")
    else:
        print("未能获取任何资产的最新价格")
