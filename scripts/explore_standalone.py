"""Exploration: standalone (break) price estimates for LITE and MRVL as of the latest snapshot."""
import pandas as pd

from mergerarb.data import yahoo
from mergerarb.analytics import standalone
from mergerarb.deal import load_deal

deal = load_deal("mrvl_lite")
syms = ["LITE", "MRVL", "SMH", "COHR", "AAOI", "FN", "CIEN", "CRDO", "SOXX", "NVDA", "AVGO", "QQQ"]
px = pd.concat({s: yahoo.load_daily(s)["close"] for s in syms}, axis=1)
as_of = px.index[-1]
tgt, acq = standalone.standalone_table(px, "LITE", "MRVL", deal.unaffected_date, as_of)
print("as of", as_of.date(), "LITE", px["LITE"].iloc[-1], "MRVL", px["MRVL"].iloc[-1])
for e in tgt + acq:
    print(f"{e.symbol:5s} {e.method:62s} est {e.estimate:8.2f}  band [{e.low:8.2f}, {e.high:8.2f}]  betas {({k: round(v, 2) for k, v in e.betas.items()})}  r2 {e.r2 if e.r2 is None else round(e.r2, 2)}  sd_d {e.resid_vol_daily if e.resid_vol_daily is None else round(e.resid_vol_daily, 4)}  h {e.horizon_days}")
for s in syms:
    print(s, "ret since unaffected", round(px[s].iloc[-1] / px.loc[:deal.unaffected_date, s].iloc[-1] - 1, 4))
# realised vols
r = px.pct_change()
print("realised vol (ann) last 60d / since 2026-11-02:")
for s in ["LITE", "MRVL", "SMH"]:
    print(s, round(r[s].tail(60).std() * 252 ** 0.5, 3), round(r[s][r.index >= "2026-11-02"].std() * 252 ** 0.5, 3))
# correlation of daily returns since announcement (LITE vs MRVL)
x = r[r.index >= "2026-11-03"][["LITE", "MRVL"]].dropna()
import numpy as np
b = np.polyfit(x["MRVL"], x["LITE"], 1)
print("since 11-03: corr", round(x.corr().iloc[0, 1], 3), "slope LITE~MRVL", round(b[0], 3), "n", len(x))
