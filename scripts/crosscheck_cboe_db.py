"""Cross-check Yahoo daily closes for LITE/MRVL against the Cboe closes stored by the optical-intel pipeline
(an independent download made before Cboe blocked scripts on 2026-11-19)."""
import json, pathlib, sqlite3, datetime as dt

import os

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = os.environ.get("CBOE_PRICES_DB") or exit("set CBOE_PRICES_DB to a SQLite file with a prices(symbol,date,close) table")
con = sqlite3.connect(DB)
tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
ptab = [t for t in tabs if "price" in t.lower()]
print("price tables:", ptab)
for t in ptab:
    cols = [r[1] for r in con.execute(f"pragma table_info({t})")]
    print(t, cols)

snap = sorted((ROOT / "data" / "raw" / "yahoo").iterdir())[-1]
def yahoo(sym):
    res = json.loads((snap / f"{sym}_daily.json").read_text())["chart"]["result"][0]
    return {dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"): c
            for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"])}

t = ptab[0]
cols = [r[1] for r in con.execute(f"pragma table_info({t})")]
tick_col = next(c for c in cols if c.lower() in ("ticker", "symbol", "company_id"))
date_col = next(c for c in cols if "date" in c.lower())
close_col = next(c for c in cols if c.lower() in ("close", "adj_close", "close_px"))
for sym in ("LITE", "MRVL"):
    y = yahoo(sym)
    rows = con.execute(f"select {date_col}, {close_col} from {t} where {tick_col}=? and {date_col}>='2026-10-20' order by {date_col}", (sym,)).fetchall()
    worst = 0.0
    for d, c in rows:
        d = str(d)[:10]
        yc = y.get(d)
        if yc is None or c is None:
            print(sym, d, "cboe", c, "yahoo", yc); continue
        diff = yc - c
        worst = max(worst, abs(diff))
        print(f"{sym} {d} cboe={c:9.2f} yahoo={yc:9.2f} diff={diff:+.4f}")
    print(sym, "rows", len(rows), "max |diff|", round(worst, 4))
