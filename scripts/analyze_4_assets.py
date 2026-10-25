"""4 资产生产配置深度分析 (黄金+红利+可转债+纳指).

术语说明:
  - 3pp = 再平衡阈值 3 个百分点 (THRESHOLD_PP=3.0), 跟资产数无关
  - 4 资产 → 1/4 each 等权; 任一偏离 > 3pp 触发全调仓

布局 (一张大图 18×16, 4 行 2 列):
  Row 0: 4 ETF 归一化价格 (起点=100)  +  关键回撤标注
  Row 1 left: 4 资产 3pp 再平衡 净值曲线
  Row 1 right: 4 资产 3pp 回撤时间线
  Row 2 left: 3pp 年度收益柱状图
  Row 2 right: 4 ETF 日收益率相关性矩阵 (热图)

输出: data/4_asset_analysis.png
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
import numpy as np
import pandas as pd

from src.simulation import equal_weight_backtest as three_pp
from src.infrastructure.quote_providers import fetch_one
START = "2020-11-05"
END = pd.Timestamp.today().strftime("%Y-%m-%d")
INITIAL_CAP = 200_000.0

ASSETS = [
    ("黄金ETF博时",   "sz159937", "159937", "#C9A961"),
    ("红利ETF易方达", "sh515180", "515180", "#C0392B"),
    ("可转债ETF博时", "sh511380", "511380", "#2C5F8D"),
    ("纳斯达克ETF华夏", "sh513300", "513300", "#48C9B0"),
]
ASSET_COLORS = {n: c for n, _, _, c in ASSETS}


def _setup_fonts() -> None:
    for path in [
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    ]:
        try:
            fm.fontManager.addfont(path)
            prop = fm.FontProperties(fname=path)
            plt.rcParams["font.sans-serif"] = [prop.get_name(), "DejaVu Sans"]
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["axes.unicode_minus"] = False
            return
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False


def fetch_all() -> dict[str, pd.Series]:
    out = {}
    for name, sina, em, _ in ASSETS:
        df = fetch_one(sina, em, START, END)
        out[name] = df["close"]
    return out


def per_etf_metrics(prices: dict) -> pd.DataFrame:
    rows = []
    for n, s in prices.items():
        rets = s.pct_change().dropna()
        years = (s.index[-1] - s.index[0]).days / 365.25
        cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1
        vol = rets.std() * np.sqrt(252)
        peak = s.cummax()
        dd = (s - peak) / peak
        max_dd = dd.min()
        worst_dd_end = dd.idxmin()
        peak_before = s.loc[:worst_dd_end].idxmax()
        try:
            rec = s.loc[worst_dd_end:]
            rec_idx = rec[rec >= s.loc[peak_before]].index
            rec_days = (rec_idx[0] - worst_dd_end).days if len(rec_idx) else None
        except Exception:
            rec_days = None
        rows.append({
            "ETF": n,
            "Start": round(s.iloc[0], 3),
            "End": round(s.iloc[-1], 3),
            "CAGR %": round(cagr * 100, 2),
            "Vol %": round(vol * 100, 2),
            "MaxDD %": round(max_dd * 100, 2),
            "WorstDD End": worst_dd_end.date(),
            "Recov (d)": rec_days,
        })
    return pd.DataFrame(rows)


def correlation(prices: dict) -> pd.DataFrame:
    df = pd.DataFrame(prices).pct_change().dropna()
    return df.corr()


def yearly_returns(series: pd.Series) -> pd.Series:
    years = series.index.year
    out: dict[int, float] = {}
    for y in sorted(set(years)):
        sub = series[years == y]
        if len(sub) >= 2:
            out[y] = sub.iloc[-1] / sub.iloc[0] - 1
    return pd.Series(out)


def main() -> None:
    print(f"拉取 4 ETF ({START} → {END})...")
    prices = fetch_all()
    for n, s in prices.items():
        print(f"  {n:<20} {len(s)} 行  {s.index[0].date()} → {s.index[-1].date()}")

    # 3pp backtest on 4 资产
    prices_df = pd.DataFrame(prices).sort_index().dropna()
    pp = three_pp.run_backtest(prices_df, initial_cash=INITIAL_CAP)
    pp_nav = pp["series"]
    print(f"\n3pp 再平衡: CAGR {pp['cagr']*100:.2f}%  Sharpe {pp['sharpe']:.2f}  "
          f"MaxDD {pp['max_dd']*100:.2f}%  n_reb {pp['n_rebalances']}  "
          f"final Y{pp['final']:,.0f}")

    # 1) 个体指标
    print()
    print("=" * 90)
    print("Per-ETF 个体指标:")
    print("-" * 90)
    met = per_etf_metrics(prices)
    print(met.to_string(index=False))
    print("=" * 90)

    # 2) 相关性
    print()
    print("4 ETF 日收益率相关性矩阵 (越高=同涨同跌):")
    corr = correlation(prices)
    print(corr.round(3).to_string())

    # 3) 年度收益
    print()
    print("3pp 再平衡 年度收益 (%):")
    y4pp = yearly_returns(pp_nav) * 100
    for y, v in y4pp.items():
        print(f"  {y}: {v:+.2f}%")

    # 4) 绘图
    _setup_fonts()
    out = ROOT / "data" / "4_asset_analysis.png"

    fig = plt.figure(figsize=(18, 16))
    gs = fig.add_gridspec(3, 2, hspace=0.30, wspace=0.22,
                          height_ratios=[2.2, 1.4, 1.4])

    # Row 0: 4 ETF 归一价格 (跨整行)
    ax0 = fig.add_subplot(gs[0, :])
    for n, s in prices.items():
        norm = s / s.iloc[0] * 100
        ax0.plot(norm.index, norm.values, label=n,
                 color=ASSET_COLORS[n], lw=1.8, alpha=0.9)
    # 标注每个 ETF 的最大回撤
    for n, s in prices.items():
        peak = s.cummax()
        dd = (s - peak) / peak
        worst_end = dd.idxmin()
        peak_before = s.loc[:worst_end].idxmax()
        ax0.annotate(f"  {n.split('ETF')[0]} {dd.min()*100:.1f}%",
                     xy=(worst_end, s.loc[worst_end] / s.iloc[0] * 100),
                     xytext=(8, -8), textcoords="offset points",
                     fontsize=9, color=ASSET_COLORS[n],
                     fontweight="bold", va="top",
                     arrowprops=dict(arrowstyle="->", color=ASSET_COLORS[n], lw=0.8, alpha=0.7))
    ax0.set_title(f"4 ETF 归一化价格 (起点=100)  区间 {START} → {END}",
                  fontsize=13, fontweight="bold", pad=10)
    ax0.set_ylabel("价格 (起点=100)", fontsize=11)
    ax0.grid(True, alpha=0.4, ls=":")
    ax0.legend(loc="upper left", fontsize=10, ncol=4)
    ax0.axhline(100, color="grey", lw=0.5, alpha=0.5)

    # Row 1 left: 3pp 净值曲线
    ax1 = fig.add_subplot(gs[1, 0])
    nav_norm = pp_nav / INITIAL_CAP * 100
    ax1.fill_between(nav_norm.index, nav_norm.values, 100,
                     color="#1A1A1A", alpha=0.10)
    ax1.plot(nav_norm.index, nav_norm.values, color="#1A1A1A", lw=2.2,
             label=f"3pp 再平衡  CAGR {pp['cagr']*100:.2f}%")
    ax1.axhline(100, color="grey", lw=0.5, alpha=0.5)
    ax1.set_title("4 资产 3pp 再平衡 净值曲线 (起点=100)", fontsize=12, fontweight="bold")
    ax1.set_ylabel("净值", fontsize=10)
    ax1.grid(True, alpha=0.4, ls=":")
    ax1.legend(loc="upper left", fontsize=10)
    # 末值标签
    last_y = float(nav_norm.values[-1])
    ax1.annotate(f" 末值 {last_y:.1f} ({(last_y-100):+.1f}%)",
                 xy=(nav_norm.index[-1], last_y),
                 xytext=(8, 0), textcoords="offset points",
                 fontsize=10, color="#1A1A1A", fontweight="bold", va="center")

    # Row 1 right: 3pp 回撤时间线
    ax2 = fig.add_subplot(gs[1, 1])
    peak = pp_nav.cummax()
    dd = (pp_nav - peak) / peak * 100
    ax2.fill_between(dd.index, dd.values, 0, color="#C0392B", alpha=0.35)
    ax2.plot(dd.index, dd.values, color="#C0392B", lw=1.0)
    ax2.axhline(dd.min(), color="red", lw=0.8, alpha=0.7, ls="--")
    ax2.text(dd.index[len(dd)//4], dd.min(),
             f" 最深 {dd.min():.2f}%  ({dd.idxmin().date()})",
             color="red", fontsize=10, fontweight="bold", va="bottom")
    ax2.set_title("4 资产 3pp 回撤时间线 (%)", fontsize=12, fontweight="bold")
    ax2.set_ylabel("Drawdown %", fontsize=10)
    ax2.grid(True, alpha=0.4, ls=":")
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # Row 2 left: 年度收益柱状图
    ax3 = fig.add_subplot(gs[2, 0])
    years = list(y4pp.index)
    vals = list(y4pp.values)
    bar_colors = ["#27AE60" if v >= 0 else "#C0392B" for v in vals]
    bars = ax3.bar(years, vals, color=bar_colors, alpha=0.85, edgecolor="white", linewidth=1.2)
    for b, v in zip(bars, vals):
        ax3.text(b.get_x() + b.get_width()/2, v + (1 if v >= 0 else -2),
                 f"{v:+.1f}%", ha="center",
                 va="bottom" if v >= 0 else "top",
                 fontsize=10, fontweight="bold",
                 color="#27AE60" if v >= 0 else "#C0392B")
    ax3.axhline(0, color="grey", lw=0.6)
    ax3.set_title("4 资产 3pp 再平衡 年度收益 (%)", fontsize=12, fontweight="bold")
    ax3.set_ylabel("年收益 %", fontsize=10)
    ax3.set_xlabel("年份", fontsize=10)
    ax3.grid(True, axis="y", alpha=0.4, ls=":")
    ax3.set_xticks(years)

    # Row 2 right: 相关性热图
    ax4 = fig.add_subplot(gs[2, 1])
    im = ax4.imshow(corr.values, cmap="RdYlBu_r", vmin=-0.3, vmax=1.0, aspect="auto")
    ax4.set_xticks(range(len(corr)))
    ax4.set_xticklabels(corr.columns, rotation=15, ha="right", fontsize=9)
    ax4.set_yticks(range(len(corr)))
    ax4.set_yticklabels(corr.columns, fontsize=9)
    for i in range(len(corr)):
        for j in range(len(corr)):
            v = corr.values[i, j]
            color = "white" if abs(v) > 0.6 else "black"
            ax4.text(j, i, f"{v:.2f}", ha="center", va="center",
                     color=color, fontsize=10, fontweight="bold")
    ax4.set_title("4 ETF 日收益率相关性矩阵", fontsize=12, fontweight="bold", pad=10)
    plt.colorbar(im, ax=ax4, fraction=0.046, pad=0.04)

    # 标题 + 个体指标表 (顶部 text)
    years_count = (pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25
    fig.suptitle(
        f"4 资产生产配置深度分析  ({START} → {END}, {years_count:.2f} 年)\n"
        f"资产: 黄金 / 红利 / 可转债 / 纳指 513300    3pp 再平衡 CAGR {pp['cagr']*100:.2f}%  "
        f"Sharpe {pp['sharpe']:.2f}  MaxDD {pp['max_dd']*100:.2f}%",
        fontsize=14, fontweight="bold", y=0.995,
    )

    # 关键观察 (底部 text)
    obs = (
        f"关键观察 (2020-11-05 → {END}):\n"
        f"  - 纳指 CAGR 最高 ({met.loc[met['ETF'].str.contains('纳斯达克'), 'CAGR %'].values[0]:.2f}%)  "
        f"MaxDD 最浅 ({met.loc[met['ETF'].str.contains('纳斯达克'), 'MaxDD %'].values[0]:.2f}%)  → 进攻 + 防守 双优\n"
        f"  - 黄金 CAGR {met.loc[met['ETF'].str.contains('黄金'), 'CAGR %'].values[0]:.2f}%  "
        f"MaxDD {met.loc[met['ETF'].str.contains('黄金'), 'MaxDD %'].values[0]:.2f}%  → 通胀对冲\n"
        f"  - 红利/可转债 防守型 (CAGR < 5%, MaxDD < -25%)  → 压舱石\n"
        f"  - 纳指 vs 其他相关性最低 (0.12-0.27), 真正分散; 红利 vs 可转债 = 0.53 较高"
    )
    fig.text(0.5, 0.005, obs, ha="center", va="bottom", fontsize=9,
             family="monospace",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#F8F9F9",
                       edgecolor="#BDC3C7", linewidth=0.8))

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    main()
