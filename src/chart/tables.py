"""
表格数据（图表模块）

把回测结果整理成可直接展示的纯文本表格：

- grid_trigger_log  网格触发日志（资产 / 日期 / 交易类型 / 档位）
- grid_trade_table   网格成交明细（按资产分组 + 盈亏分解）
- format_levels      档位文本格式化（连续区间用 1-5，否则用 1,3,5）

不依赖 matplotlib，可在控制台、CLI 报告、日志中直接使用。
"""

from typing import Dict, List


def format_levels(levels) -> str:
    """档位文本：连续区间用 1-5，否则用 1,3,5"""
    levels = sorted(levels)
    if not levels:
        return "-"
    if len(levels) == 1:
        return str(levels[0])
    if levels == list(range(levels[0], levels[-1] + 1)):
        return f"{levels[0]}-{levels[-1]}"
    return ",".join(str(x) for x in levels)


def grid_trigger_log(grid_result) -> str:
    """
    生成网格触发日志

    展示每个触发网格的资产：代码、名称、触发日期、交易类型
    （建仓 / 加仓 / 止盈）

    领域层保证「一轮网格 = 1 条建仓 + N 条加仓 + 1 条止盈」，
    因此这里无需再做止盈聚合。

    Args:
        grid_result: 网格回测结果

    Returns:
        可直接打印的文本
    """
    names = grid_result.config.asset_names

    lines = [
        "网格触发日志（资产代码 / 名称 / 触发日期 / 交易类型）",
        "=" * 104,
        f"{'触发日期':<12}| {'资产代码':<9}| {'资产名称':<10}| {'交易类型':<8}| "
        f"{'轮次':<5}| {'档位':<7}| {'股数':>8}| {'价格':>9}| {'金额':>11}",
        "-" * 104,
    ]

    for trade in grid_result.trades:
        level_txt = str(trade.level or '-')
        price = trade.price
        shares = trade.shares
        amount = trade.amount

        lines.append(
            f"{str(trade.date):<12}| {trade.asset_code:<9}| "
            f"{names.get(trade.asset_code, trade.asset_code):<10}| "
            f"{trade.type_label:<8}| {trade.round_no:<5}| "
            f"{level_txt:<7}| {float(shares):>8,.0f}| "
            f"{float(price):>9.4f}| {float(amount):>11,.0f}"
        )

    # ---- 网格轮次概览 ----
    if grid_result.round_events:
        lines.append("-" * 104)
        lines.append("网格轮次（从跌破前N日最低价，到本轮完全止盈）:")
        for event in grid_result.round_events:
            name = names.get(event.asset_code, event.asset_code)
            if event.closed:
                period = f"{event.start_date} ~ {event.end_date}"
                outcome = "已完全止盈"
            else:
                period = f"{event.start_date} ~ 进行中"
                outcome = "持仓中"
            lines.append(
                f"  第{event.round_no}轮 {event.asset_code}({name}) "
                f"{period} 档位{event.levels} {outcome}"
            )

    if not grid_result.trades:
        lines.append("（本次回测无网格成交）")

    return "\n".join(lines)

def grid_trade_table(grid_result) -> str:
    """
    生成网格成交明细表（按资产分组）

    用于在控制台/报告中直观看到每个资产的买卖情况与盈亏归因
    """
    names = grid_result.config.asset_names
    df = grid_result.daily_values

    lines = [
        "网格成交明细（按资产分组）",
        "=" * 96,
        "日期        | 资产         | 代码   | 方向 | 档位 | 价格      | 金额",
        "-" * 96,
    ]

    by_asset: Dict[str, List] = {}
    for trade in grid_result.trades:
        by_asset.setdefault(trade.asset_code, []).append(trade)

    for code in sorted(by_asset):
        trades = sorted(by_asset[code], key=lambda t: (t.date, t.level or 0))
        name = names.get(code, code)

        for trade in trades:
            lines.append(
                f"{str(trade.date):<12}| {name:<13}| {code:<7}| "
                f"{trade.type_label:<4}| "
                f"{str(trade.level or '-'):<4}| "
                f"{float(trade.price):<10.4f}| {float(trade.amount):>10,.0f}"
            )

        # ---- 该资产的盈亏分解 ----
        buy_amt = sum(float(t.amount) for t in trades if t.side == 'buy')
        sell_amt = sum(float(t.amount) for t in trades if t.side == 'sell')

        # 已实现盈亏：卖出金额 - 对应买入成本 - 卖出成本
        buy_cost_by_level = {
            t.level: float(t.amount) for t in trades if t.side == 'buy'
        }
        realized = 0.0
        for t in trades:
            if t.side != 'sell':
                continue
            cost = buy_cost_by_level.get(t.level, 0.0)
            realized += float(t.amount) - cost - float(t.cost)

        # 浮动盈亏：当前持仓市值 - 持仓成本
        unrealized = 0.0
        position = grid_result.strategy.portfolio.position_for(code)
        if position.shares > 0:
            close_col = f'{code}_close'
            if close_col in df.columns:
                last_close = df[close_col].iloc[-1]
                if last_close == last_close:  # 非 NaN
                    unrealized = (float(position.shares) * float(last_close)
                          - float(position.cost_basis))

        lines.append(
            f"{'小计':<12}| {name:<13}| {code:<7}| "
            f"买{len([t for t in trades if t.side == 'buy'])}"
            f"卖{len([t for t in trades if t.side == 'sell'])} | "
            f"    | 买入 {buy_amt:>9,.0f}  卖出 {sell_amt:>9,.0f}"
        )
        lines.append(
            f"{'':<12}| {'  已实现盈亏':<13}| {'':<7}| "
            f"{'':<4}| {'':<4}| {realized:>+10,.0f} | 浮动盈亏 {unrealized:>+10,.0f}"
        )
        lines.append("-" * 96)

    if not by_asset:
        lines.append("（本次回测无网格成交）")

    return "\n".join(lines)
