"""
交易流水（策略模块 · 共享内核）

核心实体: Trade（一笔账务流水）
值对象:   TradeSide / CashFlowType / TradeId

设计要点（源自真实账本实践）:
1. **追加式记账（append-only）**
   Trade 是现金与持仓的**唯一真相源**；持仓状态由流水重演得出，
   不做"就地修改"。纠错通过新增一笔相反流水完成，历史不可篡改。
2. **出入金与买卖分离**
   入金/出金改变现金但不改变持仓；买卖改变持仓也可能改变现金。
   用 `TradeSide` 区分，避免"把入金当成买入 0 股"这类歧义。
3. **双资金池**
   同一笔流水归属某个资金池（`pool`），例如"真实交易池"与"网格虚拟预算池"。
   各池现金独立核算，互不挪用。
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional
import uuid


class TradeSide(Enum):
    """
    流水方向

    - BUY:             买入证券（减现金、加持仓）
    - SELL:            卖出证券（加现金、减持仓）
    - DEPOSIT:         入金（加现金，不加持仓）
    - WITHDRAW:        出金（减现金，不减持仓）
    - DIVIDEND:        分红派息（加现金，不加持仓）
    """
    BUY = "buy"
    SELL = "sell"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    DIVIDEND = "dividend"

    @property
    def label(self) -> str:
        return {
            TradeSide.BUY: "买入",
            TradeSide.SELL: "卖出",
            TradeSide.DEPOSIT: "入金",
            TradeSide.WITHDRAW: "出金",
            TradeSide.DIVIDEND: "分红",
        }[self]

    @property
    def is_security(self) -> bool:
        """是否为证券买卖（需要 code / shares / price）"""
        return self in (TradeSide.BUY, TradeSide.SELL)

    @property
    def is_cash_flow(self) -> bool:
        """是否为纯现金流水（出入金/分红，不需要证券字段）"""
        return not self.is_security


class CashFlowType(Enum):
    """资金池类型（双池隔离）"""
    REAL = "real"        # 真实现金池（实盘交易用）
    GRID = "grid"        # 网格虚拟预算池（网格记账用，不代表真实现金）

    @property
    def label(self) -> str:
        return {CashFlowType.REAL: "真实资金", CashFlowType.GRID: "网格预算"}[self]


class TradeAction(Enum):
    """
    交易类型（业务语义）

    与 `TradeSide`（事实方向）区分：
    - TradeSide   客观事实：买了 / 卖了
    - TradeAction 业务语义：建仓 / 加仓 / 减仓 / 止盈

    由录入方指定（他最清楚当时在做什么），
    页面表格与录入表单使用同一套取值。
    """

    OPEN = "open"                # 建仓
    ADD = "add"                  # 加仓
    REDUCE = "reduce"            # 减仓
    TAKE_PROFIT = "take_profit"  # 止盈

    @property
    def label(self) -> str:
        return {
            TradeAction.OPEN: "建仓",
            TradeAction.ADD: "加仓",
            TradeAction.REDUCE: "减仓",
            TradeAction.TAKE_PROFIT: "止盈",
        }[self]

    @property
    def side(self) -> TradeSide:
        """该类型对应的客观方向"""
        return (TradeSide.BUY if self in (TradeAction.OPEN, TradeAction.ADD)
                else TradeSide.SELL)

    @classmethod
    def from_label(cls, label: str) -> 'TradeAction':
        """按中文标签或英文值解析"""
        for action in cls:
            if action.value == label or action.label == label:
                return action
        raise ValueError(f"未知的交易类型: {label}")


@dataclass(frozen=True)
class TradeId:
    """
    流水号（值对象）

    约定: {日期}-{方向}-{序号}，如 `2026-10-19-sell-002`
    保证唯一性，便于对账与追溯。
    """
    value: str

    def __post_init__(self):
        if not self.value or not self.value.strip():
            raise ValueError("流水号不能为空")

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def build(on_date: date, side: TradeSide, sequence: int) -> 'TradeId':
        """按约定生成流水号"""
        if sequence < 1:
            raise ValueError(f"序号必须>=1: {sequence}")
        return TradeId(f"{on_date.isoformat()}-{side.value}-{sequence:03d}")


@dataclass(frozen=True)
class Trade:
    """
    账务流水（值对象，不可变）

    不变量:
    - 日期必须有值
    - 证券买卖必须有 asset_code / shares > 0 / price >= 0
    - 纯现金流水 amount 必须非零
    - 费用不能为负

    金额口径:
    - BUY  : amount = shares * price（成交金额，不含费用）
    - SELL : amount = shares * price
    - 其它 : amount 为现金变动额（入金/出金/分红）

    交易类型:
    `action` 记录业务语义（建仓/加仓/减仓/止盈），由录入方指定；
    缺省时按重演推导（买入前无持仓=建仓，卖出后清零=止盈）。
    """
    id: TradeId
    on_date: date
    side: TradeSide
    pool: CashFlowType = CashFlowType.REAL
    asset_code: Optional[str] = None
    shares: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    action: Optional[str] = None   # 交易类型：open/add/reduce/take_profit
    note: str = ""
    recorded_at: Optional[datetime] = None

    def __post_init__(self):
        if not isinstance(self.side, TradeSide):
            raise ValueError(f"非法流水方向: {self.side}")
        if self.fee < 0:
            raise ValueError(f"费用不能为负: {self.fee}")

        # 交易类型与方向必须一致（建仓/加仓=买入，减仓/止盈=卖出）
        if self.action:
            parsed = TradeAction.from_label(self.action)
            if parsed.side is not self.side:
                raise ValueError(
                    f"交易类型 {parsed.label} 与方向 {self.side.label} 不一致")
            object.__setattr__(self, 'action', parsed.value)

        if self.side.is_security:
            if not self.asset_code:
                raise ValueError(f"{self.side.label}必须指定资产代码")
            if self.shares <= 0:
                raise ValueError(f"{self.side.label}数量必须>0: {self.shares}")
            if self.price < 0:
                raise ValueError(f"成交价不能为负: {self.price}")
            # 派生成交金额，避免与 shares*price 不一致
            expected = self.shares * self.price
            if self.amount == 0:
                object.__setattr__(self, 'amount', expected)
        else:
            if self.amount == 0:
                raise ValueError(f"{self.side.label}金额不能为0")

    # ------------------------------------------------------------------
    # 推导
    # ------------------------------------------------------------------

    @property
    def gross_amount(self) -> Decimal:
        """证券买卖成交金额"""
        return self.shares * self.price

    @property
    def cash_delta(self) -> Decimal:
        """
        对现金的影响（正为流入）

        - 买入: -(成交金额 + 费用)
        - 卖出: +(成交金额 - 费用)
        - 入金: +金额
        - 出金: -金额
        - 分红: +金额
        """
        if self.side is TradeSide.BUY:
            return -(self.gross_amount + self.fee)
        if self.side is TradeSide.SELL:
            return self.gross_amount - self.fee
        if self.side is TradeSide.DEPOSIT:
            return self.amount
        if self.side is TradeSide.WITHDRAW:
            return -self.amount
        if self.side is TradeSide.DIVIDEND:
            return self.amount
        return Decimal("0")

    @property
    def shares_delta(self) -> Decimal:
        """对持仓数量的影响（正为增加）"""
        if self.side is TradeSide.BUY:
            return self.shares
        if self.side is TradeSide.SELL:
            return -self.shares
        return Decimal("0")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（便于落盘为 JSON 账本）"""
        data = {
            'id': str(self.id),
            'date': self.on_date.isoformat(),
            'side': self.side.value,
            'pool': self.pool.value,
            'fee': float(self.fee),
            'note': self.note,
        }
        if self.action:
            data['action'] = self.action
        if self.side.is_security:
            data.update({
                'code': self.asset_code,
                'shares': float(self.shares),
                'price': float(self.price),
            })
        else:
            data['amount'] = float(self.amount)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trade':
        """从字典创建（兼容旧账本的纯字符串方向）"""
        side = _parse_side(data['side'], data.get('pool'))
        pool = CashFlowType(data.get('pool', CashFlowType.REAL.value))

        return cls(
            id=TradeId(data.get('id') or str(uuid.uuid4())),
            on_date=date.fromisoformat(data['date']),
            side=side,
            pool=pool,
            asset_code=data.get('code'),
            shares=Decimal(str(data.get('shares', 0))),
            price=Decimal(str(data.get('price', 0))),
            amount=Decimal(str(data.get('amount', 0))),
            fee=Decimal(str(data.get('fee', 0))),
            action=data.get('action'),
            note=data.get('note', ''),
            recorded_at=(datetime.fromisoformat(data['recorded_at'])
                         if data.get('recorded_at') else None),
        )

    def __repr__(self) -> str:
        if self.side.is_security:
            return (f"Trade({self.id}, {self.side.value} {self.asset_code} "
                    f"{self.shares}@{self.price})")
        return f"Trade({self.id}, {self.side.value} {self.amount})"


def _parse_side(raw: str, pool: Optional[str]) -> TradeSide:
    """
    解析方向，兼容旧账本写法

    旧账本用 `deposit_rebalance` / `deposit_grid` 表示"入金到某个池"，
    这里归一为 TradeSide.DEPOSIT + pool 字段。
    """
    raw = (raw or "").strip().lower()

    if raw.startswith('deposit'):
        return TradeSide.DEPOSIT
    if raw.startswith('withdraw'):
        return TradeSide.WITHDRAW
    if raw.startswith('dividend'):
        return TradeSide.DIVIDEND

    try:
        return TradeSide(raw)
    except ValueError:
        raise ValueError(f"无法识别的流水方向: {raw}")


def infer_pool(raw_side: str) -> CashFlowType:
    """从旧账本的方向字符串推断资金池"""
    raw = (raw_side or "").lower()
    return CashFlowType.GRID if 'grid' in raw else CashFlowType.REAL
