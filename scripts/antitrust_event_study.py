"""Event study: how antitrust / merger-control headlines moved targets, acquirers and spreads in past deals.

Inputs : data/derived/antitrust/events.csv (dated, sourced events), local licensed CRSP panels (MERGERARB_CRSP_DIR)
         for events up to 2024-12-31, Yahoo daily bars for later events / non-S&P targets (raw files stay local).
Output : data/derived/antitrust/event_study.csv (derived returns, spread changes, implied-probability changes) and
         a console summary by event class. Market-adjusted returns: AR = r_stock - r_SPY (beta 1).
Implied completion probability (crude, transparent): q = (P - D) / (V - D), with V the deal value from the terms and
D the downside = target's pre-announcement close x the market's move since then (beta 1).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time

import numpy as np
import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "derived" / "antitrust"
RAWY = ROOT / "data" / "raw" / "yahoo_events"
RAWY.mkdir(parents=True, exist_ok=True)
CRSP = os.environ.get("MERGERARB_CRSP_DIR")
UA = {"User-Agent": "Mozilla/5.0 (research script; merger-arb-lab)"}

# deal terms: target, acquirer, cash, ratio, announcement (first session with terms), CRSP permnos (None = Yahoo)
DEALS = {
    "MSFT-ATVI": dict(t="ATVI", a="MSFT", cash=95.0, ratio=0.0, ann="2022-01-18", tp=79678, ap=10107),
    "AMD-XLNX": dict(t="XLNX", a="AMD", cash=0.0, ratio=1.7234, ann="2020-10-27", tp=76201, ap=61241),
    "HAL-BHI": dict(t="BHI", a="HAL", cash=19.0, ratio=1.12, ann="2014-11-17", tp=75034, ap=23819),
    "AET-HUM": dict(t="HUM", a="AET", cash=125.0, ratio=0.8375, ann="2015-07-06", tp=48653, ap=88845),
    "ANTM-CI": dict(t="CI", a="ANTM", cash=103.40, ratio=0.5152, ann="2015-07-24", tp=64186, ap=89179),
    "CMCSA-TWC": dict(t="TWC", a="CMCSA", cash=0.0, ratio=2.875, ann="2014-02-13", tp=91883, ap=89525, a_split=2.0),
    "LRCX-KLAC": dict(t="KLAC", a="LRCX", cash=32.0, ratio=0.5, ann="2015-10-21", tp=46886, ap=48486, a_split=10.0),
    "SNPS-ANSS": dict(t="ANSS", a="SNPS", cash=197.0, ratio=0.345, ann="2024-01-16", tp=83621, ap=77357),
    "COF-DFS": dict(t="DFS", a="COF", cash=0.0, ratio=1.0192, ann="2024-02-20", tp=92121, ap=81055),
    "CVX-HES": dict(t="HES", a="CVX", cash=0.0, ratio=1.025, ann="2023-10-23", tp=28484, ap=14541),
    "XOM-PXD": dict(t="PXD", a="XOM", cash=0.0, ratio=2.3234, ann="2023-10-11", tp=75241, ap=11850),
    "HPE-JNPR": dict(t="JNPR", a="HPE", cash=40.0, ratio=0.0, ann="2024-01-10", tp=None, ap=None),
    "INTC-TSEM": dict(t="TSEM", a="INTC", cash=53.0, ratio=0.0, ann="2022-02-15", tp=None, ap=None),
    "QCOM-NXPI": dict(t="NXPI", a="QCOM", cash=127.5, ratio=0.0, ann="2016-10-27", tp=None, ap=None),
    "KR-ACI": dict(t="ACI", a="KR", cash=None, ratio=0.0, ann="2022-10-14", tp=None, ap=None),
    "TPR-CPRI": dict(t="CPRI", a="TPR", cash=57.0, ratio=0.0, ann="2023-08-10", tp=None, ap=None),
    "JBLU-SAVE": dict(t="SAVE", a="JBLU", cash=None, ratio=0.0, ann="2022-07-28", tp=None, ap=None),
    "AMGN-HZNP": dict(t="HZNP", a="AMGN", cash=116.5, ratio=0.0, ann="2022-12-12", tp=None, ap=None),
    "AVGO-VMW": dict(t="VMW", a="AVGO", cash=71.25, ratio=0.126, ann="2022-05-26", tp=None, ap=None, a_split=10.0),
}


def yahoo_daily(sym: str) -> pd.Series | None:
    f = RAWY / f"{sym}.json"
    if f.exists():
        js = json.loads(f.read_text(encoding="utf-8"))
    else:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         params={"period1": 1356998400, "period2": int(time.time()), "interval": "1d"},
                         headers=UA, timeout=30)
        time.sleep(0.7)
        if r.status_code != 200:
            return None
        js = r.json()
        f.write_text(json.dumps(js), encoding="utf-8")
    try:
        res = js["chart"]["result"][0]
        idx = pd.to_datetime(res["timestamp"], unit="s").normalize()
        adj = res["indicators"]["adjclose"][0]["adjclose"]
        close = res["indicators"]["quote"][0]["close"]
        return pd.DataFrame({"adj": adj, "close": close}, index=idx).dropna()
    except (KeyError, TypeError, IndexError):
        return None


def load_crsp():
    if not CRSP:
        return None, None, None
    C = pd.read_parquet(os.path.join(CRSP, "panel_close.parquet"))
    TR = pd.read_parquet(os.path.join(CRSP, "panel_tr.parquet"))
    B = pd.read_parquet(os.path.join(CRSP, "benchmarks_daily.parquet"))
    return C, TR, B


def main():
    ev = pd.read_csv(OUT / "events.csv")
    C, TR, B = load_crsp()
    spy = yahoo_daily("SPY")
    mkt_y = spy["adj"].pct_change()
    tr_is_level = None
    rows = []
    for _, e in ev.iterrows():
        d = DEALS.get(e.deal)
        if d is None or pd.isna(e.event_date):
            continue
        edate = pd.Timestamp(e.event_date)
        use_crsp = C is not None and d["tp"] and d["ap"] and edate <= pd.Timestamp("2024-12-20")
        if use_crsp:
            tc, ac = C[str(d["tp"])], C[str(d["ap"])] * d.get("a_split", 1.0)
            trt, tra = TR[str(d["tp"])], TR[str(d["ap"])]
            if tr_is_level is None:
                x = trt.dropna()
                tr_is_level = bool(x.abs().median() > 0.5)
            rt = trt.pct_change() if tr_is_level else trt
            ra = tra.pct_change() if tr_is_level else tra
            rm = B["SP500TR"].pct_change() if B["SP500TR"].abs().median() > 0.5 else B["SP500TR"]
            src = "CRSP"
        else:
            yt, ya = yahoo_daily(d["t"]), yahoo_daily(d["a"])
            if yt is None or ya is None:
                rows.append({"deal": e.deal, "event_date": e.event_date, "event_type": e.event_type,
                             "jurisdiction": e.jurisdiction, "direction": e.direction, "source": "no price data"})
                continue
            tc, ac = yt["close"], ya["close"] * (d.get("a_split", 1.0) if d["a"] == "AVGO" else 1.0)
            rt, ra, rm = yt["adj"].pct_change(), ya["adj"].pct_change(), mkt_y
            src = "Yahoo"
        days = tc.dropna().index
        pos = days.searchsorted(edate)
        if e.timing == "after_close":
            pos += 1
        if pos >= len(days) - 1 or pos < 25:
            continue
        d0 = days[pos]
        w = lambda a, b: days[max(pos + a, 0)]  # noqa: E731
        def car(r, a, b):
            seg = r.reindex(days[pos + a: pos + b + 1])
            m = rm.reindex(seg.index)
            return float((seg - m).sum())
        # deal value and spread (cash deals: V = cash; stock/cash+stock: cash + ratio x acquirer close)
        def V(i):
            if d["cash"] is None:
                return np.nan
            return d["cash"] + d["ratio"] * float(ac.reindex([days[i]]).iloc[0]) if d["ratio"] else d["cash"]
        def P(i):
            return float(tc.reindex([days[i]]).iloc[0])
        sp_before, sp_after = V(pos - 2) / P(pos - 2) - 1, V(pos + 1) / P(pos + 1) - 1
        # implied q with a beta-1 downside from the pre-announcement close
        ann = pd.Timestamp(d["ann"])
        apos = days.searchsorted(ann)
        unaff = float(tc.iloc[max(apos - 1, 0)])
        mlev = (1 + rm.fillna(0)).cumprod()
        def D(i):
            return unaff * float(mlev.reindex([days[i]]).iloc[0] / mlev.reindex([days[max(apos - 1, 0)]]).iloc[0])
        def q(i):
            v, p, dn = V(i), P(i), D(i)
            return (p - dn) / (v - dn) if v - dn > 0 else np.nan
        rows.append({"deal": e.deal, "event_date": e.event_date, "reaction_day": d0.date(), "event_type": e.event_type,
                     "jurisdiction": e.jurisdiction, "direction": e.direction, "confidence": e.confidence,
                     "source": src, "tgt_ar_d0": car(rt, 0, 0), "tgt_car_m1p1": car(rt, -1, 1),
                     "tgt_car_0p5": car(rt, 0, 5), "acq_ar_d0": car(ra, 0, 0), "acq_car_m1p1": car(ra, -1, 1),
                     "tgt_pre_drift_m20m2": car(rt, -20, -2), "spread_before": sp_before, "spread_after": sp_after,
                     "spread_change_pts": sp_after - sp_before, "q_before": q(pos - 2), "q_after": q(pos + 1),
                     "dq": q(pos + 1) - q(pos - 2)})
    es = pd.DataFrame(rows)
    es.to_csv(OUT / "event_study.csv", index=False)
    pd.set_option("display.width", 250)
    cols = ["deal", "reaction_day", "event_type", "jurisdiction", "direction", "source", "tgt_ar_d0", "tgt_car_m1p1",
            "acq_car_m1p1", "spread_change_pts", "q_before", "dq"]
    ok = es.dropna(subset=["tgt_car_m1p1"]) if "tgt_car_m1p1" in es else es
    print(ok[cols].to_string(index=False, float_format=lambda v: f"{v:+.3f}"))
    print("\nno data:", es[es.get("source") == "no price data"][["deal", "event_date"]].to_dict("records") if "source" in es else [])

    def cls(r):
        if r.event_type == "termination":
            return "termination"
        if r.direction == "negative" and r.event_type in ("lawsuit", "court_block", "phase2_open", "statement_of_objections",
                                                          "delay_or_concern_report", "other", "refile_or_clock_stop"):
            return "adverse regulatory news (pre-termination)"
        if r.direction == "positive":
            return "clearance / approval / settlement"
        return "other"
    ok = ok.copy()
    ok["class"] = ok.apply(cls, axis=1)
    g = ok.groupby("class").agg(n=("deal", "size"), tgt_car_mean=("tgt_car_m1p1", "mean"),
                                tgt_car_median=("tgt_car_m1p1", "median"), acq_car_mean=("acq_car_m1p1", "mean"),
                                spread_pts_mean=("spread_change_pts", "mean"), spread_pts_median=("spread_change_pts", "median"),
                                dq_mean=("dq", "mean"), dq_median=("dq", "median"))
    print("\n", g.to_string(float_format=lambda v: f"{v:+.3f}"))
    g.to_csv(OUT / "event_study_by_class.csv")


if __name__ == "__main__":
    main()
