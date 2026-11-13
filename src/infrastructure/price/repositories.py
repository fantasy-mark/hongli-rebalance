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

    # ------------------------------------------------------------------
    # 可选能力：标记"已缓存完整历史"
    #
    # 某些资产上市较晚，请求区间早于其首个交易日时，无法再向前扩展。
    # 仓储记录该标记后，缓存规划器就不会反复触发全量重拉。
    # ------------------------------------------------------------------

    def mark_history_complete(self, asset_code: AssetCode) -> None:
        """标记该资产已缓存完整历史（默认空实现）"""
        pass

    def is_history_complete(self, asset_code: AssetCode) -> bool:
        """该资产的缓存是否为完整历史（默认未知）"""
        return False

    def get_adjustment(self, asset_code: AssetCode) -> Optional[str]:
        """该资产缓存的价格口径（hfq/qfq/none），未知时返回 None"""
        return None

    def set_adjustment(self, asset_code: AssetCode, adjustment: str) -> None:
        """记录缓存的价格口径（默认空实现）"""
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
        # 资产代码 -> 是否已缓存完整历史
        self._history_complete: Dict[str, bool] = {}
        # 资产代码 -> 缓存的价格口径
        self._adjustment: Dict[str, str] = {}
    
    def _get_file_path(self, asset_code: AssetCode) -> Path:
        """获取文件路径"""
        return self.storage_dir / f"{asset_code.code}.json"
    
    def save(self, series: AdjustedPriceSeries) -> None:
        """保存到JSON文件（Decimal 以字符串写入，保证精度可逆）"""
        file_path = self._get_file_path(series.asset_code)

        data = {
            'asset_code': series.asset_code.code,
            'asset_type': series.asset_type.value,
            'source': series.source.value,
            'calculated_at': series.calculated_at.isoformat(),
            # 用元组列表而非 dict，体积更小、反序列化更快
            'price_fields': ['date', 'open', 'high', 'low', 'close', 'volume', 'amount'],
            'history_complete': self._history_complete.get(str(series.asset_code), False),
            'adjustment': self._adjustment.get(str(series.asset_code)),
            'prices': [
                [
                    p.date.isoformat(),
                    str(p.open), str(p.high), str(p.low), str(p.close),
                    int(p.volume), str(p.amount),
                ]
                for p in series.prices
            ],
        }

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

        # 同时更新内存缓存
        self._memory_cache.save(series)

        logger.debug(f"已保存 {series.asset_code} 到 {file_path}")

    def find_by_code(self, asset_code: AssetCode) -> Optional[AdjustedPriceSeries]:
        """从内存或文件加载（直接构造领域对象，避免 DataFrame 中转）"""
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

            from src.domain.price.entities import AssetType, DataSource, PricePoint
            from datetime import datetime
            from decimal import Decimal

            raw_prices = data.get('prices', [])

            # 兼容两种存储格式：紧凑数组 / 旧版 dict 列表
            if raw_prices and isinstance(raw_prices[0], list):
                points = [
                    PricePoint(
                        date=date.fromisoformat(row[0]),
                        open=Decimal(row[1]),
                        high=Decimal(row[2]),
                        low=Decimal(row[3]),
                        close=Decimal(row[4]),
                        volume=int(row[5]),
                        amount=Decimal(row[6]) if len(row) > 6 else Decimal("0"),
                    )
                    for row in raw_prices
                ]
            else:
                points = [
                    PricePoint(
                        date=date.fromisoformat(str(p['date'])[:10]),
                        open=Decimal(str(p['open'])),
                        high=Decimal(str(p['high'])),
                        low=Decimal(str(p['low'])),
                        close=Decimal(str(p['close'])),
                        volume=int(p['volume']),
                        amount=Decimal(str(p.get('amount', 0))),
                    )
                    for p in raw_prices
                ]

            self._history_complete[str(asset_code)] = bool(
                data.get('history_complete', False)
            )
            if data.get('adjustment'):
                self._adjustment[str(asset_code)] = data['adjustment']

            series = AdjustedPriceSeries(
                asset_code=asset_code,
                asset_type=AssetType(data['asset_type']),
                prices=points,
                source=DataSource(data['source']),
                calculated_at=(datetime.fromisoformat(data['calculated_at'])
                               if data.get('calculated_at') else datetime.now()),
            )

            # 缓存到内存
            self._memory_cache.save(series)

            return series

        except Exception as e:
            logger.error(f"加载 {asset_code} 失败: {e}")
            return None
    
    def mark_history_complete(self, asset_code: AssetCode) -> None:
        """标记该资产已缓存完整历史（随文件持久化）"""
        key = str(asset_code)
        if self._history_complete.get(key):
            return

        self._history_complete[key] = True

        # 立即落到文件，避免进程退出后丢失
        series = self._memory_cache.find_by_code(asset_code)
        if series is not None:
            self.save(series)

    def is_history_complete(self, asset_code: AssetCode) -> bool:
        """该资产的缓存是否为完整历史"""
        return self._history_complete.get(str(asset_code), False)

    def get_adjustment(self, asset_code: AssetCode) -> Optional[str]:
        """该资产缓存的价格口径"""
        return self._adjustment.get(str(asset_code))

    def set_adjustment(self, asset_code: AssetCode, adjustment: str) -> None:
        """记录缓存的价格口径（随文件持久化）"""
        key = str(asset_code)
        if self._adjustment.get(key) == adjustment:
            return
        self._adjustment[key] = adjustment
        series = self._memory_cache.find_by_code(asset_code)
        if series is not None:
            self.save(series)

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
