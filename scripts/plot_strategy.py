"""Plot strategy chart (v3 grid only, 4 assets).

2 stacked subplots:
  Top:    4 ETF normalized prices (log Y)  +  3pp 净值  +  网格 ROI
  Bot:    网格 资金占用 堆叠 (per-asset ¥)

Output: data/grid_chart.png  (consumed by GitHub Pages <img>)
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

warnings.filterwarnings("ignore")
for _n in ("matplotlib", "akshare", "urllib3", "requests"):
    import logging
    logging.getLogger(_n).setLevel(logging.ERROR)

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

from src import grid as grid_mod
from src import three_pp
from src.fetch import fetch_one

# 4 资产 (与 src/record.py 一致), 起点取 513300 上市日以保证窗口一致
START = "2020-11-05"
END = pd.Timestamp.today().strftime("%Y-%m-%d")
ASSETS = [
    ("黄金ETF博时", "sz159937", "159937"),
    ("红利ETF易方达", "sh515180", "515180"),
    ("可转债ETF博时", "sh511380", "511380"),
    ("纳斯达克ETF华夏", "sh513300", "513300"),
]

OUT = ROOT / "data" / "grid_chart.png"

ASSET_COLORS = {
    "黄金ETF博时":   "#C9A961",
    "红利ETF易方达": "#C0392B",
    "可转债ETF博时": "#2C5F8D",
    "纳斯达克ETF华夏": "#48C9B0",
}


def _setup_fonts() -> None:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    ]
    for path in candidates:
        try:
            fm.fontManager.addfont(path)
            prop = fm.FontProperties(fname=path)
            plt.rcParams["font.sans-serif"] = [prop.get_name(), "DejaVu Sans"]
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["axes.unicode_minus"] = False
            return
        except Exception:
            continue
    for fam in ("Noto Sans CJK SC", "Noto Sans CJK", "Microsoft YaHei", "SimHei"):
        try:
            path = fm.findfont(fam, fallback_to_default=False)
            if path and "DejaVu" not in path:
                plt.rcParams["font.sans-serif"] = [fam, "DejaVu Sans"]
                plt.rcParams["font.family"] = "sans-serif"
                plt.rcParams["axes.unicode_minus"] = False
                return
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False


def _pct_to_log(arr: np.ndarray) -> np.ndarray:
    clipped = np.maximum(arr / 100.0, 0.0)
    return np.log1p(clipped)


def _log_to_pct(x, pos):
    pct = (np.expm1(x)) * 100
    if abs(pct) < 0.1:
        return "0%"
    if abs(pct) >= 100:
        return f"{pct:+.0f}%"
    return f"{pct:+.1f}%"


def main() -> None:
    print(f"拉取 {len(ASSETS)} ETF 历史 ({START} → {END})...")
    cols = {}
    for name, sina, em in ASSETS:
        df = fetch_one(sina, em, START, END)
        cols[name] = df["close"]
    prices = pd.DataFrame(cols).sort_index().dropna()
    print(f"  对齐 {len(prices)} 行  {prices.index[0].date()} → {prices.index[-1].date()}")

    # 3pp 净值
    pp = three_pp.run_backtest(prices, initial_cash=200_000.0)
    pp_idx = pp["series"] / 200_000.0 * 100
    pp_idx = pp_idx.reindex(prices.index).ffill()

    # 各 ETF 起点 100 norm
    norm = {n: prices[n] / prices[n].iloc[0] * 100 for n in prices.columns}
    norm_log = {n: pd.Series(_pct_to_log((s - 100).values), index=s.index, name=n)
                for n, s in norm.items()}
    pp_log = pd.Series(_pct_to_log((pp_idx - 100).values), index=pp_idx.index, name="3pp 再平衡")

    # v3 网格回放 (v3 = first_buy, 当前默认)
    print("回放 v3 网格 (first_buy)...")
    v3_hist = {}
    for n in prices.columns:
        v3_hist[n] = grid_mod.run_compound_grid(
            prices[n], pos_yuan=grid_mod.GRID_BASE_YUAN,
        )["history"]

    v3_total = (sum(h["current_value"] for h in v3_hist.values())
                .reindex(prices.index).fillna(0.0))
    v3_stack = pd.concat({n: h["current_value"] for n, h in v3_hist.items()},
                          axis=1).reindex(prices.index).fillna(0.0)

    realized = sum(h["cum_realized"] for h in v3_hist.values())
    unrealized = sum(h["unrealized_pnl"] for h in v3_hist.values())
    v3_pnl = (realized + unrealized).reindex(prices.index).ffill().fillna(0.0)
    GRID_DENOM = max(v3_total.max(), 1.0)
    v3_pnl_pct = v3_pnl / GRID_DENOM * 100
    v3_roi_log = pd.Series(_pct_to_log(v3_pnl_pct.values), index=v3_pnl_pct.index)

    # ---- Plot ----
    _setup_fonts()

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(14, 9.5),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1.4]},
    )
    fig.subplots_adjust(hspace=0.10)

    # ---- Top: ETF norm + 3pp + 网格 ROI ----
    style = {
        "黄金ETF博时":   dict(color="#C9A961", lw=1.4, ls="--", alpha=0.85),
        "红利ETF易方达": dict(color="#C0392B", lw=1.4, ls="--", alpha=0.85),
        "可转债ETF博时": dict(color="#2C5F8D", lw=1.4, ls="--", alpha=0.85),
        "纳斯达克ETF华夏": dict(color="#48C9B0", lw=1.4, ls="--", alpha=0.85),
    }
    for n, st in style.items():
        if n in norm_log:
            ax_top.plot(norm_log[n].index, norm_log[n].values, label=n, **st)
    ax_top.plot(pp_log.index, pp_log.values,
                color="#1A1A1A", lw=2.6, ls="-", alpha=1.0, label="3pp 再平衡")
    ax_top.plot(v3_roi_log.index, v3_roi_log.values,
                color="#27AE60", lw=2.4, ls="-", alpha=0.95, label="网格 ROI (v3)")

    ax_top.axhline(0, color="grey", lw=0.6, alpha=0.4)
    ax_top.set_title(
        f"4 资产 + 3pp 再平衡 + 网格 ROI  (对数 Y, 起点=0%)\n"
        f"区间 {prices.index[0].date()} → {prices.index[-1].date()}  "
        f"({(prices.index[-1] - prices.index[0]).days/365.25:.2f} 年)",
        fontsize=13, fontweight="bold", pad=12,
    )
    ax_top.set_ylabel("收益 % (对数刻度)", fontsize=11)
    ax_top.yaxis.set_major_formatter(mticker.FuncFormatter(_log_to_pct))
    ax_top.grid(True, which="major", color="#888", alpha=0.55, ls=":", lw=0.7)
    ax_top.grid(True, which="minor", color="#AAA", alpha=0.30, ls=":", lw=0.5)
    ax_top.set_axisbelow(True)
    leg = ax_top.legend(loc="upper left", frameon=True, framealpha=0.92,
                        fontsize=10, edgecolor="grey", ncol=2)
    leg.get_frame().set_linewidth(0.6)

    # ---- Bot: 资金占用 堆叠 ----
    stack_cols = [n for n in v3_stack.columns if n in ASSET_COLORS]
    ax_bot.stackplot(
        v3_stack.index,
        *[v3_stack[n].values for n in stack_cols],
        labels=stack_cols,
        colors=[ASSET_COLORS[n] for n in stack_cols],
        alpha=0.90, edgecolor="white", linewidth=0.8,
    )
    ax_bot.set_facecolor("#FAFAFA")
    if not v3_total.empty:
        last = float(v3_total.values[-1])
        ax_bot.annotate(f"合计 ¥{last:,.0f}",
                        xy=(v3_total.index[-1], last),
                        xytext=(8, 4), textcoords="offset points",
                        fontsize=10, color="#1B4F72", fontweight="bold", va="bottom")
    ax_bot.set_title("网格 资金占用 堆叠 ¥",
                     fontsize=12, fontweight="bold", color="#27AE60", pad=8)
    ax_bot.set_ylabel("资金占用 ¥", fontsize=10, color="#1B4F72")
    ax_bot.tick_params(axis="y", labelcolor="#1B4F72")
    ax_bot.set_xlabel("日期", fontsize=11)
    ax_bot.grid(True, which="major", color="#888", alpha=0.55, ls=":", lw=0.9)
    ax_bot.grid(True, which="minor", color="#AAA", alpha=0.30, ls=":", lw=0.6)
    ax_bot.set_axisbelow(True)
    leg2 = ax_bot.legend(loc="upper left", frameon=True, framealpha=0.92,
                         fontsize=10, edgecolor="grey", ncol=3)
    leg2.get_frame().set_linewidth(0.6)
    if not v3_total.empty:
        ax_bot.set_ylim(0, max(v3_total.max() * 1.2, 1))

    ax_bot.xaxis.set_major_locator(mdates.YearLocator())
    ax_bot.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 10]))
    ax_bot.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved chart → {OUT}")
    print(f"  资金占用峰值 Y{v3_total.max():,.0f}  末值 Y{v3_total.values[-1]:,.0f}")
    print(f"  累计 PnL Y{v3_pnl.values[-1]:+,.0f}")


if __name__ == "__main__":
    main()
