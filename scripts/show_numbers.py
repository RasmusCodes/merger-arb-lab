"""Print the headline numbers from docs/report_numbers.json (for writing the report)."""
import json
import pathlib

n = json.loads((pathlib.Path(__file__).resolve().parents[1] / "docs" / "report_numbers.json").read_text())
m = n["signal"]["metrics"]
print("signal:", n["signal"]["decision"], n["signal"]["reasons"])
for k in ("gross_spread", "completion_pnl_unit", "break_pnl_unit_base", "break_pnl_unit_stress", "breakeven_p_base",
          "breakeven_p_stress", "expected_pnl_unit", "expected_annualized", "expected_excess_annual",
          "required_spread_for_hurdle", "max_borrow_fee_for_hurdle", "edge"):
    print(f"  {k}: {m[k]:.4f}")
print("bottom-up:", [(r["component"][:28], round(r["low"], 3), round(r["base"], 3), round(r["high"], 3)) for r in n["bottom_up"]])
for k in ("mc", "mc_optimistic", "mc_stress"):
    s = n[k]
    print(k, {x: round(s[x], 4) for x in ("p_complete_input", "mean_pnl", "median_pnl", "p_loss", "var95", "es95", "p_liquidation", "mean_years")})
print("sizing:", n["sizing"]["units"], n["sizing"]["binding"], "recommended", n["sizing"]["recommended_units"], "es/unit", round(n["sizing"]["es95_per_unit"]))
print("ev grid base:", [(r["p_complete"], round(r["excess_over_rf"], 4)) for r in n["ev_grid"] if r["break_case"] == "base break" and r["p_complete"] in (0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8)])
