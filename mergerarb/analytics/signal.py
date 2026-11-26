"""Trade / watch / no-trade signal that recomputes from the live inputs (spread, borrow fee, expected close, p).

The signal is a transparent rule set, not a forecast:
  hard stops  -> NO TRADE (stale data, borrow unavailable/too expensive, negative expected excess return, ...)
  soft checks -> WATCH    (positive but below hurdle, thin probability edge, negative under the stress break case)
  otherwise   -> TRADE    (still requires manual approval in the execution module; the signal never sends orders)

Economics per unit (long 1 target share, short `ratio` acquirer shares):
  * Completion pays the spread minus carry (dividends owed on the short, borrow) minus entry costs.
  * A break pays (S - L) - ratio * (M_break - M) minus carry and round-trip costs. A share ``reg_break_share`` of breaks
    are regulatory terminations that pay the reverse fee (merger agreement s8.2(d)): the target receives it (+fee per
    target share after tax) and the acquirer pays it (-fee per acquirer share after tax, a gain on the short leg).
    Failed votes and other terminations pay nothing.
  * Financing: at IBKR the short-sale proceeds do not fund the long leg for interest purposes ("adjusted cash" excludes
    short collateral) and earn 0% below USD 100k, so the cash that pays for the long has an opportunity cost
    (``financing_rate``, default the Treasury rate) for as long as the position is open.
  * Expected excess return = expected P&L net of that financing, per year of Reg T capital actually used
    (capital x expected holding time: breaks resolve earlier than completion).
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from ..deal import Deal
from .probability import BreakCase, breakeven_probability, mixed_break_pnl


@dataclasses.dataclass(frozen=True)
class SignalInputs:
    target_price: float
    acquirer_price: float
    valuation_date: dt.date
    expected_close: dt.date
    borrow_fee: float
    p_complete: float
    base_break: BreakCase                # without any fee (the fee is mixed in by reg_break_share)
    stress_break: BreakCase              # without any fee
    risk_free: float = 0.0459            # 1y Treasury (2026-11-24)
    hurdle_excess: float = 0.05          # required expected return per year on Reg T capital, after financing
    min_edge: float = 0.10               # required p - p* (probability points), p* net of financing
    max_borrow_fee: float = 0.03
    shortable: bool | None = True        # None = unknown
    data_fresh: bool = True
    require_stress_nonnegative: bool = True
    regt_capital_fraction: float = 0.5   # Reg T: 50% of gross per unit
    reg_break_share: float = 0.0         # P(regulatory termination | break)
    fee_target: float = 0.0              # after-tax reverse fee per target share (regulatory break)
    fee_acquirer: float = 0.0            # after-tax cost of that fee per acquirer share
    financing_rate: float | None = None  # opportunity cost of the cash paying for the long; None = risk_free
    entry_cost_unit: float = 0.0         # commissions + half-spread + slippage to enter one unit
    exit_cost_unit_break: float = 0.0    # cost to unwind one unit after a break (completion converts, no exit trade)
    break_years: float | None = None     # expected years until a break resolves; None = half the time to close


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


def _economics(deal: Deal, x: SignalInputs, L: float, fee: float) -> dict:
    """All per-unit quantities at target price ``L`` and borrow fee ``fee`` (used for today and for bisection)."""
    R, M = deal.exchange_ratio, x.acquirer_price
    V = deal.implied_value(M)
    years = max((x.expected_close - x.valuation_date).days / 365.25, 1 / 365.25)
    half = x.valuation_date + (x.expected_close - x.valuation_date) / 2
    t_b = x.break_years if x.break_years is not None else years / 2
    fin_rate = x.risk_free if x.financing_rate is None else x.financing_rate
    carry_c = _carry_per_unit(deal, M, x.valuation_date, x.expected_close, fee)
    carry_b = _carry_per_unit(deal, M, x.valuation_date, half, fee)
    w = x.reg_break_share
    C = (V - L) - carry_c - x.entry_cost_unit
    costs_b = x.entry_cost_unit + x.exit_cost_unit_break
    B = mixed_break_pnl(L, M, R, x.base_break, carry_b, w, x.fee_target, x.fee_acquirer) - costs_b
    Bs = mixed_break_pnl(L, M, R, x.stress_break, carry_b, w, x.fee_target, x.fee_acquirer) - costs_b
    B0 = mixed_break_pnl(L, M, R, x.base_break, carry_b, 0.0, 0.0, 0.0) - costs_b
    B1 = mixed_break_pnl(L, M, R, x.base_break, carry_b, 1.0, x.fee_target, x.fee_acquirer) - costs_b
    fin_c, fin_b = fin_rate * L * years, fin_rate * L * t_b
    p = x.p_complete
    cap = x.regt_capital_fraction * (L + R * M)
    cap_years = cap * (p * years + (1 - p) * t_b)
    ev, ev_s = p * C + (1 - p) * B, p * C + (1 - p) * Bs
    ev_net = p * (C - fin_c) + (1 - p) * (B - fin_b)
    ev_s_net = p * (C - fin_c) + (1 - p) * (Bs - fin_b)
    return dict(V=V, years=years, t_b=t_b, C=C, B=B, Bs=Bs, B0=B0, B1=B1, fin_c=fin_c, fin_b=fin_b, cap=cap,
                cap_years=cap_years, ev=ev, ev_s=ev_s, ev_net=ev_net, ev_s_net=ev_s_net,
                excess=ev_net / cap_years, excess_s=ev_s_net / cap_years,
                p_star=breakeven_probability(C - fin_c, B - fin_b),
                p_star_s=breakeven_probability(C - fin_c, Bs - fin_b),
                p_star_gross=breakeven_probability(C, B))


def economics(deal: Deal, x: SignalInputs) -> dict:
    """Per-unit economics at the inputs' own target price and borrow fee (no decision rules)."""
    return _economics(deal, x, x.target_price, x.borrow_fee)


def evaluate(deal: Deal, x: SignalInputs) -> Signal:
    L, M = x.target_price, x.acquirer_price
    e = _economics(deal, x, L, x.borrow_fee)
    V, years = e["V"], e["years"]
    gross = V / L - 1
    exp_ret = e["ev"] / e["cap"]
    ann = (1 + exp_ret) ** (1 / years) - 1 if exp_ret > -1 else -1.0

    # spread needed to clear the hurdle at the same p and break assumptions (bisection on the target price)
    lo, hi = 0.2 * V, V
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _economics(deal, x, mid, x.borrow_fee)["excess"] >= x.hurdle_excess:
            lo = mid
        else:
            hi = mid
    req_spread = V / lo - 1
    if _economics(deal, x, L, 0.0)["excess"] < x.hurdle_excess:
        max_fee = 0.0
    else:
        f_lo, f_hi = 0.0, 2.0
        for _ in range(80):
            mid = 0.5 * (f_lo + f_hi)
            if _economics(deal, x, L, mid)["excess"] >= x.hurdle_excess:
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
    excess = e["excess"]
    if excess < 0:
        hard.append(f"expected excess return {excess:.1%}/yr (after financing the long) is negative at "
                    f"p={x.p_complete:.0%}")
    elif excess < x.hurdle_excess:
        soft.append(f"expected excess return {excess:.1%}/yr is below the {x.hurdle_excess:.0%} hurdle")
    edge = x.p_complete - e["p_star"]
    if edge < x.min_edge:
        (hard if edge < 0 else soft).append(f"probability edge {edge:+.0%} (p={x.p_complete:.0%} vs break-even "
                                            f"{e['p_star']:.0%}) is below {x.min_edge:.0%}")
    if x.require_stress_nonnegative and e["ev_s_net"] < 0:
        soft.append(f"expected P&L is negative under the stress break case ({x.stress_break.name}: break-even p "
                    f"{e['p_star_s']:.0%})")
    decision = "NO TRADE" if hard else ("WATCH" if soft else "TRADE")
    metrics = dict(gross_spread=gross, spread_usd=V - L, implied_value=V, years_to_close=years,
                   annualized_if_complete=(1 + e["C"] / L) ** (1 / years) - 1, completion_pnl_unit=e["C"],
                   break_pnl_unit_base=e["B"], break_pnl_unit_base_nofee=e["B0"],
                   break_pnl_unit_base_regulatory=e["B1"], break_pnl_unit_stress=e["Bs"],
                   reg_break_share=x.reg_break_share, breakeven_p_base=e["p_star"],
                   breakeven_p_base_gross=e["p_star_gross"], breakeven_p_stress=e["p_star_s"],
                   expected_pnl_unit=e["ev"], expected_pnl_unit_stress=e["ev_s"],
                   expected_pnl_unit_net=e["ev_net"], expected_pnl_unit_stress_net=e["ev_s_net"],
                   financing_cost_complete=e["fin_c"], financing_cost_break=e["fin_b"],
                   years_to_break=e["t_b"], regt_capital_unit=e["cap"], capital_years_unit=e["cap_years"],
                   expected_return_on_capital=exp_ret, expected_annualized=ann,
                   expected_excess_annual=excess, expected_excess_annual_stress=e["excess_s"],
                   expected_annualized_stress=((1 + e["ev_s"] / e["cap"]) ** (1 / years) - 1
                                               if e["ev_s"] / e["cap"] > -1 else -1.0),
                   required_spread_for_hurdle=req_spread, max_borrow_fee_for_hurdle=max_fee, edge=edge)
    return Signal(decision, hard + soft, metrics)
