"""Quote objects with explicit provenance and freshness labels.

Rule: nothing is ever labelled "live" unless the source says it is real-time AND the quote is recent AND the market is
in its regular session. Delayed, closing and after-hours prints are labelled as such, and bid/ask pairs that fail basic
sanity checks are flagged unusable (Yahoo returns stale after-hours bid/ask pairs such as bid > ask).
"""
from __future__ import annotations

import dataclasses
import datetime as dt

ET = dt.timezone(dt.timedelta(hours=-5))  # display only; freshness math is done in UTC


@dataclasses.dataclass(frozen=True)
class Quote:
    symbol: str
    last: float | None
    time_utc: dt.datetime | None          # time of the last trade/print
    source: str                           # e.g. "yahoo:Nasdaq Real Time Price", "ibkr:delayed", "manual"
    delay_minutes: int | None = None      # source-declared delay (Yahoo exchangeDataDelayedBy / IBKR market data type)
    market_state: str | None = None       # REGULAR / PRE / POST / POSTPOST / CLOSED
    bid: float | None = None
    ask: float | None = None
    prev_close: float | None = None
    post_market: float | None = None
    post_market_time_utc: dt.datetime | None = None


@dataclasses.dataclass(frozen=True)
class QuoteAssessment:
    label: str              # "real-time", "delayed 15 min", "closing print", "after-hours", "stale", "unavailable"
    is_live: bool
    age_seconds: float | None
    bid_ask_usable: bool
    warnings: tuple[str, ...]


def assess(q: Quote, now_utc: dt.datetime | None = None, stale_after_s: float = 15 * 60) -> QuoteAssessment:
    now = now_utc or dt.datetime.now(dt.timezone.utc)
    warn: list[str] = []
    if q.last is None or q.time_utc is None:
        return QuoteAssessment("unavailable", False, None, False, ("no last price",))
    age = (now - q.time_utc).total_seconds()
    regular = (q.market_state or "").upper() == "REGULAR"
    realtime_src = (q.delay_minutes or 0) == 0 and "delayed" not in q.source.lower()

    if not regular:
        label = "closing print" if (q.market_state or "").upper() in {"POST", "POSTPOST", "CLOSED", "PREPRE"} else "pre-market"
        live = False
    elif not realtime_src:
        label = f"delayed {q.delay_minutes or '?'} min"
        live = False
    elif age > stale_after_s:
        label = "stale"
        live = False
        warn.append(f"last print is {age / 60:.0f} min old during the regular session")
    else:
        label = "real-time"
        live = True
    if "yahoo" in q.source.lower() and live:
        warn.append("Yahoo 'real-time' is Nasdaq last sale only, not the consolidated tape or NBBO")

    usable = False
    if q.bid and q.ask and q.bid > 0 and q.ask > 0:
        mid = 0.5 * (q.bid + q.ask)
        if q.bid > q.ask:
            warn.append(f"bid {q.bid} > ask {q.ask}: crossed/stale pair")
        elif q.last and abs(mid / q.last - 1) > 0.02:
            warn.append(f"bid/ask mid {mid:.2f} is {100 * (mid / q.last - 1):+.1f}% from last {q.last}: stale pair")
        elif not regular:
            warn.append("bid/ask outside the regular session is not executable at size")
        else:
            usable = True
    return QuoteAssessment(label, live, age, usable, tuple(warn))
