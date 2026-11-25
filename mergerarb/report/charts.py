"""Static report charts (matplotlib, light theme). Palette and chrome follow the validated reference palette:
categorical slots blue/orange/aqua (validated all-pairs, light + dark), sequential blue ramp, hairline grids, ink-coloured
text, one y-axis per chart, legends for >= 2 series plus selective direct labels."""
from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 9.5, "text.parse_math": False,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "axes.titlesize": 11, "axes.titleweight": "semibold", "axes.titlelocation": "left",
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False, "legend.labelcolor": INK2,
    "lines.linewidth": 1.8, "lines.solid_capstyle": "round",
})


def _save(fig, out: pathlib.Path, name: str) -> pathlib.Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{name}.png"
    fig.savefig(p, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return p


def _pct(ax, axis="y", decimals=0):
    fmt = matplotlib.ticker.PercentFormatter(1.0, decimals=decimals)
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def spread_history(hist: pd.DataFrame, announced, out: pathlib.Path) -> pathlib.Path:
    fig, ax = plt.subplots(figsize=(7.6, 3.4))
    s = hist["gross_spread"]
    ax.plot(s.index, s.values, color=S1, marker="o", markersize=3.5)
    ax.axvline(pd.Timestamp(announced), color=AXIS, linewidth=0.9)
    ax.text(pd.Timestamp(announced), s.max() * 1.02, " terms announced (Nov 2)", color=INK2, fontsize=8.5, va="bottom")
    ax.annotate(f"{s.iloc[-1]:.1%} on {s.index[-1]:%b %d}", (s.index[-1], s.iloc[-1]), xytext=(-8, 10),
                textcoords="offset points", ha="right", color=INK, fontsize=9)
    ax.set_ylim(0, max(0.26, s.max() * 1.15))
    _pct(ax)
    ax.set_title("Gross spread: 4.5 x MRVL / LITE - 1 (daily closes)")
    ax.set_ylabel("gross spread")
    ax.xaxis.set_major_locator(matplotlib.dates.WeekdayLocator(byweekday=matplotlib.dates.MO))
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b %d"))
    ax.text(0, -0.17, "Oct 29-30: speculation, terms unknown (ratio applied for reference). Source: Yahoo closes, "
            "verified vs Cboe (to Nov 18) and Google Finance (Nov 23-24).", transform=ax.transAxes, color=MUTED, fontsize=7.5)
    return _save(fig, out, "spread_history")


def comparables(comp: pd.DataFrame, current: float, out: pathlib.Path) -> pathlib.Path:
    c = comp[comp["validation"].isin(["OK", "PARTIAL_NO_VALIDATION", "FAILED_NA"])].copy()
    c = c[c["spread_d16"].notna()]
    c["label"] = c["acquirer"] + " / " + c["target"]
    late = "closed in 2025 (spread data end 2024)"
    c["group"] = np.where(c["outcome"] == "failed", "failed", np.where(c["outcome"] == "closed", "closed", late))
    c = c.sort_values("spread_d16")
    fig, ax = plt.subplots(figsize=(7.6, 5.0))
    colors = {"closed": S1, late: S3, "failed": S2}
    y = np.arange(len(c))
    for g, col in colors.items():
        m = c["group"] == g
        ax.scatter(c.loc[m, "spread_d16"] / 100, y[m.values], s=36, color=col, label=g, zorder=3,
                   edgecolors=SURFACE, linewidths=1.5)
    ax.set_yticks(y)
    ax.set_yticklabels(c["label"], fontsize=8.5)
    ax.axvline(current, color=INK, linewidth=1.2)
    ax.text(current, len(c) - 0.4, f" MRVL / LITE {current:.1%}", color=INK, fontsize=9, va="bottom")
    _pct(ax, "x")
    ax.set_xlim(left=min(0, c["spread_d16"].min() / 100 - 0.01))
    ax.grid(axis="y", visible=False)
    ax.set_title("Spread 16 trading days after announcement: large US deals (CRSP)")
    ax.set_xlabel("gross spread at day 16")
    ax.legend(loc="lower center", bbox_to_anchor=(0.6, 0.0), fontsize=8.5)
    ax.text(0, -0.13, "Cash-and-stock and cash deals use deal value incl. cash. Deals whose terms failed the "
            "final-day convergence check are excluded.", transform=ax.transAxes, color=MUTED, fontsize=7.5)
    return _save(fig, out, "comparables")


def implied_p_heatmap(grid: pd.DataFrame, marks: dict, out: pathlib.Path) -> pathlib.Path:
    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    z = grid.to_numpy(dtype=float)
    im = ax.imshow(z, aspect="auto", origin="lower", cmap=cmap, vmin=0, vmax=1,
                   extent=[-0.5, z.shape[1] - 0.5, -0.5, z.shape[0] - 0.5])
    ax.set_xticks(range(z.shape[1]))
    ax.set_xticklabels([f"+{c:.0%}" for c in grid.columns])
    ys = list(range(0, z.shape[0], 2))
    ax.set_yticks(ys)
    ax.set_yticklabels([f"${grid.index[i]:.0f}" for i in ys])
    ax.grid(False)
    cs = ax.contour(np.arange(z.shape[1]), np.arange(z.shape[0]), z, levels=[0.5, 0.63], colors=[SURFACE, INK],
                    linewidths=[1.2, 1.2])
    ax.clabel(cs, fmt={0.5: "50%", 0.63: "63%"}, fontsize=8, colors=[SURFACE, INK])
    for name, (S, j) in marks.items():
        xi = np.interp(j, list(grid.columns), range(z.shape[1]))
        yi = np.interp(S, list(grid.index), range(z.shape[0]))
        ax.plot(xi, yi, "o", color=SURFACE, markersize=9, markeredgecolor=INK, markeredgewidth=1.2)
        ax.annotate(name, (xi, yi), xytext=(8, -3), textcoords="offset points", color=INK, fontsize=8.5,
                    bbox=dict(boxstyle="round,pad=0.2", fc=SURFACE, ec="none", alpha=0.85))
    cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
    cb.ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    cb.outline.set_visible(False)
    ax.set_title("Break-even completion probability by break assumption")
    ax.set_xlabel("MRVL jump if the deal breaks (reversal of its deal discount)")
    ax.set_ylabel("LITE price if the deal breaks")
    return _save(fig, out, "implied_p_heatmap")


def ev_vs_probability(ev: pd.DataFrame, p_market: float, p_bottom_up: float, hurdle: float,
                      out: pathlib.Path) -> pathlib.Path:
    fig, ax = plt.subplots(figsize=(7.6, 3.8))
    series = [("optimistic break", S3, 5), ("base break", S1, 0), ("stress break", S2, -5)]
    for name, col, dy in series:
        d = ev[ev.break_case == name].sort_values("p_complete")
        ax.plot(d.p_complete, d.excess_over_rf, color=col, label=name)
        ax.annotate(name.replace(" break", ""), (d.p_complete.iloc[-1], d.excess_over_rf.iloc[-1]), xytext=(6, dy),
                    textcoords="offset points", color=INK2, fontsize=8.5, va="center")
    ax.axhline(0, color=AXIS, linewidth=1.0)
    ax.axhline(hurdle, color=MUTED, linewidth=0.8)
    ax.text(0.665, hurdle, f"{hurdle:.0%} hurdle ", color=INK2, fontsize=8, va="bottom", ha="right")
    for p, lab in ((p_market, "arb desks ~50% (Bloomberg)"), (p_bottom_up, f"bottom-up base {p_bottom_up:.0%}")):
        ax.axvline(p, color=AXIS, linewidth=0.9)
        ax.text(p, ax.get_ylim()[0], f" {lab}", rotation=90, color=INK2, fontsize=7.8, va="bottom")
    _pct(ax)
    _pct(ax, "x")
    ax.set_xlim(0.29, 1.0)
    ax.set_title("Expected annual return above Treasuries vs completion probability")
    ax.set_xlabel("probability the deal closes")
    ax.set_ylabel("excess return / yr (Reg T capital)")
    ax.legend(loc="lower right", fontsize=8.5)
    return _save(fig, out, "ev_vs_probability")


def mc_distribution(pnl_base: np.ndarray, pnl_stress: np.ndarray, equity: float, out: pathlib.Path) -> pathlib.Path:
    """Cumulative distribution of account P&L (a CDF shows both the completion jump and the break tails)."""
    fig, ax = plt.subplots(figsize=(7.6, 3.8))
    for arr, col, lab in ((pnl_base, S1, "base model"), (pnl_stress, S2, "stress model")):
        x = np.sort(arr) / equity
        y = np.arange(1, len(x) + 1) / len(x)
        ax.plot(x, y, color=col, label=lab)
        q = np.percentile(arr, 5) / equity
        ax.plot([q], [0.05], "o", color=col, markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5)
        ax.annotate(f"5% worst: {q:.0%} or less ({lab.split()[0]}); mean {arr.mean() / equity:+.1%}", (q, 0.05),
                    xytext=(10, 14 if col == S1 else 34), textcoords="offset points", color=INK2, fontsize=8)
    ax.axvline(0, color=AXIS, linewidth=1.0)
    lo = np.percentile(np.r_[pnl_base, pnl_stress], 0.5) / equity
    hi = np.percentile(np.r_[pnl_base, pnl_stress], 99.5) / equity
    ax.set_xlim(lo, hi)
    ax.set_ylim(0, 1.0)
    _pct(ax, "x")
    _pct(ax)
    ax.set_title("Monte Carlo: account P&L at resolution (4 LITE long / 18 MRVL short, $10k equity)")
    ax.set_xlabel("P&L as % of account equity")
    ax.set_ylabel("share of paths with P&L at or below x")
    ax.legend(loc="upper left", fontsize=8.5)
    ax.text(0, -0.2, "The near-vertical step at about +4% is completion (spread locked). Everything left of it is a deal "
            "break, where the hedge becomes a volatile LITE/MRVL pair.", transform=ax.transAxes, color=MUTED, fontsize=7.5)
    return _save(fig, out, "mc_distribution")


def scenarios(tab: pd.DataFrame, out: pathlib.Path, equity: float) -> pathlib.Path:
    d = tab.copy()
    d["label"] = d["key"].str.replace("_", " ", regex=False).str.replace(r"^(\w+) ", r"\1. ", regex=True)
    d = d.iloc[::-1]
    fig, ax = plt.subplots(figsize=(7.6, 4.6))
    ax.barh(d["label"], d["net_pnl"] / equity, color=S1, height=0.62)
    for i, (v, liq) in enumerate(zip(d["net_pnl"] / equity, d["liquidated"])):
        ax.text(v + (0.002 if v >= 0 else -0.002), i, f"{v:+.1%}" + (" (liquidated)" if liq else ""),
                va="center", ha="left" if v >= 0 else "right", color=INK2, fontsize=8)
    ax.axvline(0, color=AXIS, linewidth=1.0)
    _pct(ax, "x")
    ax.grid(axis="y", visible=False)
    ax.set_title("Scenario P&L, 4 LITE / 18 MRVL on $10k (path-simulated through margin)")
    ax.set_xlabel("net P&L as % of account equity")
    xmin, xmax = (d["net_pnl"] / equity).min(), (d["net_pnl"] / equity).max()
    ax.set_xlim(xmin - 0.03, xmax + 0.03)
    ax.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.02))
    return _save(fig, out, "scenarios")


def margin_sensitivity(rows: pd.DataFrame, out: pathlib.Path) -> pathlib.Path:
    """rows: columns acquirer_move, label, excess_liquidity_pct (excess liquidity / equity)."""
    fig, ax = plt.subplots(figsize=(7.6, 3.6))
    for (lab, d), col in zip(rows.groupby("label", sort=False), (S1, S3, S2)):
        ax.plot(d.acquirer_move, d.excess_liquidity_pct, color=col, label=lab)
        ax.annotate(lab.split(" (")[0], (d.acquirer_move.iloc[-1], d.excess_liquidity_pct.iloc[-1]), xytext=(6, 0),
                    textcoords="offset points", color=INK2, fontsize=8, va="center")
    ax.axhline(0, color=INK, linewidth=1.0)
    ax.text(rows.acquirer_move.min(), 0, " margin call below this line", color=INK2, fontsize=8, va="bottom")
    _pct(ax)
    _pct(ax, "x")
    ax.set_title("Excess liquidity if MRVL rallies and the spread widens to 35%")
    ax.set_xlabel("MRVL move from today")
    ax.set_ylabel("excess liquidity / equity")
    ax.legend(loc="lower left", fontsize=8.5)
    return _save(fig, out, "margin_sensitivity")
