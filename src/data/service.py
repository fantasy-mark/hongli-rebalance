"""
价格查询服务（数据模块）

编排缓存与数据源完成用例:
- 缓存命中则只做增量同步（仅拉取缺失的尾部区间）
- 用 PriceCachePlanner 决定"全量 / 增量 / 不需要"
- 用 PriceSeriesMerger 做接缝校验后合并
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional, List, Dict, Tuple
import logging

import pandas as pd

from src.data.models import AdjustedPriceSeries, AssetCode, DataSource
from src.data.value_objects import ReturnMetrics
from src.data.cache import (
    CachePolicy,
    PriceCachePlanner,
    PriceSeriesMerger,
    RefreshMode,
)
from src.data.indicators import PriceCalculator
from src.data.sources import (
    PriceAdjustment,
    PriceBasisAligner,
)
from src.data.cache_store import PriceSeriesRepository, JsonFilePriceRepository
from src.data.fallback import (
    FallbackPriceProvider,
    FetchedPrices,
)

logger = logging.getLogger(__name__)


class _SourceConflict(Exception):
    """
    数据源与缓存不兼容（内部信号）

    含义：本次取数无法安全地并入缓存（口径冲突、数据异常、长度倒退等），
    调用方应**换下一个数据源**重试，而不是放弃更新。
    """


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
            from src.data.cache_store import InMemoryPriceRepository
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

        cached_basis = self._cached_adjustment(asset_code, cached)

        # ---- 逐源尝试 ----------------
        # 某个源的数据与缓存冲突（如腾讯对个别 ETF 的后复权价存在缩放错误）时，
        # 换下一个源重试，而不是"冲突就放弃"——否则最新价会被卡在旧日期。
        series = None
        failures: List[str] = []
        raw_factor: Optional[Decimal] = None
        try:
            for fetched in self._iter_provider_fetches(
                key, fetch_start, end_date
            ):
                self._last_checked_at[key] = datetime.now()
                try:
                    series, raw_factor = self._merge_from_source(
                        asset_code, key, fetched, cached, plan,
                        start_date, end_date, cached_basis,
                    )
                except _SourceConflict as conflict:
                    logger.warning(
                        f"{key} {fetched.source_id} 数据不可用（{conflict}），换下一个源"
                    )
                    failures.append(f"{fetched.source_id}: {conflict}")
                    continue
                except Exception as error:      # noqa: BLE001
                    logger.warning(f"{key} {fetched.source_id} 同步失败: {error}")
                    failures.append(f"{fetched.source_id}: {error}")
                    continue
                break
        except Exception as error:      # noqa: BLE001
            # 所有候选源都取数失败（网络/限流/全不可用）
            failures.append(str(error))

        if series is None:
            # ---- 兜底：全量重建 ----
            # 所有源都与缓存冲突（口径真的变了）时，尝试全量重建一次；
            # 重建结果不得比原缓存短，否则保留原缓存（宁可旧也不能缺历史）
            if (cached is not None and cached.count > 0
                    and plan.mode is RefreshMode.INCREMENTAL):
                logger.warning(
                    f"{key} 各数据源均与缓存冲突，尝试全量重建"
                    + (f"（{'; '.join(failures)}）" if failures else "")
                )
                rebuilt = self._fetch_full(
                    asset_code, key, start_date, end_date,
                    preferred_adjustment=cached_basis,
                )
                if rebuilt is not None and rebuilt.count >= cached.count:
                    # 非权威源重建时，结果必须与原缓存口径一致（重叠区偏差在容差内），
                    # 否则价格会整体偏移，市值/收益跟着错
                    overlaps, deviation = cached.overlap_close_deviation(rebuilt)
                    tolerance = self.cache_policy.seam_tolerance
                    if (not self._is_authoritative_rebuild()
                            and deviation is not None
                            and deviation > tolerance
                            and overlaps > 0):
                        logger.warning(
                            f"{key} 全量重建结果与原缓存口径不一致"
                            f"（重叠{overlaps}点，最大偏差{float(deviation):.4%}），"
                            f"保留原缓存"
                        )
                    else:
                        series = rebuilt
                        self._remember_adjustment(asset_code, cached_basis)
                else:
                    logger.warning(
                        f"{key} 全量重建未获得更完整的数据，保留原缓存"
                        f"（原 {cached.count} 条）"
                    )

        if series is None:
            if cached is not None and cached.count > 0:
                logger.warning(
                    f"{key} 所有数据源均无法更新缓存，沿用缓存"
                    + (f"（{'; '.join(failures)}）" if failures else "")
                )
                # 沿用缓存，但仍尝试用实时报价补齐最近交易日
                # （历史接口不可用时，报价往往是唯一能拿到最新价的地方）
                series = cached
            else:
                raise ValueError(
                    f"无法获取 {key} 的价格数据"
                    + (f"（{'; '.join(failures)}）" if failures else "")
                )

        errors = series.validate()
        if errors:
            logger.warning(f"{key} 数据验证警告: {errors[:3]}")

        # ---- 补齐最新交易日（历史接口常常不含当天/最近交易日）----
        series = self._fill_latest_bar(
            asset_code, key, series, cached_basis, raw_factor)

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

    def _iter_provider_fetches(self, key: str, fetch_start: Optional[str],
                                end_date: Optional[str]):
        """
        依次产出各候选源的取数结果

        优先用 provider 的 `iter_fetches`（可逐源重试）；
        provider 只实现 `fetch` 时降级为单次取数。
        """
        iter_fetches = getattr(self.price_provider, 'iter_fetches', None)
        if callable(iter_fetches):
            yield from iter_fetches(key, start_date=fetch_start,
                                    end_date=end_date)
            return
        yield self.price_provider.fetch(key, start_date=fetch_start,
                                        end_date=end_date)

    # ------------------------------------------------------------------
    # 单个数据源的合并（供多源回退调用）
    # ------------------------------------------------------------------

    def _merge_from_source(self,
                            asset_code: AssetCode,
                            key: str,
                            fetched: FetchedPrices,
                            cached,
                            plan,
                            start_date: Optional[str],
                            end_date: Optional[str],
                            cached_basis: PriceAdjustment
                            ) -> Tuple[AdjustedPriceSeries, Optional[Decimal]]:
        """
        把某个源的取数结果并入缓存

        Returns:
            (合并后的序列, 已知的后复权/不复权倍率)
            倍率来自"不复权源对齐到后复权缓存"的实测值，用于校验实时报价

        Raises:
            _SourceConflict: 该源的数据无法安全并入缓存（调用方应换源重试）
        """
        df_raw = fetched.frame
        if df_raw is None or df_raw.empty:
            raise _SourceConflict("数据源返回空数据")

        update = AdjustedPriceSeries.from_dataframe(
            asset_code=asset_code,
            df=df_raw.drop(columns=['source'], errors='ignore'),
            source=self._infer_source(df_raw),
        )

        # ---- 空缓存：直接采用当前源的口径 ----
        raw_factor: Optional[Decimal] = None

        if cached is None or cached.count == 0:
            if fetched.adjustment is not self.cache_adjustment:
                logger.warning(
                    f"{key} 缓存将以 {fetched.adjustment.label} 口径建立"
                    f"（源 {fetched.source_id}）——非规范口径"
                    f"（{self.cache_adjustment.label}），"
                    f"分红收益可能被低估；主源恢复后会自动重建"
                )
            self._remember_adjustment(asset_code, fetched.adjustment)
            return update, None

        # ---- 口径对齐（不同源可能复权口径不同）----
        if fetched.adjustment != cached_basis:
            if self._is_primary_source(fetched.source_id):
                # 主源口径为规范口径：直接全量重建，不做近似换算
                logger.info(
                    f"{key} 用主源重建缓存：{cached_basis.label} -> "
                    f"{fetched.adjustment.label}"
                )
                rebuilt = self._fetch_full(
                    asset_code, key, start_date, end_date,
                    preferred_adjustment=fetched.adjustment,
                )
                if rebuilt is None:
                    raise _SourceConflict("主源全量重建失败")
                if rebuilt.count < cached.count:
                    raise _SourceConflict(
                        f"全量重建结果({rebuilt.count}条)少于原缓存({cached.count}条)"
                    )
                self._remember_adjustment(asset_code, fetched.adjustment)
                return rebuilt, None

            alignment = PriceBasisAligner.align(
                cached, update, cached_basis, fetched.adjustment,
                allow_unadjusted=True,
            )
            if alignment.incompatible:
                raise _SourceConflict(
                    f"口径 {fetched.adjustment.label} 与缓存 {cached_basis.label} "
                    f"不可换算"
                )
            if not alignment.applied or alignment.factor <= 0:
                raise _SourceConflict(
                    f"口径 {fetched.adjustment.label} 无重叠可对齐"
                )
            logger.info(
                f"{key} 数据源口径 {fetched.adjustment.label} -> "
                f"缓存口径 {cached_basis.label}，倍率 {float(alignment.factor):.6f}"
                f"（重叠 {alignment.overlaps} 点，"
                f"对齐后偏差 {float(alignment.max_deviation):.4%}）"
            )
            update = alignment.series
            # 不复权源对齐到后复权缓存时，实测倍率就是"后复权/不复权"的换算系数，
            # 后面用它校验实时报价能否安全补齐
            raw_factor = alignment.factor

        # ---- 合并 ----
        if plan.mode is RefreshMode.INCREMENTAL:
            result = PriceSeriesMerger.merge(cached, update, self.cache_policy)
            if result.conflict:
                raise _SourceConflict(
                    f"口径变化（重叠{result.overlap_points}点，"
                    f"最大偏差{float(result.max_deviation):.4%}）"
                )
            series = result.series
        else:
            series = update

        # ---- 安全网：合并结果不得比原缓存更短（防止历史被静默截断）----
        if series.count < cached.count:
            raise _SourceConflict(
                f"合并结果({series.count}条)少于原缓存({cached.count}条)"
            )

        return series, raw_factor

    def _fill_latest_bar(self, asset_code: AssetCode, key: str,
                        series: AdjustedPriceSeries,
                        cache_basis: PriceAdjustment,
                        raw_factor: Optional[Decimal] = None) -> AdjustedPriceSeries:
        """
        用实时报价补齐"最近交易日"这根 K 线

        背景:
        历史 K 线接口（东财/腾讯/新浪）在当天收盘后往往还没有当天那根 bar，
        新浪的 ETF 接口更是常滞后一个交易日。结果是缓存落后于市场，
        页面上的"最新价"就成了旧日期的价——后面的市值、收益全错。

        处理要点:
        - 实时报价是**不复权**口径，需换算到缓存口径
        - 换算倍率 = 缓存最后交易日收盘价 / 报价昨收
          （仅在"缓存末日 = 报价日的前一交易日"时成立，跨除权日不保证）
        - 报价自带日期（休市时是上一个交易日），不必强行等于"今天"
        - 历史接口已包含该交易日时不重复追加
        """
        fetch_today = getattr(self.price_provider, 'fetch_today_bar', None)
        if not callable(fetch_today):
            return series

        bar, prev_close = fetch_today(key)
        if bar is None or bar.empty:
            return series

        bar_date = bar.index[-1].date() if hasattr(bar.index[-1], 'date') else bar.index[-1]

        # 缓存已覆盖报价日（或更新）时无需补齐
        if series.end_date is not None and series.end_date >= bar_date:
            return series

        # ---- 换算到缓存口径 ----
        close = Decimal(str(bar['close'].iloc[-1]))
        factor = Decimal('1')
        if cache_basis is not PriceAdjustment.NONE:
            last_close = series.latest_close if series.count else None
            if last_close is None or prev_close is None or prev_close <= 0:
                logger.info(
                    f'{key} 最近交易日 bar 未补齐：无法确定口径倍率'
                    f'（缓存末日 {series.end_date}，报价昨收 {prev_close}）'
                )
                return series
            factor = last_close / prev_close
            # 缓存末日必须是报价日的前一交易日，否则"昨收"对不上，
            # 算出的倍率会把价格写错（宁可不补，也不能写错）。
            # 校验方式：用已知的"后复权/不复权"倍率反推核对。
            if raw_factor is not None:
                if abs(factor / raw_factor - Decimal('1')) > Decimal('0.005'):
                    logger.info(
                        f'{key} 最近交易日 bar 未补齐：缓存末日 {series.end_date} '
                        f'与报价日 {bar_date} 不相邻（倍率 {float(factor):.6f} '
                        f'≠ 已知倍率 {float(raw_factor):.6f}），放弃补齐'
                    )
                    return series
                factor = raw_factor
            else:
                # 没有已知倍率可核对：退而求其次，要求缓存末日与报价日
                # 足够接近，且倍率在合理范围内（排除 0.1 / 10 倍这类量级错误）
                if not self._is_adjacent_trading_day(series.end_date, bar_date):
                    logger.info(
                        f'{key} 最近交易日 bar 未补齐：缓存末日 {series.end_date} '
                        f'与报价日 {bar_date} 之间不连续，倍率不可信'
                    )
                    return series
                if not (Decimal('0.5') <= factor <= Decimal('2.0')):
                    logger.info(
                        f'{key} 最近交易日 bar 未补齐：口径倍率 {float(factor):.6f} '
                        f'不合理，放弃补齐'
                    )
                    return series
            close = close * factor

        # ---- 组装并追加 ----
        from src.data.models import PricePoint

        point = PricePoint(
            date=bar_date,
            open=Decimal(str(bar['open'].iloc[-1])) * factor,
            high=Decimal(str(bar['high'].iloc[-1])) * factor,
            low=Decimal(str(bar['low'].iloc[-1])) * factor,
            close=close,
            volume=int(bar['volume'].iloc[-1]),
            amount=Decimal(str(bar['amount'].iloc[-1])),
        )

        merged = series.merged_with(
            AdjustedPriceSeries(
                asset_code=asset_code,
                asset_type=series.asset_type,
                prices=[point],
                source=series.source,
            )
        )
        logger.info(
            f'{key} 已用实时报价补齐最近交易日 bar {bar_date}'
            f'（口径 {cache_basis.label}，倍率 {float(factor):.6f}）'
        )
        return merged

    @staticmethod
    def _is_adjacent_trading_day(last_cached: Optional[date],
                                  quote_date: date) -> bool:
        """
        缓存末日是否为报价日的前一个交易日

        判定：日历日差不超过 4 天（跨一个周末），
        且不为同一天。超出则无法保证"报价昨收"就是缓存末日那个交易日的收盘价。
        """
        if last_cached is None:
            return False
        gap = (quote_date - last_cached).days
        return 0 < gap <= 4

    def _fetch_full(self,
                    asset_code: AssetCode,
                    key: str,
                    start_date: Optional[str],
                    end_date: Optional[str],
                    preferred_adjustment: Optional[PriceAdjustment] = None
                    ) -> Optional[AdjustedPriceSeries]:
        """全量重建（后复权口径变化时使用）：拉全历史，不带起始限制"""
        try:
            fetched = self.price_provider.fetch(
                key, start_date=None, end_date=end_date,
                preferred_adjustment=preferred_adjustment)
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

    def _is_authoritative_rebuild(self) -> bool:
        """
        全量重建的结果是否可信（无需再校验口径）

        只有一个数据源时（或它就是主源），重建结果就是最新真相；
        多源场景下备用源的口径可能与缓存不同，需要重叠区实测校验。
        """
        provider = self.price_provider
        sources = getattr(provider, 'sources', None)
        if not sources:
            return True
        return len(sources) == 1

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
        """
        是否为权威源

        - 声明了多个源时：第一个（优先级最高）为主源，其口径最可信
        - 只声明了一个源（即只有这一个数据源）：它就是权威源
        """
        provider = self.price_provider
        sources = getattr(provider, 'sources', None)
        if not sources:
            return True
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
