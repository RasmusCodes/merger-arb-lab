"""Trade / watch / no-trade signal that recomputes from the live inputs (spread, borrow fee, expected close, p).

The signal is a transparent rule set, not a forecast:
  hard stops  -> NO TRADE (stale data, borrow unavailable/too expensive, negative expected excess return, ...)
  soft checks -> WATCH    (positive but below hurdle, thin probability edge, negative under the stress break case)
  otherwise   -> TRADE    (still requires manual approval in the execution module; the signal never sends orders)
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from ..deal import Deal
from .probability import BreakCase, break_pnl, breakeven_probability


@dataclasses.dataclass(frozen=True)
class SignalInputs:
    target_price: float
    acquirer_price: float
    valuation_date: dt.date
    expected_close: dt.date
    borrow_fee: float
    p_complete: float
    base_break: BreakCase
    stress_break: BreakCase
    risk_free: float = 0.0459            # 1y Treasury (2026-11-24); opportunity cost of the equity tied up
    hurdle_excess: float = 0.05          # required expected return above risk-free, per year, on Reg T capital
    min_edge: float = 0.10               # required p - p* (probability points)
    max_borrow_fee: float = 0.03
    shortable: bool | None = True        # None = unknown
    data_fresh: bool = True
    require_stress_nonnegative: bool = True
    regt_capital_fraction: float = 0.5   # Reg T: 50% of gross per unit


@dataclasses.dataclass
class Signal:
    decision: str
    reasons: list[str]
    metrics: dict


def _carry_per_unit(deal: Deal, M: float, start: dt.date, end: dt.date, fee: float) -> float:
    years = max((end - start).days / 365.25, 0.0)
    n_div = len(deal.acquirer_dividend.ex_dates_between(start, end))
    R = deal.exchange_ratio
    return R * (n_div * deal.acquirer_dividend.amount + fee * M * years)


def evaluate(deal: Deal, x: SignalInputs) -> Signal:
    R, L, M = deal.exchange_ratio, x.target_price, x.acquirer_price
    V = deal.implied_value(M)
    years = max((x.expected_close - x.valuation_date).days / 365.25, 1 / 365.25)
    carry_c = _carry_per_unit(deal, M, x.valuation_date, x.expected_close, x.borrow_fee)
    C = (V - L) - carry_c
    half = x.valuation_date + (x.expected_close - x.valuation_date) / 2
    carry_b = _carry_per_unit(deal, M, x.valuation_date, half, x.borrow_fee)
    B = break_pnl(L, M, R, x.base_break, carry_b)
    Bs = break_pnl(L, M, R, x.stress_break, carry_b)
    p_star = breakeven_probability(C, B)
    p_star_s = breakeven_probability(C, Bs)
    cap = x.regt_capital_fraction * (L + R * M)
    ev = x.p_complete * C + (1 - x.p_complete) * B
    ev_s = x.p_complete * C + (1 - x.p_complete) * Bs
    exp_ret = ev / cap
    ann = (1 + exp_ret) ** (1 / years) - 1 if exp_ret > -1 else -1.0
    excess = ann - x.risk_free
    ann_s = (1 + ev_s / cap) ** (1 / years) - 1 if ev_s / cap > -1 else -1.0
    gross = V / L - 1

    # spread needed to clear the hurdle at the same p and break assumptions (bisection on the target price)
    def excess_at(Lx: float, fee: float = x.borrow_fee) -> float:
        cc = (V - Lx) - _carry_per_unit(deal, M, x.valuation_date, x.expected_close, fee)
        bb = break_pnl(Lx, M, R, x.base_break, _carry_per_unit(deal, M, x.valuation_date, half, fee))
        e = (x.p_complete * cc + (1 - x.p_complete) * bb) / (x.regt_capital_fraction * (Lx + R * M))
        return ((1 + e) ** (1 / years) - 1 if e > -1 else -1.0) - x.risk_free

    lo, hi = 0.2 * V, V
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if excess_at(mid) >= x.hurdle_excess:
            lo = mid
        else:
            hi = mid
    L_req = lo
    req_spread = V / L_req - 1
    f_lo, f_hi = 0.0, 2.0
    if excess_at(L, 0.0) < x.hurdle_excess:
        max_fee = 0.0
    else:
        for _ in range(80):
            mid = 0.5 * (f_lo + f_hi)
            if excess_at(L, mid) >= x.hurdle_excess:
                f_lo = mid
            else:
                f_hi = mid
        max_fee = f_lo

    hard, soft = [], []
    if not x.data_fresh:
        hard.append("market data stale or unavailable - do not act on these numbers")
    if x.shortable is False:
        hard.append("acquirer not shortable / no borrow available")
    if x.shortable is None:
        soft.append("borrow availability unknown (connect to IBKR to check shortable shares)")
    if x.borrow_fee > x.max_borrow_fee:
        hard.append(f"borrow fee {x.borrow_fee:.2%} above the {x.max_borrow_fee:.2%} limit")
    if x.expected_close > deal.final_outside_date:
        hard.append(f"expected close {x.expected_close} is after the final outside date {deal.final_outside_date}")
    if excess < 0:
        hard.append(f"expected excess return {excess:.1%}/yr is negative at p={x.p_complete:.0%}")
    elif excess < x.hurdle_excess:
        soft.append(f"expected excess return {excess:.1%}/yr is below the {x.hurdle_excess:.0%} hurdle")
    edge = x.p_complete - p_star
    if edge < x.min_edge:
        (hard if edge < 0 else soft).append(f"probability edge {edge:+.0%} (p={x.p_complete:.0%} vs break-even "
                                            f"{p_star:.0%}) is below {x.min_edge:.0%}")
    if x.require_stress_nonnegative and ev_s < 0:
        soft.append(f"expected P&L is negative under the stress break case ({x.stress_break.name}: break-even p "
                    f"{p_star_s:.0%})")
    decision = "NO TRADE" if hard else ("WATCH" if soft else "TRADE")
    metrics = dict(gross_spread=gross, spread_usd=V - L, implied_value=V, years_to_close=years,
                   annualized_if_complete=(1 + C / L) ** (1 / years) - 1, completion_pnl_unit=C,
                   break_pnl_unit_base=B, break_pnl_unit_stress=Bs, breakeven_p_base=p_star,
                   breakeven_p_stress=p_star_s, expected_pnl_unit=ev, expected_pnl_unit_stress=ev_s,
                   regt_capital_unit=cap, expected_return_on_capital=exp_ret, expected_annualized=ann,
                   expected_excess_annual=excess, expected_annualized_stress=ann_s,
                   required_spread_for_hurdle=req_spread, max_borrow_fee_for_hurdle=max_fee, edge=edge)
    return Signal(decision, hard + soft, metrics)
