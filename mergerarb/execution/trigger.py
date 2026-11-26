"""Spread triggers: alert (never trade) when the EXECUTABLE spread reaches an entry level.

Executable spread for entering the pair = what you can actually lock in by crossing both spreads:
    sell the acquirer at its bid, buy the target at its ask  ->  g_exec = (R * M_bid + cash) / L_ask - 1
A ladder arms several rungs with a lot count each. A rung only fires on live, fresh quotes (younger than
``max_quote_age_s``) during the regular session. A hit is journalled with suggested limits (at the touch, inside a
slippage allowance); any order still goes through pre-flight and typed approval.
"""
from __future__ import annotations

import dataclasses
import datetime as dt


@dataclasses.dataclass(frozen=True)
class Rung:
    target_gross_spread: float
    lots: int


@dataclasses.dataclass
class TriggerState:
    rung: Rung
    executable_spread: float | None
    mid_spread: float | None
    hit: bool
    target_limit: float | None
    acquirer_limit: float | None
    note: str
    checked_utc: str
    quote_age_s: float | None = None
    session_open: bool | None = None


def executable_spread(L_ask: float, M_bid: float, ratio: float, cash: float = 0.0) -> float:
    return (ratio * M_bid + cash) / L_ask - 1.0


def evaluate_rung(rung: Rung, L_bid, L_ask, M_bid, M_ask, ratio: float, cash: float = 0.0,
                  slippage_bps: float = 10.0, quotes_live: bool = True, quote_age_s: float | None = None,
                  max_quote_age_s: float = 15.0, session_open: bool | None = None,
                  now_utc: dt.datetime | None = None) -> TriggerState:
    """``quote_age_s`` None = unknown age (never fires); ``session_open`` None = computed from the exchange calendar."""
    now_dt = now_utc or dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    if session_open is None:
        from .executor import market_session
        session_open = market_session(now_dt)[0]
    if not all(x and x > 0 for x in (L_bid, L_ask, M_bid, M_ask)) or L_bid > L_ask or M_bid > M_ask:
        return TriggerState(rung, None, None, False, None, None, "no usable two-sided quotes", now, quote_age_s,
                            session_open)
    ge = executable_spread(L_ask, M_bid, ratio, cash)
    gm = (ratio * 0.5 * (M_bid + M_ask) + cash) / (0.5 * (L_bid + L_ask)) - 1.0
    fresh = quote_age_s is not None and 0 <= quote_age_s <= max_quote_age_s
    level = ge >= rung.target_gross_spread
    hit = level and quotes_live and fresh and session_open
    if hit:
        note = "hit: executable spread at or above the rung"
    elif not level:
        note = f"needs {rung.target_gross_spread - ge:+.2%} more"
    elif not quotes_live:
        note = "quotes not live - triggers only fire on live quotes"
    elif not fresh:
        note = ("quote age unknown" if quote_age_s is None else f"quotes {quote_age_s:.0f}s old") + \
               f" - triggers need quotes younger than {max_quote_age_s:.0f}s"
    else:
        note = "outside the regular session - triggers only fire 09:30-16:00 ET on trading days"
    s = slippage_bps / 1e4
    return TriggerState(rung, ge, gm, hit, round(L_ask * (1 + s), 2), round(M_bid * (1 - s), 2), note, now,
                        quote_age_s, session_open)


def default_ladder(equity: float = 10_000.0) -> list[Rung]:
    """Rungs from the corrected analysis (docs/REPORT.md, triple check 2026-11-25). The base case clears the 5% hurdle
    after financing at a 21.0% spread, so 23% leaves a 2-point cushion. At 27% the pessimistic break case (the first
    version's blended LITE break value) breaks even. Size never exceeds the Monte Carlo tail cap of one lot
    (2 LITE / 9 MRVL) per USD 10k: a USD 10k account gets one lot at 23%; from USD 20k the cap is split between the two
    rungs."""
    cap = max(1, int(equity // 10_000))
    first = max(1, cap // 2) if cap >= 2 else 1
    rungs = [Rung(0.23, first)]
    if equity >= 20_000 and cap - first > 0:
        rungs.append(Rung(0.27, cap - first))
    return rungs
