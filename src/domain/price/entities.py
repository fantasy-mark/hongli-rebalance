"""
价格领域实体

核心聚合根: AdjustedPriceSeries
值对象: PricePoint
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Dict, Tuple
from enum import Enum

import pandas as pd


class AssetType(Enum):
    """资产类型"""
    ETF = "etf"
    STOCK = "stock"
    INDEX = "index"
    BOND = "bond"


class Exchange(Enum):
    """交易所"""
    SH = "sh"  # 上交所
    SZ = "sz"  # 深交所


class DataSource(Enum):
    """数据来源"""
    EASTMONEY_HFQ = "eastmoney_hfq"  # 东财后复权
    EASTMONEY_UNADJ = "eastmoney_unadj"  # 东财不复权
    TENCENT_QFQ = "tencent_qfq"  # 腾讯前复权
    CALCULATED = "calculated"  # 自算总收益


@dataclass(frozen=True)
class AssetCode:
    """
    资产代码值对象
    
    不变量:
    - code 必须是6位数字
    - exchange 必须有效
    """
    code: str
    exchange: Exchange
    
    def __post_init__(self):
        if not self.code.isdigit() or len(self.code) != 6:
            raise ValueError(f"无效的资产代码: {self.code}，必须是6位数字")
    
    @classmethod
    def from_string(cls, code: str) -> 'AssetCode':
        """从6位代码字符串创建"""
        code = code.strip()
        if code.startswith(("5", "6", "9")):
            exchange = Exchange.SH
        else:
            exchange = Exchange.SZ
        return cls(code=code, exchange=exchange)
    
    def is_etf(self) -> bool:
        """判断是否为ETF"""
        return self.code.startswith(("5", "1"))
    
    def to_sina_symbol(self) -> str:
        """转换为新浪格式符号"""
        return f"{self.exchange.value}{self.code}"
    
    def __str__(self) -> str:
        return self.code
    
    def __hash__(self):
        return hash((self.code, self.exchange))


@dataclass(frozen=True)
class PricePoint:
    """
    单个交易日的价格数据（值对象）
    
    不变量:
    - high >= open, high >= close
    - low <= open, low <= close
    - high >= low
    - close > 0
    - volume >= 0
    """
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal = Decimal("0")
    
    def __post_init__(self):
        # 验证价格合理性
        if self.close <= 0:
            raise ValueError(f"收盘价必须大于0: {self.close}")
        if self.high < self.low:
            raise ValueError(f"最高价({self.high})不能低于最低价({self.low})")
        if self.high < self.open or self.high < self.close:
            raise ValueError(f"最高价({self.high})不能低于开盘价或收盘价")
        if self.low > self.open or self.low > self.close:
            raise ValueError(f"最低价({self.low})不能高于开盘价或收盘价")
        if self.volume < 0:
            raise ValueError(f"成交量不能为负: {self.volume}")
    
    def daily_return(self, previous_close: Decimal) -> Decimal:
        """计算日收益率"""
        if previous_close <= 0:
            return Decimal("0")
        return (self.close - previous_close) / previous_close
    
    def price_range(self) -> Decimal:
        """计算当日价格区间"""
        return self.high - self.low
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'date': self.date,
            'open': float(self.open),
            'high': float(self.high),
            'low': float(self.low),
            'close': float(self.close),
            'volume': self.volume,
            'amount': float(self.amount),
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'PricePoint':
        """从字典创建"""
        return cls(
            date=data['date'] if isinstance(data['date'], date) else pd.to_datetime(data['date']).date(),
            open=Decimal(str(data['open'])),
            high=Decimal(str(data['high'])),
            low=Decimal(str(data['low'])),
            close=Decimal(str(data['close'])),
            volume=int(data['volume']),
            amount=Decimal(str(data.get('amount', 0))),
        )


@dataclass
class AdjustedPriceSeries:
    """
    后复权价格序列（聚合根）
    
    职责:
    - 维护一个资产的完整价格历史
    - 提供价格查询和收益率计算入口
    - 保证数据一致性和完整性
    """
    asset_code: AssetCode
    asset_type: AssetType
    prices: List[PricePoint] = field(default_factory=list)
    source: DataSource = DataSource.EASTMONEY_HFQ
    calculated_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        # 确保价格按日期排序
        if self.prices:
            self.prices.sort(key=lambda p: p.date)
    
    @property
    def start_date(self) -> Optional[date]:
        """起始日期"""
        return self.prices[0].date if self.prices else None
    
    @property
    def end_date(self) -> Optional[date]:
        """结束日期"""
        return self.prices[-1].date if self.prices else None
    
    @property
    def latest_price(self) -> Optional[PricePoint]:
        """最新价格点"""
        return self.prices[-1] if self.prices else None
    
    @property
    def latest_close(self) -> Optional[Decimal]:
        """最新收盘价"""
        return self.latest_price.close if self.latest_price else None
    
    @property
    def count(self) -> int:
        """价格点数量"""
        return len(self.prices)
    
    def get_price(self, target_date: date) -> Optional[PricePoint]:
        """获取指定日期的价格"""
        for price in self.prices:
            if price.date == target_date:
                return price
        return None
    
    def get_prices_in_range(self, start: date, end: date) -> List[PricePoint]:
        """获取日期范围内的价格"""
        return [p for p in self.prices if start <= p.date <= end]
    
    def add_price(self, price: PricePoint) -> None:
        """添加新的价格点（保持排序）"""
        # 检查是否已存在
        if self.get_price(price.date):
            raise ValueError(f"日期 {price.date} 的价格已存在")
        
        self.prices.append(price)
        self.prices.sort(key=lambda p: p.date)
        self.calculated_at = datetime.now()

    # ------------------------------------------------------------------
    # 缓存与增量同步（聚合根行为）
    # ------------------------------------------------------------------

    def covers(self, start: Optional[date] = None,
               end: Optional[date] = None) -> bool:
        """
        判断当前序列是否已覆盖指定日期区间

        Args:
            start: 起始日期，None 表示不限制
            end: 结束日期，None 表示不限制
        """
        if not self.prices:
            return False

        if start is not None and (self.start_date is None or self.start_date > start):
            return False

        if end is not None and (self.end_date is None or self.end_date < end):
            return False

        return True

    def merged_with(self,
                    update: 'AdjustedPriceSeries',
                    prefer_update: bool = True) -> 'AdjustedPriceSeries':
        """
        合并另一份价格序列（增量更新）

        规则:
        - 以日期为唯一键，同一天以 update 为准（数据源可能修正旧值）
        - 结果按日期升序，保持聚合根不变量
        - 返回新实例，不修改自身（保持聚合根封装）

        Args:
            update: 增量价格序列
            prefer_update: 重叠日期是否以 update 为准

        Returns:
            合并后的新序列
        """
        if str(update.asset_code) != str(self.asset_code):
            raise ValueError(
                f"资产不一致，无法合并: {self.asset_code} vs {update.asset_code}"
            )

        by_date = {p.date: p for p in self.prices}

        for point in update.prices:
            if point.date in by_date and not prefer_update:
                continue
            by_date[point.date] = point

        merged_prices = [by_date[d] for d in sorted(by_date)]

        return AdjustedPriceSeries(
            asset_code=self.asset_code,
            asset_type=self.asset_type,
            prices=merged_prices,
            source=update.source if update.prices else self.source,
            calculated_at=datetime.now(),
        )

    def overlap_close_deviation(
            self, other: 'AdjustedPriceSeries') -> Tuple[int, Optional[Decimal]]:
        """
        计算与另一份序列在重叠日期上的收盘价最大相对偏差

        用途: 检测后复权口径是否因分红除权而重算
        （若口径变了，重叠区的历史价格会整体不同，不能直接拼接）

        Returns:
            (重叠点数, 最大相对偏差)，无重叠时偏差为 None
        """
        other_by_date = {p.date: p for p in other.prices}

        overlap = 0
        max_dev: Optional[Decimal] = None

        for point in self.prices:
            counterpart = other_by_date.get(point.date)
            if counterpart is None or point.close <= 0:
                continue

            overlap += 1
            deviation = abs(counterpart.close - point.close) / point.close
            if max_dev is None or deviation > max_dev:
                max_dev = deviation

        return overlap, max_dev
    
    def update_source(self, new_source: DataSource) -> None:
        """更新数据来源"""
        self.source = new_source
        self.calculated_at = datetime.now()
    
    def to_dataframe(self) -> pd.DataFrame:
        """转换为DataFrame"""
        if not self.prices:
            return pd.DataFrame()
        
        records = [p.to_dict() for p in self.prices]
        df = pd.DataFrame(records)
        df.set_index('date', inplace=True)
        return df
    
    @classmethod
    def from_dataframe(cls, 
                       asset_code: AssetCode,
                       df: pd.DataFrame,
                       source: DataSource = DataSource.EASTMONEY_HFQ) -> 'AdjustedPriceSeries':
        """从DataFrame创建"""
        if df.empty:
            return cls(asset_code=asset_code, asset_type=cls._infer_asset_type(asset_code), source=source)
        
        prices = []
        for idx, row in df.iterrows():
            try:
                price = PricePoint(
                    date=idx.date() if hasattr(idx, 'date') else pd.to_datetime(idx).date(),
                    open=Decimal(str(row['open'])),
                    high=Decimal(str(row['high'])),
                    low=Decimal(str(row['low'])),
                    close=Decimal(str(row['close'])),
                    volume=int(row['volume']),
                    amount=Decimal(str(row.get('amount', 0))),
                )
                prices.append(price)
            except (ValueError, KeyError) as e:
                # 跳过无效行
                continue
        
        asset_type = cls._infer_asset_type(asset_code)
        return cls(
            asset_code=asset_code,
            asset_type=asset_type,
            prices=prices,
            source=source,
        )
    
    @staticmethod
    def _infer_asset_type(asset_code: AssetCode) -> AssetType:
        """推断资产类型"""
        if asset_code.is_etf():
            return AssetType.ETF
        elif asset_code.code.startswith(("0", "3", "6")):
            return AssetType.STOCK
        else:
            return AssetType.ETF  # 默认
    
    def validate(self) -> List[str]:
        """
        验证数据完整性
        
        Returns:
            错误消息列表，空列表表示验证通过
        """
        errors = []
        
        if not self.prices:
            errors.append("价格序列为空")
            return errors
        
        # 检查连续性（允许最多5天缺口）
        for i in range(1, len(self.prices)):
            prev_date = self.prices[i-1].date
            curr_date = self.prices[i].date
            gap = (curr_date - prev_date).days
            
            # 考虑周末，正常应该是1-3天
            if gap > 5:
                errors.append(f"日期不连续: {prev_date} 到 {curr_date} 间隔 {gap} 天")
        
        # 检查异常涨跌幅
        for i in range(1, len(self.prices)):
            prev_close = self.prices[i-1].close
            curr_close = self.prices[i].close
            if prev_close > 0:
                daily_return = abs((curr_close - prev_close) / prev_close)
                if daily_return > Decimal("0.5"):  # 50%
                    errors.append(
                        f"异常涨跌幅: {self.prices[i].date} "
                        f"收益率 {daily_return:.2%}"
                    )
        
        return errors
    
    def __len__(self) -> int:
        return len(self.prices)
    
    def __getitem__(self, index: int) -> PricePoint:
        return self.prices[index]
    
    def __repr__(self) -> str:
        return (
            f"AdjustedPriceSeries(code={self.asset_code}, "
            f"type={self.asset_type.value}, "
            f"points={len(self.prices)}, "
            f"source={self.source.value})"
        )
