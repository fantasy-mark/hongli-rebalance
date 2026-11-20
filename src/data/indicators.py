"""
技术指标与收益率计算（数据模块）

纯计算逻辑，无副作用、无 IO。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Tuple, Optional
import math

from src.data.models import AdjustedPriceSeries
from src.data.value_objects import ReturnMetrics, TechnicalIndicators


class PriceCalculator:
    """
    价格计算器 - 纯领域逻辑
    
    职责:
    - 计算收益率指标
    - 计算技术指标
    - 所有方法都是无状态的纯函数
    """
    
    @staticmethod
    def calculate_total_return(series: AdjustedPriceSeries,
                                start_date: Optional[date] = None,
                                end_date: Optional[date] = None) -> Decimal:
        """
        计算累计收益率
        
        公式: (结束价 - 开始价) / 开始价
        """
        if not series.prices:
            return Decimal("0")
        
        # 过滤日期范围
        prices = series.prices
        if start_date:
            prices = [p for p in prices if p.date >= start_date]
        if end_date:
            prices = [p for p in prices if p.date <= end_date]
        
        if len(prices) < 2:
            return Decimal("0")
        
        start_price = prices[0].close
        end_price = prices[-1].close
        
        if start_price <= 0:
            return Decimal("0")
        
        return (end_price - start_price) / start_price
    
    @staticmethod
    def calculate_cagr(series: AdjustedPriceSeries,
                       start_date: Optional[date] = None,
                       end_date: Optional[date] = None) -> Decimal:
        """
        计算年化收益率 (CAGR)
        
        公式: (结束价/开始价)^(1/年数) - 1
        """
        if not series.prices:
            return Decimal("0")
        
        prices = series.prices
        if start_date:
            prices = [p for p in prices if p.date >= start_date]
        if end_date:
            prices = [p for p in prices if p.date <= end_date]
        
        if len(prices) < 2:
            return Decimal("0")
        
        start_price = prices[0].close
        end_price = prices[-1].close
        start_dt = prices[0].date
        end_dt = prices[-1].date
        
        if start_price <= 0:
            return Decimal("0")
        
        years = Decimal(str((end_dt - start_dt).days)) / Decimal("365.25")
        if years <= 0:
            return Decimal("0")
        
        ratio = end_price / start_price
        
        # 使用浮点计算幂，然后转回Decimal
        cagr = float(ratio) ** (1.0 / float(years)) - 1.0
        return Decimal(str(cagr))
    
    @staticmethod
    def calculate_volatility(series: AdjustedPriceSeries,
                              annualize: bool = True) -> Decimal:
        """
        计算波动率（日收益率标准差）
        
        Args:
            annualize: 是否年化（乘以sqrt(252)）
        """
        if len(series.prices) < 2:
            return Decimal("0")
        
        # 计算日收益率
        returns = []
        for i in range(1, len(series.prices)):
            prev_close = series.prices[i-1].close
            curr_close = series.prices[i].close
            if prev_close > 0:
                daily_return = (curr_close - prev_close) / prev_close
                returns.append(float(daily_return))
        
        if not returns:
            return Decimal("0")
        
        # 计算标准差
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance)
        
        if annualize:
            std_dev *= math.sqrt(252)  # 年化
        
        return Decimal(str(std_dev))
    
    @staticmethod
    def calculate_sharpe_ratio(series: AdjustedPriceSeries,
                                risk_free_rate: Decimal = Decimal("0.03")) -> Decimal:
        """
        计算夏普比率
        
        公式: (年化收益 - 无风险利率) / 年化波动率
        """
        cagr = PriceCalculator.calculate_cagr(series)
        volatility = PriceCalculator.calculate_volatility(series, annualize=True)
        
        if volatility <= 0:
            return Decimal("0")
        
        return (cagr - risk_free_rate) / volatility
    
    @staticmethod
    def calculate_max_drawdown(series: AdjustedPriceSeries) -> Tuple[Decimal, date, date]:
        """
        计算最大回撤及发生时间
        
        Returns:
            (最大回撤, 开始日期, 结束日期)
            最大回撤为负值，如 -0.2 表示下跌20%
        """
        if len(series.prices) < 2:
            return (Decimal("0"), series.start_date or date.today(), series.end_date or date.today())
        
        max_dd = Decimal("0")
        dd_start = series.prices[0].date
        dd_end = series.prices[0].date
        
        peak_price = series.prices[0].close
        peak_date = series.prices[0].date
        
        for price in series.prices[1:]:
            if price.close > peak_price:
                peak_price = price.close
                peak_date = price.date
            
            drawdown = (price.close - peak_price) / peak_price
            
            if drawdown < max_dd:
                max_dd = drawdown
                dd_start = peak_date
                dd_end = price.date
        
        return (max_dd, dd_start, dd_end)
    
    @staticmethod
    def calculate_full_metrics(series: AdjustedPriceSeries,
                                start_date: Optional[date] = None,
                                end_date: Optional[date] = None) -> ReturnMetrics:
        """
        计算完整的收益率指标
        
        一次性计算所有指标，避免重复遍历
        """
        if not series.prices:
            raise ValueError("价格序列为空")
        
        # 过滤日期范围
        prices = series.prices
        if start_date:
            prices = [p for p in prices if p.date >= start_date]
        if end_date:
            prices = [p for p in prices if p.date <= end_date]
        
        if len(prices) < 2:
            raise ValueError("价格数据不足")
        
        # 创建临时序列用于计算
        from src.data.models import AdjustedPriceSeries as APS

        temp_series = APS(
            asset_code=series.asset_code,
            asset_type=series.asset_type,
            prices=prices,
            source=series.source,
            calculated_at=datetime.now(),
        )
        
        total_return = PriceCalculator.calculate_total_return(temp_series)
        annualized_return = PriceCalculator.calculate_cagr(temp_series)
        volatility = PriceCalculator.calculate_volatility(temp_series, annualize=True)
        sharpe = PriceCalculator.calculate_sharpe_ratio(temp_series)
        max_dd, dd_start, dd_end = PriceCalculator.calculate_max_drawdown(temp_series)
        
        return ReturnMetrics(
            total_return=total_return,
            annualized_return=annualized_return,
            volatility=volatility,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            max_drawdown_start=dd_start,
            max_drawdown_end=dd_end,
            start_date=prices[0].date,
            end_date=prices[-1].date,
            trading_days=len(prices),
        )
    
    @staticmethod
    def calculate_moving_average(series: AdjustedPriceSeries,
                                  window: int) -> List[Tuple[date, Decimal]]:
        """
        计算移动平均线
        
        Returns:
            [(日期, MA值), ...]
        """
        if len(series.prices) < window:
            return []
        
        result = []
        for i in range(window - 1, len(series.prices)):
            window_prices = series.prices[i-window+1:i+1]
            ma = sum(p.close for p in window_prices) / Decimal(str(window))
            result.append((series.prices[i].date, ma))
        
        return result
    
    @staticmethod
    def calculate_rsi(series: AdjustedPriceSeries, period: int = 14) -> List[Tuple[date, Decimal]]:
        """
        计算RSI指标
        
        Returns:
            [(日期, RSI值), ...]
        """
        if len(series.prices) < period + 1:
            return []
        
        # 计算价格变化
        changes = []
        for i in range(1, len(series.prices)):
            change = series.prices[i].close - series.prices[i-1].close
            changes.append(change)
        
        result = []
        for i in range(period, len(changes)):
            window = changes[i-period:i]
            
            gains = sum(c for c in window if c > 0) / Decimal(str(period))
            losses = abs(sum(c for c in window if c < 0)) / Decimal(str(period))
            
            if losses == 0:
                rsi = Decimal("100")
            else:
                rs = gains / losses
                rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
            
            result.append((series.prices[i+1].date, rsi))
        
        return result
    
    @staticmethod
    def calculate_bollinger_bands(series: AdjustedPriceSeries,
                                   window: int = 20,
                                   num_std: Decimal = Decimal("2")) -> List[Tuple[date, Decimal, Decimal, Decimal]]:
        """
        计算布林带
        
        Returns:
            [(日期, 上轨, 中轨, 下轨), ...]
        """
        if len(series.prices) < window:
            return []
        
        result = []
        for i in range(window - 1, len(series.prices)):
            window_prices = [float(p.close) for p in series.prices[i-window+1:i+1]]
            
            # 中轨 = MA
            middle = sum(window_prices) / len(window_prices)
            
            # 标准差
            variance = sum((p - middle) ** 2 for p in window_prices) / len(window_prices)
            std_dev = math.sqrt(variance)
            
            upper = middle + float(num_std) * std_dev
            lower = middle - float(num_std) * std_dev
            
            result.append((
                series.prices[i].date,
                Decimal(str(upper)),
                Decimal(str(middle)),
                Decimal(str(lower)),
            ))
        
        return result
    
    @staticmethod
    def calculate_true_ranges(series: AdjustedPriceSeries) -> List[Decimal]:
        """
        计算真实波幅序列 (True Range)
        
        TR = max(high - low, |high - prev_close|, |low - prev_close|)
        首个交易日无前收盘价，退化为 high - low
        
        Returns:
            与价格序列等长的 TR 列表
        """
        true_ranges: List[Decimal] = []
        
        for i, point in enumerate(series.prices):
            if i == 0:
                true_ranges.append(point.high - point.low)
            else:
                prev_close = series.prices[i - 1].close
                true_ranges.append(max(
                    point.high - point.low,
                    abs(point.high - prev_close),
                    abs(point.low - prev_close),
                ))
        
        return true_ranges
    
    @staticmethod
    def calculate_atr_series(series: AdjustedPriceSeries,
                             period: int = 14) -> List[Tuple[date, Decimal]]:
        """
        计算 ATR 序列（Wilder 平滑法）
        
        首个 ATR 值出现在第 period 根K线（索引 period-1），
        之后每个 ATR = (前ATR * (period-1) + 当日TR) / period
        
        Args:
            series: 价格序列
            period: ATR 周期
        
        Returns:
            [(日期, ATR值), ...]，数据不足时返回空列表
        """
        if period < 1:
            raise ValueError(f"ATR周期必须>=1: {period}")
        
        if len(series.prices) < period:
            return []
        
        true_ranges = PriceCalculator.calculate_true_ranges(series)
        period_dec = Decimal(str(period))
        
        # 首个 ATR：前 period 个 TR 的简单平均
        atr = sum(true_ranges[:period]) / period_dec
        result: List[Tuple[date, Decimal]] = [(series.prices[period - 1].date, atr)]
        
        # Wilder 平滑
        for i in range(period, len(true_ranges)):
            atr = (atr * (period - 1) + true_ranges[i]) / period_dec
            result.append((series.prices[i].date, atr))
        
        return result
    
    @staticmethod
    def calculate_atr(series: AdjustedPriceSeries,
                      period: int = 14,
                      index: int = -1) -> Decimal:
        """
        计算指定位置的 ATR
        
        Args:
            series: 价格序列
            period: ATR 周期
            index: 价格索引，-1 表示最新
        
        Returns:
            ATR 值，数据不足时返回 0
        """
        atr_series = PriceCalculator.calculate_atr_series(series, period)
        if not atr_series:
            return Decimal("0")
        
        target_idx = index if index >= 0 else len(series.prices) + index
        if target_idx < 0 or target_idx >= len(series.prices):
            return Decimal("0")
        
        target_date = series.prices[target_idx].date
        for d, value in atr_series:
            if d == target_date:
                return value
        
        return Decimal("0")
    
    @staticmethod
    def calculate_prior_low_series(series: AdjustedPriceSeries,
                                   window: int) -> List[Tuple[date, Optional[Decimal]]]:
        """
        计算"前 N 日最低价"序列（不包含当日）
        
        用于网格策略的启动监控：当日收盘价跌破前 N 个交易日的最低价即触发
        
        Args:
            series: 价格序列
            window: 回看窗口（交易日数）
        
        Returns:
            [(日期, 前N日最低价), ...]，历史不足时最低价为 None
        """
        if window < 1:
            raise ValueError(f"回看窗口必须>=1: {window}")
        
        result: List[Tuple[date, Optional[Decimal]]] = []
        
        for i, point in enumerate(series.prices):
            if i < window:
                result.append((point.date, None))
            else:
                lows = [p.low for p in series.prices[i - window:i]]
                result.append((point.date, min(lows)))
        
        return result
    
    @staticmethod
    def calculate_prior_low(series: AdjustedPriceSeries,
                            window: int,
                            index: int = -1) -> Optional[Decimal]:
        """
        计算指定位置的前 N 日最低价（不含当日）
        
        Returns:
            最低价，历史不足时返回 None
        """
        if window < 1:
            raise ValueError(f"回看窗口必须>=1: {window}")
        
        target_idx = index if index >= 0 else len(series.prices) + index
        if target_idx < 0 or target_idx >= len(series.prices):
            return None
        
        if target_idx < window:
            return None
        
        lows = [p.low for p in series.prices[target_idx - window:target_idx]]
        return min(lows)
    
    @staticmethod
    def calculate_technical_indicators(series: AdjustedPriceSeries,
                                        index: int = -1) -> Optional[TechnicalIndicators]:
        """
        计算指定位置的技术指标
        
        Args:
            index: 价格序列中的索引，-1表示最新
        """
        if len(series.prices) < 60:
            return None
        
        target_idx = index if index >= 0 else len(series.prices) + index
        
        # 计算各均线
        ma_5 = PriceCalculator.calculate_moving_average(series, 5)
        ma_10 = PriceCalculator.calculate_moving_average(series, 10)
        ma_20 = PriceCalculator.calculate_moving_average(series, 20)
        ma_60 = PriceCalculator.calculate_moving_average(series, 60)
        
        # 找到对应日期的值
        target_date = series.prices[target_idx].date
        
        def find_ma_value(ma_list, target):
            for d, v in ma_list:
                if d == target:
                    return v
            return Decimal("0")
        
        val_ma_5 = find_ma_value(ma_5, target_date)
        val_ma_10 = find_ma_value(ma_10, target_date)
        val_ma_20 = find_ma_value(ma_20, target_date)
        val_ma_60 = find_ma_value(ma_60, target_date)
        
        # RSI
        rsi_list = PriceCalculator.calculate_rsi(series, 14)
        val_rsi = Decimal("50")  # 默认值
        for d, v in rsi_list:
            if d == target_date:
                val_rsi = v
                break
        
        # 布林带
        bb_list = PriceCalculator.calculate_bollinger_bands(series, 20)
        val_bb_upper = val_ma_20
        val_bb_middle = val_ma_20
        val_bb_lower = val_ma_20
        for d, u, m, l in bb_list:
            if d == target_date:
                val_bb_upper = u
                val_bb_middle = m
                val_bb_lower = l
                break
        
        return TechnicalIndicators(
            ma_5=val_ma_5,
            ma_10=val_ma_10,
            ma_20=val_ma_20,
            ma_60=val_ma_60,
            rsi_14=val_rsi,
            bollinger_upper=val_bb_upper,
            bollinger_middle=val_bb_middle,
            bollinger_lower=val_bb_lower,
        )
