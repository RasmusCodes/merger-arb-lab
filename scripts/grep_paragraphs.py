"""Print whole paragraphs (lines) of a text file that match any regex, with line numbers, capped per paragraph.

Usage: python grep_paragraphs.py FILE CAP REGEX [REGEX ...] > out.txt
"""
import re, sys, pathlib

path, cap, pats = pathlib.Path(sys.argv[1]), int(sys.argv[2]), [re.compile(p, re.I) for p in sys.argv[3:]]
lines = path.read_text(encoding="utf-8").splitlines()
for i, line in enumerate(lines, 1):
    if any(p.search(line) for p in pats) and len(line) > 60:
        t = line if len(line) <= cap else line[:cap] + f" [...{len(line) - cap} more chars]"
        print(f"--- line {i} ({len(line)} chars)\n{t}\n")
