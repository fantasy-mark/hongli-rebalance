"""
价格数据源适配器

把不同数据源（东财 / 腾讯 / 新浪）的原始响应适配为**统一格式**：

    索引: date
    列:   open / high / low / close / volume / amount
    volume 单位: 手（与主源东财一致）
    amount 单位: 元（缺失时记 0）

各源差异（实测）:
┌──────────┬──────────┬────────────────────┬────────┬──────────┐
│ 源       │ 复权口径 │ 原始字段顺序        │ 量单位 │ 历史深度 │
├──────────┼──────────┼────────────────────┼────────┼──────────┤
│ 东财     │ hfq      │ 日期/开/高/低/收/量 │ 手     │ 全历史   │
│ 腾讯     │ hfq      │ 日期/开/收/高/低/量 │ 手     │ ~640 根  │
│ 新浪     │ 不复权   │ 日期/开/高/低/收/量 │ 股     │ 全历史   │
└──────────┴──────────┴────────────────────┴────────┴──────────┘
"""

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import List, Optional, Protocol, Tuple

import logging

import pandas as pd

from src.domain.price.sources import PriceAdjustment

logger = logging.getLogger(__name__)

# 统一后的列顺序
UNIFIED_COLUMNS = ['open', 'high', 'low', 'close', 'volume', 'amount']


class PriceSource(Protocol):
    """
    价格数据源端口（Protocol）

    实现方约定:
    - `fetch` 返回统一格式 DataFrame（索引 date，列见 UNIFIED_COLUMNS）
    - `volume` 统一为「手」，`amount` 统一为「元」（缺失记 0）
    - `adjustment` 声明该源的价格口径，供领域层决定是否需要对齐
    """

    @property
    def source_id(self) -> str:
        ...

    @property
    def adjustment(self) -> PriceAdjustment:
        ...

    @property
    def provides_full_history(self) -> bool:
        ...

    @property
    def max_bars(self) -> Optional[int]:
        ...

    def fetch(self,
              code: str,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> pd.DataFrame:
        ...


def _finalize(records: List[dict], source: str) -> pd.DataFrame:
    """把记录列表整理成统一格式 DataFrame"""
    if not records:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)

    df = pd.DataFrame(records)
    df['date'] = pd.to_datetime(df['date'])
    df = df.set_index('date').sort_index()

    for column in UNIFIED_COLUMNS:
        if column not in df.columns:
            df[column] = 0.0
        df[column] = pd.to_numeric(df[column], errors='coerce')

    df['source'] = source
    return df[UNIFIED_COLUMNS + ['source']]


class EastmoneyPriceSource:
    """
    东方财富（主源）

    - 后复权(hfq)
    - 字段顺序与我们的统一格式一致，但成交量单位为「手」
    - 有 IP 限流风险
    """

    source_id = 'eastmoney'
    adjustment = PriceAdjustment.HFQ
    provides_full_history = True
    max_bars = None

    def fetch(self, code: str,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> pd.DataFrame:
        import akshare as ak

        kwargs = {'symbol': code, 'period': 'daily', 'adjust': 'hfq'}
        if start_date:
            kwargs['start_date'] = _to_ak_date(start_date)
        if end_date:
            kwargs['end_date'] = _to_ak_date(end_date)

        raw = ak.fund_etf_hist_em(**kwargs)
        if raw is None or raw.empty:
            raise ValueError('东财返回空数据')

        raw = raw.rename(columns={
            '日期': 'date', '开盘': 'open', '最高': 'high', '最低': 'low',
            '收盘': 'close', '成交量': 'volume', '成交额': 'amount',
        })
        return _finalize(raw.to_dict('records'), self.source_id)


class TencentPriceSource:
    """
    腾讯（备用源）

    - 后复权(hfq) —— 与主源同口径，可直接拼接
    - 字段顺序为 日期/开/**收**/高/低/量（容易踩坑）
    - 成交量单位为「手」，与主源一致
    - 不返回成交额
    - 历史深度约 640 根（约 2.5 年）
    """

    source_id = 'tencent'
    adjustment = PriceAdjustment.HFQ
    provides_full_history = False      # 最多约 640 根
    max_bars = 640

    BASE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
    MAX_BARS = 640

    def fetch(self, code: str,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> pd.DataFrame:
        import requests

        symbol = _to_sina_symbol(code)
        params = {'param': f'{symbol},day,,,{self.MAX_BARS},hfq'}
        response = requests.get(self.BASE_URL, params=params, timeout=15)
        response.raise_for_status()

        payload = response.json().get('data', {}).get(symbol, {})
        # 注意：键是 hfqday / qfqday / day，不是复权字符串本身
        rows = payload.get('hfqday') or payload.get('day') or []
        if not rows:
            raise ValueError(f'腾讯返回 {symbol} 的 hfq 数据为空')

        records = []
        for item in rows:
            if len(item) < 6:
                continue
            records.append({
                'date': item[0],
                'open': float(item[1]),
                'close': float(item[2]),     # 腾讯顺序: 开、收、高、低
                'high': float(item[3]),
                'low': float(item[4]),
                'volume': float(item[5]),
                'amount': 0.0,               # 腾讯不返回成交额
            })

        df = _finalize(records, self.source_id)

        # 按请求区间裁剪（腾讯只支持 bar 数，不支持起止日期）
        if start_date:
            df = df[df.index >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df.index <= pd.to_datetime(end_date)]

        return df


class SinaPriceSource:
    """
    新浪（第三备用源）

    - 不复权口径，与后复权相差常数倍率，需由领域层
      `PriceBasisAligner` 对齐后才能合并
    - 成交量单位为「股」，需转换为「手」
    """

    source_id = 'sina'
    adjustment = PriceAdjustment.NONE
    provides_full_history = True
    max_bars = None

    def fetch(self, code: str,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> pd.DataFrame:
        import akshare as ak

        raw = ak.fund_etf_hist_sina(symbol=_to_sina_symbol(code))
        if raw is None or raw.empty:
            raise ValueError('新浪返回空数据')

        # 新浪 volume 单位为股 -> 统一为手
        raw = raw.copy()
        raw['volume'] = pd.to_numeric(raw['volume'], errors='coerce') / 100.0

        df = _finalize(raw.to_dict('records'), self.source_id)

        if start_date:
            df = df[df.index >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df.index <= pd.to_datetime(end_date)]

        return df


class RealtimeQuoteSource:
    """
    实时报价源（仅用于补齐"当日"这根 K 线）

    背景:
    历史 K 线接口（东财/腾讯/新浪）在盘中往往**不包含当天**这条记录，
    导致缓存永远落后一个交易日。因此用实时报价把当天补上。

    口径:
    实时报价是**不复权**的成交价，与后复权缓存相差一个局部倍率，
    由调用方（领域层对齐器）用"缓存最后交易日收盘价 / 报价昨收"换算。
    该倍率仅在相邻交易日内有效（跨除权日会变化）。

    能力:
    provides_full_history = False, max_bars = 1 —— 只提供一根当日 bar
    """

    source_id = 'realtime'
    adjustment = PriceAdjustment.NONE
    provides_full_history = False
    max_bars = 1

    SINA_URL = 'https://hq.sinajs.cn/list={symbol}'
    HEADERS = {'Referer': 'https://finance.sina.com.cn'}

    def fetch(self, code: str,
              start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> pd.DataFrame:
        """
        返回只含"当日"一根 bar 的 DataFrame

        实时接口没有历史，start/end 仅用于过滤（通常传当天）。
        """
        import requests

        symbol = _to_sina_symbol(code)
        response = requests.get(self.SINA_URL.format(symbol=symbol),
                                headers=self.HEADERS, timeout=10)
        response.raise_for_status()
        response.encoding = 'gbk'

        text = response.text
        if '="' not in text:
            raise ValueError(f'实时接口返回格式异常: {text[:60]}')

        payload = text.split('="', 1)[1].strip().rstrip(';').rstrip('"')
        fields = payload.split(',')
        if len(fields) < 32:
            raise ValueError(f'实时接口字段不足: {len(fields)}')

        name = fields[0]
        open_ = float(fields[1])
        prev_close = float(fields[2])
        current = float(fields[3])
        high = float(fields[4])
        low = float(fields[5])
        volume_shares = float(fields[8])       # 股
        amount = float(fields[9])              # 元
        day = fields[30]                       # YYYY-MM-DD
        clock = fields[31]                     # HH:MM:SS

        if current <= 0 or open_ <= 0:
            raise ValueError(f'{code} 实时报价无效（可能停牌或未开盘）')

        # 未开盘时 最高/最低 可能为 0，退化为开盘价
        high = high if high > 0 else max(open_, current)
        low = low if low > 0 else min(open_, current)

        records = [{
            'date': day,
            'open': open_,
            'high': high,
            'low': low,
            'close': current,
            'volume': volume_shares / 100.0,   # 股 -> 手
            'amount': amount,
        }]

        df = _finalize(records, self.source_id)
        df.attrs['quote_time'] = f'{day} {clock}'
        df.attrs['prev_close'] = prev_close
        df.attrs['name'] = name

        if start_date:
            df = df[df.index >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df.index <= pd.to_datetime(end_date)]

        return df


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------

def _to_ak_date(value: str) -> str:
    """'YYYY-MM-DD' -> 'YYYYMMDD'"""
    return pd.to_datetime(value).strftime('%Y%m%d')


def _to_sina_symbol(code: str) -> str:
    """6 位代码 -> 带市场前缀（sh/sz）"""
    return f"sh{code}" if code.startswith(('5', '6', '9')) else f"sz{code}"


def default_sources() -> List[PriceSource]:
    """
    默认数据源链（按优先级）

    东财（主，全历史）-> 腾讯（备，同口径，~640 根）-> 新浪（兜底，需对齐口径）
    """
    return [EastmoneyPriceSource(), TencentPriceSource(), SinaPriceSource()]
