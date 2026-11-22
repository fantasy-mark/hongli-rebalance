"""
交易账本（策略模块 · 共享内核）

核心聚合根: TradeLedger

设计要点（源自真实账本实践）:
1. **追加式（append-only）**：只允许 record()，不允许修改/删除历史流水；
   纠错靠新增一笔相反流水。
2. **流水是唯一真相源**：持仓与各池现金都由流水**重演**得出，
   持仓文件只是派生视图，可随时由账本重建。
3. **双资金池**：每个池独立核算现金；卖出所得回到该笔流水所属的池。
4. **重演即校验**：重演过程中若出现"卖出超过持仓"或"现金为负"，立即报错，
   保证账本自洽（不依赖外部核对）。
"""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.strategy.portfolio import Portfolio
from src.strategy.ledger.trade import CashFlowType, Trade, TradeSide


class LedgerViolation(ValueError):
    """账本自洽性被破坏（卖出超持仓 / 现金不足等）"""


@dataclass(frozen=True)
class PoolBalance:
    """资金池余额（值对象）"""
    pool: CashFlowType
    cash: Decimal
    deposits: Decimal = Decimal("0")     # 累计入金
    withdrawals: Decimal = Decimal("0")  # 累计出金
    dividends: Decimal = Decimal("0")    # 累计分红
    fees: Decimal = Decimal("0")         # 累计费用
    realized_pnl: Decimal = Decimal("0")  # 累计已实现盈亏

    @property
    def net_deposit(self) -> Decimal:
        """净投入 = 入金 - 出金"""
        return self.deposits - self.withdrawals

    def to_dict(self) -> Dict[str, Any]:
        return {
            'pool': self.pool.value,
            'cash': float(self.cash),
            'deposits': float(self.deposits),
            'withdrawals': float(self.withdrawals),
            'dividends': float(self.dividends),
            'fees': float(self.fees),
            'realized_pnl': float(self.realized_pnl),
            'net_deposit': float(self.net_deposit),
        }


@dataclass(frozen=True)
class LedgerSnapshot:
    """
    账本快照（值对象）

    由流水重演得到的某一时点的组合状态：
    - 各池的 Portfolio（现金 + 持仓）
    - 各池的余额统计
    """
    as_of: Optional[date]
    pools: Dict[CashFlowType, Portfolio]
    balances: Dict[CashFlowType, PoolBalance]
    trade_count: int

    def portfolio(self, pool: CashFlowType = CashFlowType.REAL) -> Portfolio:
        """取某池的组合"""
        return self.pools.get(pool, Portfolio(name=pool.label))

    def total_positions(self) -> Dict[str, Decimal]:
        """所有池的持仓合计（同资产跨池合并）"""
        total: Dict[str, Decimal] = {}
        for portfolio in self.pools.values():
            for code, position in portfolio.positions.items():
                total[code] = total.get(code, Decimal("0")) + position.shares
        return {code: shares for code, shares in total.items() if shares > 0}

    def to_dict(self) -> Dict[str, Any]:
        return {
            'as_of': self.as_of.isoformat() if self.as_of else None,
            'trade_count': self.trade_count,
            'balances': {p.value: b.to_dict() for p, b in self.balances.items()},
            'pools': {
                p.value: {
                    'cash': float(portfolio.cash),
                    'positions': {
                        code: float(pos.shares)
                        for code, pos in portfolio.positions.items()
                        if pos.shares > 0
                    },
                }
                for p, portfolio in self.pools.items()
            },
        }


@dataclass
class TradeLedger:
    """
    交易账本（聚合根）

    只追加流水，持仓与现金均由其重演得出。

    不变量:
    - 流水按 (日期, 录入顺序) 单调追加
    - 同一流水号不可重复
    - 任一池内卖出不得超过该池持仓
    """
    trades: List[Trade] = field(default_factory=list)
    # 允许现金为负（费用造成的微小透支）；False 时重演会拒绝
    allow_overdraft: bool = True

    # ------------------------------------------------------------------
    # 追加流水
    # ------------------------------------------------------------------

    def record(self, trade: Trade) -> None:
        """
        追加一笔流水

        Args:
            trade: 交易流水

        Raises:
            ValueError: 流水号重复
        """
        if any(existing.id.value == trade.id.value for existing in self.trades):
            raise ValueError(f"流水号已存在: {trade.id}")

        self.trades.append(trade)

    def record_many(self, trades: List[Trade]) -> None:
        """批量追加（按给定顺序）"""
        for trade in trades:
            self.record(trade)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def trades_of(self, pool: Optional[CashFlowType] = None,
                  asset_code: Optional[str] = None) -> List[Trade]:
        """按池 / 资产筛选流水"""
        result = self.trades
        if pool is not None:
            result = [t for t in result if t.pool is pool]
        if asset_code is not None:
            result = [t for t in result if t.asset_code == asset_code]
        return list(result)

    def last_date(self) -> Optional[date]:
        """最后一笔流水的日期"""
        return max((t.on_date for t in self.trades), default=None)

    # ------------------------------------------------------------------
    # 重演（流水 -> 组合状态）
    # ------------------------------------------------------------------

    def replay(self, as_of: Optional[date] = None) -> LedgerSnapshot:
        """
        重演流水，得到指定时点的组合状态

        Args:
            as_of: 截止日期（含）；None 表示全部流水

        Returns:
            LedgerSnapshot

        Raises:
            LedgerViolation: 出现卖出超持仓等不自洽情况
        """
        portfolios: Dict[CashFlowType, Portfolio] = {
            pool: Portfolio(portfolio_id=pool.value, name=pool.label,
                            allow_overdraft=self.allow_overdraft)
            for pool in CashFlowType
        }
        stats: Dict[CashFlowType, Dict[str, Decimal]] = {
            pool: {
                'deposits': Decimal("0"), 'withdrawals': Decimal("0"),
                'dividends': Decimal("0"), 'fees': Decimal("0"),
                'realized': Decimal("0"),
            }
            for pool in CashFlowType
        }

        applied = 0
        for trade in self._sorted_trades(as_of):
            portfolio = portfolios[trade.pool]
            bucket = stats[trade.pool]
            applied += 1

            if trade.side is TradeSide.BUY:
                self._apply_buy(portfolio, trade)
                bucket['fees'] += trade.fee

            elif trade.side is TradeSide.SELL:
                realized = self._apply_sell(portfolio, trade)
                bucket['fees'] += trade.fee
                bucket['realized'] += realized

            elif trade.side is TradeSide.DEPOSIT:
                portfolio.cash += trade.amount
                bucket['deposits'] += trade.amount

            elif trade.side is TradeSide.WITHDRAW:
                portfolio.cash -= trade.amount
                bucket['withdrawals'] += trade.amount

            elif trade.side is TradeSide.DIVIDEND:
                portfolio.cash += trade.amount
                bucket['dividends'] += trade.amount

        balances = {
            pool: PoolBalance(
                pool=pool,
                cash=portfolios[pool].cash,
                deposits=stats[pool]['deposits'],
                withdrawals=stats[pool]['withdrawals'],
                dividends=stats[pool]['dividends'],
                fees=stats[pool]['fees'],
                realized_pnl=stats[pool]['realized'],
            )
            for pool in CashFlowType
        }

        return LedgerSnapshot(
            as_of=as_of or self.last_date(),
            pools=portfolios,
            balances=balances,
            trade_count=applied,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _sorted_trades(self, as_of: Optional[date]) -> List[Trade]:
        """按日期稳定排序（同日保持录入顺序）"""
        selected = [t for t in self.trades
                    if as_of is None or t.on_date <= as_of]
        return sorted(selected, key=lambda t: t.on_date)

    @staticmethod
    def _apply_buy(portfolio: Portfolio, trade: Trade) -> None:
        try:
            portfolio.buy(trade.asset_code, trade.shares,
                          trade.gross_amount, trade.fee)
        except ValueError as error:
            raise LedgerViolation(
                f"{trade.id} 买入失败：{error}"
            ) from error

    @staticmethod
    def _apply_sell(portfolio: Portfolio, trade: Trade) -> Decimal:
        before = portfolio.position_for(trade.asset_code)
        try:
            after = portfolio.sell(trade.asset_code, trade.shares,
                                   trade.gross_amount, trade.fee)
        except ValueError as error:
            raise LedgerViolation(
                f"{trade.id} 卖出失败（{trade.asset_code}）：{error}"
            ) from error
        # 已实现盈亏增量
        return after.realized_pnl - before.realized_pnl

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def with_resolved_actions(self) -> 'TradeLedger':
        """
        补全缺失的交易类型（返回新账本，不改原对象）

        规则（与网格策略 GridState 的判定一致）：
        - 买入：买入前无持仓 -> 建仓(open)；已有持仓 -> 加仓(add)
        - 卖出：卖出后清零 -> 止盈(take_profit)；仍有持仓 -> 减仓(reduce)

        录入方已指定的交易类型保持不变（他最清楚当时在做什么）。
        """
        from dataclasses import replace

        holdings: Dict[CashFlowType, Dict[str, Decimal]] = {
            pool: {} for pool in CashFlowType
        }
        resolved: List[Trade] = []

        for trade in self._sorted_trades(None):
            if not trade.side.is_security:
                resolved.append(trade)
                continue

            code = trade.asset_code or ""
            pool_holdings = holdings[trade.pool]
            before = pool_holdings.get(code, Decimal("0"))

            if trade.side is TradeSide.BUY:
                pool_holdings[code] = before + trade.shares
            else:
                pool_holdings[code] = before - trade.shares
            after = pool_holdings[code]

            if trade.action:
                resolved.append(trade)
                continue

            if trade.side is TradeSide.BUY:
                action = "open" if before <= 0 else "add"
            else:
                action = "take_profit" if after <= 0 else "reduce"
            resolved.append(replace(trade, action=action))

        # 保持录入顺序（_sorted_trades 只是重演顺序，输出回原顺序）
        order = {id(t): i for i, t in enumerate(self.trades)}
        resolved.sort(key=lambda t: order[id(t)])
        return TradeLedger(trades=resolved, allow_overdraft=self.allow_overdraft)

    def to_dict(self) -> Dict[str, Any]:
        """账本序列化（追加式，保持顺序）"""
        return {
            'schema': 'trade-ledger-v1',
            'trades': [t.to_dict() for t in self.trades],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TradeLedger':
        """
        从字典恢复账本

        兼容旧账本：`deposit_rebalance` / `deposit_grid` 会归一为
        DEPOSIT + 对应资金池。
        """
        from src.strategy.ledger.trade import infer_pool

        trades = []
        for raw in data.get('trades', []):
            trade = Trade.from_dict(raw)
            # 旧格式没有 pool 字段，从方向字符串推断
            if 'pool' not in raw:
                trade = Trade(
                    id=trade.id, on_date=trade.on_date, side=trade.side,
                    pool=infer_pool(raw.get('side', '')),
                    asset_code=trade.asset_code, shares=trade.shares,
                    price=trade.price, amount=trade.amount, fee=trade.fee,
                    note=trade.note, recorded_at=trade.recorded_at,
                )
            trades.append(trade)

        return cls(trades=trades)

    def validate(self) -> List[str]:
        """校验账本自洽性"""
        errors: List[str] = []
        seen = set()
        for trade in self.trades:
            if trade.id.value in seen:
                errors.append(f"流水号重复: {trade.id}")
            seen.add(trade.id.value)

        try:
            self.replay()
        except LedgerViolation as error:
            errors.append(str(error))

        return errors

    def __len__(self) -> int:
        return len(self.trades)

    def __repr__(self) -> str:
        return f"TradeLedger(trades={len(self.trades)}, last={self.last_date()})"
