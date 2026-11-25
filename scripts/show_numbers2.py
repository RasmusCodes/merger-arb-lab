import json
import pathlib

n = json.loads((pathlib.Path(__file__).resolve().parents[1] / "docs" / "report_numbers.json").read_text())
for h in n["hedges"]:
    print(f"{h['hedge']:26s} h={h['short_per_long']:.2f} mean={h['mean']:.1f} std={h['std']:.0f} p_loss={h['p_loss']:.2f} es95={h['es95']:.0f} comp={h['mean_if_complete']:.1f} brk={h['mean_if_break']:.1f}")
for s in n["mc_vol_sensitivity"]:
    print(s["standalone_vol"], round(s["vol"], 3), round(s["mean_pnl"]), round(s["es95"]))
print("p95 drawdown base", round(n["mc"]["p95_max_drawdown"], 3))
