"""First look at the Yahoo option chains for MRVL and LITE: ATM implied vols by expiry, put-call-parity implied
forward vs. spot (a borrow/financing proxy), and LITE-vs-MRVL relative IV. Raw chains: data/raw/yahoo_options/<stamp>/."""
import datetime as dt, json, math, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
snap = sorted((ROOT / "data" / "raw" / "yahoo_options").iterdir())[-1]
RATE = {0.25: 0.0436, 0.5: 0.0446, 1.0: 0.0459, 2.0: 0.0463}  # Treasury par curve 2026-11-24 (CMT, approx zero)
NOW = dt.datetime(2026, 11, 24, 21, 0, tzinfo=dt.timezone.utc)  # 16:00 ET close


def rate(t):
    ks = sorted(RATE)
    if t <= ks[0]:
        return RATE[ks[0]]
    for a, b in zip(ks, ks[1:]):
        if t <= b:
            return RATE[a] + (RATE[b] - RATE[a]) * (t - a) / (b - a)
    return RATE[ks[-1]]


def chains(sym):
    idx = json.loads((snap / f"{sym}_index.json").read_text())["optionChain"]["result"][0]
    spot = idx["quote"]["regularMarketPrice"]
    out = []
    for e in idx["expirationDates"]:
        js = json.loads((snap / f"{sym}_{e}.json").read_text())["optionChain"]["result"][0]["options"][0]
        out.append((e, js["calls"], js["puts"]))
    return spot, idx["quote"], out


def mid(o):
    b, a = o.get("bid") or 0, o.get("ask") or 0
    return (b + a) / 2 if b > 0 and a > 0 else None


for sym in ("MRVL", "LITE"):
    spot, q, ch = chains(sym)
    print(f"\n{sym} spot {spot} (quote time {dt.datetime.fromtimestamp(q['regularMarketTime'], dt.timezone.utc)}) source={q.get('quoteSourceName')}")
    print(" expiry       T     ATM-K   callIV  putIV   n2sided  impl.fwd   carry(%/y vs r)  ")
    for e, calls, puts in ch:
        T = (dt.datetime.fromtimestamp(e, dt.timezone.utc) + dt.timedelta(hours=21) - NOW).days / 365.25
        if T <= 0.01:
            continue
        cm = {c["strike"]: c for c in calls}
        pm = {p["strike"]: p for p in puts}
        ks = sorted(set(cm) & set(pm), key=lambda k: abs(k - spot))
        two = [k for k in ks if mid(cm[k]) and mid(pm[k])]
        if not two:
            print(f" {dt.date.fromtimestamp(e)}  {T:5.2f}  no two-sided pairs"); continue
        k0 = two[0]
        # implied forward from put-call parity using the 3 nearest two-sided strikes (European approx.)
        r = rate(T)
        fwds = [k + (mid(cm[k]) - mid(pm[k])) * math.exp(r * T) for k in two[:3]]
        F = sum(fwds) / len(fwds)
        carry = math.log(F / spot) / T  # = r - q - borrow (European approx; American puts bias this low)
        print(f" {dt.date.fromtimestamp(e)}  {T:5.2f}  {k0:7.1f}  {cm[k0].get('impliedVolatility',0):6.3f}  {pm[k0].get('impliedVolatility',0):6.3f}  {len(two):5d}   {F:9.2f}   {100*carry:6.2f} vs r={100*r:4.2f}")
