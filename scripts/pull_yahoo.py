"""Pull daily and intraday price history plus quote snapshots from Yahoo Finance's public chart/quote endpoints.

Writes raw JSON responses (untouched) to data/raw/yahoo/<UTC stamp>/ so every number in the analysis can be traced
back to the response it came from. Yahoo is an unofficial, unlicensed source: data are for research only and
are NOT redistributed in the public repository.

Usage: python scripts/pull_yahoo.py [TICKER ...]
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Mozilla/5.0 (research script; merger-arb-lab)"}

DEFAULT = ["LITE", "MRVL", "COHR", "AAOI", "FN", "CIEN", "CRDO", "AVGO", "NVDA", "SMH", "SOXX", "QQQ", "SPY", "^IRX"]


def chart(sym: str, rng: str, interval: str, session: requests.Session) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    params = {"range": rng, "interval": interval, "includePrePost": "true" if interval.endswith("m") else "false",
              "events": "div,splits"}
    for attempt in range(4):
        r = session.get(url, params=params, headers=UA, timeout=30)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def quotes(syms: list[str], session: requests.Session) -> dict:
    """v7 quote with crumb (bid/ask, quoteSourceName, exchangeDataDelayedBy, marketState)."""
    session.get("https://fc.yahoo.com", headers=UA, timeout=30)
    crumb = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", headers=UA, timeout=30).text.strip()
    r = session.get("https://query2.finance.yahoo.com/v7/finance/quote",
                    params={"symbols": ",".join(syms), "crumb": crumb}, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    syms = sys.argv[1:] or DEFAULT
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data" / "raw" / "yahoo" / stamp
    out.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    for sym in syms:
        safe = sym.replace("^", "_")
        for rng, interval, tag in (("5y", "1d", "daily"), ("5d", "1m", "intraday1m")):
            if sym.startswith("^") and tag != "daily":
                continue
            try:
                js = chart(sym, rng, interval, s)
                (out / f"{safe}_{tag}.json").write_text(json.dumps(js), encoding="utf-8")
                res = js["chart"]["result"][0]
                n = len(res.get("timestamp") or [])
                print(f"{sym:6s} {tag:11s} bars={n}")
            except Exception as exc:  # keep going; record the failure
                print(f"{sym:6s} {tag:11s} FAILED {exc}")
            time.sleep(0.6)
    try:
        q = quotes([x for x in syms if not x.startswith("^")], s)
        (out / "quotes_v7.json").write_text(json.dumps(q), encoding="utf-8")
        for row in q["quoteResponse"]["result"]:
            print(row.get("symbol"), row.get("regularMarketPrice"), row.get("bid"), row.get("ask"),
                  row.get("quoteSourceName"), row.get("exchangeDataDelayedBy"), row.get("marketState"),
                  row.get("postMarketPrice"))
    except Exception as exc:
        print("quotes FAILED", exc)
    (out / "_meta.json").write_text(json.dumps({"retrieved_utc": stamp, "symbols": syms}), encoding="utf-8")
    print("saved to", out)


if __name__ == "__main__":
    main()
