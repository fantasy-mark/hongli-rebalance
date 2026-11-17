"""
网格策略领域实体

核心聚合根: GridStrategy
实体: GridState, GridOrder(值对象)
值对象: GridTrigger, GridCostModel, GridDecision 等（见 value_objects.py）

聚合边界:
- GridStrategy 管理网格资金池（cash）与各资产的网格状态（GridState）
- GridState 管理单个资产的网格档位推进与持仓批次
- 网格资金与持仓完全独立于再平衡组合，互不影响
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

from src.domain.portfolio.entities import Portfolio, Position
from src.domain.grid.value_objects import (
    GridAction,
    GridAnchor,
    GridCostModel,
    GridOrder,
    GridOrderSide,
    GridTradeType,
    GridTrigger,
    GridTranche,
)


class GridStatus(Enum):
    """单个资产的网格状态"""
    IDLE = "idle"            # 未开始监控
    MONITORING = "monitoring"  # 监控中（等待跌破前低）
    ACTIVE = "active"        # 网格运行中（已跌破前低，正在分档建仓）
    COMPLETED = "completed"  # 本轮网格已完全止盈（等待重新跌破前低开启下一轮）


class GridStrategyStatus(Enum):
    """网格策略生命周期状态"""
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


def floor_to_lot(shares: Decimal, lot_size: int = 100) -> Decimal:
    """
    将股数向下取整到指定手数的整数倍

    Args:
        shares: 原始股数
        lot_size: 手数大小（ETF 为 100 股）

    Returns:
        取整后的股数
    """
    if shares <= 0:
        return Decimal("0")
    if lot_size < 1:
        raise ValueError(f"lot_size必须>=1: {lot_size}")

    lots = int(shares) // lot_size
    return Decimal(str(lots * lot_size))


@dataclass
class GridState:
    """
    单个资产的网格状态（实体）

    网格轮次生命周期:
        IDLE ──开始监控──> MONITORING ──跌破前N日最低价──> ACTIVE
                                                        │
                                          全部持仓止盈卖出 │（本轮结束）
                                                        ▼
                                          COMPLETED ──重新跌破前N日最低价──> ACTIVE(新一轮)

    职责:
    - 维护网格生命周期（监控 -> 运行 -> 完成 -> 重新触发）
    - 管理档位推进（next_level）
    - 维护网格持仓与成本
    """
    asset_code: str
    status: GridStatus = GridStatus.IDLE
    anchor_price: Optional[Decimal] = None     # 网格锚点价格
    anchor_date: Optional[date] = None         # 锚点日期（触发日）
    atr_at_trigger: Optional[Decimal] = None   # 触发日 ATR
    step: Optional[Decimal] = None             # 网格步长
    next_level: int = 1                        # 下一个待触发档位
    filled_levels: List[int] = field(default_factory=list)   # 已成交档位
    skipped_levels: List[int] = field(default_factory=list)  # 已跳过档位
    tranches: List[GridTranche] = field(default_factory=list)  # 持仓批次
    # 注：持仓数量/成本/已实现盈亏由 Portfolio 统一持有（见 GridStrategy.portfolio），
    #     此处只保留"轮次"相关状态，避免两套账本。
    last_price: Optional[Decimal] = None       # 最近价格
    last_action: Optional[GridAction] = None   # 最近动作
    triggered_at: Optional[date] = None        # 本轮触发日期
    rounds_completed: int = 0                  # 已完成的网格轮次
    round_no: int = 0                          # 当前轮次编号

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start_monitoring(self, on_date: date) -> None:
        """进入监控状态"""
        if self.status == GridStatus.IDLE:
            self.status = GridStatus.MONITORING

    def activate(self, anchor_price: Decimal,
                 atr: Decimal,
                 on_date: date,
                 step: Decimal) -> None:
        """
        激活网格并建立锚点与步长（开启新一轮）

        不变量:
        - anchor_price > 0
        - atr > 0
        - step > 0
        """
        if anchor_price <= 0:
            raise ValueError(f"锚点价格必须>0: {anchor_price}")
        if atr <= 0:
            raise ValueError(f"ATR必须>0: {atr}")
        if step <= 0:
            raise ValueError(f"网格步长必须>0: {step}")

        self.status = GridStatus.ACTIVE
        self.anchor_price = anchor_price
        self.anchor_date = on_date
        self.atr_at_trigger = atr
        self.step = step
        self.next_level = 1
        self.triggered_at = on_date
        self.round_no += 1
        self.filled_levels = []
        self.skipped_levels = []

    def complete_round(self, on_date: date) -> None:
        """
        结束当前网格轮次（全部持仓已止盈）

        结束后回到"等待重新跌破前N日最低价"的状态
        """
        self.rounds_completed += 1
        self.status = GridStatus.COMPLETED
        self.anchor_price = None
        self.anchor_date = None
        self.atr_at_trigger = None
        self.step = None
        self.next_level = 1
        self.triggered_at = None

    def rearm(self) -> None:
        """
        重新武装监控（等待下一次跌破前N日最低价）

        COMPLETED -> MONITORING
        """
        if self.status == GridStatus.COMPLETED:
            self.status = GridStatus.MONITORING

    def reset(self) -> None:
        """重置网格状态（保留持仓与已成交档位记录）"""
        self.status = GridStatus.IDLE
        self.anchor_price = None
        self.anchor_date = None
        self.atr_at_trigger = None
        self.step = None
        self.next_level = 1

    # ------------------------------------------------------------------
    # 档位计算
    # ------------------------------------------------------------------

    def level_price(self, level: int) -> Decimal:
        """
        计算指定档位的触发价格

        公式: 锚点 - 步长 * level
        """
        if self.anchor_price is None or self.step is None:
            raise ValueError("网格尚未激活，无法计算档位价格")
        if level < 1:
            raise ValueError(f"档位编号必须>=1: {level}")

        return self.anchor_price - self.step * level

    def next_level_price(self) -> Decimal:
        """下一个待触发档位的价格"""
        return self.level_price(self.next_level)

    # ------------------------------------------------------------------
    # 成交记录
    # ------------------------------------------------------------------

    def record_buy(self, order: GridOrder,
                   was_flat: bool) -> GridTradeType:
        """
        记录一笔网格买入（批次与档位层面）

        持仓数量/成本由 Portfolio 记账，本方法只维护网格轮次维度：
        - 追加持仓批次（按档位精确记录成本，供止盈时平仓）
        - 推进已成交档位

        Args:
            order: 买入成交记录
            was_flat: 买入前是否空仓（由 Portfolio 判定）

        Returns:
            GridTradeType（空仓 -> 开仓 OPEN，否则 -> 加仓 ADD）
        """
        if order.side != GridOrderSide.BUY:
            raise ValueError(f"只能记录买入成交: {order.side}")

        trade_type = GridTradeType.OPEN if was_flat else GridTradeType.ADD

        if order.level is not None:
            self.filled_levels.append(order.level)

        self.tranches.append(GridTranche(
            level=order.level or 0,
            price=order.price,
            shares=order.shares,
            amount=order.amount,
            date=order.date,
            cost=order.cost,
        ))

        return trade_type

    def record_sell(self, order: GridOrder) -> GridTradeType:
        """
        记录一笔网格卖出（止盈），移除对应持仓批次

        持仓数量/成本/已实现盈亏由 Portfolio 记账（按批次精确释放成本）。

        Args:
            order: 卖出成交记录

        Returns:
            GridTradeType（止盈 TAKE_PROFIT）
        """
        if order.side != GridOrderSide.SELL:
            raise ValueError(f"只能记录卖出成交: {order.side}")

        for tranche in list(self.tranches):
            if order.level is not None and tranche.level == order.level:
                self.tranches.remove(tranche)
                break

        # 卖出只发生在整轮止盈场景（引擎保证一次性清仓）
        return GridTradeType.TAKE_PROFIT

    def skip_level(self, level: int) -> None:
        """标记档位被跳过"""
        self.skipped_levels.append(level)

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------

    # 注：持仓数量/成本/盈亏由 Portfolio 持有（GridStrategy.portfolio），
    #     此处不再重复维护，避免两套账本。

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'asset_code': self.asset_code,
            'status': self.status.value,
            'anchor_price': float(self.anchor_price) if self.anchor_price is not None else None,
            'anchor_date': self.anchor_date.isoformat() if self.anchor_date else None,
            'atr_at_trigger': float(self.atr_at_trigger) if self.atr_at_trigger is not None else None,
            'step': float(self.step) if self.step is not None else None,
            'next_level': self.next_level,
            'filled_levels': list(self.filled_levels),
            'skipped_levels': list(self.skipped_levels),
            'tranches': [t.to_dict() for t in self.tranches],
            'last_price': float(self.last_price) if self.last_price is not None else None,
            'last_action': self.last_action.value if self.last_action else None,
            'triggered_at': self.triggered_at.isoformat() if self.triggered_at else None,
            'rounds_completed': self.rounds_completed,
            'round_no': self.round_no,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GridState':
        """从字典创建"""
        anchor_date = data.get('anchor_date')
        triggered_at = data.get('triggered_at')
        last_action = data.get('last_action')

        state = cls(
            asset_code=data['asset_code'],
            status=GridStatus(data.get('status', GridStatus.IDLE.value)),
            anchor_price=Decimal(str(data['anchor_price'])) if data.get('anchor_price') is not None else None,
            anchor_date=date.fromisoformat(anchor_date) if anchor_date else None,
            atr_at_trigger=Decimal(str(data['atr_at_trigger'])) if data.get('atr_at_trigger') is not None else None,
            step=Decimal(str(data['step'])) if data.get('step') is not None else None,
            next_level=int(data.get('next_level', 1)),
            filled_levels=list(data.get('filled_levels', [])),
            skipped_levels=list(data.get('skipped_levels', [])),
            last_price=Decimal(str(data['last_price'])) if data.get('last_price') is not None else None,
            last_action=GridAction(last_action) if last_action else None,
            triggered_at=date.fromisoformat(triggered_at) if triggered_at else None,
            rounds_completed=int(data.get('rounds_completed', 0)),
            round_no=int(data.get('round_no', 0)),
        )

        for t in data.get('tranches', []):
            state.tranches.append(GridTranche(
                level=int(t['level']),
                price=Decimal(str(t['price'])),
                shares=Decimal(str(t['shares'])),
                amount=Decimal(str(t['amount'])),
                date=date.fromisoformat(t['date']),
                cost=Decimal(str(t.get('cost', 0))),
            ))

        return state


@dataclass
class GridStrategy:
    """
    网格策略（聚合根）

    职责:
    - 管理网格触发参数与成本模型
    - 管理网格资金池（与再平衡资金完全隔离）
    - 管理各资产的网格状态
    - 提供资产组合估值

    不变量:
    - cash >= 0
    - asset_codes 不为空
    """
    name: str
    trigger: GridTrigger
    asset_codes: List[str] = field(default_factory=list)
    cost_model: GridCostModel = field(default_factory=GridCostModel)
    # 组合：网格的现金与持仓（独立于再平衡组合的 Portfolio 实例）
    portfolio: Portfolio = field(default_factory=Portfolio)
    status: GridStrategyStatus = GridStrategyStatus.ACTIVE
    states: Dict[str, GridState] = field(default_factory=dict)
    strategy_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if not self.name:
            raise ValueError("策略名称不能为空")

        # 组合标识为网格专用，便于与再平衡组合区分
        if not self.portfolio.name or self.portfolio.name == "组合":
            self.portfolio.name = "网格组合"

        # 预初始化各资产状态
        for code in self.asset_codes:
            if code not in self.states:
                self.states[code] = GridState(asset_code=code)

    # ------------------------------------------------------------------
    # 状态访问
    # ------------------------------------------------------------------

    def state_for(self, asset_code: str) -> GridState:
        """获取（必要时创建）某资产的网格状态"""
        if asset_code not in self.states:
            self.states[asset_code] = GridState(asset_code=asset_code)
        return self.states[asset_code]

    def active_assets(self) -> List[str]:
        """已激活网格的资产列表"""
        return [
            code for code, state in self.states.items()
            if state.status == GridStatus.ACTIVE
        ]

    def monitoring_assets(self) -> List[str]:
        """监控中的资产列表"""
        return [
            code for code, state in self.states.items()
            if state.status == GridStatus.MONITORING
        ]

    # ------------------------------------------------------------------
    # 估值
    # ------------------------------------------------------------------

    def positions_value(self, prices: Dict[str, Decimal]) -> Decimal:
        """网格持仓总市值"""
        return self.portfolio.market_value(prices)

    def total_value(self, prices: Dict[str, Decimal]) -> Decimal:
        """网格组合总市值（现金 + 持仓）"""
        return self.portfolio.total_value(prices)

    def total_cost_basis(self) -> Decimal:
        """网格持仓总成本"""
        return self.portfolio.total_cost_basis()

    def unrealized_pnl(self, prices: Dict[str, Decimal]) -> Decimal:
        """网格浮动盈亏"""
        return self.portfolio.unrealized_pnl(prices)

    def realized_pnl(self) -> Decimal:
        """网格累计已实现盈亏"""
        return self.portfolio.realized_pnl()

    # ------------------------------------------------------------------
    # 校验与序列化
    # ------------------------------------------------------------------

    def validate(self) -> List[str]:
        """
        验证策略有效性

        Returns:
            错误消息列表，空列表表示通过
        """
        errors = []

        if not self.name:
            errors.append("策略名称不能为空")

        if not self.asset_codes:
            errors.append("监控资产列表不能为空")

        errors.extend(self.portfolio.validate())

        for code, state in self.states.items():
            if state.status == GridStatus.ACTIVE and not state.step:
                errors.append(f"资产{code}处于激活状态但缺少网格步长")

        return errors

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'strategy_id': self.strategy_id,
            'name': self.name,
            'trigger': self.trigger.to_dict(),
            'asset_codes': list(self.asset_codes),
            'cost_model': self.cost_model.to_dict(),
            'portfolio': self.portfolio.to_dict(),
            'status': self.status.value,
            'states': {code: state.to_dict() for code, state in self.states.items()},
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GridStrategy':
        """从字典创建"""
        strategy = cls(
            name=data['name'],
            trigger=GridTrigger.from_dict(data['trigger']),
            asset_codes=list(data.get('asset_codes', [])),
            cost_model=GridCostModel.from_dict(data.get('cost_model', {})),
            status=GridStrategyStatus(data.get('status', GridStrategyStatus.ACTIVE.value)),
            strategy_id=data.get('strategy_id', str(uuid.uuid4())),
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else datetime.now(),
            updated_at=datetime.fromisoformat(data['updated_at']) if data.get('updated_at') else datetime.now(),
        )

        for code, state_data in data.get('states', {}).items():
            strategy.states[code] = GridState.from_dict(state_data)

        portfolio_data = data.get('portfolio')
        if portfolio_data:
            positions = {
                code: Position(
                    asset_code=code,
                    shares=Decimal(str(p.get('shares', 0))),
                    cost_basis=Decimal(str(p.get('cost_basis', 0))),
                    realized_pnl=Decimal(str(p.get('realized_pnl', 0))),
                )
                for code, p in portfolio_data.get('positions', {}).items()
            }
            strategy.portfolio = Portfolio(
                portfolio_id=portfolio_data.get('portfolio_id', strategy.portfolio.portfolio_id),
                name=portfolio_data.get('name', '网格组合'),
                cash=Decimal(str(portfolio_data.get('cash', 0))),
                positions=positions,
            )

        return strategy

    def __repr__(self) -> str:
        return (
            f"GridStrategy(id={self.strategy_id}, name={self.name}, "
            f"assets={len(self.asset_codes)}, active={len(self.active_assets())}, "
            f"cash={self.portfolio.cash:.2f})"
        )
