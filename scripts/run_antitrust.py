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
f = lambda v: f"{v:+.3f}"  # noqa: E731

# 1. sensitivities of each leg and the arb to a change in the market's completion probability
st = at.shock_table(L0, M0, R)
st.to_csv(OUT / "antitrust_shock_table.csv", index=False)
print(st[st.dq.isin([-0.2, -0.1, 0.1])].to_string(index=False, float_format=f))

# 2. gate-by-gate probabilities
base = at.RegTree()
optimist = dataclasses.replace(base, p_votes_pass=0.9025, p_samr_approve=0.80)
pessimist = dataclasses.replace(base, p_samr_approve=0.55, p_us_sue_after_sr=0.25, p_us_clear_after_sr=0.40)
for name, t in (("base", base), ("optimist", optimist), ("pessimist", pessimist)):
    print(f"\n{name}: P(complete) = {t.p_complete():.3f}")
    print(t.gates().to_string(index=False, float_format=lambda v: f"{v:.3f}"))

# 3. headline impacts at the market's q (~50%) for two views of how much MRVL's discount reverses
imp = {}
for j in (0.135, 0.27):
    ni = at.news_impact(base, L0, M0, R, j, market_q=0.50)
    imp[j] = ni
    ni.to_csv(OUT / f"antitrust_news_impact_j{int(j * 1000)}.csv", index=False)
    print(f"\nnews impact (market q 50%, MRVL break move +{j:.1%}):")
    print(ni[["headline", "p_event", "dq", "market_q_after", "tgt_move", "acq_move", "spread_pts", "gross_spread_after",
              "arb_pnl_unit"]].to_string(index=False, float_format=f))

# 4. news-flow Monte Carlo: outcomes from YOUR tree, prices from the MARKET's tree
res = {}
for world, you in (("you = market (no edge)", base), ("you more optimistic", optimist), ("you more pessimistic", pessimist)):
    r = at.news_mc(you, base, L0, M0, R, START, units_target=2, cfg=at.NewsMCConfig(n_paths=20000),
                   strategies=at.default_strategies(threshold=0.35))
    res[world] = r
    print(f"\n== {world}: P(complete) you {r['p_complete_you']:.3f} vs market {r['p_complete_market']:.3f}")
    print(r["table"].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
    r["table"].to_csv(OUT / f"antitrust_strategies_{world.split()[0]}_{world.split()[-1].strip('()')}.csv", index=False)

num = {
    "inputs": {"L0": L0, "M0": M0, "R": R, "as_of": str(START)},
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
