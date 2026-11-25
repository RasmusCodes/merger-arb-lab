"""Pull the non-price market inputs the arbitrage needs and save raw responses under data/raw/<source>/<UTC stamp>/.

  * iBorrowDesk JSON (IBKR's published short-stock availability + indicative fee, with history) for MRVL and LITE
  * Yahoo option chains (all expiries) for MRVL and LITE, via the crumb-authenticated v7 endpoint
  * US Treasury daily par yield curve CSV for 2026 (risk-free / discount curve)
  * FINRA consolidated short interest for MRVL and LITE (last ~12 settlement dates)

Every source is public and unauthenticated. Failures are recorded, not hidden.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Mozilla/5.0 (research script; merger-arb-lab)"}
STAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def out_dir(src: str) -> pathlib.Path:
    p = ROOT / "data" / "raw" / src / STAMP
    p.mkdir(parents=True, exist_ok=True)
    return p


def iborrowdesk(s: requests.Session) -> None:
    d = out_dir("iborrowdesk")
    for sym in ("MRVL", "LITE"):
        try:
            r = s.get(f"https://iborrowdesk.com/api/ticker/{sym}", headers=UA, timeout=30)
            print("iborrowdesk", sym, r.status_code, r.headers.get("content-type"))
            (d / f"{sym}.json").write_text(r.text, encoding="utf-8")
            if r.ok and "json" in (r.headers.get("content-type") or ""):
                js = r.json()
                for key in ("real_time", "daily"):
                    rows = js.get(key) or []
                    if rows:
                        print("  ", key, "n=", len(rows), "first:", rows[0], "last:", rows[-1])
        except Exception as exc:
            print("iborrowdesk", sym, "FAILED", exc)


def yahoo_options(s: requests.Session) -> None:
    d = out_dir("yahoo_options")
    s.get("https://fc.yahoo.com", headers=UA, timeout=30)
    crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", headers=UA, timeout=30).text.strip()
    for sym in ("MRVL", "LITE"):
        try:
            base = f"https://query2.finance.yahoo.com/v7/finance/options/{sym}"
            r = s.get(base, params={"crumb": crumb}, headers=UA, timeout=30)
            r.raise_for_status()
            first = r.json()
            res = first["optionChain"]["result"][0]
            exps = res.get("expirationDates", [])
            (d / f"{sym}_index.json").write_text(json.dumps(first), encoding="utf-8")
            print("options", sym, "expiries", len(exps),
                  [dt.datetime.fromtimestamp(e, dt.timezone.utc).date().isoformat() for e in exps])
            for e in exps:
                rr = s.get(base, params={"crumb": crumb, "date": e}, headers=UA, timeout=30)
                if rr.status_code == 429:
                    time.sleep(5)
                    rr = s.get(base, params={"crumb": crumb, "date": e}, headers=UA, timeout=30)
                rr.raise_for_status()
                (d / f"{sym}_{e}.json").write_text(rr.text, encoding="utf-8")
                time.sleep(0.4)
        except Exception as exc:
            print("options", sym, "FAILED", exc)


def treasury(s: requests.Session) -> None:
    d = out_dir("treasury")
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/2026/all"
           "?type=daily_treasury_yield_curve&field_tdr_date_value=2026&page&_format=csv")
    try:
        r = s.get(url, headers=UA, timeout=60)
        print("treasury", r.status_code, len(r.text))
        (d / "par_curve_2026.csv").write_text(r.text, encoding="utf-8")
        print("  ", r.text.splitlines()[0][:200])
        print("  ", r.text.splitlines()[1][:200])
    except Exception as exc:
        print("treasury FAILED", exc)


def finra_short_interest(s: requests.Session) -> None:
    d = out_dir("finra_si")
    url = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
    for sym in ("MRVL", "LITE"):
        body = {
            "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": sym}],
            "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": "2026-01-01", "endDate": "2026-12-31"}],
            "limit": 100,
        }
        try:
            r = s.post(url, json=body, headers={**UA, "Accept": "application/json"}, timeout=60)
            print("finra", sym, r.status_code, len(r.text))
            (d / f"{sym}.json").write_text(r.text, encoding="utf-8")
            if r.ok:
                rows = sorted(r.json(), key=lambda x: x.get("settlementDate", ""))
                for row in rows[-8:]:
                    print("  ", row.get("settlementDate"), row.get("currentShortPositionQuantity"),
                          row.get("averageDailyVolumeQuantity"), row.get("daysToCoverQuantity"))
        except Exception as exc:
            print("finra", sym, "FAILED", exc)


if __name__ == "__main__":
    sess = requests.Session()
    iborrowdesk(sess)
    treasury(sess)
    finra_short_interest(sess)
    yahoo_options(sess)
    print("stamp", STAMP)
