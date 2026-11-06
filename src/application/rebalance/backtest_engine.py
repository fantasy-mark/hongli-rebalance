"""
再平衡策略回测引擎

支持：
- 固定比例再平衡
- 阈值触发再平衡（偏离目标权重N个百分点）
- 盘中价格检测（使用当日收盘价模拟）
- 交易成本考虑
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import logging

import pandas as pd
import numpy as np

from src.domain.price.entities import AssetCode, AdjustedPriceSeries
from src.application.price.query_service import PriceQueryService

logger = logging.getLogger(__name__)


@dataclass
class AssetConfig:
    """资产配置"""
    code: str
    name: str
    target_weight: Decimal  # 目标权重
    
    def __post_init__(self):
        # 确保target_weight是Decimal
        if not isinstance(self.target_weight, Decimal):
            self.target_weight = Decimal(str(self.target_weight))
        
        if self.target_weight <= 0 or self.target_weight > 1:
            raise ValueError(f"权重必须在0-1之间: {self.target_weight}")


@dataclass
class RebalanceConfig:
    """再平衡配置"""
    assets: List[AssetConfig]
    threshold_pp: Decimal = Decimal("3")  # 触发阈值（百分点）
    initial_capital: Decimal = Decimal("1000000")  # 初始资金100万
    transaction_cost_bps: Decimal = Decimal("5")  # 交易成本5个基点(0.05%)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    
    def __post_init__(self):
        # 验证权重总和
        total_weight = sum(a.target_weight for a in self.assets)
        if abs(float(total_weight) - 1.0) > 0.01:
            raise ValueError(f"权重总和必须为1，当前为{total_weight}")
    
    @property
    def asset_codes(self) -> List[str]:
        return [a.code for a in self.assets]
    
    @property
    def asset_names(self) -> Dict[str, str]:
        return {a.code: a.name for a in self.assets}
    
    @property
    def target_weights(self) -> Dict[str, Decimal]:
        return {a.code: a.target_weight for a in self.assets}


@dataclass
class Trade:
    """交易记录"""
    date: date
    asset_code: str
    action: str  # 'buy' or 'sell'
    shares: Decimal
    price: Decimal
    amount: Decimal
    cost: Decimal  # 交易成本
    
    @property
    def net_amount(self) -> Decimal:
        """净金额（扣除成本）"""
        if self.action == 'buy':
            return self.amount + self.cost
        else:
            return self.amount - self.cost


@dataclass
class PortfolioSnapshot:
    """持仓快照"""
    date: date
    total_value: Decimal
    asset_values: Dict[str, Decimal]
    asset_weights: Dict[str, Decimal]
    asset_shares: Dict[str, Decimal]
    cash: Decimal
    trades: List[Trade] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            'date': self.date,
            'total_value': float(self.total_value),
            'asset_values': {k: float(v) for k, v in self.asset_values.items()},
            'asset_weights': {k: float(v) for k, v in self.asset_weights.items()},
            'cash': float(self.cash),
        }


@dataclass
class BacktestResult:
    """回测结果"""
    config: RebalanceConfig
    snapshots: List[PortfolioSnapshot]
    all_trades: List[Trade]
    daily_values: pd.DataFrame
    
    @property
    def total_return(self) -> Decimal:
        """总收益率"""
        if not self.snapshots:
            return Decimal("0")
        initial = self.snapshots[0].total_value
        final = self.snapshots[-1].total_value
        if initial <= 0:
            return Decimal("0")
        return (final - initial) / initial
    
    @property
    def annualized_return(self) -> Decimal:
        """年化收益率"""
        if len(self.snapshots) < 2:
            return Decimal("0")
        
        initial = self.snapshots[0].total_value
        final = self.snapshots[-1].total_value
        days = (self.snapshots[-1].date - self.snapshots[0].date).days
        
        if days <= 0 or initial <= 0:
            return Decimal("0")
        
        years = Decimal(str(days)) / Decimal("365.25")
        ratio = final / initial
        
        # CAGR = (final/initial)^(1/years) - 1
        cagr = float(ratio) ** (1.0 / float(years)) - 1.0
        return Decimal(str(cagr))
    
    @property
    def max_drawdown(self) -> Tuple[Decimal, date, date]:
        """最大回撤"""
        if not self.snapshots:
            return (Decimal("0"), date.today(), date.today())
        
        values = [(s.date, s.total_value) for s in self.snapshots]
        
        max_dd = Decimal("0")
        peak_value = values[0][1]
        peak_date = values[0][0]
        dd_start = values[0][0]
        dd_end = values[0][0]
        
        for current_date, current_value in values[1:]:
            if current_value > peak_value:
                peak_value = current_value
                peak_date = current_date
            
            drawdown = (current_value - peak_value) / peak_value
            
            if drawdown < max_dd:
                max_dd = drawdown
                dd_start = peak_date
                dd_end = current_date
        
        return (max_dd, dd_start, dd_end)
    
    @property
    def volatility(self) -> Decimal:
        """波动率（年化）"""
        if len(self.daily_values) < 2:
            return Decimal("0")
        
        returns = self.daily_values['total_value'].pct_change().dropna()
        if returns.empty:
            return Decimal("0")
        
        daily_vol = returns.std()
        annual_vol = daily_vol * np.sqrt(252)
        
        return Decimal(str(annual_vol))
    
    @property
    def sharpe_ratio(self) -> Decimal:
        """夏普比率（假设无风险利率3%）"""
        ann_return = self.annualized_return
        vol = self.volatility
        
        if vol <= 0:
            return Decimal("0")
        
        risk_free = Decimal("0.03")
        return (ann_return - risk_free) / vol
    
    @property
    def total_trades(self) -> int:
        """总交易次数"""
        return len(self.all_trades)
    
    @property
    def total_transaction_cost(self) -> Decimal:
        """总交易成本"""
        return sum(t.cost for t in self.all_trades)
    
    def summary(self) -> str:
        """生成回测摘要"""
        max_dd, dd_start, dd_end = self.max_drawdown
        
        return (
            f"再平衡策略回测结果\n"
            f"{'='*60}\n"
            f"回测期间: {self.snapshots[0].date} ~ {self.snapshots[-1].date}\n"
            f"初始资金: ¥{self.config.initial_capital:,.0f}\n"
            f"最终市值: ¥{self.snapshots[-1].total_value:,.0f}\n"
            f"\n"
            f"收益指标:\n"
            f"  累计收益: {self.total_return:.2%}\n"
            f"  年化收益: {self.annualized_return:.2%}\n"
            f"  波动率:   {self.volatility:.2%}\n"
            f"  夏普比率: {self.sharpe_ratio:.2f}\n"
            f"  最大回撤: {max_dd:.2%} ({dd_start} ~ {dd_end})\n"
            f"\n"
            f"交易统计:\n"
            f"  总交易次数: {self.total_trades}\n"
            f"  总交易成本: ¥{self.total_transaction_cost:,.2f}\n"
            f"  成本占比:   {self.total_transaction_cost/self.config.initial_capital:.2%}\n"
            f"\n"
            f"资产配置:\n"
        ) + "\n".join([
            f"  {self.config.asset_names.get(a.code, a.code)}: {float(a.target_weight)*100:.0f}%"
            for a in self.config.assets
        ])


class RebalanceBacktester:
    """
    再平衡回测引擎
    
    策略逻辑：
    1. 每日收盘后检查各资产权重
    2. 如果任一资产偏离目标权重超过threshold_pp，触发再平衡
    3. 再平衡时将所有资产调整到目标权重
    4. 考虑交易成本
    """
    
    def __init__(self, config: RebalanceConfig):
        self.config = config
        self.price_service = PriceQueryService()
        self._price_cache: Dict[str, pd.DataFrame] = {}
    
    def run(self) -> BacktestResult:
        """运行回测"""
        logger.info(f"开始回测: {len(self.config.assets)}个资产, "
                   f"阈值={self.config.threshold_pp}pp")
        
        # 1. 加载所有资产的价格数据
        price_data = self._load_all_prices()
        
        # 2. 确定回测时间范围
        start_date, end_date = self._determine_date_range(price_data)
        trading_days = self._get_trading_days(price_data, start_date, end_date)
        
        logger.info(f"回测期间: {start_date} ~ {end_date}, 共{len(trading_days)}个交易日")
        
        # 3. 初始化持仓
        snapshots = []
        all_trades = []
        current_shares: Dict[str, Decimal] = {code: Decimal("0") for code in self.config.asset_codes}
        cash = self.config.initial_capital
        
        # 4. 逐日回测
        for i, current_date in enumerate(trading_days):
            # 获取当日收盘价
            prices = self._get_prices_on_date(current_date, price_data)
            
            if not prices:
                continue
            
            # 计算当前持仓市值
            asset_values = {}
            for code in self.config.asset_codes:
                if code in prices and current_shares[code] > 0:
                    asset_values[code] = current_shares[code] * prices[code]
                else:
                    asset_values[code] = Decimal("0")
            
            total_asset_value = sum(asset_values.values())
            total_value = total_asset_value + cash
            
            # 计算当前权重
            asset_weights = {}
            if total_value > 0:
                for code in self.config.asset_codes:
                    asset_weights[code] = asset_values.get(code, Decimal("0")) / total_value
            else:
                for code in self.config.asset_codes:
                    asset_weights[code] = Decimal("0")
            
            # 检查是否需要再平衡
            trades_today = []
            if i == 0:
                # 第一天：初始建仓
                trades_today = self._initial_build(prices, cash)
            elif self._should_rebalance(asset_weights, prices, total_value):
                # 触发再平衡
                trades_today = self._rebalance(
                    current_shares, asset_values, prices, 
                    total_value, cash
                )
            
            # 执行交易
            if trades_today:
                for trade in trades_today:
                    if trade.action == 'buy':
                        current_shares[trade.asset_code] += trade.shares
                        cash -= trade.net_amount
                    else:  # sell
                        current_shares[trade.asset_code] -= trade.shares
                        cash += trade.net_amount
                
                all_trades.extend(trades_today)
                
                # 重新计算交易后的持仓
                asset_values = {}
                for code in self.config.asset_codes:
                    if code in prices and current_shares[code] > 0:
                        asset_values[code] = current_shares[code] * prices[code]
                    else:
                        asset_values[code] = Decimal("0")
                
                total_asset_value = sum(asset_values.values())
                total_value = total_asset_value + cash
                
                asset_weights = {}
                if total_value > 0:
                    for code in self.config.asset_codes:
                        asset_weights[code] = asset_values.get(code, Decimal("0")) / total_value
            
            # 记录快照
            snapshot = PortfolioSnapshot(
                date=current_date,
                total_value=total_value,
                asset_values=asset_values,
                asset_weights=asset_weights,
                asset_shares=dict(current_shares),
                cash=cash,
                trades=trades_today,
            )
            snapshots.append(snapshot)
        
        # 5. 构建每日价值序列
        daily_values = pd.DataFrame([
            {
                'date': s.date,
                'total_value': float(s.total_value),
                **{f'{code}_value': float(s.asset_values.get(code, 0)) 
                   for code in self.config.asset_codes},
                **{f'{code}_weight': float(s.asset_weights.get(code, 0)) 
                   for code in self.config.asset_codes},
            }
            for s in snapshots
        ])
        daily_values.set_index('date', inplace=True)
        
        result = BacktestResult(
            config=self.config,
            snapshots=snapshots,
            all_trades=all_trades,
            daily_values=daily_values,
        )
        
        logger.info(f"回测完成: 累计收益{result.total_return:.2%}, "
                   f"交易{result.total_trades}次")
        
        return result
    
    def _load_all_prices(self) -> Dict[str, pd.DataFrame]:
        """加载所有资产的价格数据"""
        price_data = {}
        
        for asset in self.config.assets:
            try:
                df = self.price_service.get_adjusted_prices(
                    asset.code,
                    use_cache=True
                )
                if not df.empty:
                    price_data[asset.code] = df
                    logger.info(f"加载 {asset.code} 价格数据: {len(df)}条")
                else:
                    logger.warning(f"{asset.code} 无价格数据")
            except Exception as e:
                logger.error(f"加载 {asset.code} 价格失败: {e}")
        
        return price_data
    
    def _determine_date_range(self, price_data: Dict[str, pd.DataFrame]) -> Tuple[date, date]:
        """确定回测时间范围（所有资产的共同日期范围）"""
        if not price_data:
            raise ValueError("没有可用的价格数据")
        
        # 找到所有资产都有的最早日期
        start_dates = []
        end_dates = []
        for df in price_data.values():
            min_date = df.index.min()
            max_date = df.index.max()
            # 确保是date对象
            if hasattr(min_date, 'date'):
                start_dates.append(min_date.date())
                end_dates.append(max_date.date())
            else:
                start_dates.append(min_date)
                end_dates.append(max_date)
        
        start_date = max(start_dates)  # 最晚的开始日期
        end_date = min(end_dates)      # 最早的结束日期
        
        if self.config.start_date:
            start_date = max(start_date, self.config.start_date)
        if self.config.end_date:
            end_date = min(end_date, self.config.end_date)
        
        return start_date, end_date
    
    def _get_trading_days(self, price_data: Dict[str, pd.DataFrame],
                          start_date: date, end_date: date) -> List[date]:
        """获取交易日列表（所有资产都有价格的日期）"""
        # 取第一个资产的交易日历作为基准
        first_code = list(price_data.keys())[0]
        df = price_data[first_code]
        
        # 过滤日期范围 - 索引可能是date或Timestamp
        trading_days = []
        for idx in df.index:
            d = idx.date() if hasattr(idx, 'date') else idx
            if start_date <= d <= end_date:
                trading_days.append(d)
        
        # 确保所有资产在这些日期都有价格
        valid_days = []
        for day in trading_days:
            day_ts = pd.Timestamp(day)
            if all(day_ts in price_data[code].index for code in price_data.keys()):
                valid_days.append(day)
        
        return sorted(valid_days)
    
    def _get_prices_on_date(self, target_date: date, 
                            price_data: Dict[str, pd.DataFrame]) -> Dict[str, Decimal]:
        """获取指定日期所有资产的收盘价"""
        prices = {}
        target_ts = pd.Timestamp(target_date)
        
        for code, df in price_data.items():
            if target_ts in df.index:
                close = df.loc[target_ts, 'close']
                prices[code] = Decimal(str(close))
        
        return prices
    
    def _should_rebalance(self, current_weights: Dict[str, Decimal],
                          prices: Dict[str, Decimal],
                          total_value: Decimal) -> bool:
        """
        检查是否需要再平衡
        
        规则：任一资产偏离目标权重超过threshold_pp个百分点
        """
        for asset in self.config.assets:
            code = asset.code
            target_w = asset.target_weight
            current_w = current_weights.get(code, Decimal("0"))
            
            deviation = abs(current_w - target_w)
            threshold = self.config.threshold_pp / Decimal("100")
            
            if deviation > threshold:
                logger.debug(f"触发再平衡: {code} 当前{current_w:.2%} vs 目标{target_w:.2%}, "
                           f"偏离{deviation:.2%}")
                return True
        
        return False
    
    def _initial_build(self, prices: Dict[str, Decimal], 
                       cash: Decimal) -> List[Trade]:
        """初始建仓"""
        trades = []
        
        for asset in self.config.assets:
            code = asset.code
            if code not in prices:
                continue
            
            price = prices[code]
            target_amount = cash * asset.target_weight
            shares = target_amount / price
            
            # 向下取整到整数股
            shares_int = Decimal(str(int(shares)))
            actual_amount = shares_int * price
            cost = actual_amount * self.config.transaction_cost_bps / Decimal("10000")
            
            trade = Trade(
                date=pd.Timestamp(list(prices.keys())[0]).date(),
                asset_code=code,
                action='buy',
                shares=shares_int,
                price=price,
                amount=actual_amount,
                cost=cost,
            )
            trades.append(trade)
        
        return trades
    
    def _rebalance(self, current_shares: Dict[str, Decimal],
                   current_values: Dict[str, Decimal],
                   prices: Dict[str, Decimal],
                   total_value: Decimal,
                   cash: Decimal) -> List[Trade]:
        """
        执行再平衡
        
        策略：卖出超配资产，买入低配资产，使权重回到目标
        """
        trades = []
        rebalance_date = pd.Timestamp(list(prices.keys())[0]).date()
        
        # 计算目标市值
        target_values = {
            code: total_value * weight 
            for code, weight in self.config.target_weights.items()
        }
        
        # 先卖出超配的
        for code in self.config.asset_codes:
            if code not in prices:
                continue
            
            current_val = current_values.get(code, Decimal("0"))
            target_val = target_values[code]
            
            if current_val > target_val:
                # 需要卖出
                sell_amount = current_val - target_val
                price = prices[code]
                shares_to_sell = sell_amount / price
                shares_int = Decimal(str(int(shares_to_sell)))
                
                if shares_int > 0:
                    actual_amount = shares_int * price
                    cost = actual_amount * self.config.transaction_cost_bps / Decimal("10000")
                    
                    trade = Trade(
                        date=rebalance_date,
                        asset_code=code,
                        action='sell',
                        shares=shares_int,
                        price=price,
                        amount=actual_amount,
                        cost=cost,
                    )
                    trades.append(trade)
        
        # 再买入低配的
        for code in self.config.asset_codes:
            if code not in prices:
                continue
            
            # 重新计算当前值（考虑刚卖出的）
            current_val = current_values.get(code, Decimal("0"))
            target_val = target_values[code]
            
            if current_val < target_val:
                # 需要买入
                buy_amount = target_val - current_val
                price = prices[code]
                shares_to_buy = buy_amount / price
                shares_int = Decimal(str(int(shares_to_buy)))
                
                if shares_int > 0:
                    actual_amount = shares_int * price
                    cost = actual_amount * self.config.transaction_cost_bps / Decimal("10000")
                    
                    trade = Trade(
                        date=rebalance_date,
                        asset_code=code,
                        action='buy',
                        shares=shares_int,
                        price=price,
                        amount=actual_amount,
                        cost=cost,
                    )
                    trades.append(trade)
        
        return trades
