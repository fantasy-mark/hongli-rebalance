"""
价格序列仓储实现

提供内存缓存和文件存储
"""

from abc import ABC, abstractmethod
from datetime import date
from typing import Optional, Dict
import logging
import json
from pathlib import Path

from src.domain.price.entities import AdjustedPriceSeries, AssetCode
from src.domain.price.value_objects import DateRange

logger = logging.getLogger(__name__)


class PriceSeriesRepository(ABC):
    """价格序列仓储接口"""
    
    @abstractmethod
    def save(self, series: AdjustedPriceSeries) -> None:
        """保存价格序列"""
        pass
    
    @abstractmethod
    def find_by_code(self, asset_code: AssetCode) -> Optional[AdjustedPriceSeries]:
        """根据资产代码查找"""
        pass
    
    @abstractmethod
    def find_by_date_range(self,
                           asset_code: AssetCode,
                           date_range: DateRange) -> AdjustedPriceSeries:
        """根据日期范围查找"""
        pass
    
    @abstractmethod
    def exists(self, asset_code: AssetCode) -> bool:
        """检查是否存在"""
        pass
    
    @abstractmethod
    def delete(self, asset_code: AssetCode) -> None:
        """删除"""
        pass
    
    @abstractmethod
    def clear(self) -> None:
        """清空所有"""
        pass


class InMemoryPriceRepository(PriceSeriesRepository):
    """
    内存价格仓储
    
    特点:
    - 快速访问
    - 进程内缓存
    - 重启后丢失
    """
    
    def __init__(self):
        self._cache: Dict[str, AdjustedPriceSeries] = {}
    
    def save(self, series: AdjustedPriceSeries) -> None:
        """保存到内存"""
        key = str(series.asset_code)
        self._cache[key] = series
        logger.debug(f"已缓存 {key} ({series.count}条)")
    
    def find_by_code(self, asset_code: AssetCode) -> Optional[AdjustedPriceSeries]:
        """从内存查找"""
        key = str(asset_code)
        return self._cache.get(key)
    
    def find_by_date_range(self,
                           asset_code: AssetCode,
                           date_range: DateRange) -> AdjustedPriceSeries:
        """查找并过滤日期范围"""
        series = self.find_by_code(asset_code)
        if not series:
            # 返回空序列
            from src.domain.price.entities import AssetType
            return AdjustedPriceSeries(
                asset_code=asset_code,
                asset_type=AssetType.ETF,
                prices=[],
            )
        
        # 过滤价格
        filtered_prices = [
            p for p in series.prices
            if date_range.contains(p.date)
        ]
        
        # 创建新序列
        from copy import deepcopy
        new_series = deepcopy(series)
        new_series.prices = filtered_prices
        return new_series
    
    def exists(self, asset_code: AssetCode) -> bool:
        """检查是否存在"""
        return str(asset_code) in self._cache
    
    def delete(self, asset_code: AssetCode) -> None:
        """删除"""
        key = str(asset_code)
        if key in self._cache:
            del self._cache[key]
            logger.debug(f"已删除 {key} 的缓存")
    
    def clear(self) -> None:
        """清空"""
        count = len(self._cache)
        self._cache.clear()
        logger.info(f"已清空缓存 ({count}个资产)")


class JsonFilePriceRepository(PriceSeriesRepository):
    """
    JSON文件价格仓储
    
    特点:
    - 持久化存储
    - 可跨进程共享
    - 适合少量资产
    """
    
    def __init__(self, storage_dir: str = "data/price_cache"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: InMemoryPriceRepository = InMemoryPriceRepository()
    
    def _get_file_path(self, asset_code: AssetCode) -> Path:
        """获取文件路径"""
        return self.storage_dir / f"{asset_code.code}.json"
    
    def save(self, series: AdjustedPriceSeries) -> None:
        """保存到JSON文件"""
        file_path = self._get_file_path(series.asset_code)
        
        data = {
            'asset_code': series.asset_code.code,
            'asset_type': series.asset_type.value,
            'source': series.source.value,
            'calculated_at': series.calculated_at.isoformat(),
            'prices': [p.to_dict() for p in series.prices],
        }
        
        # 序列化date对象
        for price in data['prices']:
            price['date'] = price['date'].isoformat()
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # 同时更新内存缓存
        self._memory_cache.save(series)
        
        logger.debug(f"已保存 {series.asset_code} 到 {file_path}")
    
    def find_by_code(self, asset_code: AssetCode) -> Optional[AdjustedPriceSeries]:
        """从文件或内存加载"""
        # 先查内存
        cached = self._memory_cache.find_by_code(asset_code)
        if cached:
            return cached
        
        # 再查文件
        file_path = self._get_file_path(asset_code)
        if not file_path.exists():
            return None
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 反序列化
            import pandas as pd
            prices_data = []
            for p in data['prices']:
                p['date'] = pd.to_datetime(p['date']).date()
                prices_data.append(p)
            
            df = pd.DataFrame(prices_data)
            df.set_index('date', inplace=True)
            
            from src.domain.price.entities import AssetType, DataSource
            from datetime import datetime
            
            asset_type = AssetType(data['asset_type'])
            source = DataSource(data['source'])
            calculated_at = datetime.fromisoformat(data['calculated_at'])
            
            series = AdjustedPriceSeries.from_dataframe(
                asset_code=asset_code,
                df=df,
                source=source,
            )
            series.calculated_at = calculated_at
            
            # 缓存到内存
            self._memory_cache.save(series)
            
            return series
            
        except Exception as e:
            logger.error(f"加载 {asset_code} 失败: {e}")
            return None
    
    def find_by_date_range(self,
                           asset_code: AssetCode,
                           date_range: DateRange) -> AdjustedPriceSeries:
        """查找并过滤"""
        series = self.find_by_code(asset_code)
        if not series:
            from src.domain.price.entities import AssetType
            return AdjustedPriceSeries(
                asset_code=asset_code,
                asset_type=AssetType.ETF,
                prices=[],
            )
        
        filtered_prices = [
            p for p in series.prices
            if date_range.contains(p.date)
        ]
        
        from copy import deepcopy
        new_series = deepcopy(series)
        new_series.prices = filtered_prices
        return new_series
    
    def exists(self, asset_code: AssetCode) -> bool:
        """检查是否存在"""
        return (
            self._memory_cache.exists(asset_code) or
            self._get_file_path(asset_code).exists()
        )
    
    def delete(self, asset_code: AssetCode) -> None:
        """删除文件和缓存"""
        file_path = self._get_file_path(asset_code)
        if file_path.exists():
            file_path.unlink()
            logger.debug(f"已删除 {file_path}")
        
        self._memory_cache.delete(asset_code)
    
    def clear(self) -> None:
        """清空所有文件"""
        self._memory_cache.clear()
        
        for file_path in self.storage_dir.glob("*.json"):
            file_path.unlink()
        
        logger.info(f"已清空 {self.storage_dir} 下的所有缓存文件")
