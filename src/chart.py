"""Render strategy chart to PNG.

Layout (2 stacked subplots, shared x-axis):
  Top:    N ETF buy-and-hold + 3pp rebalance + 网格 ROI (%, log scale)
  Bottom: 网格 资金占用 堆叠 (per-asset ¥)

N is derived dynamically from `data/holdings.json` (whichever ETFs the
user currently holds, codes must be registered in `src.record.ETF_DEFS`).
This keeps the chart in sync when the user adds/removes/swaps ETFs.
(.xlsx is only a legacy bootstrap fallback in `read_holdings()`.)

Font handling: tries msyh (Windows), simhei, then any CJK font found on
the system. On GitHub Actions (Linux, no Microsoft fonts), the script
relies on the pre-installed `fonts-noto-cjk` package — the workflow
installs it explicitly.

The chart is generated from `data/history.json` + a full backtest:
  - 3pp series: from `three_pp.run_backtest()` on fetched ETF prices
    (起点 ¥200,000, 跨 2020-11-05 → 今; 与 scripts/plot_strategy.py 同口径).
    This avoids the misleading "X% in 4 days" artefact that comes from
    normalizing history.json (which is too short to draw a meaningful curve).
  - Per-asset normalized returns: re-fetched from akshare for the same window
  - Per-asset grid ¥ in-use: rebuilt by replaying run_compound_grid on the
    current grid_state.json
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
for _n in ("akshare", "urllib3", "requests", "matplotlib"):
    import logging
    logging.getLogger(_n).setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"
GRID_STATE = ROOT / "data" / "grid_state.json"
OUT_PNG = ROOT / "data" / "grid_chart.png"
START = "2020-11-05"     # 纳斯达克ETF 上市日, 4 资产公共起点 (3 rebalance + 1 grid-only)
END_FALLBACK = pd.Timestamp.today().strftime("%Y-%m-%d")

INITIAL_3PP_CAPITAL = 200_000.0

# Curated colors for the original 4 ETFs (preserve visual identity).
# New ETFs added via ETF_DEFS get cycled from EXTRA_PALETTE below.
KNOWN_COLORS = {
    "黄金ETF博时":     "#C9A961",
    "红利ETF易方达":   "#C0392B",
    "可转债ETF博时":   "#2C5F8D",
    "纳斯达克ETF华夏": "#48C9B0",
}
# Fallback palette for any additional ETF (5th, 6th, ...).
EXTRA_PALETTE = [
    "#8E44AD", "#E67E22", "#16A085", "#D35400", "#7F8C8D",
    "#2ECC71", "#F39C12", "#9B59B6", "#1ABC9C", "#34495E",
]


def _load_chart_assets() -> list[tuple[str, str, str]]:
    """Resolve (name, sina, eastmoney) for the user's current holdings.

    Reads `data/holdings.json` (via `src.record.read_holdings()`) and joins
    with `src.record.ETF_DEFS` (which is the registry of known codes with
    their sina/eastmoney symbols). Falls back to the full ETF_DEFS when
    the holdings file is unreadable — e.g. in tests or if the user is
    rendering the chart before record.py has run.
    """
    try:
        from src.record import read_holdings, ETF_DEFS
        holdings, _ = read_holdings()
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for h in holdings:
            code = h["code"]
            if code in seen:
                continue
            if code not in ETF_DEFS:
                print(f"WARN chart: code {code} ({h['name']}) not in ETF_DEFS, skipping",
                      file=sys.stderr)
                continue
            sina, em, name, _qdii = ETF_DEFS[code]
            # Keep the user's display name from holdings.json, fall back to registry.
            display = h["name"] or name
            out.append((display, sina, em))
            seen.add(code)
        if out:
            return out
    except Exception as ex:
        print(f"WARN chart: cannot read holdings ({ex}); using ETF_DEFS fallback",
              file=sys.stderr)
    # Fallback: full registry
    from src.record import ETF_DEFS
    return [(name, sina, em) for _code, (sina, em, name, _qdii) in ETF_DEFS.items()]


def _colors_for(names: list[str]) -> dict[str, str]:
    """Color for each asset name; curated for known ones, cycled for extras."""
    out: dict[str, str] = {}
    extra_idx = 0
    for n in names:
        if n in KNOWN_COLORS and n not in out:
            out[n] = KNOWN_COLORS[n]
        else:
            out[n] = EXTRA_PALETTE[extra_idx % len(EXTRA_PALETTE)]
            extra_idx += 1
    return out


# -----------------------------------------------------------------------------
# Font setup (Windows → Noto fallback for Linux)
# -----------------------------------------------------------------------------

def _setup_fonts() -> None:
    """Try to register a CJK font for matplotlib.

    Order:
      1. Windows: msyh.ttc, simhei.ttf
      2. Linux:   /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
      3. Search matplotlib's font cache for any CJK family
    """
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/System/Library/Fonts/PingFang.ttc",
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
    # Last-ditch: find any CJK family already in matplotlib's cache
    try:
        for fam in ("Noto Sans CJK SC", "Noto Sans CJK", "Microsoft YaHei",
                    "PingFang SC", "SimHei"):
            try:
                path = fm.findfont(fam, fallback_to_default=False)
                if path and "DejaVu" not in path:
                    plt.rcParams["font.sans-serif"] = [fam, "DejaVu Sans"]
                    plt.rcParams["font.family"] = "sans-serif"
                    plt.rcParams["axes.unicode_minus"] = False
                    return
            except Exception:
                continue
    except Exception:
        pass
    # Fallback to defaults (Chinese will render as boxes, but won't crash)
    plt.rcParams["axes.unicode_minus"] = False


# -----------------------------------------------------------------------------
# Data assembly
# -----------------------------------------------------------------------------

def _fetch_asset_window(assets: list[tuple[str, str, str]]) -> dict[str, pd.Series]:
    """Fetch the full price history window for the user's N ETFs.

    Args:
        assets: list of (display_name, sina_symbol, eastmoney_code) tuples.
                Resolved by `_load_chart_assets()` from holdings.xlsx.

    Imported lazily so chart rendering can be tested without network.
    """
    from src.fetch import fetch_one
    out: dict[str, pd.Series] = {}
    for name, sina, em in assets:
        df = fetch_one(sina, em)
        df = df[(df.index >= START) & (df.index <= END_FALLBACK)]
        s = df.iloc[:, 0] if isinstance(df, pd.DataFrame) else df
        s.name = name
        out[name] = s
    return out


def _fetch_csi300() -> Optional[pd.Series]:
    try:
        import akshare as ak
        raw = ak.stock_zh_index_daily(symbol="sh000300")
        raw["date"] = pd.to_datetime(raw["date"])
        raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
        s = raw.set_index("date").sort_index()["close"]
        return s
    except Exception:
        return None


def _build_pct_to_log_vec():
    def f(arr):
        clipped = np.maximum(np.asarray(arr, dtype=float) / 100.0, 0.0)
        return np.log1p(clipped)
    return f


def _replay_grid_for_chart(prices_by_asset: dict[str, pd.Series],
                           grid_state: dict) -> dict[str, pd.DataFrame]:
    """Replay grid using current state to populate per-asset history frames.

    The Action persists `grid_state.json` but not the per-asset history
    DataFrame. We re-run `run_compound_grid` from the start, then
    splice the current state into the tail to keep the chart consistent
    with the persisted state.

    trigger_mode='window' (120-day rolling low) matches the live rule.
    """
    from src.grid import run_compound_grid
    out: dict[str, pd.DataFrame] = {}
    for name, s in prices_by_asset.items():
        result = run_compound_grid(s, trigger_mode="window")
        out[name] = result["history"]
    return out


def _load_grid_state_safe() -> dict:
    if not GRID_STATE.exists():
        return {}
    try:
        return json.loads(GRID_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# -----------------------------------------------------------------------------
# Main render
# -----------------------------------------------------------------------------

def render_chart(history: list[dict], out_path: Path = OUT_PNG) -> Path:
    """Build the chart from history.json + current grid_state.json.

    Args:
        history: list of record dicts (one per day). May be empty (no-op).
        out_path: where to save the PNG.

    Returns:
        out_path for chaining.
    """
    if not history:
        print("WARN history.json empty; skipping chart render", file=sys.stderr)
        return out_path

    # Resolve assets dynamically from the user's holdings (N-aware).
    ASSETS = _load_chart_assets()
    ASSET_NAMES = [n for n, _, _ in ASSETS]
    ASSET_COLORS = _colors_for(ASSET_NAMES)
    if not ASSETS:
        print("WARN no assets resolved; skipping chart render", file=sys.stderr)
        return out_path

    _setup_fonts()
    pct_to_log_vec = _build_pct_to_log_vec()

    try:
        prices = _fetch_asset_window(ASSETS)
    except Exception as ex:
        print(f"WARN asset fetch failed: {ex}", file=sys.stderr)
        return out_path
    try:
        csi300 = _fetch_csi300()
    except Exception:
        csi300 = None

    common = pd.concat([prices[n] for n in ASSET_NAMES], axis=1, join="inner").dropna()
    common.columns = ASSET_NAMES
    start_date, end_date = common.index[0], common.index[-1]

    etf_w = {n: prices[n][(prices[n].index >= start_date) & (prices[n].index <= end_date)]
             for n in ASSET_NAMES}

    norm = {n: s / s.iloc[0] * 100 for n, s in etf_w.items()}
    if csi300 is not None:
        csi_w = csi300[(csi300.index >= start_date) & (csi300.index <= end_date)]
        norm["沪深300"] = csi_w / csi_w.iloc[0] * 100

    pp_series = pd.Series(
        [r["total"] for r in history if "total" in r],
        index=pd.to_datetime([r["date"] for r in history if "total" in r]),
    ).sort_index()
    # Compute the same "3pp 再平衡" line via full backtest on the fetched
    # price window. This is what scripts/plot_strategy.py does too, and
    # gives a proper multi-year curve (vs. history.json which usually has
    # only a handful of entries and produces a misleading flat-then-spike).
    from src.three_pp import run_backtest
    prices_df = pd.DataFrame(prices).sort_index().dropna()
    pp_bt = run_backtest(prices_df, initial_cash=INITIAL_3PP_CAPITAL)
    pp_series_bt = pp_bt["series"]
    # Prefer the backtested curve (long history); use it as the plotted line.
    pp_series = pp_series_bt
    norm["3pp 再平衡"] = pp_series / pp_series.iloc[0] * 100

    norm_ret_pct = {n: s - 100 for n, s in norm.items()}
    norm_log = {n: pd.Series(pct_to_log_vec(s.values), index=s.index, name=n)
                for n, s in norm_ret_pct.items()}

    # Replay grid with trigger_mode="window" (120-day rolling low) — the live rule.
    grid_state = _load_grid_state_safe()
    grid_histories = _replay_grid_for_chart(etf_w, grid_state)
    grid_in_use_by_asset = {n: grid_histories[n]["current_value"] for n in ASSET_NAMES}
    grid_in_use_total = sum(grid_in_use_by_asset[n] for n in ASSET_NAMES)
    grid_pnl_total = (
        sum(grid_histories[n]["cum_realized"] for n in ASSET_NAMES)
        + sum(grid_histories[n]["unrealized_pnl"] for n in ASSET_NAMES)
    )

    # Clip to common window
    def _clip(s, lo=start_date, hi=end_date):
        return s[(s.index >= lo) & (s.index <= hi)]
    grid_in_use_total = _clip(grid_in_use_total)
    grid_pnl_total = _clip(grid_pnl_total)
    for n in ASSET_NAMES:
        grid_in_use_by_asset[n] = _clip(grid_in_use_by_asset[n])

    GRID_DENOMINATOR = float(grid_in_use_total.max()) if not grid_in_use_total.empty else 1.0
    grid_roi_pct = grid_pnl_total / GRID_DENOMINATOR * 100 if GRID_DENOMINATOR else grid_pnl_total * 0
    grid_roi_log = pd.Series(pct_to_log_vec(grid_roi_pct.values),
                             index=grid_roi_pct.index, name="网格 ROI")

    # ---- Plot ----
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 9.5),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1.6]},
    )
    fig.subplots_adjust(hspace=0.10)

    style_map: dict[str, dict] = {
        n: dict(color=ASSET_COLORS[n], lw=1.4, ls="--", alpha=0.85)
        for n in ASSET_NAMES
    }
    style_map["沪深300"]    = dict(color="#7F8C8D", lw=1.4, ls=":",  alpha=0.85)
    style_map["3pp 再平衡"] = dict(color="#1A1A1A", lw=2.6, ls="-",  alpha=1.0)
    # Order: 沪深300 first (benchmark), then user's ETFs in registration order,
    # 3pp rebalance last (the "main" series).
    order_top = (
        [n for n in ["沪深300"] if n in norm_log]
        + [n for n in ASSET_NAMES if n in norm_log]
        + [n for n in ["3pp 再平衡"] if n in norm_log]
    )
    for name in order_top:
        s = norm_log[name]
        st = style_map[name]
        ax1.plot(s.index, s.values, label=name, **st)

    pnl_color = "#27AE60"
    ax1.plot(grid_roi_log.index, grid_roi_log.values,
             color=pnl_color, lw=2.4, ls="-", alpha=0.95, label="网格 ROI")
    ax1.axhline(0, color=pnl_color, lw=0.6, alpha=0.4, ls="--")

    # Greedy right-edge label placement
    label_meta = []
    for name in order_top:
        s_log = norm_log[name]
        s_pct = norm_ret_pct[name]
        is_bold = (name == "3pp 再平衡")
        label_meta.append({"name": name, "y": float(s_log.values[-1]),
                           "y_pct": float(s_pct.values[-1]),
                           "bold": is_bold, "color": style_map[name]["color"]})
    if not grid_roi_log.empty:
        label_meta.append({
            "name": "网格 ROI",
            "y": float(grid_roi_log.values[-1]),
            "y_pct": float(grid_roi_pct.values[-1]),
            "bold": True, "color": pnl_color,
        })
    label_meta.sort(key=lambda d: d["y"])

    if label_meta:
        y_data_min = min(d["y"] for d in label_meta)
        y_data_max = max(d["y"] for d in label_meta)
        y_data_range = y_data_max - y_data_min
        pad_y = y_data_range * 0.05
        ylim_min = max(y_data_min - pad_y, 0)
        ylim_max = y_data_max + pad_y * 3
        ylim_range = ylim_max - ylim_min
        min_gap = ylim_range * 0.06
        placed_y: list[float] = []
        candidates = [0]
        n_steps = 25
        step = ylim_range * 0.04
        for i in range(1, n_steps + 1):
            candidates.extend([i * step, -i * step])
        for d in label_meta:
            candidate = d["y"]
            for delta in candidates:
                cand = d["y"] + delta
                if cand < 0:
                    continue
                if all(abs(cand - py) >= min_gap for py in placed_y):
                    candidate = cand
                    break
            placed_y.append(candidate)
            d["y_text"] = candidate
        for d in label_meta:
            text = f"{d['name']} {d['y_pct']:+.2f}%"
            ax1.annotate(
                text,
                xy=(end_date, d["y_text"]),
                xytext=(8, 0), textcoords="offset points",
                fontsize=10,
                fontweight="bold" if d["bold"] else "normal",
                color=d["color"], va="center",
            )

    ax1.set_title(
        f"{len(ASSET_NAMES)} 资产 3pp 再平衡 vs 单资产持有 vs 沪深300  +  网格 ROI\n"
        f"区间 {start_date.date()} → {end_date.date()} "
        f"({(end_date - start_date).days/365.25:.2f} 年) — 收益 % (起点=0%)",
        fontsize=13, fontweight="bold", pad=14,
    )
    ax1.set_ylabel("收益 % (对数刻度)", fontsize=11)
    ax1.axhline(0, color="grey", lw=0.6, alpha=0.4)
    ax1.grid(True, which="major", color="#888888", alpha=0.4, ls=":", lw=0.7)
    ax1.grid(True, which="minor", color="#AAAAAA", alpha=0.2, ls=":", lw=0.5)
    ax1.set_axisbelow(True)
    ax1.legend(loc="upper left", frameon=True, framealpha=0.92, fontsize=10, edgecolor="grey")
    if ax1.get_legend() is not None:
        ax1.get_legend().get_frame().set_linewidth(0.6)

    if label_meta:
        ax1.set_ylim(ylim_min, ylim_max)

    def log_to_pct_str(x, pos):
        pct = (np.expm1(x)) * 100
        if abs(pct) < 0.1:
            return "0%"
        if abs(pct) >= 100:
            return f"{pct:+.0f}%"
        return f"{pct:+.1f}%"
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(log_to_pct_str))
    pct_ticks = [0, 5, 10, 20, 50, 100, 200]
    if label_meta:
        log_ticks = [pct_to_log_vec(np.array([p]))[0] for p in pct_ticks]
        log_ticks = [t for t in log_ticks if ylim_min <= t <= ylim_max]
        ax1.set_yticks(log_ticks)

    x_left, x_right = ax1.get_xlim()
    span = (x_right - x_left)
    ax1.set_xlim(x_left, x_right + span * 0.02)

    # ---- Bottom: stacked in-use (C variant, opaque) ----
    stack_df = pd.concat(grid_in_use_by_asset, axis=1).fillna(0.0)
    # Reorder columns to ASSET_NAMES order (stackplot requires explicit series)
    stack_df = stack_df.reindex(columns=ASSET_NAMES).fillna(0.0)

    ax2.stackplot(
        stack_df.index,
        *(stack_df[n].values for n in ASSET_NAMES),
        labels=[f"{n} (in-use)" for n in ASSET_NAMES],
        colors=[ASSET_COLORS[n] for n in ASSET_NAMES],
        alpha=0.95,
        edgecolor="white",
        linewidth=0.8,
    )

    ax2.set_facecolor("#FAFAFA")
    if not grid_in_use_total.empty:
        last_val = float(grid_in_use_total.values[-1])
        ax2.annotate(f" 合计 ¥{last_val:,.0f}",
                     xy=(grid_in_use_total.index[-1], last_val),
                     xytext=(8, 4), textcoords="offset points",
                     fontsize=10, color="#1B4F72", fontweight="bold", va="bottom")
    ax2.set_ylabel("资金占用 (¥)", fontsize=10, color="#1B4F72")
    ax2.tick_params(axis="y", labelcolor="#1B4F72")
    ax2.set_xlabel("日期", fontsize=11)
    ax2.grid(True, which="major", color="#888888", alpha=0.55, ls=":", lw=0.9)
    ax2.grid(True, which="minor", color="#AAAAAA", alpha=0.30, ls=":", lw=0.6)
    ax2.set_axisbelow(True)
    if not grid_in_use_total.empty:
        ax2.set_ylim(0, max(grid_in_use_total.values.max() * 1.2, 1))
    leg2 = ax2.legend(loc="upper left", frameon=True, framealpha=0.92,
                      fontsize=10, edgecolor="grey")
    if leg2 is not None:
        leg2.get_frame().set_linewidth(0.6)
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=[4, 10]))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved chart → {out_path}")
    return out_path


def main() -> None:
    if not HISTORY.exists():
        print("WARN no history.json; nothing to render", file=sys.stderr)
        return
    history = json.loads(HISTORY.read_text(encoding="utf-8"))
    render_chart(history, OUT_PNG)


if __name__ == "__main__":
    main()
