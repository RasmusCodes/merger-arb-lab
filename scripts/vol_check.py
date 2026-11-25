"""Realised-vol windows for the standalone-vol calibration and the post-break pair's relative vol."""
import numpy as np
import pandas as pd

from mergerarb.data import yahoo

px = pd.concat({s: yahoo.load_daily(s)["close"] for s in ["LITE", "MRVL", "SMH", "COHR"]}, axis=1)
r = np.log(px).diff()
u = pd.Timestamp("2026-10-28")
pre = r[r.index <= u]
for w, lab in ((252, "1y"), (504, "2y"), (756, "3y"), (1250, "~5y")):
    x = pre.tail(w)
    rel = (x["LITE"] - x["MRVL"]).std() * np.sqrt(252)
    print(f"{lab:4s} n={len(x):4d}  vol LITE {x['LITE'].std() * np.sqrt(252):.3f}  MRVL {x['MRVL'].std() * np.sqrt(252):.3f}  "
          f"SMH {x['SMH'].std() * np.sqrt(252):.3f}  corr(LITE,MRVL) {x[['LITE', 'MRVL']].corr().iloc[0, 1]:.2f}  rel vol {rel:.3f}  "
          f"LITE start {px['LITE'].reindex(x.index).iloc[0]:.1f}")
