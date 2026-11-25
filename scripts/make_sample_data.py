"""Generate SYNTHETIC sample data for offline tests and demos (data/sample/). These are NOT market prices.

The generator mimics the structure of the real case so the full pipeline can run without network access or licensed
data: correlated GBM paths for the target, acquirer and peers until an "unaffected" date, then the target trades at
ratio x acquirer x (1 - discount) with a noisy discount. Every consumer labels these as synthetic.
The US Treasury par curve file is real public-domain data (home.treasury.gov) and is copied as is.
"""
from __future__ import annotations

import json
import pathlib
import shutil

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "sample"
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(42)

days = pd.bdate_range("2024-06-03", "2026-11-24")
n = len(days)
syms = ["LITE", "MRVL", "SMH", "COHR", "AAOI", "FN", "CIEN", "CRDO", "SOXX", "NVDA", "AVGO", "QQQ"]
vol = np.array([0.85, 0.70, 0.38, 0.60, 0.95, 0.50, 0.55, 0.80, 0.38, 0.55, 0.45, 0.22]) / np.sqrt(252)
beta = np.array([1.4, 1.4, 1.0, 1.2, 1.6, 1.0, 1.1, 1.5, 1.0, 1.3, 1.1, 0.6])
mkt = rng.standard_normal(n) * 0.018
idio = rng.standard_normal((n, len(syms))) * vol * 0.75
r = beta * mkt[:, None] + idio
start = np.array([200.0, 90.0, 230.0, 80.0, 25.0, 150.0, 60.0, 50.0, 220.0, 120.0, 160.0, 450.0])
px = start * np.exp(np.cumsum(r, axis=0))
df = pd.DataFrame(px, index=days, columns=syms)
u = days.get_loc(pd.Timestamp("2026-10-28"))
disc = np.clip(0.06 + np.cumsum(rng.standard_normal(n - u - 1) * 0.012), 0.04, 0.30)
df.iloc[u + 1:, 0] = 4.5 * df.iloc[u + 1:, 1].to_numpy() * (1 - disc)
df.index.name = "date"
df.round(4).to_csv(OUT / "synthetic_daily.csv")
last = df.iloc[-1]
quotes = {s: {"last": float(round(last[s], 2)), "prev_close": float(round(df.iloc[-2][s], 2)),
              "time_utc": "2026-11-24T21:00:00+00:00", "source": "SYNTHETIC SAMPLE (not market data)",
              "market_state": "POSTPOST", "delay_minutes": 0} for s in syms}
(OUT / "synthetic_quotes.json").write_text(json.dumps(quotes, indent=1), encoding="utf-8")
tre = sorted((ROOT / "data" / "raw" / "treasury").glob("*/par_curve_*.csv"))
if tre:
    shutil.copy(tre[-1], OUT / "treasury_par_curve_2026.csv")
print("wrote", sorted(p.name for p in OUT.iterdir()))
