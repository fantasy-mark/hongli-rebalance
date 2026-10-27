"""回测: 网格出场时「只卖出成本金额」(现行规则) vs「全部卖出」(旧规则)。

现行规则 (src/simulation/grid_simulator.py run_compound_grid + src/domain/grid/exit_policy.py
cost_only_exit): 出场时只卖
`total_invested / price` 份, 恰好取回本金 -> 已实现盈亏恒为 0; 剩余
`total_shares - total_invested/price` 份**归并到再平衡的持仓**
(merged_shares), 此后按组合的规则参与权重计算 —— 网格实质上变成了
「买入持有」。

对照 exit_mode="all": 旧规则, 出场时卖出**全部**份额, 把利润一次性
落袋变成闲置现金, 不再产生收益。留着这个口径是为了回答「换规则到底
赚了多少」—— 它不是候选方案。

WHY A RE-IMPLEMENTATION INSTEAD OF A PARAMETER:
`run_compound_grid` 的返回值不暴露「跨周期累计的归并份额」和
「逐日资金占用」, 而这两个都是本次比较必需的量。这里重写一遍循环,
并用 `--selftest` 断言 exit_mode="cost" 模式下 cum_realized / 周期数 /
入场笔数 / 累计投入 / 归并份额 与 run_compound_grid 逐项相等 ——
重实现若与生产逻辑漂移, 这个断言会先炸。

口径与 data/grid_chart.png 的「网格 ROI」完全一致 (src/infrastructure/chart_renderer.py):
    网格 ROI   = (Σ已实现 + Σ浮盈 + Σ归并份额市值) / 峰值资金占用 × 100
    资金占用   = 网格**在周期内**的层 (出场后本金收回, 占用回落)
另加一个更诚实的口径 (本金反复周转时分母会很大):
    本金收益率 = 期末总盈亏 / 期间累计投入本金 × 100

Run:
    python scripts/compare_grid_exit_mode.py            # 默认离线, 读快照
    python scripts/compare_grid_exit_mode.py --selftest # 只跑一致性校验
    python scripts/compare_grid_exit_mode.py --fetch    # 联网重取价格
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.ERROR)

import pandas as pd

from src.simulation import grid_simulator as grid_mod
PRICE_SNAPSHOT = ROOT / "data" / "grid_prices_snapshot.json"


def load_prices(fetch: bool) -> dict[str, pd.Series]:
    """价格序列, 优先用离线快照 (与 grid_chart.png / ops 同源)。"""
    if not fetch and PRICE_SNAPSHOT.exists():
        raw = json.loads(PRICE_SNAPSHOT.read_text(encoding="utf-8"))
        return {
            name: pd.Series({pd.Timestamp(d): float(p) for d, p in series.items()}).sort_index()
            for name, series in raw.items()
        }
    from src.infrastructure.chart_renderer import _fetch_asset_window, _load_chart_assets
    return _fetch_asset_window(_load_chart_assets())


def replay(prices: pd.Series, exit_mode: str,
           pos_yuan: float = None,
           step_pct: float = None, tp_pct: float = None,
           tp_basis: str = None, lookback: int = None) -> dict:
    """run_compound_grid 的忠实重写 + exit_mode 开关。

    exit_mode="cost" — 与 run_compound_grid 逐位一致 (由 --selftest 断言);
                      出场只回本, 剩余份额归并到再平衡持仓
    exit_mode="all"  — 旧规则, 止盈卖光 (留作对照口径)
    """
    pos_yuan = grid_mod.GRID_BASE_YUAN if pos_yuan is None else pos_yuan
    step_pct = grid_mod.GRID_STEP_PCT if step_pct is None else step_pct
    tp_pct = grid_mod.GRID_TP_PCT if tp_pct is None else tp_pct
    tp_basis = grid_mod.GRID_TP_BASIS if tp_basis is None else tp_basis
    lookback = grid_mod.GRID_LOOKBACK if lookback is None else lookback

    in_cycle = False
    entries: list[dict] = []
    cycles: list[dict] = []
    cum_realized = 0.0      # 变体下恒为 0
    cum_invested = 0.0      # 累计投入本金 (每次加仓 +pos_yuan)
    merged_shares = 0.0       # 变体专属: 累计归并到再平衡的份额
    n_buys = 0
    history = []
    s = prices.sort_index()

    for i, (date, price) in enumerate(s.items()):
        if not in_cycle:
            if i > 0:
                past = s.iloc[max(0, i - lookback):i]
                if len(past) >= 1 and price < past.min():
                    entries.append({"date": date, "price": float(price),
                                    "shares": pos_yuan / price})
                    in_cycle = True
                    cum_invested += pos_yuan
                    n_buys += 1
        else:
            last_price = entries[-1]["price"]
            if price <= last_price * (1 - step_pct / 100):
                entries.append({"date": date, "price": float(price),
                                "shares": pos_yuan / price})
                cum_invested += pos_yuan
                n_buys += 1
            total_shares = sum(e["shares"] for e in entries)
            total_invested = sum(e["shares"] * e["price"] for e in entries)
            if tp_basis == "first_buy" and entries:
                tp_target = entries[0]["price"] * (1 + tp_pct / 100)
            else:
                avg = total_invested / total_shares if total_shares else 0.0
                tp_target = avg * (1 + tp_pct / 100)
            if price >= tp_target:
                if exit_mode == "all":
                    exit_value = total_shares * price
                    pnl = exit_value - total_invested
                    cum_realized += pnl
                    cycles.append({
                        "entries": entries, "exit_date": date,
                        "exit_price": float(price), "num_buys": len(entries),
                        "total_invested": total_invested, "exit_value": exit_value,
                        "pnl": pnl, "merged_gained": 0.0,
                    })
                else:  # exit_mode == "cost"
                    sold = total_invested / price      # 恰好回本
                    leftover = total_shares - sold     # 归并到再平衡持仓
                    merged_shares += leftover
                    cycles.append({
                        "entries": entries, "exit_date": date,
                        "exit_price": float(price), "num_buys": len(entries),
                        "total_invested": total_invested, "exit_value": sold * price,
                        "pnl": 0.0, "merged_gained": leftover,
                    })
                in_cycle = False
                entries = []

        if in_cycle:
            total_shares = sum(e["shares"] for e in entries)
            total_invested = sum(e["shares"] * e["price"] for e in entries)
            current_value = total_shares * price
            unrealized = current_value - total_invested
        else:
            current_value = 0.0
            unrealized = 0.0

        merged_value = merged_shares * price
        # 资金占用 = 网格**在周期内**的层。出场时本金全额收回, 所以占用会
        # 回落 —— 归并出去的那批份额归再平衡组合管, 不占网格的钱。
        capital = current_value
        history.append({
            "date": date, "in_cycle": in_cycle,
            "capital": capital, "cum_realized": cum_realized,
            "unrealized": unrealized, "merged_value": merged_value,
            "total_pnl": cum_realized + unrealized + merged_value,
            "cum_invested": cum_invested, "merged_shares": merged_shares,
        })

    return {"cycles": cycles, "history": pd.DataFrame(history).set_index("date"),
            "cum_realized": cum_realized, "cum_invested": cum_invested,
            "n_buys": n_buys, "merged_shares": merged_shares}


def summarize(res: dict, label: str) -> dict:
    h = res["history"]
    final_pnl = float(h["total_pnl"].iloc[-1])
    peak_capital = float(h["capital"].max())
    cum_inv = res["cum_invested"]
    return {
        "label": label,
        "n_cycles": len(res["cycles"]),
        "n_buys": res["n_buys"],
        "total_invested": cum_inv,
        "cum_realized": res["cum_realized"],
        "merged_shares": res["merged_shares"],
        "final_merged_value": float(h["merged_value"].iloc[-1]),
        "final_pnl": final_pnl,
        "peak_capital": peak_capital,
        # 与 chart.py:331-332 同口径
        "chart_roi_pct": final_pnl / peak_capital * 100 if peak_capital else 0.0,
        # 累计投入本金收益率
        "capital_roi_pct": final_pnl / cum_inv * 100 if cum_inv else 0.0,
    }


def selftest(prices: dict) -> None:
    """exit_mode="cost" 必须与生产 run_compound_grid 逐项一致。"""
    print("一致性校验: 本脚本 exit_mode='cost' vs src.simulation.grid_simulator.run_compound_grid\n")
    ok = True
    for name, s in prices.items():
        ref = grid_mod.run_compound_grid(s, trigger_mode="window")
        mine = replay(s, "cost")
        # 未平周期 (open_cycle) 的入场也要计入 —— run_compound_grid 把仍在持仓的
        # entries 单独作为 open_cycle 返回 (它就是 entries 列表本身, 不是 dict),
        # 不在 cycles 里。漏掉它会让 n_buys 少算 (可转债 3 笔)。
        oc = ref["open_cycle"] or []
        n_ref = sum(c["num_buys"] for c in ref["cycles"]) + len(oc)
        checks = [
            ("cum_realized", ref["cum_realized"], mine["cum_realized"]),
            ("n_cycles", len(ref["cycles"]), mine["cycles"].__len__()),
            ("n_buys", n_ref, mine["n_buys"]),
            ("cum_invested",
             sum(c["total_invested"] for c in ref["cycles"]) + (ref["open_invested"] or 0),
             mine["cum_invested"]),
            ("merged_shares", ref["merged_shares"], mine["merged_shares"]),
        ]
        line = []
        for k, a, b in checks:
            good = abs(a - b) < 1e-6 if isinstance(a, float) else a == b
            ok &= good
            line.append(f"{k}:{'✓' if good else '✗ ' + str(a) + ' vs ' + str(b)}")
        print(f"  {name:<16} " + "  ".join(line))
    print(f"\n  总体: {'✓ 重实现与生产逻辑一致' if ok else '✗ 不一致, 对比结果不可信'}")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true", help="联网重取价格, 不用快照")
    ap.add_argument("--selftest", action="store_true", help="只跑一致性校验")
    args = ap.parse_args()

    prices = load_prices(args.fetch)
    first = min(s.index[0] for s in prices.values())
    last = max(s.index[-1] for s in prices.values())
    print(f"价格区间 {first.date()} → {last.date()}"
          f"  ({len(prices)} 个资产, 每层 ¥{grid_mod.GRID_BASE_YUAN:,.0f},"
          f" 止盈 {grid_mod.GRID_TP_PCT}% on {grid_mod.GRID_TP_BASIS})\n")

    selftest(prices)
    if args.selftest:
        return

    print()
    results = {m: {n: replay(s, m) for n, s in prices.items()}
               for m in ("all", "cost")}

    rows = []
    for name in prices:
        rows.append({**summarize(results["all"][name], f"{name} · 全部卖出(旧规则)"),
                     "asset": name, "mode": "all"})
        rows.append({**summarize(results["cost"][name], f"{name} · 只卖出成本(现行)"),
                     "asset": name, "mode": "cost"})

    hdr = f"{'资产 / 模式':<26}{'周期':>5}{'投入本金':>11}{'已实现':>10}" \
          f"{'归并份额':>11}{'期末总盈亏':>12}{'峰值占用':>10}" \
          f"{'图表ROI':>9}{'本金ROI':>9}"
    print(hdr)
    print("-" * len(hdr.encode("gbk", errors="replace")))
    for r in rows:
        print(f"{r['label']:<26}{r['n_cycles']:>5}{r['total_invested']:>11,.0f}"
              f"{r['cum_realized']:>10,.0f}{r['merged_shares']:>11,.0f}"
              f"{r['final_pnl']:>12,.0f}{r['peak_capital']:>10,.0f}"
              f"{r['chart_roi_pct']:>8.2f}%{r['capital_roi_pct']:>8.2f}%")

    tot = {}
    for m in ("all", "cost"):
        h = pd.concat([results[m][n]["history"]["total_pnl"] for n in prices],
                      axis=1).sum(axis=1)
        use = pd.concat([results[m][n]["history"]["capital"] for n in prices],
                        axis=1).sum(axis=1)
        inv = sum(results[m][n]["cum_invested"] for n in prices)
        # 「归并到组合」的风险不在期末而在路径: 归并份额会跟着一起跌。峰值
        # 占用取的是 max(), 完全看不到中间的回撤, 所以把最低浮盈单列出来。
        trough = h.idxmin()
        tot[m] = {
            "final_pnl": float(h.iloc[-1]),
            "peak_capital": float(use.max()),
            "peak_date": use.idxmax().date(),
            "trough_pnl": float(h.min()),
            "trough_date": trough.date(),
            "cum_invested": inv,
            "merged_shares": sum(results[m][n]["merged_shares"] for n in prices),
            "n_cycles": sum(len(results[m][n]["cycles"]) for n in prices),
        }
        tot[m]["chart_roi_pct"] = tot[m]["final_pnl"] / tot[m]["peak_capital"] * 100
        tot[m]["capital_roi_pct"] = tot[m]["final_pnl"] / tot[m]["cum_invested"] * 100

    print("-" * len(hdr.encode("gbk", errors="replace")))
    for m, label in (("all", "合计 · 全部卖出 (旧规则)"),
                     ("cost", "合计 · 只卖出成本 (现行)")):
        t = tot[m]
        print(f"{label:<26}{t['n_cycles']:>5}{t['cum_invested']:>11,.0f}"
              f"{'':>10}{t['merged_shares']:>11,.0f}{t['final_pnl']:>12,.0f}"
              f"{t['peak_capital']:>10,.0f}"
              f"{t['chart_roi_pct']:>8.2f}%{t['capital_roi_pct']:>8.2f}%")

    a, c = tot["all"], tot["cost"]
    print()
    print(f"图表 ROI    {a['chart_roi_pct']:.2f}%  →  {c['chart_roi_pct']:.2f}%"
          f"   ({c['chart_roi_pct'] - a['chart_roi_pct']:+.2f}pp)")
    print(f"本金收益率  {a['capital_roi_pct']:.2f}%  →  {c['capital_roi_pct']:.2f}%"
          f"   ({c['capital_roi_pct'] - a['capital_roi_pct']:+.2f}pp)")
    print(f"期末总盈亏  ¥{a['final_pnl']:,.0f}  →  ¥{c['final_pnl']:,.0f}")
    print(f"峰值占用    ¥{a['peak_capital']:,.0f} ({a['peak_date']})"
          f"  →  ¥{c['peak_capital']:,.0f} ({c['peak_date']})")
    print(f"累计归并份额 {c['merged_shares']:,.0f} 份 (现行规则, 归并到再平衡持仓)")
    print()
    print(f"路径风险 (全程最低浮盈):")
    print(f"  旧规则  ¥{a['trough_pnl']:>9,.0f}  ({a['trough_date']})")
    print(f"  现行    ¥{c['trough_pnl']:>9,.0f}  ({c['trough_date']})")

    # 收益集中度: 新规则的增益若来自单一资产, 那它就不是普适结论。
    print()
    print("增益归因 (期末总盈亏 相对旧规则的增量):")
    for n in prices:
        d = (summarize(results["cost"][n], n)["final_pnl"]
             - summarize(results["all"][n], n)["final_pnl"])
        total_d = c["final_pnl"] - a["final_pnl"]
        print(f"  {n:<16} ¥{d:>9,.0f}   ({d / total_d * 100:>5.1f}% of 合计增量)")


if __name__ == "__main__":
    main()