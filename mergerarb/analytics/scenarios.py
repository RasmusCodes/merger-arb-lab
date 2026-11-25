"""Deterministic scenario paths, simulated day by day through a margin account.

Each scenario is a piecewise-linear path of (acquirer price multiple, gross spread) knots plus an outcome at the end
date (completion, break at given prices, or forced close-out). Along the path the account accrues borrow fees on the
short, pays acquirer dividends on projected ex-dates, earns/pays interest per the broker terms, and is checked for a
maintenance-margin breach every business day; a breach liquidates the pair at that day's prices (plus slippage) and
ends the scenario, so path risk shows up even when the deal eventually closes.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np
import pandas as pd

from ..deal import Deal
from .carry import BrokerTerms
from .margin import MarginRules, Position, account_state


@dataclasses.dataclass(frozen=True)
class ScenarioSpec:
    key: str
    name: str
    end_date: dt.date
    outcome: str                                   # "complete" | "break" | "forced_close"
    knots: tuple[tuple[dt.date, float, float], ...]  # (date, acquirer multiple vs start, gross spread)
    break_target_price: float | None = None        # absolute target price on a break
    break_acquirer_multiple: float | None = None   # acquirer price multiple (vs start) right after a break
    fee_cash_per_share: float = 0.0                # reverse termination fee value per target share (break only)
    borrow_path: tuple[tuple[dt.date, float], ...] = ()  # step changes in the annual borrow fee
    note: str = ""


@dataclasses.dataclass
class ScenarioResult:
    key: str
    name: str
    outcome: str
    end_date: dt.date
    years: float
    pnl_long: float
    pnl_short: float
    dividends_paid: float
    borrow_cost: float
    interest_net: float           # + income, - expense
    commissions_slippage: float
    net_pnl: float
    return_on_equity: float
    annualized_roe: float
    return_on_regt_capital: float
    peak_maint_req: float
    min_excess_liquidity: float
    max_drawdown: float           # fraction of peak equity
    liquidated: bool
    liquidation_date: dt.date | None
    note: str
    path: pd.DataFrame = dataclasses.field(repr=False, default=None)


def _interp(knots, dates):
    kd = np.array([np.datetime64(k[0]) for k in knots], dtype="datetime64[D]")
    x = np.array(dates, dtype="datetime64[D]").astype(float)
    kx = kd.astype(float)
    m = np.interp(x, kx, [k[1] for k in knots])
    g = np.interp(x, kx, [k[2] for k in knots])
    return m, g


def run_scenario(spec: ScenarioSpec, deal: Deal, pos: Position, equity0: float, L0: float, M0: float,
                 start: dt.date, borrow_fee0: float, rules: MarginRules = MarginRules(),
                 broker: BrokerTerms = BrokerTerms(), liquidation_slippage_bps: float = 25.0) -> ScenarioResult:
    R = pos.hedge_ratio()
    days = pd.bdate_range(start, spec.end_date)
    if len(days) == 0 or days[0].date() != start:
        days = pd.DatetimeIndex([pd.Timestamp(start)]).append(days[days > pd.Timestamp(start)])
    m, g = _interp(spec.knots, days.values.astype("datetime64[D]"))
    M = M0 * m
    L = deal.exchange_ratio * M / (1.0 + g) + deal.cash_per_share / (1.0 + g)
    L[0], M[0] = L0, M0
    exd = set(deal.acquirer_dividend.ex_dates_between(start, spec.end_date))
    fee_steps = sorted(spec.borrow_path)

    def fee_on(d: dt.date) -> float:
        f = borrow_fee0
        for sd, v in fee_steps:
            if d >= sd:
                f = v
        return f

    entry_cost = sum(broker.commission(q, p) for q, p in ((pos.target_shares, L0), (pos.acquirer_short_shares, M0)))
    entry_cost += (pos.target_shares * L0 + pos.acquirer_short_shares * M0) * (broker.half_spread_bps + broker.slippage_bps) / 1e4
    cum_div = cum_borrow = cum_int = 0.0
    cum_comm = entry_cost
    eq_path, maint_path, exl_path = [], [], []
    liquidated, liq_date = False, None
    prev = days[0].date()
    peak_eq = equity0 - entry_cost
    max_dd = 0.0
    last_i = len(days) - 1
    for i, ts in enumerate(days):
        d = ts.date()
        cal = (d - prev).days
        if i > 0:
            sv = pos.acquirer_short_shares * M[i - 1]
            cum_borrow += fee_on(d) * sv * cal / 365.0
            cash = equity0 - pos.target_shares * L0 + pos.acquirer_short_shares * M0  # approx. cash incl. proceeds
            nav = eq_path[-1] if eq_path else equity0
            free = equity0 - pos.target_shares * L0
            cum_int += broker.credit_rate(free, nav) * max(free, 0) * cal / 365.0
            cum_int += broker.short_proceeds_rate(sv, nav) * sv * cal / 365.0
            if cash < 0:
                cum_int -= broker.debit_rate(-cash) * (-cash) * cal / 365.0
            if any(prev < x <= d for x in exd):
                cum_div += deal.acquirer_dividend.amount * pos.acquirer_short_shares
        mtm = pos.target_shares * (L[i] - L0) - pos.acquirer_short_shares * (M[i] - M0)
        eq = equity0 + mtm - cum_div - cum_borrow + cum_int - cum_comm
        st = account_state(eq, pos, L[i], M[i], rules)
        eq_path.append(eq)
        maint_path.append(st.maint_req)
        exl_path.append(st.excess_liquidity)
        peak_eq = max(peak_eq, eq)
        max_dd = max(max_dd, (peak_eq - eq) / peak_eq if peak_eq > 0 else 0)
        if st.margin_call and i < last_i:
            liquidated, liq_date = True, d
            cum_comm += (pos.target_shares * L[i] + pos.acquirer_short_shares * M[i]) * liquidation_slippage_bps / 1e4
            last_i = i
            break
        prev = d

    i = last_i
    Lend, Mend = L[i], M[i]
    outcome = spec.outcome
    if liquidated:
        outcome = "liquidated (margin call)"
    elif spec.outcome == "complete":
        Lend = deal.exchange_ratio * Mend + deal.cash_per_share   # shares convert; short is covered by delivery
    elif spec.outcome == "break":
        Lend = float(spec.break_target_price) + spec.fee_cash_per_share
        Mend = M0 * float(spec.break_acquirer_multiple)
        cum_comm += sum(broker.commission(q, p) for q, p in ((pos.target_shares, Lend), (pos.acquirer_short_shares, Mend)))
    elif spec.outcome == "forced_close":
        cum_comm += sum(broker.commission(q, p) for q, p in ((pos.target_shares, Lend), (pos.acquirer_short_shares, Mend)))
        cum_comm += (pos.target_shares * Lend + pos.acquirer_short_shares * Mend) * (broker.half_spread_bps + broker.slippage_bps) / 1e4

    pnl_long = pos.target_shares * (Lend - L0)
    pnl_short = -pos.acquirer_short_shares * (Mend - M0)
    net = pnl_long + pnl_short - cum_div - cum_borrow + cum_int - cum_comm
    end_d = days[i].date()
    years = max((end_d - start).days / 365.25, 1 / 365.25)
    roe = net / equity0
    regt_cap = rules.regt(pos.target_shares * L0, pos.acquirer_short_shares * M0)
    eq_final = equity0 + net
    if eq_path:
        eq_path[-1] = eq_final
        peak_eq = max(peak_eq, eq_final)
        max_dd = max(max_dd, (peak_eq - eq_final) / peak_eq if peak_eq > 0 else 0)
    path = pd.DataFrame({"target": L[: i + 1], "acquirer": M[: i + 1], "gross_spread": g[: i + 1],
                         "equity": eq_path[: i + 1], "maint_req": maint_path[: i + 1],
                         "excess_liquidity": exl_path[: i + 1]}, index=days[: i + 1])
    return ScenarioResult(
        spec.key, spec.name, outcome, end_d, years, pnl_long, pnl_short, cum_div, cum_borrow, cum_int, cum_comm, net,
        roe, (1 + roe) ** (1 / years) - 1 if roe > -1 else -1.0, net / regt_cap if regt_cap else float("nan"),
        float(np.max(maint_path)), float(np.min(exl_path)), max_dd, liquidated, liq_date, spec.note, path)


def default_scenarios(deal: Deal, start: dt.date, L0: float, M0: float, g0: float, standalone_base: float,
                      standalone_stress: float, rebound_base: float, rebound_full: float,
                      vote_date: dt.date = dt.date(2027, 6, 30), borrow_fee0: float = 0.0032) -> list[ScenarioSpec]:
    """The nine scenarios required by the brief (+ break variants), parameterised by today's market."""
    close = deal.expected_close
    fod = deal.final_outside_date
    d3 = close + dt.timedelta(days=91)
    d6 = close + dt.timedelta(days=183)
    fee = deal.regulatory_fee_per_target_share()
    s = []
    s.append(ScenarioSpec("1_on_schedule", f"Closes on schedule ({close})", close, "complete",
                          ((start, 1.0, g0), (close, 1.0, 0.0))))
    s.append(ScenarioSpec("2_delay_3m", f"Closes 3 months late ({d3})", d3, "complete",
                          ((start, 1.0, g0), (d3, 1.0, 0.0))))
    s.append(ScenarioSpec("3_delay_6m", f"Closes 6 months late ({d6})", d6, "complete",
                          ((start, 1.0, g0), (d6, 1.0, 0.0)),
                          note=f"{d6} is after the final outside date {fod}: needs a negotiated extension; until then "
                               "either side may terminate (Marvell then owes the USD 3.2bn regulatory fee)"))
    s.append(ScenarioSpec("4_break_unaffected", "Fails; both stocks return to unaffected Oct-28 prices", vote_date,
                          "break", ((start, 1.0, g0), (vote_date, 1.0, max(g0, 0.30))),
                          break_target_price=deal.unaffected_target,
                          break_acquirer_multiple=deal.unaffected_acquirer / M0,
                          note="literal reading of the brief: LITE to $913.95 AND MRVL to $251.65"))
    s.append(ScenarioSpec("4b_break_model", "Fails at the votes; LITE to factor-model standalone, MRVL recovers half "
                          "its deal discount", vote_date, "break",
                          ((start, 1.0, g0), (vote_date, 1.0, max(g0, 0.30))),
                          break_target_price=standalone_base, break_acquirer_multiple=1 + rebound_base))
    s.append(ScenarioSpec("4c_break_stress", "Fails at the votes; LITE trades like the worst optical peer, MRVL "
                          "recovers its full deal discount", vote_date, "break",
                          ((start, 1.0, g0), (vote_date, 1.0, max(g0, 0.30))),
                          break_target_price=standalone_stress, break_acquirer_multiple=1 + rebound_full))
    s.append(ScenarioSpec("4d_break_regulatory", f"Regulatory failure at the final outside date ({fod}); stress "
                          f"standalone + USD 3.2bn fee (~${fee:.0f}/sh)", fod, "break",
                          ((start, 1.0, g0), (fod - dt.timedelta(days=1), 1.0, max(g0, 0.30))),
                          break_target_price=standalone_stress, break_acquirer_multiple=1 + rebound_full,
                          fee_cash_per_share=fee))
    mid = start + dt.timedelta(days=183)
    s.append(ScenarioSpec("5_mrvl_down40", "MRVL falls 40% over 6 months; deal closes on schedule", close, "complete",
                          ((start, 1.0, g0), (mid, 0.6, g0), (close, 0.6, 0.0))))
    q = start + dt.timedelta(days=92)
    s.append(ScenarioSpec("6_mrvl_up60_lite_lags", "MRVL +60% in 3 months while LITE lags (spread to 35%); closes on "
                          "schedule", close, "complete", ((start, 1.0, g0), (q, 1.6, 0.35), (close, 1.6, 0.0))))
    s.append(ScenarioSpec("7_both_down40", "Both fall 40% (spread % unchanged); closes on schedule", close, "complete",
                          ((start, 1.0, g0), (mid, 0.6, g0), (close, 0.6, 0.0))))
    s.append(ScenarioSpec("8_borrow_5pct", "MRVL borrow fee rises to 5% from 2027-01-01; closes on schedule", close,
                          "complete", ((start, 1.0, g0), (close, 1.0, 0.0)),
                          borrow_path=((dt.date(2027, 1, 1), 0.05),)))
    s.append(ScenarioSpec("9_htb_recall", "MRVL hard to borrow: fee 30% from 2027-03-01, recall forces a close-out on "
                          "2027-06-01 at a 30% spread", dt.date(2027, 6, 1), "forced_close",
                          ((start, 1.0, g0), (dt.date(2027, 6, 1), 1.0, 0.30)),
                          borrow_path=((dt.date(2027, 3, 1), 0.30),)))
    return s


def results_table(results: list[ScenarioResult]) -> pd.DataFrame:
    cols = ["key", "name", "outcome", "end_date", "years", "pnl_long", "pnl_short", "dividends_paid", "borrow_cost",
            "interest_net", "commissions_slippage", "net_pnl", "return_on_equity", "annualized_roe",
            "return_on_regt_capital", "peak_maint_req", "min_excess_liquidity", "max_drawdown", "liquidated",
            "liquidation_date", "note"]
    return pd.DataFrame([{c: getattr(r, c) for c in cols} for r in results])
