"""Antitrust charts for the report: headline impacts on the trade, and how past antitrust news moved spreads."""
from __future__ import annotations

import pathlib

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from mergerarb.report import charts as C  # applies the shared style

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIG = ROOT / "docs" / "figures"
NEG, POS = "#e34948", C.S1

# 1. headline impacts (base tree, market q 50%, half of MRVL's deal discount reverses on a break)
ni = pd.read_csv(ROOT / "outputs" / "antitrust_news_impact_j135.csv")
keep = ["US agency sues to block", "SAMR acceptance delayed / pull-and-refile", "EU opens phase 2",
        "HSR second request issued", "SAMR accepts filing on time", "EU clears in phase 1",
        "HSR expires, no second request", "Both stockholder votes pass", "SAMR approves (conditional)",
        "A stockholder vote fails"]
d = ni.set_index("headline").loc[keep].reset_index().iloc[::-1]
fig, ax = plt.subplots(figsize=(7.8, 4.6))
cols = [NEG if v < 0 else POS for v in d.arb_pnl_unit]
ax.barh(d.headline, d.arb_pnl_unit, color=cols, height=0.62)
for i, r in enumerate(d.itertuples()):
    lab = f"${r.arb_pnl_unit:+.0f}  ({r.spread_pts * 100:+.1f} pts; LITE {r.tgt_move:+.1%}, MRVL {r.acq_move:+.1%})"
    # labels always sit right of the bar end or of zero, so they never collide with the category labels
    ax.text(max(r.arb_pnl_unit, 0) + 3, i, lab, va="center", ha="left", color=C.INK2, fontsize=7.6)
ax.axvline(0, color=C.AXIS, linewidth=1)
ax.set_xlim(-120, 230)
ax.grid(axis="y", visible=False)
ax.set_title("What each regulatory headline does to the trade (per LITE share hedged with 4.5 MRVL)")
ax.set_xlabel("arb P&L per LITE share, USD (market odds 50%, half of MRVL's deal discount reverses on a break)")
C._save(fig, FIG, "antitrust_headlines")

# 2. event study: spread change around past antitrust news
es = pd.read_csv(ROOT / "data" / "derived" / "antitrust" / "event_study.csv")
es = es.dropna(subset=["spread_change_pts"])
cls = np.where(es.event_type.eq("termination"), "termination",
               np.where(es.direction.eq("positive"), "clearance / approval", "adverse news"))
es = es.assign(cls=cls)
order = ["adverse news", "termination", "clearance / approval"]
colors = {"adverse news": C.S2, "termination": C.S3, "clearance / approval": C.S1}
fig, ax = plt.subplots(figsize=(7.8, 4.2))
rng = np.random.default_rng(1)
for k, c in enumerate(order):
    sub = es[es.cls == c]
    y = np.clip(sub.spread_change_pts * 100, -20, 30)
    x = k + rng.uniform(-0.12, 0.12, len(sub))
    ax.scatter(x, y, s=40, color=colors[c], edgecolors=C.SURFACE, linewidths=1.5, zorder=3, label=c)
    med = float(np.median(sub.spread_change_pts * 100))
    ax.plot([k - 0.25, k + 0.25], [med, med], color=C.INK, linewidth=1.4)
    ax.text(k - 0.27, med, f"median {med:+.1f} ", va="center", ha="right", fontsize=8, color=C.INK2)
    big = sub[sub.spread_change_pts.abs() * 100 > 8].sort_values("spread_change_pts")
    for jj, r in enumerate(big.itertuples()):
        ax.annotate(f"{r.deal} ({r.event_type.replace('_', ' ')})" + (" [clipped, +133]" if r.spread_change_pts > 0.3 else ""),
                    (k, min(r.spread_change_pts * 100, 30)), xytext=(12, -9 + 9 * jj if c == "clearance / approval" else 0),
                    textcoords="offset points", fontsize=7.2, color=C.INK2, va="center")
ax.axhline(0, color=C.AXIS, linewidth=1)
ax.set_xticks(range(3))
ax.set_xticklabels(order)
ax.grid(axis="x", visible=False)
ax.set_ylabel("change in gross spread, points (days -2 to +1)")
ax.set_title("How past antitrust news moved merger spreads (n = %d events, 17 deals)" % len(es))
ax.text(0, -0.17, "Spread = deal value / target price - 1 from the deal terms. Sources: CRSP (to 2024), Yahoo (later / non-S&P "
        "targets); dated events with sources in data/derived/antitrust/events.csv.", transform=ax.transAxes,
        color=C.MUTED, fontsize=7.2)
C._save(fig, FIG, "antitrust_event_study")
print("ok")
