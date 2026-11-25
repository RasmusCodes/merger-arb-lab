"""Yahoo Finance access (unofficial, unlicensed; research use only - raw data are never committed or redistributed).

Two layers:
  * ``load_*`` read raw JSON snapshots saved by ``scripts/pull_yahoo.py`` under data/raw/yahoo/<UTC stamp>/
  * ``fetch_*`` call the public chart / quote endpoints directly (used by the local dashboard's refresh button)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time

import pandas as pd

from .quotes import Quote

ROOT = pathlib.Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "yahoo"
SAMPLE = ROOT / "data" / "sample"
UA = {"User-Agent": "Mozilla/5.0 (research script; merger-arb-lab)"}


def data_mode() -> str:
    """'raw' (default: local snapshots from scripts/pull_yahoo.py) or 'sample' (SYNTHETIC data in data/sample/,
    for offline tests and demos; every consumer labels it as synthetic). Set with MERGERARB_DATA=sample."""
    return os.environ.get("MERGERARB_DATA", "raw").lower()


def _sample_daily(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(SAMPLE / "synthetic_daily.csv", index_col="date", parse_dates=True)
    out = pd.DataFrame({"close": df[symbol]})
    out["from_meta"] = False
    return out


# ----------------------------------------------------------------------------------------------------------------
def latest_snapshot_dir(base: pathlib.Path = RAW) -> pathlib.Path | None:
    if not base.exists():
        return None
    dirs = sorted(p for p in base.iterdir() if p.is_dir())
    return dirs[-1] if dirs else None


def _chart_to_frame(js: dict) -> pd.DataFrame:
    res = js["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    idx = pd.to_datetime(ts, unit="s", utc=True)
    df = pd.DataFrame({
        "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
        "close": q.get("close"), "volume": q.get("volume"),
    }, index=idx)
    if adj is not None:
        df["adjclose"] = adj
    return df


def load_daily(symbol: str, snapshot: pathlib.Path | None = None) -> pd.DataFrame:
    """Daily bars indexed by session date (America/New_York calendar date), raw (unadjusted) close.

    Yahoo's daily series often lacks the most recent session (or carries a null close) when pulled after the close;
    the session's official close is then taken from the chart meta (regularMarketPrice at regularMarketTime) and the
    row is marked ``from_meta=True`` so it can be audited.
    """
    if data_mode() == "sample":
        return _sample_daily(symbol)
    snap = snapshot or latest_snapshot_dir()
    if snap is None:
        raise FileNotFoundError("no Yahoo snapshot found; run scripts/pull_yahoo.py, or set MERGERARB_DATA=sample "
                                "for the synthetic offline sample")
    js = json.loads((snap / f"{symbol.replace('^', '_')}_daily.json").read_text(encoding="utf-8"))
    df = _chart_to_frame(js)
    # Yahoo daily timestamps are the session open in UTC (13:30/14:30); convert to the ET calendar date
    df.index = df.index.tz_convert("America/New_York").normalize().tz_localize(None)
    df.index.name = "date"
    df["from_meta"] = False
    meta = js["chart"]["result"][0].get("meta", {})
    px, t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if px and t:
        ts = pd.Timestamp(t, unit="s", tz="UTC").tz_convert("America/New_York")
        session = ts.normalize().tz_localize(None)
        after_close = ts.hour > 16 or (ts.hour == 16 and ts.minute >= 0)
        if after_close:
            if session in df.index and pd.isna(df.at[session, "close"]):
                df.at[session, "close"] = px
                df.at[session, "from_meta"] = True
            elif session not in df.index and (df.index.max() < session):
                df.loc[session, ["close", "from_meta"]] = [px, True]
    df = df.sort_index()
    return df.dropna(subset=["close"])


def load_intraday(symbol: str, snapshot: pathlib.Path | None = None) -> pd.DataFrame:
    snap = snapshot or latest_snapshot_dir()
    js = json.loads((snap / f"{symbol}_intraday1m.json").read_text(encoding="utf-8"))
    df = _chart_to_frame(js)
    df.index = df.index.tz_convert("America/New_York")
    return df.dropna(subset=["close"])


def load_dividends(symbol: str, snapshot: pathlib.Path | None = None) -> pd.Series:
    snap = snapshot or latest_snapshot_dir()
    res = json.loads((snap / f"{symbol}_daily.json").read_text(encoding="utf-8"))["chart"]["result"][0]
    ev = (res.get("events") or {}).get("dividends") or {}
    s = pd.Series({pd.Timestamp(int(k), unit="s").normalize(): v["amount"] for k, v in ev.items()}, dtype=float)
    return s.sort_index()


def load_quote(symbol: str, snapshot: pathlib.Path | None = None) -> Quote:
    """Quote from the saved v7 quote response (falls back to the chart meta if absent)."""
    if data_mode() == "sample":
        q = json.loads((SAMPLE / "synthetic_quotes.json").read_text(encoding="utf-8"))[symbol]
        return Quote(symbol=symbol, last=q["last"], time_utc=dt.datetime.fromisoformat(q["time_utc"]), source=q["source"],
                     delay_minutes=q["delay_minutes"], market_state=q["market_state"], prev_close=q["prev_close"])
    snap = snapshot or latest_snapshot_dir()
    qfile = snap / "quotes_v7.json"
    if qfile.exists():
        for row in json.loads(qfile.read_text(encoding="utf-8"))["quoteResponse"]["result"]:
            if row.get("symbol") == symbol:
                return quote_from_v7(row)
    res = json.loads((snap / f"{symbol}_daily.json").read_text(encoding="utf-8"))["chart"]["result"][0]
    m = res["meta"]
    return Quote(symbol=symbol, last=m.get("regularMarketPrice"),
                 time_utc=dt.datetime.fromtimestamp(m["regularMarketTime"], dt.timezone.utc),
                 source="yahoo:chart-meta", delay_minutes=None, market_state=None,
                 prev_close=m.get("chartPreviousClose"))


def quote_from_v7(row: dict) -> Quote:
    def t(key: str):
        v = row.get(key)
        return dt.datetime.fromtimestamp(v, dt.timezone.utc) if v else None

    return Quote(
        symbol=row["symbol"], last=row.get("regularMarketPrice"), time_utc=t("regularMarketTime"),
        source=f"yahoo:{row.get('quoteSourceName', 'unknown')}", delay_minutes=row.get("exchangeDataDelayedBy"),
        market_state=row.get("marketState"), bid=row.get("bid"), ask=row.get("ask"),
        prev_close=row.get("regularMarketPreviousClose"), post_market=row.get("postMarketPrice"),
        post_market_time_utc=t("postMarketTime"),
    )


# ----------------------------------------------------------------------------------------------------------------
def fetch_daily(symbol: str, rng: str = "2y", session=None) -> pd.DataFrame:
    import requests

    s = session or requests.Session()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    for attempt in range(3):
        r = s.get(url, params={"range": rng, "interval": "1d", "events": "div,splits"}, headers=UA, timeout=30)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        df = _chart_to_frame(r.json())
        df.index = df.index.tz_convert("America/New_York").normalize().tz_localize(None)
        return df.dropna(subset=["close"])
    raise RuntimeError(f"Yahoo rate-limited for {symbol}")


def fetch_quotes(symbols: list[str], session=None) -> dict[str, Quote]:
    import requests

    s = session or requests.Session()
    s.get("https://fc.yahoo.com", headers=UA, timeout=30)
    crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", headers=UA, timeout=30).text.strip()
    r = s.get("https://query2.finance.yahoo.com/v7/finance/quote",
              params={"symbols": ",".join(symbols), "crumb": crumb}, headers=UA, timeout=30)
    r.raise_for_status()
    return {row["symbol"]: quote_from_v7(row) for row in r.json()["quoteResponse"]["result"]}
