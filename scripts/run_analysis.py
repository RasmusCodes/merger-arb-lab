"""Run the full analysis and write tables to outputs/ (CSV/JSON) plus a console summary."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys

import pandas as pd

from mergerarb.pipeline import Inputs, run

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 30)
pd.set_option("display.max_colwidth", 70)

res = run(inp=Inputs(mc_paths=int(sys.argv[1]) if len(sys.argv) > 1 else 20000))
d = res["deal"]
print(f"as of {res['as_of'].date()}  {d.target.ticker} {res['L0']:.2f}  {d.acquirer.ticker} {res['M0']:.2f}")
for s, (q, a) in res["quotes"].items():
    print(f"  quote {s}: {q.last} @ {q.time_utc} [{a.label}] src={q.source} warnings={list(a.warnings)}")
print(f"implied value {res['implied_value']:.2f}  spread ${res['spread_usd']:.2f}  gross {res['gross_spread']:.2%}  "
      f"years {res['years_to_close']:.3f}  ann {res['annualized_if_complete']:.2%} (simple {res['annualized_simple']:.2%})")
st = res["spread_stats"]
print(f"spread since announcement: mean {st.mean:.2%} sd {st.std:.2%} min {st.min:.2%} max {st.max:.2%} pct {st.percentile:.0f} "
      f"z {st.zscore:.2f} daily-change sd {st.daily_change_vol:.2%} n {st.n_obs}")
print("\nstandalone target:")
for e in res["standalone_target"]:
    print(f"  {e.method:62s} {e.estimate:8.2f} [{e.low:8.2f}, {e.high:8.2f}]")
print("standalone acquirer:")
for e in res["standalone_acquirer"]:
    print(f"  {e.method:62s} {e.estimate:8.2f} [{e.low:8.2f}, {e.high:8.2f}]")
print(f"S_base {res['S_base']:.2f}  S_blend {res['S_blend']:.2f}  S_stress {res['S_stress']:.2f} ({res['stress_peer']})  M_cf {res['M_counterfactual']:.2f}  "
      f"deal discount {res['deal_discount']:.2%}  rebound base {res['rebound_base']:.2%} full {res['rebound_full']:.2%}")
cb = res["carry_unit"]
print(f"\ncarry per unit to close: dividends {cb.acquirer_dividends_paid:.2f} ({cb.n_dividends}), borrow {cb.borrow_fee:.2f}; "
      f"completion P&L/unit {res['completion_pnl_unit']:.2f}")
print(res["break_cases"].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print("\nmarket-implied standalone:", {k: round(v, 1) for k, v in res["market_implied_standalone"].items()})
print("\nimplied p grid (rows S, cols rebound):\n", res["implied_p_grid"].round(2).to_string())
print("\nbottom-up:\n", res["bottom_up"].table()[["component", "low", "base", "high"]].to_string(index=False))
print(f"\nrisk-free to close {res['risk_free']:.2%}")
g = res["ev_grid"].pivot(index="p_complete", columns="break_case", values="excess_over_rf")
print("excess annual return over rf by p:\n", g.round(3).to_string())
print("\ncalibration:", res["calibration"])
sz = res["sizing"]
print(f"\nsizing: units {sz['units']} binding '{sz['binding']}' position {sz['position']} gross lev {sz['state'].gross_leverage:.2f} "
      f"excess liq {sz['state'].excess_liquidity:.0f} liq moves {sz['liquidation_moves']}")
cols = ["key", "outcome", "end_date", "net_pnl", "return_on_equity", "annualized_roe", "dividends_paid", "borrow_cost",
        "min_excess_liquidity", "max_drawdown", "liquidated"]
print("\nscenarios (sized position):\n", res["scenario_table"][cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print(f"\nscenarios at max Reg T leverage {res['max_leverage_position']}:\n",
      res["scenario_table_max_leverage"][cols].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print("\nsize table:\n", res["sizing"]["table"][["units", "target_shares", "acquirer_short", "gross_leverage",
      "excess_liquidity", "worst_stress_loss_pct", "survival1_ok", "survival2_ok", "survival3_ok"]].head(8).to_string(index=False))
for k in ("mc", "mc_optimistic", "mc_stress"):
    m = res[k]
    print(f"\n{k}: ", {kk: round(v, 4) if isinstance(v, float) else v for kk, v in m.summary.items()})
    print(m.by_outcome.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print("\nMC standalone-vol sensitivity:\n", res["mc_vol_sensitivity"].to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
print(f"ES95 per unit {res['sizing']['es95_per_unit']:.0f}  ES-cap units {res['sizing']['es_cap_units']}  "
      f"recommended units {res['sizing']['recommended_units']}")
print("\nhedges (base):\n", res["hedges"].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
print("hedges (stress):\n", res["hedges_stress"].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
sig = res["signal"]
print(f"\nSIGNAL: {sig.decision}")
for r_ in sig.reasons:
    print("  -", r_)
print({k: round(v, 4) for k, v in sig.metrics.items()})
print("\nIV acquirer:\n", res["iv_acquirer"][["expiry", "T", "atm_iv", "call_iv", "put_iv", "implied_borrow_proxy"]].round(3).to_string(index=False))

# ---- write outputs
stamp = res["as_of"].strftime("%Y%m%d")
res["history_from_speculation"].to_csv(OUT / f"spread_history_{stamp}.csv")
res["break_cases"].to_csv(OUT / f"break_cases_{stamp}.csv", index=False)
res["implied_p_grid"].to_csv(OUT / f"implied_p_grid_{stamp}.csv")
res["ev_grid"].to_csv(OUT / f"ev_grid_{stamp}.csv", index=False)
res["scenario_table"].to_csv(OUT / f"scenarios_{stamp}.csv", index=False)
res["scenario_table_max_leverage"].to_csv(OUT / f"scenarios_max_leverage_{stamp}.csv", index=False)
res["sizing"]["table"].to_csv(OUT / f"size_table_{stamp}.csv", index=False)
res["hedges"].to_csv(OUT / f"hedges_{stamp}.csv", index=False)
res["mc"].by_outcome.to_csv(OUT / f"mc_by_outcome_{stamp}.csv", index=False)
res["mc"].fan.to_csv(OUT / f"mc_fan_{stamp}.csv")
summary = {
    "as_of": str(res["as_of"].date()), "L0": res["L0"], "M0": res["M0"], "implied_value": res["implied_value"],
    "gross_spread": res["gross_spread"], "annualized_if_complete": res["annualized_if_complete"],
    "years_to_close": res["years_to_close"], "S_base": res["S_base"], "S_stress": res["S_stress"],
    "deal_discount": res["deal_discount"], "completion_pnl_unit": res["completion_pnl_unit"],
    "calibration": res["calibration"], "mc": res["mc"].summary, "mc_stress": res["mc_stress"].summary,
    "signal": {"decision": sig.decision, "reasons": sig.reasons, "metrics": sig.metrics},
    "sizing": {"units": sz["units"], "binding": sz["binding"], "target_shares": sz["position"].target_shares,
               "acquirer_short": sz["position"].acquirer_short_shares},
}
(OUT / f"summary_{stamp}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print("\nwrote outputs to", OUT)
