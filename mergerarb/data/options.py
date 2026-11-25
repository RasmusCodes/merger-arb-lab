"""Option-chain summaries from saved Yahoo chains: ATM implied vol term structure and parity-implied carry.

The parity-implied carry log(F/S)/T = r - q - borrow (European approximation; American early exercise biases it).
If it sits near r, the market is not charging a meaningful borrow premium; a hard-to-borrow stock shows a carry far
below r (puts rich vs calls).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "yahoo_options"


def _mid(o: dict) -> float | None:
    b, a = o.get("bid") or 0, o.get("ask") or 0
    return 0.5 * (b + a) if b > 0 and a > 0 else None


def atm_term_structure(symbol: str, rate_fn, snapshot: pathlib.Path | None = None,
                       now: dt.datetime | None = None) -> pd.DataFrame:
    from .yahoo import data_mode

    if data_mode() == "sample" or not RAW.exists() or not any(p.is_dir() for p in RAW.iterdir()):
        raise FileNotFoundError("no option-chain snapshot (run scripts/pull_market_extras.py)")
    snap = snapshot or sorted(p for p in RAW.iterdir() if p.is_dir())[-1]
    idx = json.loads((snap / f"{symbol}_index.json").read_text(encoding="utf-8"))["optionChain"]["result"][0]
    spot = idx["quote"]["regularMarketPrice"]
    now = now or dt.datetime.fromtimestamp(idx["quote"]["regularMarketTime"], dt.timezone.utc)
    rows = []
    for e in idx["expirationDates"]:
        f = snap / f"{symbol}_{e}.json"
        if not f.exists():
            continue
        opt = json.loads(f.read_text(encoding="utf-8"))["optionChain"]["result"][0]["options"][0]
        exp = dt.datetime.fromtimestamp(e, dt.timezone.utc) + dt.timedelta(hours=21)
        T = (exp - now).total_seconds() / (365.25 * 86400)
        if T <= 7 / 365:
            continue
        cm = {c["strike"]: c for c in opt["calls"]}
        pm = {p["strike"]: p for p in opt["puts"]}
        pairs = [k for k in sorted(set(cm) & set(pm), key=lambda k: abs(k - spot)) if _mid(cm[k]) and _mid(pm[k])]
        if not pairs:
            continue
        r = rate_fn(T)
        fwds = [k + (_mid(cm[k]) - _mid(pm[k])) * math.exp(r * T) for k in pairs[:3]]
        F = sum(fwds) / len(fwds)
        k0 = min(pairs, key=lambda k: abs(k - F))
        civ, piv = cm[k0].get("impliedVolatility") or float("nan"), pm[k0].get("impliedVolatility") or float("nan")
        rows.append({"expiry": exp.date(), "T": T, "forward": F, "atm_strike": k0, "call_iv": civ, "put_iv": piv,
                     "atm_iv": 0.5 * (civ + piv), "parity_carry": math.log(F / spot) / T, "r": r,
                     "implied_borrow_proxy": r - math.log(F / spot) / T, "n_pairs": len(pairs)})
    return pd.DataFrame(rows)


def treasury_rate_fn(curve: dict[float, float]):
    """Linear interpolation on a {years: rate} par curve (decimal rates)."""
    ks = sorted(curve)

    def f(t: float) -> float:
        if t <= ks[0]:
            return curve[ks[0]]
        for a, b in zip(ks, ks[1:]):
            if t <= b:
                return curve[a] + (curve[b] - curve[a]) * (t - a) / (b - a)
        return curve[ks[-1]]

    return f


def load_treasury_curve(path: pathlib.Path | None = None) -> tuple[dt.date, dict[float, float]]:
    base = ROOT / "data" / "raw" / "treasury"
    found = sorted(base.glob("*/par_curve_*.csv")) if base.exists() else []
    p = path or (found[-1] if found else ROOT / "data" / "sample" / "treasury_par_curve_2026.csv")
    df = pd.read_csv(p)
    row = df.iloc[0]
    tenor = {"1 Mo": 1 / 12, "2 Mo": 2 / 12, "3 Mo": 0.25, "4 Mo": 1 / 3, "6 Mo": 0.5, "1 Yr": 1.0, "2 Yr": 2.0,
             "3 Yr": 3.0, "5 Yr": 5.0, "7 Yr": 7.0, "10 Yr": 10.0}
    curve = {t: float(row[c]) / 100 for c, t in tenor.items() if c in row and pd.notna(row[c])}
    return pd.to_datetime(row["Date"]).date(), curve
