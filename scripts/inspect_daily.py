"""Quick look at the raw Yahoo daily bars around the deal: closes, split/dividend events, and the implied spread."""
import json, pathlib, sys, datetime as dt

ROOT = pathlib.Path(__file__).resolve().parents[1]
snap = sorted((ROOT / "data" / "raw" / "yahoo").iterdir())[-1]
start = sys.argv[1] if len(sys.argv) > 1 else "2026-10-15"
tick = ["LITE", "MRVL", "COHR", "AAOI", "SMH", "NVDA", "QQQ", "SPY"]

def load(sym):
    js = json.loads((snap / f"{sym}_daily.json").read_text())
    res = js["chart"]["result"][0]
    ts = res["timestamp"]; q = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose")
    rows = {}
    for i, t in enumerate(ts):
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
        rows[d] = (q["close"][i], q["volume"][i], adj[i] if adj else None)
    return rows, res.get("events", {}), res.get("meta", {})

data = {}
for s in tick:
    rows, ev, meta = load(s)
    data[s] = rows
    if ev.get("splits"):
        print(s, "SPLITS:", [(dt.datetime.fromtimestamp(int(k), dt.timezone.utc).date().isoformat(), v.get("splitRatio")) for k, v in ev["splits"].items()])
    if ev.get("dividends") and s in ("MRVL", "LITE"):
        divs = sorted((dt.datetime.fromtimestamp(int(k), dt.timezone.utc).date().isoformat(), v["amount"]) for k, v in ev["dividends"].items())
        print(s, "last dividends:", divs[-6:])
    print(s, "meta:", {k: meta.get(k) for k in ("regularMarketPrice", "regularMarketTime", "exchangeTimezoneName", "currency")})

dates = sorted(d for d in data["LITE"] if d >= start)
print("\ndate        LITE      MRVL   4.5xMRVL  spread%   LITEvol    MRVLvol    COHR    AAOI     SMH    NVDA     QQQ")
for d in dates:
    l = data["LITE"][d]; m = data["MRVL"].get(d)
    if not (l and m and l[0] and m[0]):
        continue
    imp = 4.5 * m[0]
    print(f"{d}  {l[0]:8.2f}  {m[0]:8.2f}  {imp:8.2f}  {100*(imp/l[0]-1):7.2f}  {l[1]:9,d}  {m[1]:10,d}  "
          + "  ".join(f"{data[s].get(d,(float('nan'),))[0]:7.2f}" for s in ("COHR", "AAOI", "SMH", "NVDA", "QQQ")))
