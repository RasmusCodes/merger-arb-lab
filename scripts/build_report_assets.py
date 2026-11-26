"""Build report charts (docs/figures/) and a numbers JSON (docs/report_numbers.json) from one pipeline run."""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd

from mergerarb.analytics import margin as mg
from mergerarb.pipeline import Inputs, run
from mergerarb.report import charts

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIG = ROOT / "docs" / "figures"

res = run(inp=Inputs(mc_paths=20000))
deal = res["deal"]
L0, M0, R = res["L0"], res["M0"], deal.exchange_ratio

charts.spread_history(res["history_from_speculation"], deal.announced, FIG)
comp = pd.read_csv(ROOT / "data" / "derived" / "comparables" / "comparables_summary.csv")
charts.comparables(comp, res["gross_spread"], FIG)
cn = res["case_names"]
bc = res["break_cases"].set_index("case")
marks = {k: (bc.loc[cn[k], "target_standalone"], bc.loc[cn[k], "acquirer_rebound"])
         for k in ("optimistic", "base", "pessimistic", "stress")}
charts.implied_p_heatmap(res["implied_p_grid"], marks, FIG, res["inputs"].p_complete)
charts.ev_vs_probability(res["ev_grid"], 0.50, res["inputs"].p_complete, 0.05, FIG)
charts.mc_distribution(res["mc"].pnl, res["mc_stress"].pnl, res["inputs"].equity, FIG)
charts.scenarios(res["scenario_table"], FIG, res["inputs"].equity)

# margin sensitivity: MRVL rally with the spread widening to 35% (LITE = 4.5*M/1.35)
rows = []
equity = res["inputs"].equity
for lab, pos in (("1 lot (2 LITE / 9 MRVL), tail-risk cap", mg.units_to_position(1, R)),
                 ("2 lots (4 / 18), loss-budget cap", mg.units_to_position(2, R)),
                 (f"max Reg T ({res['max_leverage_position'].target_shares} / {res['max_leverage_position'].acquirer_short_shares})",
                  res["max_leverage_position"])):
    for mv in np.linspace(0, 1.5, 31):
        M = M0 * (1 + mv)
        L = R * M / 1.35 if mv > 0 else L0
        eq = equity + pos.target_shares * (L - L0) - pos.acquirer_short_shares * (M - M0)
        st = mg.account_state(eq, pos, L, M)
        rows.append({"label": lab, "acquirer_move": mv, "excess_liquidity_pct": st.excess_liquidity / equity})
charts.margin_sensitivity(pd.DataFrame(rows), FIG)

sig = res["signal"]
sz = res["sizing"]
num = {
    "as_of": str(res["as_of"].date()), "L0": L0, "M0": M0, "implied_value": res["implied_value"],
    "spread_usd": res["spread_usd"], "gross_spread": res["gross_spread"], "years_to_close": res["years_to_close"],
    "annualized_if_complete": res["annualized_if_complete"], "spread_stats": res["spread_stats"].__dict__,
    "S_base": res["S_base"], "S_sigma": res["S_sigma"], "S_blend": res["S_blend"], "S_stress": res["S_stress"],
    "stress_peer": res["stress_peer"], "M_counterfactual": res["M_counterfactual"], "deal_discount": res["deal_discount"],
    "rebound_half": res["rebound_half"], "rebound_base": res["rebound_base"], "rebound_full": res["rebound_full"],
    "rebound_kappa": res["rebound_kappa"], "kappa_range": res["kappa_range"], "peer_returns": res["peer_returns"],
    "p_complete": res["inputs"].p_complete, "reg_break_share": res["reg_break_share"],
    "fee_target_after_tax": res["fee_target_after_tax"], "fee_acquirer_after_tax": res["fee_acquirer_after_tax"],
    "entry_cost_unit": res["entry_cost_unit"], "years_to_break": res["years_to_break"],
    "financing_rate": res["financing_rate"],
    "completion_pnl_unit": res["completion_pnl_unit"], "carry_unit": res["carry_unit"].__dict__,
    "break_cases": res["break_cases"].to_dict(orient="records"),
    "market_implied_standalone": res["market_implied_standalone"],
    "bottom_up": res["bottom_up"].table().to_dict(orient="records"), "risk_free": res["risk_free"],
    "ev_grid": res["ev_grid"].to_dict(orient="records"), "calibration": res["calibration"],
    "sizing": {"units": sz["units"], "binding": sz["binding"], "recommended_units": sz["recommended_units"],
               "es95_per_unit": sz["es95_per_unit"], "liquidation_moves": sz["liquidation_moves"],
               "table": sz["table"].head(8).to_dict(orient="records")},
    "scenarios": res["scenario_table"].drop(columns=["note"]).to_dict(orient="records"),
    "scenarios_max_leverage": res["scenario_table_max_leverage"].drop(columns=["note"]).to_dict(orient="records"),
    "max_leverage_position": [res["max_leverage_position"].target_shares, res["max_leverage_position"].acquirer_short_shares],
    "mc": res["mc"].summary, "mc_by_outcome": res["mc"].by_outcome.to_dict(orient="records"),
    "mc_optimistic": res["mc_optimistic"].summary, "mc_stress": res["mc_stress"].summary,
    "mc_v1_base": res["mc_v1_base"].summary,
    "mc_stress_by_outcome": res["mc_stress"].by_outcome.to_dict(orient="records"),
    "mc_vol_sensitivity": res["mc_vol_sensitivity"].to_dict(orient="records"),
    "hedges": res["hedges"].to_dict(orient="records"), "hedges_stress": res["hedges_stress"].to_dict(orient="records"),
    "signal": {"decision": sig.decision, "reasons": sig.reasons, "metrics": sig.metrics},
    "signal_pessimistic": {"decision": res["signal_pessimistic"].decision, "reasons": res["signal_pessimistic"].reasons,
                           "metrics": res["signal_pessimistic"].metrics},
    "iv_acquirer": res["iv_acquirer"].to_dict(orient="records"), "iv_target": res["iv_target"].to_dict(orient="records"),
}
(ROOT / "docs").mkdir(exist_ok=True)
(ROOT / "docs" / "report_numbers.json").write_text(json.dumps(num, indent=1, default=str), encoding="utf-8")
print("figures:", sorted(p.name for p in FIG.glob("*.png")))
print("signal", sig.decision, sig.reasons)
