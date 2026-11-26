"""How far do single names stray from their factor model in this crash? (evidence for the break-value distribution)

For ~50 AI / semiconductor / AI-infrastructure names: 252-session OLS betas on SMH and an equal-weighted optical index
(COHR, AAOI, FN, CIEN), estimated up to the unaffected date (2026-10-28); factor-predicted log return over the crash
window (unaffected date -> as-of date); actual log return; residual. Then:
  * the cross-sectional distribution of residuals = an empirical prior for how far LITE's standalone value could sit
    from its own factor-model projection (used as the Monte Carlo's standalone uncertainty);
  * a test of the "momentum crash" story (past winners falling more than their betas say): residual ~ 12m / 6m momentum.
LITE and MRVL are excluded from the fits (both are deal-affected).

Writes derived numbers only (betas, returns, residuals) to data/derived/standalone/crash_crosssection.csv; raw prices
are not saved. Yahoo chart API (unofficial; research use).

    python scripts/crash_crosssection.py [--asof 2026-11-24]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time

import numpy as np
import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
NAMES = ["NVDA", "AVGO", "AMD", "MU", "TSM", "ANET", "ALAB", "CRDO", "COHR", "AAOI", "FN", "CIEN", "GLW", "VRT", "SMCI",
         "CLS", "MPWR", "ARM", "QCOM", "INTC", "AMAT", "LRCX", "KLAC", "TXN", "ADI", "NXPI", "ON", "MCHP", "SNPS", "CDNS",
         "DELL", "HPE", "ORCL", "PLTR", "VST", "CEG", "NBIS", "CRWV", "IREN", "APLD", "SITM", "MTSI", "LSCC", "ONTO",
         "AMKR", "STX", "WDC", "SNDK", "TER", "LITE", "MRVL", "SMH"]
OPT = ["COHR", "AAOI", "FN", "CIEN"]


def fetch(names: list[str]) -> pd.DataFrame:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (research script)"
    tz = dt.timezone(dt.timedelta(hours=-5))
    out = {}
    for n in names:
        r = s.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{n}", params={"range": "3y", "interval": "1d"},
                  timeout=20)
        res = r.json()["chart"]["result"][0]
        idx = pd.to_datetime([dt.datetime.fromtimestamp(t, tz).date() for t in res["timestamp"]])
        adj = pd.Series(res["indicators"]["adjclose"][0]["adjclose"], index=idx, dtype=float)
        adj = adj.fillna(pd.Series(res["indicators"]["quote"][0]["close"], index=idx, dtype=float))
        out[n] = adj[~adj.index.duplicated(keep="last")]
        time.sleep(0.4)
    return pd.DataFrame(out).sort_index()


def crosssection(P: pd.DataFrame, unaffected: str, asof: str, window: int = 252) -> pd.DataFrame:
    P = P.loc[:asof]
    lr = np.log(P).diff()
    lr["OPT"] = lr[OPT].mean(axis=1)
    U, E = pd.Timestamp(unaffected), pd.Timestamp(asof)
    est, fwd = lr[lr.index <= U].tail(window), lr[(lr.index > U) & (lr.index <= E)]
    rows = []
    for n in [c for c in P.columns if c != "SMH"]:
        d = est[[n, "SMH", "OPT"]].dropna()
        if len(d) < 0.8 * window:
            continue
        X = np.column_stack([np.ones(len(d)), d[["SMH", "OPT"]].to_numpy()])
        b, *_ = np.linalg.lstsq(X, d[n].to_numpy(), rcond=None)
        pred = float(fwd[["SMH", "OPT"]].sum().to_numpy() @ b[1:])
        act = float(fwd[n].sum())
        p0 = P[n].loc[:U].dropna()
        rows.append(dict(name=n, beta_smh=b[1], beta_opt=b[2], predicted=pred, actual=act, residual=act - pred,
                         mom12=float(np.log(p0.iloc[-1] / p0.iloc[-253])) if len(p0) > 253 else np.nan,
                         mom6=float(np.log(p0.iloc[-1] / p0.iloc[-127])) if len(p0) > 127 else np.nan))
    return pd.DataFrame(rows).set_index("name")


def summarise(T: pd.DataFrame) -> dict:
    sample = T.drop(index=[x for x in ("LITE", "MRVL") if x in T.index]).dropna()
    r = sample["residual"]
    out = {"n": int(len(r)), "mean": float(r.mean()), "sd": float(r.std()),
           **{f"p{q}": float(r.quantile(q / 100)) for q in (5, 10, 25, 50, 75, 90, 95)}}
    for m in ("mom12", "mom6"):
        X = np.column_stack([np.ones(len(sample)), sample[m]])
        b, *_ = np.linalg.lstsq(X, r.to_numpy(), rcond=None)
        e = r.to_numpy() - X @ b
        se = np.sqrt(e.var(ddof=2) * np.linalg.inv(X.T @ X).diagonal())
        out[f"slope_{m}"], out[f"t_{m}"] = float(b[1]), float(b[1] / se[1])
        out[f"lite_predicted_residual_{m}"] = float(b[0] + b[1] * T.loc["LITE", m]) if "LITE" in T.index else None
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--unaffected", default="2026-10-28")
    ap.add_argument("--asof", default="2026-11-24")
    a = ap.parse_args()
    T = crosssection(fetch(NAMES), a.unaffected, a.asof)
    dest = ROOT / "data" / "derived" / "standalone"
    dest.mkdir(parents=True, exist_ok=True)
    T.round(6).to_csv(dest / "crash_crosssection.csv")
    summ = {"unaffected": a.unaffected, "asof": a.asof, **summarise(T)}
    (dest / "crash_crosssection_summary.json").write_text(json.dumps(summ, indent=2), encoding="utf-8")
    print(T.round(3).sort_values("residual").to_string())
    print(json.dumps(summ, indent=2))
