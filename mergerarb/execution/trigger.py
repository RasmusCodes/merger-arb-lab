"""Spread triggers: alert (never trade) when the EXECUTABLE spread reaches an entry level.

Executable spread for entering the pair = what you can actually lock in by crossing both spreads:
    sell the acquirer at its bid, buy the target at its ask  ->  g_exec = (R * M_bid + cash) / L_ask - 1
A ladder arms several rungs (e.g. 25% / 30% / 35%) with a lot count each. A hit produces a pre-filled order plan
(limits at the touch, inside a slippage allowance) that still goes through pre-flight and typed approval.
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


def executable_spread(L_ask: float, M_bid: float, ratio: float, cash: float = 0.0) -> float:
    return (ratio * M_bid + cash) / L_ask - 1.0


def evaluate_rung(rung: Rung, L_bid, L_ask, M_bid, M_ask, ratio: float, cash: float = 0.0,
                  slippage_bps: float = 10.0, quotes_live: bool = True) -> TriggerState:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not all(x and x > 0 for x in (L_bid, L_ask, M_bid, M_ask)) or L_bid > L_ask or M_bid > M_ask:
        return TriggerState(rung, None, None, False, None, None, "no usable two-sided quotes", now)
    ge = executable_spread(L_ask, M_bid, ratio, cash)
    gm = (ratio * 0.5 * (M_bid + M_ask) + cash) / (0.5 * (L_bid + L_ask)) - 1.0
    hit = ge >= rung.target_gross_spread and quotes_live
    s = slippage_bps / 1e4
    note = "hit: executable spread at or above the rung" if hit else (
        "quotes not live - triggers only fire on live quotes" if not quotes_live and ge >= rung.target_gross_spread
        else f"needs {rung.target_gross_spread - ge:+.2%} more")
    return TriggerState(rung, ge, gm, hit, round(L_ask * (1 + s), 2), round(M_bid * (1 - s), 2), note, now)


def default_ladder(equity: float = 10_000.0) -> list[Rung]:
    """Rungs from the antitrust analysis (docs/ANTITRUST_AND_TRADING.md): 35% is the spread that clears a 5% excess
    hurdle at the 48% base completion probability; 40% is roughly where a US-lawsuit-type scare took the spread in
    the model. Sizing follows the survival policy: 1 lot (2 LITE / 9 MRVL) per USD 10k at 35%, and a second rung at
    40% only from USD 20k of equity."""
    rungs = [Rung(0.35, max(1, int(equity // 10_000)))]
    if equity >= 20_000:
        rungs.append(Rung(0.40, int(equity // 20_000)))
    return rungs
