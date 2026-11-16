"""
价格查询应用服务

编排领域对象完成用例:
- 缓存命中则只做增量同步（仅拉取缺失的尾部区间）
- 用领域服务 PriceCachePlanner 决定"全量 / 增量 / 不需要"
- 用领域服务 PriceSeriesMerger 做接缝校验后合并
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict
import logging

import pandas as pd

from src.domain.price.entities import AdjustedPriceSeries, AssetCode, DataSource
from src.domain.price.value_objects import CachePolicy, RefreshMode, ReturnMetrics, DateRange
from src.domain.price.services import (
    PriceCalculator,
    PriceCachePlanner,
    PriceSeriesMerger,
)
from src.domain.price.sources import (
    PriceAdjustment,
    PriceBasisAligner,
)
from src.infrastructure.price.repositories import PriceSeriesRepository, JsonFilePriceRepository
from src.infrastructure.price.fallback_provider import (
    FallbackPriceProvider,
    FetchedPrices,
)

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
                 calculator: Optional[PriceCalculator] = None,
                 cache_policy: Optional[CachePolicy] = None,
                 persist_cache: bool = True,
                 price_provider: Optional[FallbackPriceProvider] = None):
        """
        Args:
            repository: 价格仓储，默认使用 JSON 文件仓储（跨进程复用缓存）
            calculator: 价格计算器
            cache_policy: 缓存策略（重叠校验天数、接缝容差、最小刷新间隔）
            persist_cache: 是否使用持久化仓储；False 时退回纯内存缓存
        """
        if repository is not None:
            self.repository = repository
        elif persist_cache:
            self.repository = JsonFilePriceRepository()
        else:
            from src.infrastructure.price.repositories import InMemoryPriceRepository
            self.repository = InMemoryPriceRepository()

        self.calculator = calculator or PriceCalculator()
        self.cache_policy = cache_policy or CachePolicy()
        # 主备数据源（使用前探测、主源优先、失败降级）
        self.price_provider = price_provider or FallbackPriceProvider()
        # 缓存统一以"后复权"口径存储，其他口径的源需先对齐
        self.cache_adjustment = PriceAdjustment.HFQ
        # 每只资产上次访问数据源的时刻（用于最小刷新间隔）
        self._last_checked_at: Dict[str, datetime] = {}
    
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

        series = self._sync_series(
            asset_code,
            start_date=start_date,
            end_date=end_date,
            use_cache=use_cache,
        )

        return self._slice_frame(series, start_date, end_date)
    
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
        """加载或增量同步价格序列"""
        return self._sync_series(
            asset_code,
            start_date=start_date,
            end_date=end_date,
            use_cache=True,
        )

    # ------------------------------------------------------------------
    # 缓存与增量同步（应用层编排，决策在领域层）
    # ------------------------------------------------------------------

    def _sync_series(self,
                     asset_code: AssetCode,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None,
                     use_cache: bool = True) -> AdjustedPriceSeries:
        """
        同步价格序列：缓存命中时只拉增量

        流程:
        1. 读缓存
        2. PriceCachePlanner 给出刷新计划（NONE / INCREMENTAL / FULL）
        3. 需要时仅拉取计划区间
        4. PriceSeriesMerger 做接缝校验后合并（口径变化则全量重建）
        5. 回写缓存
        """
        key = str(asset_code)
        requested_start = self._parse_date(start_date)
        requested_end = self._parse_date(end_date)

        cached = self.repository.find_by_code(asset_code) if use_cache else None

        plan = PriceCachePlanner.plan(
            cached,
            requested_start=requested_start,
            requested_end=requested_end,
            policy=self.cache_policy,
            last_checked_at=self._last_checked_at.get(key),
            history_complete=getattr(self.repository, 'is_history_complete',
                                     lambda _code: False)(asset_code),
        )

        if not plan.needs_fetch:
            logger.debug(f"{key} 命中缓存（{plan.reason}）")
            return cached

        logger.info(f"{key} {plan.reason}")

        # ---- 拉取 ----------------
        # 全量重建时必须拉全历史（不能按请求区间拉，否则缓存只剩残缺窗口，
        # 后续其他调用会因缓存不完整而算错）；窄区间的检索由返回时切片完成。
        if plan.mode is RefreshMode.INCREMENTAL:
            fetch_start = plan.fetch_start.isoformat()
        else:
            fetch_start = None

        try:
            fetched: FetchedPrices = self.price_provider.fetch(
                key, start_date=fetch_start, end_date=end_date
            )
            df_raw = fetched.frame
        except Exception as e:
            if cached is not None and cached.count > 0:
                logger.warning(f"{key} 主备源均失败，沿用缓存: {e}")
                return cached
            logger.error(f"获取 {key} 价格数据失败: {e}")
            raise

        self._last_checked_at[key] = datetime.now()

        # ---- 数据源无新数据：保留缓存 ----
        if df_raw is None or df_raw.empty:
            if cached is not None and cached.count > 0:
                logger.info(f"{key} 数据源暂无新数据，沿用缓存")
                return cached
            raise ValueError(f"无法获取 {key} 的价格数据")

        update = AdjustedPriceSeries.from_dataframe(
            asset_code=asset_code,
            df=df_raw.drop(columns=['source'], errors='ignore'),
            source=self._infer_source(df_raw),
        )

        # ---- 口径对齐（不同源可能复权口径不同）----
        cached_basis = self._cached_adjustment(asset_code, cached)

        if cached is None or cached.count == 0:
            # 空缓存：直接采用当前源的口径，并记录下来
            if fetched.adjustment is not self.cache_adjustment:
                logger.warning(
                    f"{key} 缓存将以 {fetched.adjustment.label} 口径建立"
                    f"（源 {fetched.source_id}）——非规范口径"
                    f"（{self.cache_adjustment.label}），"
                    f"分红收益可能被低估；主源恢复后会自动重建"
                )
            self._remember_adjustment(asset_code, fetched.adjustment)
            cached_basis = fetched.adjustment

        elif fetched.adjustment != cached_basis:
            if self._is_primary_source(fetched.source_id):
                # 主源口径为规范口径：直接全量重建，不做近似换算
                logger.info(
                    f"{key} 用主源重建缓存：{cached_basis.label} -> "
                    f"{fetched.adjustment.label}"
                )
                self._remember_adjustment(asset_code, fetched.adjustment)
                self.repository.save(update)
                return update

            alignment = PriceBasisAligner.align(
                cached, update, cached_basis, fetched.adjustment
            )
            if alignment.incompatible:
                logger.warning(
                    f"{key} 数据源口径 {fetched.adjustment.label} 与缓存口径 "
                    f"{cached_basis.label} 本质不可换算（除权阶梯关系），"
                    f"本次不更新缓存，等待主源恢复"
                )
                return cached
            if not alignment.applied or alignment.factor <= 0:
                logger.warning(
                    f"{key} 数据源口径为 {fetched.adjustment.label}，"
                    f"与缓存口径 {cached_basis.label} 不同且无重叠可对齐，"
                    f"本次不更新缓存（等待主源恢复）"
                )
                return cached
            logger.info(
                f"{key} 数据源口径 {fetched.adjustment.label} -> "
                f"缓存口径 {cached_basis.label}，倍率 {float(alignment.factor):.6f}"
                f"（重叠 {alignment.overlaps} 点，"
                f"对齐后偏差 {float(alignment.max_deviation):.4%}）"
            )
            update = alignment.series

        # ---- 合并 ----
        if plan.mode is RefreshMode.INCREMENTAL and cached is not None:
            result = PriceSeriesMerger.merge(cached, update, self.cache_policy)
            if result.conflict:
                logger.warning(
                    f"{key} 检测到后复权口径变化"
                    f"（重叠{result.overlap_points}点，"
                    f"最大偏差{float(result.max_deviation):.4%}），改为全量重建"
                )
                series = self._fetch_full(asset_code, key, start_date, end_date)
                if series is None:
                    # 重建失败：保留原缓存，绝不用残缺窗口覆盖
                    logger.warning(f"{key} 全量重建失败，保留原缓存")
                    return cached
            else:
                series = result.series
        else:
            series = update

        errors = series.validate()
        if errors:
            logger.warning(f"{key} 数据验证警告: {errors[:3]}")

        # ---- 补齐当日行情（历史接口盘中通常不含当天）----
        series = self._fill_today_bar(asset_code, key, series, cached_basis)

        self.repository.save(series)

        # 全量拉取（无起始限制）得到的即为完整历史，打标记避免反复重拉
        if plan.mode is RefreshMode.FULL and fetch_start is None:
            mark = getattr(self.repository, 'mark_history_complete', None)
            if callable(mark):
                mark(asset_code)

        logger.info(
            f"{key} 缓存已更新，共 {series.count} 条"
            f"（{series.start_date} ~ {series.end_date}）"
        )

        return series

    def _fill_today_bar(self, asset_code: AssetCode, key: str,
                        series: AdjustedPriceSeries,
                        cache_basis: PriceAdjustment) -> AdjustedPriceSeries:
        """
        用实时报价补齐"当日"这根 K 线

        处理要点:
        - 实时报价是**不复权**口径，需换算到缓存口径
        - 换算倍率 = 缓存最后交易日收盘价 / 报价昨收
          （仅在"缓存末日 = 今日的前一交易日"时成立，跨除权日不保证）
        - 历史接口已包含当日时不重复追加
        """
        today = date.today()
        fetch_today = getattr(self.price_provider, 'fetch_today_bar', None)
        if not callable(fetch_today):
            return series

        if series.end_date is not None and series.end_date >= today:
            return series                      # 已有当日数据

        bar, prev_close = fetch_today(key)
        if bar is None or bar.empty:
            return series

        bar_date = bar.index[-1].date() if hasattr(bar.index[-1], 'date') else bar.index[-1]
        if bar_date != today:
            return series                      # 报价日期不是今天（非交易日/休市）

        # ---- 换算到缓存口径 ----
        close = Decimal(str(bar['close'].iloc[-1]))
        factor = Decimal('1')
        if cache_basis is not PriceAdjustment.NONE:
            last_close = series.latest_close if series.count else None
            gap_ok = (series.end_date is not None
                      and 0 < (today - series.end_date).days <= 5)
            if last_close is None or prev_close is None or prev_close <= 0 or not gap_ok:
                logger.info(
                    f'{key} 当日 bar 未补齐：无法确定口径倍率'
                    f'（缓存末日 {series.end_date}，报价昨收 {prev_close}）'
                )
                return series
            factor = last_close / prev_close
            close = close * factor

        # ---- 组装并追加 ----
        from src.domain.price.entities import PricePoint

        point = PricePoint(
            date=bar_date,
            open=Decimal(str(bar['open'].iloc[-1])) * factor,
            high=Decimal(str(bar['high'].iloc[-1])) * factor,
            low=Decimal(str(bar['low'].iloc[-1])) * factor,
            close=close,
            volume=int(bar['volume'].iloc[-1]),
            amount=Decimal(str(bar['amount'].iloc[-1])),
        )

        if series.end_date is not None and series.end_date == bar_date:
            logger.info(f'{key} 当日 bar 已存在，跳过补齐')
            return series

        merged = series.merged_with(
            AdjustedPriceSeries(
                asset_code=asset_code,
                asset_type=series.asset_type,
                prices=[point],
                source=series.source,
            )
        )
        logger.info(
            f'{key} 已用实时报价补齐当日 bar {bar_date}'
            f'（口径 {cache_basis.label}，倍率 {float(factor):.6f}）'
        )
        return merged

    def _fetch_full(self,
                    asset_code: AssetCode,
                    key: str,
                    start_date: Optional[str],
                    end_date: Optional[str]) -> Optional[AdjustedPriceSeries]:
        """全量重建（后复权口径变化时使用）：拉全历史，不带起始限制"""
        try:
            fetched = self.price_provider.fetch(key, start_date=None, end_date=end_date)
            df_raw = fetched.frame
            if df_raw is None or df_raw.empty:
                return None
            return AdjustedPriceSeries.from_dataframe(
                asset_code=asset_code,
                df=df_raw.drop(columns=['source'], errors='ignore'),
                source=self._infer_source(df_raw),
            )
        except Exception as e:
            logger.error(f"{key} 全量重建失败: {e}")
            return None

    def _cached_adjustment(self, asset_code: AssetCode,
                           cached) -> PriceAdjustment:
        """读取缓存口径（未记录时按后复权处理，与历史缓存一致）"""
        raw = None
        getter = getattr(self.repository, 'get_adjustment', None)
        if callable(getter):
            raw = getter(asset_code)
        try:
            return PriceAdjustment(raw) if raw else self.cache_adjustment
        except ValueError:
            return self.cache_adjustment

    def _remember_adjustment(self, asset_code: AssetCode,
                             adjustment: PriceAdjustment) -> None:
        """记录缓存口径"""
        setter = getattr(self.repository, 'set_adjustment', None)
        if callable(setter):
            setter(asset_code, adjustment.value)

    def _is_primary_source(self, source_id: str) -> bool:
        """是否为最高优先级（主）数据源"""
        provider = self.price_provider
        sources = getattr(provider, 'sources', None)
        if not sources:
            return False
        first = sources[0]
        return getattr(first, 'source_id', None) == source_id

    @staticmethod
    def _parse_date(value):
        """字符串日期 -> date"""
        if value is None:
            return None
        if isinstance(value, date):
            return value
        return pd.to_datetime(value).date()

    @staticmethod
    def _slice_frame(series: AdjustedPriceSeries,
                     start_date: Optional[str],
                     end_date: Optional[str]) -> pd.DataFrame:
        """按请求区间裁剪 DataFrame（供绘图/计算使用）"""
        df = series.to_dataframe()

        if df.empty or (not start_date and not end_date):
            return df

        index = pd.to_datetime(df.index)
        mask = pd.Series(True, index=df.index)
        if start_date:
            mask &= index >= pd.to_datetime(start_date)
        if end_date:
            mask &= index <= pd.to_datetime(end_date)

        return df[mask]
    
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
