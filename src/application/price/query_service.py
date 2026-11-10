"""
价格查询应用服务

编排领域对象完成用例
"""

from datetime import date
from decimal import Decimal
from typing import Optional, List, Dict
import logging

import pandas as pd

from src.domain.price.entities import AdjustedPriceSeries, AssetCode, DataSource
from src.domain.price.value_objects import ReturnMetrics, DateRange
from src.domain.price.services import PriceCalculator
from src.infrastructure.price.repositories import PriceSeriesRepository, InMemoryPriceRepository
from src.infrastructure.adjusted_price_provider import fetch_adjusted_kline

logger = logging.getLogger(__name__)


class PriceQueryService:
    """
    价格查询服务
    
    职责:
    - 协调仓储和领域服务
    - 提供简化的API供外部使用
    - 处理缓存和数据源降级
    """
    
    def __init__(self, 
                 repository: Optional[PriceSeriesRepository] = None,
                 calculator: Optional[PriceCalculator] = None):
        self.repository = repository or InMemoryPriceRepository()
        self.calculator = calculator or PriceCalculator()
    
    def get_adjusted_prices(self,
                            code: str,
                            start_date: Optional[str] = None,
                            end_date: Optional[str] = None,
                            use_cache: bool = True) -> pd.DataFrame:
        """
        获取后复权价格数据（用于图表）
        
        Args:
            code: 6位资产代码
            start_date: 起始日期 'YYYY-MM-DD'
            end_date: 结束日期 'YYYY-MM-DD'
            use_cache: 是否使用缓存
        
        Returns:
            DataFrame with columns: open, high, low, close, volume, amount, source
        """
        asset_code = AssetCode.from_string(code)
        
        # 尝试从缓存获取
        if use_cache:
            cached = self.repository.find_by_code(asset_code)
            if cached and cached.count > 0:
                logger.info(f"从缓存获取 {code} 的价格数据 ({cached.count}条)")
                df = cached.to_dataframe()
                
                # 过滤日期范围（索引可能是 date 对象，统一转为 DatetimeIndex 比较）
                if start_date or end_date:
                    index = pd.to_datetime(df.index)
                    mask = pd.Series(True, index=df.index)
                    if start_date:
                        mask &= index >= pd.to_datetime(start_date)
                    if end_date:
                        mask &= index <= pd.to_datetime(end_date)
                    df = df[mask]
                
                return df
        
        # 从数据源获取
        logger.info(f"从数据源获取 {code} 的价格数据")
        try:
            df_raw = fetch_adjusted_kline(code, start_date=start_date, end_date=end_date)
            
            if df_raw.empty:
                raise ValueError(f"无法获取 {code} 的价格数据")
            
            # 转换为领域对象并缓存
            series = AdjustedPriceSeries.from_dataframe(
                asset_code=asset_code,
                df=df_raw.drop(columns=['source'], errors='ignore'),
                source=self._infer_source(df_raw),
            )
            
            # 验证数据
            errors = series.validate()
            if errors:
                logger.warning(f"{code} 数据验证警告: {errors}")
            
            # 保存到缓存
            self.repository.save(series)
            
            return series.to_dataframe()
            
        except Exception as e:
            logger.error(f"获取 {code} 价格数据失败: {e}")
            raise
    
    def calculate_returns(self,
                          code: str,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None) -> ReturnMetrics:
        """
        计算收益率指标
        
        Args:
            code: 6位资产代码
            start_date: 起始日期
            end_date: 结束日期
        
        Returns:
            ReturnMetrics 对象
        """
        asset_code = AssetCode.from_string(code)
        
        # 获取或加载价格序列
        series = self._load_or_fetch_series(asset_code, start_date, end_date)
        
        # 计算指标
        metrics = self.calculator.calculate_full_metrics(
            series,
            start_date=pd.to_datetime(start_date).date() if start_date else None,
            end_date=pd.to_datetime(end_date).date() if end_date else None,
        )
        
        return metrics
    
    def get_latest_price(self, code: str) -> Dict:
        """
        获取最新价格信息
        
        Returns:
            {
                'code': str,
                'date': date,
                'close': Decimal,
                'change_pct': Decimal,
                'source': str
            }
        """
        asset_code = AssetCode.from_string(code)
        series = self._load_or_fetch_series(asset_code)
        
        if not series.latest_price:
            raise ValueError(f"{code} 无价格数据")
        
        latest = series.latest_price
        prev_close = series.prices[-2].close if len(series.prices) >= 2 else latest.close
        
        change_pct = (latest.close - prev_close) / prev_close if prev_close > 0 else Decimal("0")
        
        return {
            'code': code,
            'date': latest.date,
            'close': latest.close,
            'change_pct': change_pct,
            'source': series.source.value,
        }
    
    def compare_assets(self,
                       codes: List[str],
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None,
                       normalize: bool = True) -> pd.DataFrame:
        """
        对比多个资产的价格走势
        
        Args:
            codes: 资产代码列表
            start_date: 起始日期
            end_date: 结束日期
            normalize: 是否归一化到起点为1
        
        Returns:
            DataFrame，每列是一个资产的收盘价
        """
        dfs = {}
        for code in codes:
            df = self.get_adjusted_prices(code, start_date, end_date)
            if not df.empty:
                dfs[code] = df['close']
        
        if not dfs:
            return pd.DataFrame()
        
        # 合并数据
        result = pd.DataFrame(dfs)
        
        # 归一化
        if normalize:
            for col in result.columns:
                first_valid = result[col].dropna().iloc[0]
                if first_valid > 0:
                    result[col] = result[col] / first_valid
        
        return result
    
    def get_technical_indicators(self,
                                  code: str,
                                  index: int = -1) -> Optional[Dict]:
        """
        获取技术指标
        
        Args:
            code: 资产代码
            index: 价格索引，-1表示最新
        
        Returns:
            技术指标字典，或None
        """
        asset_code = AssetCode.from_string(code)
        series = self._load_or_fetch_series(asset_code)
        
        indicators = self.calculator.calculate_technical_indicators(series, index)
        
        if indicators:
            return indicators.to_dict()
        return None
    
    def invalidate_cache(self, code: Optional[str] = None) -> None:
        """
        清除缓存
        
        Args:
            code: 指定资产代码，None则清除所有
        """
        if code:
            asset_code = AssetCode.from_string(code)
            self.repository.delete(asset_code)
            logger.info(f"已清除 {code} 的缓存")
        else:
            self.repository.clear()
            logger.info("已清除所有缓存")
    
    def _load_or_fetch_series(self,
                               asset_code: AssetCode,
                               start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> AdjustedPriceSeries:
        """加载或获取价格序列"""
        # 尝试缓存
        cached = self.repository.find_by_code(asset_code)
        if cached and cached.count > 0:
            return cached
        
        # 从数据源获取
        df_raw = fetch_adjusted_kline(
            str(asset_code),
            start_date=start_date,
            end_date=end_date,
        )
        
        if df_raw.empty:
            raise ValueError(f"无法获取 {asset_code} 的价格数据")
        
        series = AdjustedPriceSeries.from_dataframe(
            asset_code=asset_code,
            df=df_raw.drop(columns=['source'], errors='ignore'),
            source=self._infer_source(df_raw),
        )
        
        self.repository.save(series)
        return series
    
    @staticmethod
    def _infer_source(df: pd.DataFrame) -> DataSource:
        """从DataFrame推断数据来源"""
        if 'source' in df.columns:
            source_val = df['source'].iloc[0]
            if source_val == 'eastmoney':
                return DataSource.EASTMONEY_HFQ
            elif source_val == 'tencent':
                return DataSource.TENCENT_QFQ
            elif source_val == 'total_return':
                return DataSource.CALCULATED
        
        return DataSource.EASTMONEY_HFQ  # 默认
