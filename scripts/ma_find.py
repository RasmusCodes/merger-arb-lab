"""Find where defined terms are DEFINED in the merger agreement (pattern: (the “Term”) or “Term” means) and print
the surrounding sentence window. Usage: python scripts/ma_find.py OUTFILE TERM [TERM ...]"""
import re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
text = (ROOT / "data/raw/sec/merger_agreement_ex21_2026-11-01.txt").read_text(encoding="utf-8")
out = []
for term in sys.argv[2:]:
    pats = [re.compile(r"“" + re.escape(term) + r"”\)"), re.compile(r"“" + re.escape(term) + r"” (shall )?means?"),
            re.compile(re.escape(term), re.I)]
    found = False
    for p in pats[:2]:
        for m in p.finditer(text):
            s, e = max(0, m.start() - 2500), min(len(text), m.end() + 2500)
            out.append(f"===== {term} @ {m.start()} ({p.pattern[:30]})\n{text[s:e]}\n")
            found = True
    if not found:
        hits = [m.start() for m in pats[2].finditer(text)]
        out.append(f"===== {term}: no definition pattern; {len(hits)} plain hits at {hits[:15]}")
        for h in hits[:3]:
            out.append(text[max(0, h - 600): h + 900] + "\n")
pathlib.Path(sys.argv[1]).write_text("\n".join(out), encoding="utf-8")
print("written", sum(len(x) for x in out))
