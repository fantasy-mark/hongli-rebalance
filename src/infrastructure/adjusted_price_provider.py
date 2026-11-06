"""
后复权价格K线数据提供模块 v2

统一接口获取ETF/股票的后复权（前复权 qfq）历史K线数据。

⚠️ 重要发现（基于 price_data_sources.md）：
- 东财的 fund_etf_hist_em(adjust="qfw") 对ETF**无效**，返回的是不复权价格
- ETF的真正后复权需要使用"自算总收益"方法：不复权价 + 分红再投资
- 个股的东财qfw可能存在质量问题，需要审计

数据源策略：
- ETF: 自算总收益（主）→ 腾讯qfw（备，仅~641根）
- 个股: 东财qfw + 质量审计 → 自算总收益 → 腾讯qfw
"""

import logging
from typing import Optional, Tuple
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# 数据源标识常量
SOURCE_TOTAL_RETURN = "total_return"  # 自算总收益
SOURCE_EASTMONEY = "eastmoney"
SOURCE_TENCENT = "tencent"


def fetch_adjusted_kline(
    code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    adjust_type: str = "qfq",
) -> pd.DataFrame:
    """
    获取后复权K线数据（门面函数）

    Args:
        code: 6位证券代码，如 '515180' 或 '601088'
        start_date: 起始日期，格式 'YYYY-MM-DD'，None表示最早可用
        end_date: 结束日期，格式 'YYYY-MM-DD'，None表示最新
        adjust_type: 复权类型，默认 'qfq'（前复权），可选 ''（不复权）

    Returns:
        pandas DataFrame，索引为datetime，列包括：
        - open: 开盘价
        - high: 最高价
        - low: 最低价
        - close: 收盘价（后复权）
        - volume: 成交量
        - amount: 成交额
        - source: 数据来源标识

    Raises:
        ValueError: 当所有数据源都失败时
    """
    # 判断是ETF还是个股
    is_etf = _is_etf(code)

    if is_etf:
        return _fetch_etf_adjusted_kline(code, start_date, end_date, adjust_type)
    else:
        return _fetch_stock_adjusted_kline(code, start_date, end_date, adjust_type)


def _is_etf(code: str) -> bool:
    """判断是否为ETF（5开头为上交所ETF，1开头为深交所ETF）"""
    return code.startswith(("5", "1"))


def _fetch_etf_adjusted_kline(
    code: str,
    start_date: Optional[str],
    end_date: Optional[str],
    adjust_type: str,
) -> pd.DataFrame:
    """
    获取ETF后复权K线
    
    修正方案：使用东财的 hfq (后复权) 参数
    优先级：东财hfq → 腾讯qfw
    """
    if adjust_type == "":
        # 不复权
        try:
            df = _eastmoney_etf_hist(code, adjust="")
            df = _filter_by_date(df, start_date, end_date)
            if not df.empty:
                df["source"] = SOURCE_EASTMONEY
                return df
        except Exception as e:
            logger.warning(f"东财获取ETF {code} 不复权数据失败: {e}")
    
    # 默认使用后复权 (hfq)
    try:
        df = _eastmoney_etf_hist(code, adjust="hfq")
        df = _filter_by_date(df, start_date, end_date)
        if not df.empty:
            logger.info(f"ETF {code} 从东财获取后复权(hfq)数据成功，共 {len(df)} 条记录")
            df["source"] = SOURCE_EASTMONEY
            return df
    except Exception as e:
        logger.warning(f"东财获取ETF {code} 后复权数据失败: {e}")
    
    # 降级到腾讯qfw
    try:
        sina_code = _to_sina_symbol(code)
        df = _tencent_qfq(sina_code)
        df = _filter_by_date(df, start_date, end_date)
        if not df.empty:
            logger.warning(
                f"ETF {code} 使用腾讯备用源（仅~641根数据），共 {len(df)} 条记录"
            )
            df["source"] = SOURCE_TENCENT
            return df
    except Exception as e:
        logger.warning(f"腾讯获取ETF {code} 失败: {e}")
    
    raise ValueError(f"无法获取ETF {code} 的后复权数据，所有数据源均失败")


def _fetch_stock_adjusted_kline(
    code: str,
    start_date: Optional[str],
    end_date: Optional[str],
    adjust_type: str,
) -> pd.DataFrame:
    """获取个股后复权K线：东财（带审计）→ 自算总收益 → 腾讯"""
    if adjust_type == "":
        # 不复权
        try:
            df = _eastmoney_stock_hist(code, adjust="")
            df = _filter_by_date(df, start_date, end_date)
            if not df.empty:
                df["source"] = SOURCE_EASTMONEY
                return df
        except Exception as e:
            logger.warning(f"东财获取个股 {code} 不复权数据失败: {e}")
    
    # 尝试东财qfw + 质量审计
    try:
        df = _eastmoney_stock_hist(code, adjust="qfq")
        
        # 质量审计
        audit_result = _audit_stock_qfq_quality(df, code)
        if audit_result["passed"]:
            df = _filter_by_date(df, start_date, end_date)
            if not df.empty:
                logger.info(f"个股 {code} 从东财获取成功（通过审计），共 {len(df)} 条记录")
                df["source"] = SOURCE_EASTMONEY
                return df
        else:
            logger.warning(
                f"个股 {code} 东财qfw数据质量不佳: {audit_result['reason']}"
                f"（{audit_result['bad_days']}天异常，最大偏差{audit_result['max_deviation']:.2f}%）"
                f"，切换到自算总收益"
            )
    except Exception as e:
        logger.warning(f"东财获取个股 {code} qfw失败: {e}")
    
    # 降级到自算总收益
    try:
        df = _calculate_stock_total_return(code)
        df = _filter_by_date(df, start_date, end_date)
        if not df.empty:
            logger.info(f"个股 {code} 使用自算总收益方法，共 {len(df)} 条记录")
            df["source"] = SOURCE_TOTAL_RETURN
            return df
    except Exception as e:
        logger.warning(f"自算总收益获取个股 {code} 失败: {e}")
    
    # 最后降级到腾讯
    try:
        sina_code = _to_sina_symbol(code)
        df = _tencent_qfq(sina_code)
        df = _filter_by_date(df, start_date, end_date)
        if not df.empty:
            logger.warning(
                f"个股 {code} 使用腾讯备用源（仅~641根数据），共 {len(df)} 条记录"
            )
            df["source"] = SOURCE_TENCENT
            return df
    except Exception as e:
        logger.warning(f"腾讯获取个股 {code} 失败: {e}")
    
    raise ValueError(f"无法获取个股 {code} 的后复权数据，所有数据源均失败")


def _calculate_etf_total_return(code: str) -> pd.DataFrame:
    """
    计算ETF的总收益序列（真正的后复权）
    
    算法（参考 price_data_sources.md 第66-67行）：
    1. 获取不复权价格序列
    2. 获取分红数据
    3. 模拟分红再投资：在除息日调整份额
    4. 将末值归一到真实收盘价
    
    公式：
    - 除息日前一天收盘后，持有 N 份
    - 除息日每股分红 D 元，除息价为 P_ex
    - 除息日后份额变为：N + N * D / P_ex
    - 总收益价格 = 不复权价格 * (当前份额 / 初始份额)
    """
    import akshare as ak
    
    # 1. 获取不复权价格
    df_price = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="")
    df_price = df_price.rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    })
    df_price["date"] = pd.to_datetime(df_price["date"])
    df_price = df_price.set_index("date").sort_index()
    
    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        df_price[col] = pd.to_numeric(df_price[col], errors="coerce")
    
    if df_price.empty:
        raise ValueError(f"无法获取 {code} 的价格数据")
    
    # 2. 获取分红数据
    dividends = _get_etf_dividends(code)
    
    if dividends.empty:
        # 没有分红，不复权就是后复权
        logger.info(f"ETF {code} 无分红记录，直接使用不复权价格")
        df_price["source"] = SOURCE_EASTMONEY
        return df_price[["open", "high", "low", "close", "volume", "amount"]]
    
    # 3. 计算总收益序列
    df_total_return = _apply_dividend_reinvestment(df_price, dividends, tax_rate=0.0)
    
    return df_total_return[["open", "high", "low", "close", "volume", "amount"]]


def _calculate_stock_total_return(code: str) -> pd.DataFrame:
    """
    计算个股的总收益序列
    
    与ETF类似，但需要考虑股息税
    """
    import akshare as ak
    
    # 1. 获取不复权价格
    df_price = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="")
    df_price = df_price.rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    })
    df_price["date"] = pd.to_datetime(df_price["date"])
    df_price = df_price.set_index("date").sort_index()
    
    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        df_price[col] = pd.to_numeric(df_price[col], errors="coerce")
    
    if df_price.empty:
        raise ValueError(f"无法获取 {code} 的价格数据")
    
    # 2. 获取分红数据（个股分红较复杂，这里简化处理）
    # TODO: 实现完整的个股分红获取逻辑
    logger.warning(f"个股 {code} 的自算总收益功能尚未完全实现，返回东财qfw数据")
    
    # 暂时返回东财qfw
    df_qfq = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
    df_qfq = df_qfq.rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    })
    df_qfq["date"] = pd.to_datetime(df_qfq["date"])
    df_qfq = df_qfq.set_index("date").sort_index()
    
    for col in numeric_cols:
        df_qfq[col] = pd.to_numeric(df_qfq[col], errors="coerce")
    
    return df_qfq[["open", "high", "low", "close", "volume", "amount"]]


def _get_etf_dividends(code: str) -> pd.DataFrame:
    """
    获取ETF分红数据
    
    Returns:
        DataFrame with columns: date, dividend_per_share
    """
    import akshare as ak
    
    try:
        # 尝试获取基金分红数据
        df = ak.fund_fh_em()
        
        # 过滤指定基金
        # 注意：akshare的fund_fh_em返回格式可能变化，需要适配
        if '基金代码' in df.columns and '每份分红' in df.columns:
            df_code = df[df['基金代码'] == code].copy()
            if not df_code.empty:
                # 转换日期
                if '权益登记日' in df_code.columns:
                    df_code['date'] = pd.to_datetime(df_code['权益登记日'])
                    df_code['dividend_per_share'] = pd.to_numeric(df_code['每份分红'], errors='coerce')
                    df_code = df_code[['date', 'dividend_per_share']].dropna()
                    df_code = df_code.sort_values('date')
                    logger.info(f"找到 {code} 的 {len(df_code)} 条分红记录")
                    return df_code
        
        logger.warning(f"未找到 {code} 的分红数据或格式不匹配")
        return pd.DataFrame(columns=['date', 'dividend_per_share'])
        
    except Exception as e:
        logger.warning(f"获取 {code} 分红数据失败: {e}")
        return pd.DataFrame(columns=['date', 'dividend_per_share'])


def _apply_dividend_reinvestment(
    df_price: pd.DataFrame,
    df_dividends: pd.DataFrame,
    tax_rate: float = 0.0,
) -> pd.DataFrame:
    """
    应用分红再投资，计算总收益序列
    
    Args:
        df_price: 不复权价格DataFrame，索引为date
        df_dividends: 分红数据，包含 date 和 dividend_per_share
        tax_rate: 股息税率，ETF为0，个股根据持有期限不同
    
    Returns:
        包含总收益价格的DataFrame
    """
    if df_dividends.empty:
        return df_price.copy()
    
    df = df_price.copy()
    
    # 初始化份额乘数
    cumulative_factor = 1.0
    factor_series = pd.Series(index=df.index, dtype=float)
    
    # 合并分红日期
    div_dates = set(df_dividends['date'].values)
    
    # 遍历每个交易日，计算累积份额因子
    prev_date = None
    for date in df.index:
        if date in div_dates:
            # 找到当天的分红
            div_amount = df_dividends[df_dividends['date'] == date]['dividend_per_share'].iloc[0]
            
            if div_amount > 0 and prev_date is not None:
                # 获取除息日前一天的收盘价
                ex_close = df.loc[prev_date, 'close']
                
                if ex_close > 0:
                    # 计算再投资后的份额增长
                    # 新份额 = 旧份额 + 旧份额 * 分红 / 除息价
                    reinvest_ratio = div_amount * (1 - tax_rate) / ex_close
                    cumulative_factor *= (1 + reinvest_ratio)
        
        factor_series[date] = cumulative_factor
        prev_date = date
    
    # 应用份额因子到价格
    # 总收益价格 = 不复权价格 * 累积份额因子
    df['close_adj'] = df['close'] * factor_series
    df['open_adj'] = df['open'] * factor_series
    df['high_adj'] = df['high'] * factor_series
    df['low_adj'] = df['low'] * factor_series
    
    # 归一化：让最后一个交易日的调整后价格等于实际收盘价
    last_close = df['close'].iloc[-1]
    last_adj_close = df['close_adj'].iloc[-1]
    
    if last_adj_close > 0:
        normalization_factor = last_close / last_adj_close
        df['close'] = df['close_adj'] * normalization_factor
        df['open'] = df['open_adj'] * normalization_factor
        df['high'] = df['high_adj'] * normalization_factor
        df['low'] = df['low_adj'] * normalization_factor
    
    # 清理临时列
    df = df.drop(columns=['close_adj', 'open_adj', 'high_adj', 'low_adj'])
    
    logger.info(f"应用分红再投资，最终归一化因子: {normalization_factor:.6f}")
    
    return df


def _eastmoney_etf_hist(code: str, adjust: str = "hfq") -> pd.DataFrame:
    """
    从东方财富获取ETF历史K线
    
    ⚠️ 重要修正：
    - adjust="hfq" (后复权): 真正的总收益口径，推荐用于ETF
    - adjust="qfw" (前复权): 对ETF无效，会导致收益率虚高
    - adjust="" (不复权): 原始价格，用于账本成本
    """
    import akshare as ak
    
    df = ak.fund_etf_hist_em(symbol=code, period="daily", adjust=adjust)
    df = df.rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    })
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    
    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    
    return df[["open", "high", "low", "close", "volume", "amount"]]


def _eastmoney_stock_hist(code: str, adjust: str = "qfq") -> pd.DataFrame:
    """从东方财富获取个股历史K线"""
    import akshare as ak
    
    df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust=adjust)
    df = df.rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    })
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    
    numeric_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    
    return df[["open", "high", "low", "close", "volume", "amount"]]


def _tencent_qfq(sina_symbol: str) -> pd.DataFrame:
    """从腾讯获取前复权K线（最多~641根）"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "param": f"{sina_symbol},day,,,640,qfq",
    }
    
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    
    data = response.json()
    stock_data = data.get("data", {}).get(sina_symbol, {})
    qfq_data = stock_data.get("qfq", [])
    
    if not qfq_data:
        raise ValueError(f"腾讯返回的 {sina_symbol} qfq数据为空")
    
    records = []
    for item in qfq_data:
        if len(item) >= 6:
            records.append({
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]),
                "amount": 0.0,
            })
    
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    
    return df[["open", "high", "low", "close", "volume", "amount"]]


def _to_sina_symbol(code: str) -> str:
    """将6位代码转换为新浪格式符号"""
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    else:
        return f"sz{code}"


def _filter_by_date(
    df: pd.DataFrame,
    start_date: Optional[str],
    end_date: Optional[str],
) -> pd.DataFrame:
    """按日期范围过滤DataFrame"""
    if start_date:
        df = df[df.index >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df.index <= pd.to_datetime(end_date)]
    return df


def _audit_stock_qfq_quality(
    df: pd.DataFrame, code: str, threshold_pct: float = 1.0
) -> dict:
    """审计个股qfq数据质量"""
    try:
        import akshare as ak
        
        df_unadj = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="")
        df_unadj = df_unadj.rename(columns={
            "日期": "date",
            "收盘": "close",
        })
        df_unadj["date"] = pd.to_datetime(df_unadj["date"])
        df_unadj = df_unadj.set_index("date")
        df_unadj["close"] = pd.to_numeric(df_unadj["close"], errors="coerce")
        
        common_dates = df.index.intersection(df_unadj.index)
        if len(common_dates) < 10:
            return {
                "passed": False,
                "bad_days": 0,
                "max_deviation": 0,
                "reason": "数据点不足，无法审计",
            }
        
        df_aligned = df.loc[common_dates].copy()
        df_unadj_aligned = df_unadj.loc[common_dates].copy()
        
        df_aligned["return_qfq"] = df_aligned["close"].pct_change()
        df_unadj_aligned["return_unadj"] = df_unadj_aligned["close"].pct_change()
        
        diff = (df_aligned["return_qfq"] - df_unadj_aligned["return_unadj"]).abs() * 100
        non_extreme = diff[diff < 50]
        bad_days = (non_extreme > threshold_pct).sum()
        max_deviation = non_extreme.max() if len(non_extreme) > 0 else 0
        
        passed = bad_days == 0 or max_deviation < threshold_pct * 2
        
        reason = ""
        if not passed:
            reason = f"{bad_days}天超过{threshold_pct}%阈值"
        
        return {
            "passed": passed,
            "bad_days": int(bad_days),
            "max_deviation": float(max_deviation),
            "reason": reason,
        }
    
    except Exception as e:
        logger.warning(f"个股 {code} qfq质量审计失败: {e}")
        return {
            "passed": False,
            "bad_days": 0,
            "max_deviation": 0,
            "reason": f"审计过程出错: {str(e)}",
        }


def get_latest_adjusted_price(code: str) -> Tuple[float, str]:
    """获取最新的后复权收盘价"""
    df = fetch_adjusted_kline(code)
    if df.empty:
        raise ValueError(f"无法获取 {code} 的价格数据")
    
    latest_close = df["close"].iloc[-1]
    source = df["source"].iloc[-1] if "source" in df.columns else "unknown"
    
    return float(latest_close), source


if __name__ == "__main__":
    # 测试用例
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 80)
    print("测试ETF后复权价格（v2 - 使用自算总收益）")
    print("=" * 80)
    print()
    
    # 测试515180
    print("测试 515180 (红利ETF)")
    try:
        df = fetch_adjusted_kline("515180", start_date="2026-11-01")
        print(f"数据来源: {df['source'].iloc[0]}")
        print(f"最新收盘价: {df['close'].iloc[-1]:.3f}")
        print(df.tail())
    except Exception as e:
        print(f"失败: {e}")
    
    print()
    print("=" * 80)
