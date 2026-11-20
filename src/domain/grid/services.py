"""
网格策略领域服务

GridEngine: 网格引擎（纯业务规则，无外部依赖）

业务规则:
1. 监控启动: 当日价格 < 前 N 个交易日最低价（不含当日）-> 开启网格轮次
2. 网格步长: step = ATR(触发日, atr_period) * step_ratio（固定，不随行情变化）
3. 档位推进: 档位N价格 = 锚点 - step * N，价格 <= 档位价格时触发买入
4. 单格买入: buy_amount 元，股数向下取整到 lot_size 整数倍
5. 资金隔离: 仅使用网格自己的资金池，不影响再平衡持仓
6. 轮次结束: 本轮持仓全部止盈卖出后，本轮结束；
   必须**重新跌破前 N 日最低价**才能开启下一轮（不自动重启）

价格口径（GridPriceBasis）:
- CLOSE: 以收盘价判定触发与成交（保守）
- LOW:   以当日最低价判定触发，按限价单在档位价格成交（跳空时按开盘价成交）
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import List

from src.domain.grid.entities import (
    GridState,
    GridStatus,
    GridStrategy,
    GridStrategyStatus,
    floor_to_lot,
)
from src.domain.grid.value_objects import (
    GridAction,
    GridAnchor,
    GridDecision,
    GridMarketSnapshot,
    GridOrder,
    GridOrderSide,
    GridPriceBasis,
)


class GridEngine:
    """
    网格引擎（领域服务）

    职责:
    - 判断是否跌破前 N 日最低价
    - 激活网格并建立锚点与步长
    - 推进网格档位并生成买入订单
    - 处理网格止盈卖出
    """

    @staticmethod
    def evaluate(strategy: GridStrategy,
                 asset_code: str,
                 snapshot: GridMarketSnapshot) -> GridDecision:
        """
        评估某资产在某交易日的网格状态并推进网格

        该方法是网格策略的核心入口，会修改 strategy 内部状态
        （网格状态、档位推进、成交记录、现金变动）

        Args:
            strategy: 网格策略（聚合根）
            asset_code: 资产代码
            snapshot: 当日行情快照

        Returns:
            GridDecision 决策结果（含产生的成交）
        """
        state = strategy.state_for(asset_code)
        state.last_price = snapshot.close

        # 策略未启用时不推进网格
        if strategy.status != GridStrategyStatus.ACTIVE:
            state.last_action = GridAction.HOLD
            return GridDecision(
                action=GridAction.HOLD,
                asset_code=asset_code,
                date=snapshot.date,
                reason=f"策略状态为{strategy.status.value}，不推进网格",
            )

        # 尚未开始监控 -> 开始监控
        if state.status == GridStatus.IDLE:
            state.start_monitoring(snapshot.date)
            state.last_action = GridAction.MONITOR

        # 上一轮已完全止盈 -> 重新武装监控，等待再次跌破前 N 日最低价
        if state.status == GridStatus.COMPLETED:
            state.rearm()

        # 监控中：等待跌破前 N 日最低价
        if state.status == GridStatus.MONITORING:
            if snapshot.prior_low is None:
                state.last_action = GridAction.MONITOR
                return GridDecision(
                    action=GridAction.MONITOR,
                    asset_code=asset_code,
                    date=snapshot.date,
                    reason="历史数据不足，无法计算前N日最低价",
                )

            basis_price = GridEngine._basis_price(strategy.trigger, snapshot)

            if basis_price >= snapshot.prior_low:
                state.last_action = GridAction.MONITOR
                return GridDecision(
                    action=GridAction.MONITOR,
                    asset_code=asset_code,
                    date=snapshot.date,
                    reason=(
                        f"价格{basis_price:.4f}未跌破前"
                        f"{strategy.trigger.lookback_days}日最低价{snapshot.prior_low:.4f}"
                    ),
                )

            # ATR 不可用时无法确定网格步长，继续监控
            if snapshot.atr is None or snapshot.atr <= 0:
                state.last_action = GridAction.MONITOR
                return GridDecision(
                    action=GridAction.MONITOR,
                    asset_code=asset_code,
                    date=snapshot.date,
                    reason="ATR不可用，无法确定网格步长，继续监控",
                )

            # 激活网格
            anchor = GridEngine._resolve_anchor(strategy, state, snapshot)
            step = snapshot.atr * strategy.trigger.step_ratio
            state.activate(anchor, snapshot.atr, snapshot.date, step)
            state.last_action = GridAction.ACTIVATE

            activate_decision = GridDecision(
                action=GridAction.ACTIVATE,
                asset_code=asset_code,
                date=snapshot.date,
                reason=(
                    f"价格{basis_price:.4f}跌破前"
                    f"{strategy.trigger.lookback_days}日最低价{snapshot.prior_low:.4f}，"
                    f"网格激活：锚点{anchor:.4f}，ATR({strategy.trigger.atr_period})"
                    f"={snapshot.atr:.4f}，步长={step:.4f}"
                ),
                anchor_price=anchor,
                step=step,
            )

            # 激活当日可能已经跌破第一档，继续处理档位
            orders = GridEngine._process_levels(strategy, state, snapshot)
            if orders:
                activate_decision = GridDecision(
                    action=GridAction.BUY,
                    asset_code=asset_code,
                    date=snapshot.date,
                    reason=activate_decision.reason,
                    orders=tuple(orders),
                    anchor_price=anchor,
                    step=step,
                    level=state.filled_levels[-1] if state.filled_levels else None,
                )
                state.last_action = GridAction.BUY

            return activate_decision

        # 已激活：推进网格
        orders = GridEngine._process_levels(strategy, state, snapshot)

        # ---- 本轮是否完全止盈？----
        round_closed = False
        if (strategy.portfolio.shares_of(asset_code) <= 0
                and not state.tranches and state.filled_levels):
            state.complete_round(snapshot.date)
            round_closed = True

        if orders:
            if round_closed:
                # 本轮完全止盈：动作标记为 CLOSED，成交明细仍在 orders 中
                action = GridAction.CLOSED
                reason = (
                    f"本轮网格已完全止盈（第{state.round_no}轮），"
                    f"等待重新跌破前{strategy.trigger.lookback_days}日最低价开启下一轮"
                )
            else:
                action = GridAction.BUY if any(
                    o.side == GridOrderSide.BUY for o in orders
                ) else GridAction.SELL
                reason = f"触发{len(orders)}笔网格成交"

            state.last_action = action
            return GridDecision(
                action=action,
                asset_code=asset_code,
                date=snapshot.date,
                reason=reason,
                orders=tuple(orders),
                anchor_price=state.anchor_price,
                step=state.step,
                level=state.filled_levels[-1] if state.filled_levels else None,
                round_closed=round_closed,
            )

        if round_closed:
            state.last_action = GridAction.CLOSED
            return GridDecision(
                action=GridAction.CLOSED,
                asset_code=asset_code,
                date=snapshot.date,
                reason=(
                    f"本轮网格已完全止盈（第{state.round_no}轮），"
                    f"等待重新跌破前{strategy.trigger.lookback_days}日最低价开启下一轮"
                ),
                round_closed=True,
            )

        state.last_action = GridAction.HOLD
        return GridDecision(
            action=GridAction.HOLD,
            asset_code=asset_code,
            date=snapshot.date,
            reason="未触发任何网格档位",
            anchor_price=state.anchor_price,
            step=state.step,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @staticmethod
    def _basis_price(trigger, snapshot: GridMarketSnapshot) -> Decimal:
        """
        返回用于触发判定的价格

        CLOSE 口径: 收盘价
        LOW   口径: 当日最低价（缺失时退化为收盘价）
        """
        if trigger.price_basis == GridPriceBasis.LOW:
            if snapshot.low is not None:
                return snapshot.low
        return snapshot.close

    @staticmethod
    def _resolve_anchor(strategy: GridStrategy,
                        state: GridState,
                        snapshot: GridMarketSnapshot) -> Decimal:
        """
        确定网格锚点

        TRIGGER_PRICE: 以触发日价格（按口径）为锚（默认）
        PRIOR_LOW:     以被跌破的前N日最低价为锚
        """
        if strategy.trigger.anchor == GridAnchor.PRIOR_LOW:
            assert snapshot.prior_low is not None
            return snapshot.prior_low

        return GridEngine._basis_price(strategy.trigger, snapshot)

    @staticmethod
    def _process_levels(strategy: GridStrategy,
                        state: GridState,
                        snapshot: GridMarketSnapshot) -> List[GridOrder]:
        """
        推进网格档位，生成买入/卖出订单

        买入规则:
        - 收盘价 <= 档位价格 时触发
        - 买入金额 = trigger.buy_amount，股数向下取整到 lot_size
        - 资金不足时跳过该档位（记录到 skipped_levels）

        卖出规则（可选）:
        - 触发 take_profit_ratio 时，按批次止盈卖出
        """
        orders: List[GridOrder] = []
        trigger = strategy.trigger

        # ---- 买入档位推进 ----
        while True:
            if trigger.max_levels is not None and state.next_level > trigger.max_levels:
                break

            level_price = state.level_price(state.next_level)

            # 价格尚未跌到该档位
            if GridEngine._basis_price(trigger, snapshot) > level_price:
                break

            # 成交价：CLOSE 口径按收盘价，LOW 口径按限价单档位价（跳空则按开盘价）
            fill_price = GridEngine._fill_price(trigger, snapshot, level_price)

            # 计算可买股数
            raw_shares = trigger.buy_amount / fill_price
            shares = floor_to_lot(raw_shares, trigger.lot_size)

            if shares <= 0:
                # 资金不足以买入最小交易单位，跳过该档
                state.skip_level(state.next_level)
                state.next_level += 1
                continue

            amount = shares * fill_price
            cost = strategy.cost_model.calculate_cost(amount)

            # 网格资金隔离：仅消耗网格资金池
            if strategy.portfolio.cash < amount + cost:
                state.skip_level(state.next_level)
                state.next_level += 1
                continue

            order = GridOrder(
                asset_code=state.asset_code,
                side=GridOrderSide.BUY,
                price=fill_price,
                shares=shares,
                amount=amount,
                cost=cost,
                date=snapshot.date,
                level=state.next_level,
                order_id=f"grid-{state.asset_code}-{snapshot.date}-{state.next_level}",
            )

            # 持仓与现金由组合记账（与再平衡组合完全隔离）
            was_flat = strategy.portfolio.is_flat(order.asset_code)
            strategy.portfolio.buy(order.asset_code, order.shares, amount, cost)
            # 轮次维度：批次与档位由网格状态维护，并判定交易类型
            order = replace(order,
                            trade_type=state.record_buy(order, was_flat),
                            shares_after=strategy.portfolio.shares_of(order.asset_code))
            orders.append(order)

            state.next_level += 1

        # ---- 止盈卖出 ----
        if trigger.take_profit_ratio is not None and state.step is not None:
            orders.extend(GridEngine._process_take_profit(strategy, state, snapshot))

        return orders

    @staticmethod
    def _fill_price(trigger, snapshot: GridMarketSnapshot, level_price: Decimal) -> Decimal:
        """
        计算买入成交价

        CLOSE 口径: 当日收盘价
        LOW 口径:   档位限价（若当日开盘已跳空低于档位价，则按开盘价成交）
        """
        if trigger.price_basis == GridPriceBasis.LOW:
            if snapshot.open is not None and snapshot.open < level_price:
                return snapshot.open
            return level_price

        return snapshot.close

    @staticmethod
    def _process_take_profit(strategy: GridStrategy,
                              state: GridState,
                              snapshot: GridMarketSnapshot) -> List[GridOrder]:
        """
        处理网格止盈（整轮一次性清仓）

        止盈价 = 开仓价（该轮首笔买入价）+ 步长 × take_profit_ratio
        
        触发后**一次性卖出本轮全部持仓**，合并为**一条**止盈成交：
        - LOW 口径：各批次按自己的止盈限价成交（限价卖单撮合），
                    合并后取加权平均成交价
        - CLOSE 口径：全部按当日收盘价成交
        
        因此一轮网格的成交序列恒为：
            1 条建仓 + N 条加仓 + 1 条止盈
        不产生减仓（REDUCE）。
        
        T+1 规则: 当日买入的批次当日不可卖出（A 股限制）
        """
        orders: List[GridOrder] = []
        trigger = strategy.trigger

        if trigger.take_profit_ratio is None or state.step is None:
            return orders

        # T+1：仅可卖出非当日买入的批次
        sellable = [t for t in state.tranches if t.date < snapshot.date]
        if not sellable:
            return orders

        # 开仓批次（本轮首笔买入）作为止盈基准
        opening = min(sellable, key=lambda t: (t.date, t.level))
        target_price = opening.price + state.step * trigger.take_profit_ratio

        # 触发判定
        if trigger.price_basis == GridPriceBasis.LOW:
            # 限价卖单：当日最高价触及目标价即成交
            if snapshot.high is None or snapshot.high < target_price:
                return orders
        else:
            if snapshot.close < target_price:
                return orders

        # ---- 汇总各批次的成交金额，合并为一条止盈成交 ----
        total_shares = Decimal("0")
        total_amount = Decimal("0")
        total_cost = Decimal("0")
        total_outlay = Decimal("0")

        for tranche in sellable:
            if trigger.price_basis == GridPriceBasis.LOW:
                # 各批次按自己的止盈限价成交
                fill_price = tranche.price + state.step * trigger.take_profit_ratio
            else:
                fill_price = snapshot.close

            total_shares += tranche.shares
            total_amount += tranche.shares * fill_price
            total_cost += strategy.cost_model.calculate_cost(tranche.shares * fill_price)
            total_outlay += tranche.total_outlay

        if total_shares <= 0:
            return orders

        # 合并成交价 = 加权平均成交价
        avg_price = total_amount / total_shares

        order = GridOrder(
            asset_code=state.asset_code,
            side=GridOrderSide.SELL,
            price=avg_price,
            shares=total_shares,
            amount=total_amount,
            cost=total_cost,
            date=snapshot.date,
            level=None,  # 整轮清仓：不绑定单个档位
            order_id=f"grid-tp-{state.asset_code}-{snapshot.date}",
        )

        # 按批次精确释放持仓成本（网格的批次成本是精确值）
        strategy.portfolio.sell(order.asset_code, total_shares,
                                total_amount, total_cost,
                                cost_basis_released=total_outlay)
        # 轮次维度：移除本轮全部批次
        order = replace(order,
                        trade_type=state.record_sell(order),
                        shares_after=strategy.portfolio.shares_of(order.asset_code))
        orders.append(order)

        return orders

    @staticmethod
    def simulate(strategy: GridStrategy,
                 asset_code: str,
                 snapshot: GridMarketSnapshot) -> GridDecision:
        """
        模拟评估（不修改状态）

        用于查询"如果今天执行会怎样"，例如实盘信号提示
        """
        import copy

        temp = copy.deepcopy(strategy)
        return GridEngine.evaluate(temp, asset_code, snapshot)
