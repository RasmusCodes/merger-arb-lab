"""Extract the merger-agreement clauses that drive the arbitrage (ratio adjustments, approvals, regulatory efforts,
dividends, outside date, fees, MAE carve-outs) into a scratch text file for reading.

Usage: python scripts/ma_clauses.py OUTFILE
"""
import re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
lines = (ROOT / "data/raw/sec/merger_agreement_ex21_2026-11-01.txt").read_text(encoding="utf-8").splitlines()

PATTERNS = {
    "burdensome": r"^.{0,40}Burdensome Condition",
    "outside_date": r"^.{0,80}Outside Date",
    "adjustment": r"equitably adjusted",
    "stockholder_approval": r"^.{0,60}(Parent|Company) Stockholder Approval",
    "required_reg": r"^.{0,80}Required Regulatory Approvals",
    "reg_fee": r"^.{0,120}Regulatory Termination Fee",
    "mae_def": r"^.{0,60}Material Adverse Effect. (shall )?mean",
    "dividends": r"(declare|set aside|pay).{0,80}dividend",
    "efforts": r"reasonable best efforts.{0,200}(Antitrust|Regulatory|Governmental)",
    "litigation": r"^.{0,80}(Restraint|Legal Restraint)",
    "fractional": r"^.{0,40}Fractional Shares",
}
cap = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
out = []
for name, pat in PATTERNS.items():
    rx = re.compile(pat, re.I)
    hits = [(i, l) for i, l in enumerate(lines, 1) if rx.search(l) and len(l) > 80]
    out.append(f"===== {name}: {len(hits)} hits")
    for i, l in hits[:8]:
        t = l if len(l) <= cap else l[:cap] + f" [...{len(l) - cap} more]"
        out.append(f"--- line {i} ({len(l)} chars)\n{t}\n")
pathlib.Path(sys.argv[1]).write_text("\n".join(out), encoding="utf-8")
print("written", sum(len(x) for x in out))
