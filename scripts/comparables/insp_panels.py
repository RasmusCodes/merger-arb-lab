"""Inspect CRSP panel layout (licensed data; prints structure only)."""
import os
import pandas as pd

d = os.environ["MERGERARB_CRSP_DIR"]
s = pd.read_parquet(os.path.join(d, "securities.parquet"))
print("securities cols:", list(s.columns)[:15], len(s))
for f in ("panel_close.parquet", "panel_tr.parquet", "benchmarks_daily.parquet"):
    x = pd.read_parquet(os.path.join(d, f))
    print(f, x.shape, "index:", type(x.index).__name__, str(x.index[:2].tolist()), "cols:", list(x.columns)[:6])
