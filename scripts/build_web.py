"""Build the public static dashboard's data (web/data/snapshot.json) and the JS-vs-Python parity vectors
(web/data/parity.json). Publishes DERIVED data only: the spread series (a ratio), two closing prints with their
timestamp, model estimates and comparables statistics - no raw vendor price tables, no account data."""
from __future__ import annotations

import datetime as dt
import json
import pathlib

import numpy as np
import pandas as pd

from mergerarb.analytics import margin as mg
from mergerarb.analytics import scenarios as sc
from mergerarb.analytics import signal as sg
from mergerarb.analytics.probability import BreakCase
from mergerarb.pipeline import Inputs, run

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "data"
OUT.mkdir(parents=True, exist_ok=True)

res = run(inp=Inputs(mc_paths=4000))
deal = res["deal"]
L0, M0, R = res["L0"], res["M0"], deal.exchange_ratio
start = res["as_of"].date()
quotes = {s: {"last": q.last, "time_utc": q.time_utc.isoformat(), "label": a.label, "source": q.source}
          for s, (q, a) in res["quotes"].items()}
hist = res["history_from_speculation"]
comp = pd.read_csv(ROOT / "data" / "derived" / "comparables" / "comparables_summary.csv")
comp = comp[comp["validation"].isin(["OK", "PARTIAL_NO_VALIDATION", "FAILED_NA"])]
from mergerarb.data import options as op  # noqa: E402

curve_date, curve = op.load_treasury_curve()
snap = {
    "as_of": str(start), "built_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    "deal": {"id": deal.id, "name": deal.name, "target": deal.target.ticker, "acquirer": deal.acquirer.ticker,
             "ratio": R, "cash": deal.cash_per_share, "announced": str(deal.announced),
             "unaffected_date": str(deal.unaffected_date), "expected_close": str(deal.expected_close),
             "expected_close_text": deal.expected_close_text, "outside_date": str(deal.outside_date),
             "final_outside_date": str(deal.final_outside_date), "unaffected_target": deal.unaffected_target,
             "unaffected_acquirer": deal.unaffected_acquirer,
             "acq_dividend": {"amount": deal.acquirer_dividend.amount, "freq": deal.acquirer_dividend.frequency_per_year,
                              "last_ex_dates": [str(d) for d in deal.acquirer_dividend.last_ex_dates]},
             "regulatory_fee_per_share": deal.regulatory_fee_per_target_share(),
             "regulatory_fee_per_acquirer_share": deal.regulatory_fee_per_acquirer_share(),
             "fee_tax_rate": res["inputs"].fee_tax_rate,
             "sources": deal.sources},
    "quotes": quotes, "L0": L0, "M0": M0,
    "spread_history": [{"date": str(d.date()), "gross_spread": round(float(g), 6)} for d, g in hist["gross_spread"].items()],
    "standalone": {"factor_model": res["S_base"], "sigma": res["S_sigma"], "stress": res["S_stress"],
                   "stress_peer": res["stress_peer"], "blend": res["S_blend"],
                   "acquirer_counterfactual": res["M_counterfactual"], "deal_discount": res["deal_discount"],
                   "target_estimates": [{"method": e.method, "estimate": e.estimate, "low": e.low, "high": e.high}
                                        for e in res["standalone_target"]]},
    "model": {"p_complete": res["inputs"].p_complete, "reg_break_share": res["reg_break_share"],
              "fee_target_after_tax": res["fee_target_after_tax"], "fee_acquirer_after_tax": res["fee_acquirer_after_tax"],
              "entry_cost_unit": res["entry_cost_unit"], "years_to_break": res["years_to_break"],
              "financing_rate": res["financing_rate"], "rebound_kappa": res["rebound_kappa"],
              "kappa_range": list(res["kappa_range"])},
    "calibration": res["calibration"], "curve": {str(k): v for k, v in curve.items()}, "curve_date": str(curve_date),
    "bottom_up": res["bottom_up"].table().to_dict(orient="records"),
    "signal": {"decision": res["signal"].decision, "reasons": res["signal"].reasons, "metrics": res["signal"].metrics},
    "signal_pessimistic": {"decision": res["signal_pessimistic"].decision, "reasons": res["signal_pessimistic"].reasons,
                           "metrics": res["signal_pessimistic"].metrics},
    "comparables": comp[["acquirer", "target", "consideration", "outcome", "spread_d16", "median_spread", "max_spread",
                         "calendar_days", "validation"]].to_dict(orient="records"),
    "borrow_fee": res["inputs"].borrow_fee,
}
# antitrust: gate probabilities and headline impacts (base tree, market q 50%, half / full MRVL reversal)
from mergerarb.analytics import antitrust as at  # noqa: E402

_tree = at.RegTree()
snap["antitrust"] = {
    "gates": _tree.gates().to_dict("records"),
    "impact_half": at.news_impact(_tree, L0, M0, R, res["rebound_half"], market_q=0.5).fillna(-1).to_dict("records"),
    "impact_base": at.news_impact(_tree, L0, M0, R, res["rebound_base"], market_q=0.5).fillna(-1).to_dict("records"),
    "impact_full": at.news_impact(_tree, L0, M0, R, res["rebound_full"], market_q=0.5).fillna(-1).to_dict("records"),
    "gap_at_q50": at.from_q_and_rebound(L0, M0, R, 0.5, res["rebound_base"]).gap,
}
m_, mp_ = res["signal"].metrics, res["signal_pessimistic"].metrics
snap["verdict_note"] = (
    f"Corrected for the $3.2bn regulatory fee (both legs), the cost of financing the long and a break value centred on "
    f"the factor model (${res['S_base']:.0f}): about {m_['expected_excess_annual']:+.1%} a year above Treasuries on "
    f"margin capital at {res['inputs'].p_complete:.0%} completion odds, just over the 5% hurdle (it needs a "
    f"{m_['required_spread_for_hurdle']:.1%} spread). The edge rests on {deal.target.ticker} being worth about "
    f"${res['S_base']:.0f} on its own; with the first version's blended break value (${res['S_blend']:.0f}) it is "
    f"{mp_['expected_excess_annual']:+.1%} a year, and the stress case loses. One lot per $10k at most.")
(OUT / "snapshot.json").write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")

# ---------------- parity vectors: the browser engine must reproduce these
common = dict(risk_free=res["risk_free"], financing_rate=res["financing_rate"], reg_break_share=res["reg_break_share"],
              fee_target=res["fee_target_after_tax"], fee_acquirer=res["fee_acquirer_after_tax"],
              entry_cost_unit=res["entry_cost_unit"], exit_cost_unit_break=res["entry_cost_unit"],
              break_years=res["years_to_break"])
tax = res["inputs"].fee_tax_rate
cases = []
for lots, Lx, Mx, p, S, kappa, fee in ((2, L0, M0, res["inputs"].p_complete, res["S_base"], res["rebound_kappa"], 0.0032),
                                       (1, 480.0, 135.0, 0.5, 600.0, 1.0, 0.01),
                                       (3, 500.0, 120.0, 0.8, 430.0, 0.0, 0.0032),
                                       (2, L0, M0, res["inputs"].p_complete, res["S_blend"], 0.5, 0.0032)):
    delta = 1 - Mx / res["M_counterfactual"]
    j = kappa * delta / (1 - delta)
    base, stress = BreakCase("b", S, j), BreakCase("s", res["S_stress"], delta / (1 - delta))
    si = sg.evaluate(deal, sg.SignalInputs(Lx, Mx, start, deal.expected_close, fee, p, base, stress, **common))
    pos = mg.units_to_position(lots, R)
    specs = sc.default_scenarios(deal, start, Lx, Mx, R * Mx / Lx - 1, res["S_base"], res["S_stress"], j,
                                 delta / (1 - delta), dt.date(2027, 6, 30), fee, tax)
    scen = {s.key: (lambda r: {"net_pnl": r.net_pnl, "liquidated": r.liquidated, "dividends_paid": r.dividends_paid,
                               "borrow_cost": r.borrow_cost, "min_excess_liquidity": r.min_excess_liquidity})(
        sc.run_scenario(s, deal, pos, 10_000.0, Lx, Mx, start, fee)) for s in specs}
    units, binding, _ = mg.max_units(10_000.0, Lx, Mx, R)
    cases.append({"inputs": {"lots": lots, "L0": Lx, "M0": Mx, "p": p, "S": S, "kappa": kappa, "fee": fee,
                             "delta": delta, "rf": res["risk_free"], "fin": res["financing_rate"],
                             "w": res["reg_break_share"], "fL": res["fee_target_after_tax"],
                             "fM": res["fee_acquirer_after_tax"], "entry": res["entry_cost_unit"],
                             "tb": res["years_to_break"], "tax": tax, "equity": 10_000.0},
                  "signal": si.metrics, "decision": si.decision, "scenarios": scen,
                  "max_units": units, "binding": binding})
(OUT / "parity.json").write_text(json.dumps(cases, indent=1, default=str), encoding="utf-8")
print("wrote", OUT / "snapshot.json", OUT / "parity.json")
