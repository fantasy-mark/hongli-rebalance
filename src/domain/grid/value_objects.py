"""
网格策略领域值对象

包含:
- GridTrigger: 网格触发参数（前N日低 + ATR 步长 + 单格买入金额）
- GridCostModel: 网格交易成本模型
- GridLevel: 网格档位
- GridTranche: 网格持仓批次
- GridOrder: 网格成交记录
- GridMarketSnapshot: 网格决策所需的行情快照
- GridDecision: 网格引擎决策结果

所有值对象均为不可变（frozen=True），保证计算结果可复现
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class GridAnchor(Enum):
    """网格锚点来源"""
    TRIGGER_PRICE = "trigger_price"  # 以跌破当日收盘价为锚
    PRIOR_LOW = "prior_low"          # 以被跌破的前N日最低价为锚


class GridPriceBasis(Enum):
    """网格价格口径"""
    CLOSE = "close"  # 以收盘价判定触发与成交
    LOW = "low"      # 以最低价判定触发（模拟限价单挂单成交）


class GridAction(Enum):
    """网格决策动作"""
    MONITOR = "monitor"      # 监控中（尚未跌破前低）
    ACTIVATE = "activate"    # 网格激活（跌破前低，开启新一轮）
    BUY = "buy"              # 网格买入
    SELL = "sell"            # 网格卖出（止盈）
    CLOSED = "closed"        # 本轮网格完全止盈（等待重新跌破前低开启下一轮）
    HOLD = "hold"            # 持有观望
    SKIPPED = "skipped"      # 跳过（资金不足等）


class GridOrderSide(Enum):
    """网格订单方向"""
    BUY = "buy"
    SELL = "sell"


class GridTradeType(Enum):
    """
    网格交易类型

    - OPEN:        建仓（本轮网格的首笔买入）
    - ADD:         加仓（本轮网格的后续买入，档位更深）
    - REDUCE:      减仓（卖出部分持仓，本网格策略不触发）
    - TAKE_PROFIT: 止盈（一次性卖出本轮全部持仓，本轮结束）
    """
    OPEN = "open"
    ADD = "add"
    REDUCE = "reduce"
    TAKE_PROFIT = "take_profit"

    @property
    def label(self) -> str:
        """中文标签"""
        return {
            GridTradeType.OPEN: "建仓",
            GridTradeType.ADD: "加仓",
            GridTradeType.REDUCE: "减仓",
            GridTradeType.TAKE_PROFIT: "止盈",
        }[self]


@dataclass(frozen=True)
class GridTrigger:
    """
    网格触发参数（值对象）

    业务规则:
    - 监控启动: 收盘价跌破"前 lookback_days 个交易日最低价"（不含当日）
    - 网格步长: step = ATR(atr_period) * step_ratio，ATR 取自触发日（固定不变）
    - 单格买入: buy_amount 元（向下取整到 lot_size 整数倍）
    - 轮次结束: 本轮持仓全部止盈后，需重新跌破前低才会开启下一轮

    不变量:
    - lookback_days >= 1
    - atr_period >= 1
    - step_ratio > 0
    - buy_amount > 0
    - lot_size >= 1
    """
    lookback_days: int = 120
    atr_period: int = 120
    step_ratio: Decimal = Decimal("0.5")
    buy_amount: Decimal = Decimal("10000")
    lot_size: int = 100
    anchor: GridAnchor = GridAnchor.TRIGGER_PRICE
    price_basis: GridPriceBasis = GridPriceBasis.CLOSE
    take_profit_ratio: Optional[Decimal] = Decimal("1")  # 止盈倍数（相对一个网格步长），None 表示不止盈
    max_levels: Optional[int] = None             # 最大网格层数，None 表示不限

    def __post_init__(self):
        if self.lookback_days < 1:
            raise ValueError(f"回看天数必须>=1: {self.lookback_days}")
        if self.atr_period < 1:
            raise ValueError(f"ATR周期必须>=1: {self.atr_period}")
        if self.step_ratio <= 0:
            raise ValueError(f"ATR比例必须>0: {self.step_ratio}")
        if self.buy_amount <= 0:
            raise ValueError(f"单格买入金额必须>0: {self.buy_amount}")
        if self.lot_size < 1:
            raise ValueError(f"最小交易单位必须>=1: {self.lot_size}")
        if self.take_profit_ratio is not None and self.take_profit_ratio <= 0:
            raise ValueError(f"止盈倍数必须>0: {self.take_profit_ratio}")
        if self.max_levels is not None and self.max_levels < 1:
            raise ValueError(f"最大网格层数必须>=1: {self.max_levels}")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'lookback_days': self.lookback_days,
            'atr_period': self.atr_period,
            'step_ratio': float(self.step_ratio),
            'buy_amount': float(self.buy_amount),
            'lot_size': self.lot_size,
            'anchor': self.anchor.value,
            'price_basis': self.price_basis.value,
            'take_profit_ratio': (
                float(self.take_profit_ratio) if self.take_profit_ratio is not None else None
            ),
            'max_levels': self.max_levels,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GridTrigger':
        """从字典创建"""
        take_profit = data.get('take_profit_ratio')
        return cls(
            lookback_days=int(data.get('lookback_days', 120)),
            atr_period=int(data.get('atr_period', 120)),
            step_ratio=Decimal(str(data.get('step_ratio', 0.5))),
            buy_amount=Decimal(str(data.get('buy_amount', 10000))),
            lot_size=int(data.get('lot_size', 100)),
            anchor=GridAnchor(data.get('anchor', GridAnchor.TRIGGER_PRICE.value)),
            price_basis=GridPriceBasis(data.get('price_basis', GridPriceBasis.CLOSE.value)),
            take_profit_ratio=Decimal(str(take_profit)) if take_profit is not None else None,
            max_levels=data.get('max_levels'),
        )


@dataclass(frozen=True)
class GridCostModel:
    """
    网格交易成本模型（值对象）

    不变量:
    - 所有费率 >= 0
    """
    commission_rate: Decimal = Decimal("0.0003")  # 佣金率 0.03%
    slippage_rate: Decimal = Decimal("0.001")    # 滑点率 0.1%
    min_commission: Decimal = Decimal("5")       # 最低佣金 5 元

    def __post_init__(self):
        if self.commission_rate < 0:
            raise ValueError(f"commission_rate不能为负: {self.commission_rate}")
        if self.slippage_rate < 0:
            raise ValueError(f"slippage_rate不能为负: {self.slippage_rate}")
        if self.min_commission < 0:
            raise ValueError(f"min_commission不能为负: {self.min_commission}")

    def calculate_cost(self, trade_amount: Decimal) -> Decimal:
        """
        计算交易成本（佣金 + 滑点）

        Args:
            trade_amount: 成交金额

        Returns:
            总成本
        """
        if trade_amount <= 0:
            return Decimal("0")

        commission = max(trade_amount * self.commission_rate, self.min_commission)
        slippage = trade_amount * self.slippage_rate

        return commission + slippage

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'commission_rate': float(self.commission_rate),
            'slippage_rate': float(self.slippage_rate),
            'min_commission': float(self.min_commission),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GridCostModel':
        """从字典创建"""
        return cls(
            commission_rate=Decimal(str(data.get('commission_rate', 0.0003))),
            slippage_rate=Decimal(str(data.get('slippage_rate', 0.001))),
            min_commission=Decimal(str(data.get('min_commission', 5))),
        )


@dataclass(frozen=True)
class GridLevel:
    """
    网格档位（值对象）

    level: 档位编号，从 1 开始
    price: 该档位的触发价格 = 锚点 - 步长 * level
    """
    level: int
    price: Decimal

    def __post_init__(self):
        if self.level < 1:
            raise ValueError(f"档位编号必须>=1: {self.level}")
        if self.price <= 0:
            raise ValueError(f"档位价格必须>0: {self.price}")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'level': self.level,
            'price': float(self.price),
        }


@dataclass(frozen=True)
class GridTranche:
    """
    网格持仓批次（值对象）

    记录单笔网格买入，用于止盈时按批次平仓

    属性:
    - amount: 成交金额
    - cost:   交易费用（手续费+滑点）
    - total_outlay: 总支出 = amount + cost（成本核算口径）
    """
    level: int
    price: Decimal
    shares: Decimal
    amount: Decimal
    date: date
    cost: Decimal = Decimal("0")

    def __post_init__(self):
        if self.level < 0:
            raise ValueError(f"档位编号不能为负: {self.level}")
        if self.shares <= 0:
            raise ValueError(f"持仓数量必须>0: {self.shares}")
        if self.price <= 0:
            raise ValueError(f"买入价格必须>0: {self.price}")
        if self.amount < 0:
            raise ValueError(f"成交金额不能为负: {self.amount}")
        if self.cost < 0:
            raise ValueError(f"交易费用不能为负: {self.cost}")

    @property
    def total_outlay(self) -> Decimal:
        """总支出（含手续费）"""
        return self.amount + self.cost

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'level': self.level,
            'price': float(self.price),
            'shares': float(self.shares),
            'amount': float(self.amount),
            'cost': float(self.cost),
            'total_outlay': float(self.total_outlay),
            'date': self.date.isoformat(),
        }


@dataclass(frozen=True)
class GridOrder:
    """
    网格成交记录（值对象）

    描述一次网格买入/卖出成交
    """
    asset_code: str
    side: GridOrderSide
    price: Decimal
    shares: Decimal
    amount: Decimal
    cost: Decimal
    date: date
    level: Optional[int] = None
    order_id: str = ""
    trade_type: Optional['GridTradeType'] = None   # 由领域层在记账时判定
    shares_after: Optional[Decimal] = None         # 本笔成交后的持仓（领域层回填）
    step: Optional[Decimal] = None                 # 成交时的网格步长（轮次上下文）

    def __post_init__(self):
        if not self.asset_code:
            raise ValueError("资产代码不能为空")
        if self.price <= 0:
            raise ValueError(f"成交价格必须>0: {self.price}")
        if self.shares <= 0:
            raise ValueError(f"成交数量必须>0: {self.shares}")
        if self.amount < 0:
            raise ValueError(f"成交金额不能为负: {self.amount}")
        if self.cost < 0:
            raise ValueError(f"交易成本不能为负: {self.cost}")

    @property
    def cash_flow(self) -> Decimal:
        """现金影响：买入为负，卖出为正（已扣除成本）"""
        if self.side == GridOrderSide.BUY:
            return -(self.amount + self.cost)
        return self.amount - self.cost

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'asset_code': self.asset_code,
            'side': self.side.value,
            'price': float(self.price),
            'shares': float(self.shares),
            'amount': float(self.amount),
            'cost': float(self.cost),
            'date': self.date.isoformat(),
            'level': self.level,
            'trade_type': self.trade_type.value if self.trade_type else None,
            'shares_after': float(self.shares_after) if self.shares_after is not None else None,
            'step': float(self.step) if self.step is not None else None,
        }


@dataclass(frozen=True)
class GridMarketSnapshot:
    """
    网格行情快照（值对象）

    由应用层从价格数据构造，作为领域决策的唯一输入

    不变量:
    - close > 0
    - open/high/low 若提供则必须 > 0 且满足 high >= low
    """
    date: date
    close: Decimal
    prior_low: Optional[Decimal] = None   # 前 N 日最低价（不含当日），历史不足为 None
    atr: Optional[Decimal] = None         # 当日 ATR(atr_period)，历史不足为 None
    open: Optional[Decimal] = None        # 开盘价（LOW 口径下用于模拟跳空成交）
    high: Optional[Decimal] = None        # 最高价（LOW 口径下用于止盈触发）
    low: Optional[Decimal] = None         # 最低价（LOW 口径下用于触发判定）

    def __post_init__(self):
        if self.close <= 0:
            raise ValueError(f"收盘价必须>0: {self.close}")
        if self.prior_low is not None and self.prior_low <= 0:
            raise ValueError(f"前N日最低价必须>0: {self.prior_low}")
        if self.atr is not None and self.atr < 0:
            raise ValueError(f"ATR不能为负: {self.atr}")
        for name in ('open', 'high', 'low'):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name}必须>0: {value}")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError(f"最高价({self.high})不能低于最低价({self.low})")

    def has_enough_history(self) -> bool:
        """历史数据是否足以支撑网格计算"""
        return self.prior_low is not None and self.atr is not None and self.atr > 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'date': self.date.isoformat(),
            'close': float(self.close),
            'prior_low': float(self.prior_low) if self.prior_low is not None else None,
            'atr': float(self.atr) if self.atr is not None else None,
            'open': float(self.open) if self.open is not None else None,
            'high': float(self.high) if self.high is not None else None,
            'low': float(self.low) if self.low is not None else None,
        }


@dataclass(frozen=True)
class GridDecision:
    """
    网格决策结果（值对象）

    描述某个资产在某个交易日的网格动作
    """
    action: GridAction
    asset_code: str
    date: date
    reason: str = ""
    orders: Tuple[GridOrder, ...] = ()
    anchor_price: Optional[Decimal] = None
    step: Optional[Decimal] = None
    level: Optional[int] = None
    round_closed: bool = False   # 本轮网格是否已完全止盈结束

    @property
    def has_orders(self) -> bool:
        """是否产生成交"""
        return len(self.orders) > 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'action': self.action.value,
            'asset_code': self.asset_code,
            'date': self.date.isoformat(),
            'reason': self.reason,
            'orders': [o.to_dict() for o in self.orders],
            'anchor_price': float(self.anchor_price) if self.anchor_price is not None else None,
            'step': float(self.step) if self.step is not None else None,
            'level': self.level,
            'round_closed': self.round_closed,
        }
