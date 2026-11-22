"""
生成最新资产价格数据

获取所有监控资产的最新价格，保存为 JSON 文件供前端使用。
"""

import json
from pathlib import Path
from decimal import Decimal
from typing import Dict

from src.data.service import PriceQueryService


def fetch_latest_prices(asset_codes: list[str]) -> Dict[str, float]:
    """
    获取指定资产的最新价格
    
    Args:
        asset_codes: 资产代码列表
        
    Returns:
        {asset_code: latest_price}
    """
    service = PriceQueryService()
    latest_prices = {}
    
    for code in asset_codes:
        try:
            latest = service.get_latest_price(code)
            latest_prices[code] = float(latest['close'])
        except Exception as e:
            print(f"获取 {code} 最新价格失败: {e}")
            continue
    
    return latest_prices


if __name__ == '__main__':
    # 默认监控的资产
    DEFAULT_ASSETS = ['515180', '159937', '511380', '513300']
    
    print("正在获取最新资产价格...")
    prices = fetch_latest_prices(DEFAULT_ASSETS)
    
    if prices:
        output_path = Path('data/site/prices.json')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump({'prices': prices}, f, ensure_ascii=False, indent=2)
        
        print(f"已保存最新价格到 {output_path}")
        for code, price in prices.items():
            print(f"  {code}: {price:.4f}")
    else:
        print("未能获取任何资产的最新价格")
