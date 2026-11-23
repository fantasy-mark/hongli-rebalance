"""
生成最新资产价格数据

获取所有监控资产的最新价格，保存为 JSON 文件供前端使用。

**两种口径，各有用途**：

| 字段 | 口径 | 用途 |
|---|---|---|
| `prices` | 后复权（HFQ） | **回测**：衡量含分红再投资的总收益 |
| `raw_prices` | 不复权（实际成交价） | **真实持仓**：算实际市值（份额 × 你能卖到的价） |

两者不能混用：真实持仓若用后复权价算市值，会把**买入之前**的分红
也算进持仓市值（例：红利ETF 515180 于 2026-10-16 除权，后复权倍率 1.2889，
32,000 份的后复权市值 ¥55,968，实际只有 ¥43,424）。

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
    获取指定资产的最新价格（后复权口径，回测用）

    Returns:
        {asset_code: latest_price}
    """
    return {
        code: price
        for code, (price, _) in fetch_latest_prices_with_date(asset_codes).items()
    }


def fetch_latest_prices_with_date(asset_codes: list[str]) -> Dict[str, Tuple[float, str]]:
    """
    获取指定资产的最新价格（后复权）及其日期

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


def fetch_raw_prices_with_date(asset_codes: list[str]) -> Dict[str, Tuple[float, str]]:
    """
    获取指定资产的最新**实际成交价**（不复权）及日期

    真实持仓的市值用这个。

    Returns:
        {asset_code: (price, 'YYYY-MM-DD')}
    """
    service = PriceQueryService()
    latest = {}

    for code in asset_codes:
        try:
            info = service.get_latest_raw_price(code)
            date = info['date']
            date_str = date.isoformat() if hasattr(date, 'isoformat') else str(date)
            latest[code] = (float(info['close']), date_str)
        except Exception as e:
            print(f"获取 {code} 实际成交价失败: {e}")
            continue

    return latest


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s|%(message)s')

    print("正在获取最新资产价格...")
    results = fetch_latest_prices_with_date(DEFAULT_ASSETS)

    print("正在获取最新实际成交价（不复权，真实持仓用）...")
    raw_results = fetch_raw_prices_with_date(DEFAULT_ASSETS)

    if results:
        prices = {code: value for code, (value, _) in results.items()}
        price_dates = {code: date for code, (_, date) in results.items()}

        raw_prices = {code: value for code, (value, _) in raw_results.items()}
        raw_price_dates = {code: date for code, (_, date) in raw_results.items()}

        output_path = Path('data/site/prices.json')
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(
                {
                    'prices': prices,
                    'price_dates': price_dates,
                    'raw_prices': raw_prices,
                    'raw_price_dates': raw_price_dates,
                    'as_of': max(price_dates.values()) if price_dates else None,
                },
                f, ensure_ascii=False, indent=2,
            )

        print(f"已保存最新价格到 {output_path}")
        for code, (price, date) in results.items():
            raw = raw_results.get(code)
            raw_text = f"（实际 {raw[0]:.4f} @ {raw[1]}）" if raw else "（实际价缺失）"
            print(f"  {code}: 后复权 {price:.4f}  {date}  {raw_text}")
    else:
        print("未能获取任何资产的最新价格")
