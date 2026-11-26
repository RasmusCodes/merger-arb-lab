"""Antitrust analysis run: shock sensitivities, headline impacts, gate-by-gate probabilities, news-flow strategies.
Writes outputs/antitrust_*.csv and docs/antitrust_numbers.json."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib

import pandas as pd

from mergerarb.analytics import antitrust as at

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 30)

L0, M0, R = 487.12, 131.57, 4.5
START = dt.date(2026, 11, 24)
DELTA = 0.2125                     # MRVL deal discount vs its factor-model counterfactual (docs/report_numbers.json)
J_HALF, J_BASE, J_FULL = (k * DELTA / (1 - DELTA) for k in (0.5, 0.75, 1.0))
S_FACTOR = 612.95                  # LITE two-factor standalone estimate (the report's base break value)
THRESHOLD = 0.27                   # second rung of the alert ladder (the pessimistic break case breaks even here)
f = lambda v: f"{v:+.3f}"  # noqa: E731

# 1. sensitivities of each leg and the arb to a change in the market's completion probability
st = at.shock_table(L0, M0, R, rebounds=(0.0, J_HALF, J_BASE, J_FULL))
st.to_csv(OUT / "antitrust_shock_table.csv", index=False)
print(st[st.dq.isin([-0.2, -0.1, 0.1])].to_string(index=False, float_format=f))

# 2. gate-by-gate probabilities
base = at.RegTree()
optimist = dataclasses.replace(base, p_votes_pass=0.9025, p_samr_approve=0.80)
pessimist = dataclasses.replace(base, p_samr_approve=0.55, p_us_sue_after_sr=0.25, p_us_clear_after_sr=0.40)
for name, t in (("base", base), ("optimist", optimist), ("pessimist", pessimist)):
    print(f"\n{name}: P(complete) = {t.p_complete():.3f}")
    print(t.gates().to_string(index=False, float_format=lambda v: f"{v:.3f}"))

# 3. headline impacts at the market's q (~50%) for three views of how much MRVL's discount reverses.
#    Cumulative columns = change from today's prices once the headline is known; *_day = the move on the day
#    (second-stage headlines such as a court ruling start from the post-lawsuit state).
imp = {}
for j in (J_HALF, J_BASE, J_FULL):
    ni = at.news_impact(base, L0, M0, R, j, market_q=0.50)
    imp[round(j, 4)] = ni
    ni.to_csv(OUT / f"antitrust_news_impact_j{int(round(j * 1000))}.csv", index=False)
    print(f"\nnews impact (market q 50%, MRVL break move +{j:.1%}):")
    print(ni[["headline", "p_event", "dq", "tgt_move", "acq_move", "spread_pts", "arb_pnl_unit", "dq_day",
              "tgt_move_day", "acq_move_day", "arb_pnl_unit_day"]].to_string(index=False, float_format=f))

# 4. news-flow Monte Carlo: outcomes from YOUR tree, prices from the MARKET's tree. "Your break value" worlds add the
#    report's view that LITE would trade near its factor-model value after a break (the market implies less).
res = {}
cfg0 = at.NewsMCConfig(n_paths=20000, acq_break_move=J_BASE)
worlds = (("you = market (no edge)", base, cfg0),
          ("your break value (factor model), market's odds", base, dataclasses.replace(cfg0, you_standalone0=S_FACTOR)),
          ("your break value + more optimistic odds", optimist, dataclasses.replace(cfg0, you_standalone0=S_FACTOR)),
          ("your break value + more pessimistic odds", pessimist, dataclasses.replace(cfg0, you_standalone0=S_FACTOR)),
          ("market's break value, more optimistic odds", optimist, cfg0))
for world, you, cfg in worlds:
    r = at.news_mc(you, base, L0, M0, R, START, units_target=2, cfg=cfg,
                   strategies=at.default_strategies(threshold=THRESHOLD))
    res[world] = r
    print(f"\n== {world}: P(complete) you {r['p_complete_you']:.3f} vs market {r['p_complete_market']:.3f}")
    print(r["table"].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    slug = "".join(ch if ch.isalnum() else "_" for ch in world).strip("_")[:60]
    r["table"].to_csv(OUT / f"antitrust_strategies_{slug}.csv", index=False)

num = {
    "inputs": {"L0": L0, "M0": M0, "R": R, "as_of": str(START), "deal_discount": DELTA,
               "rebounds": {"half": J_HALF, "base": J_BASE, "full": J_FULL}, "standalone_factor": S_FACTOR,
               "wait_threshold": THRESHOLD},
    "trees": {n: {"p_complete": t.p_complete(), "gates": t.gates().to_dict("records"), "params": {
        k: (str(v) if isinstance(v, (dt.date, tuple)) else v) for k, v in dataclasses.asdict(t).items()}}
              for n, t in (("base", base), ("optimist", optimist), ("pessimist", pessimist))},
    "shock_table": st.to_dict("records"),
    "news_impact": {str(j): v.to_dict("records") for j, v in imp.items()},
    "strategies": {w: r["table"].to_dict("records") for w, r in res.items()},
    "state0": {k: getattr(res["you = market (no edge)"]["state0"], k) for k in ("q", "S", "M_B", "M_C")},
}
(ROOT / "docs" / "antitrust_numbers.json").write_text(json.dumps(num, indent=1, default=str), encoding="utf-8")
print("\nwrote docs/antitrust_numbers.json")
