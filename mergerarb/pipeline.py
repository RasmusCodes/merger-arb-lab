"""End-to-end analysis for one deal: loads data, calibrates, runs every model, returns plain tables/dicts.

Used by scripts/run_analysis.py (writes outputs/), the local Streamlit dashboard and the public snapshot builder,
so every surface shows numbers produced by the same code path.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import numpy as np
import pandas as pd

from .analytics import margin as mg
from .analytics import montecarlo as mc
from .analytics import probability as pr
from .analytics import scenarios as sc
from .analytics import signal as sg
from .analytics import spread as sp
from .analytics import standalone as sa
from .analytics.antitrust import RegTree
from .analytics.carry import BrokerTerms, carry
from .data import options as op
from .data import quotes as qt
from .data import yahoo
from .deal import Deal, load_deal

PEERS = ["SMH", "COHR", "AAOI", "FN", "CIEN", "CRDO", "SOXX", "NVDA", "AVGO", "QQQ"]
_TREE = RegTree()
P_REG_FAIL_TREE = 1 - _TREE.p_us() * _TREE.p_eu() * _TREE.p_samr()   # 0.3952 with the base gates


@dataclasses.dataclass
class Inputs:
    equity: float = 10_000.0
    borrow_fee: float = 0.0032               # iBorrowDesk (IBKR file) 2026-11-24 16:44 ET
    shortable_shares: float | None = 10_000_000
    p_acq_vote_fail: float = 0.10
    p_tgt_vote_fail: float = 0.10
    p_reg_fail: float = P_REG_FAIL_TREE      # = 1 - US x EU x SAMR from antitrust.RegTree (base gates)
    p_other_fail: float = 0.03
    # MRVL's deal discount appeared on the leak and announcement days (-22.5% abnormal, flat since), so most of it
    # should reverse on a break: base 3/4, range 1/2 to all of it (Tapestry reversed all of its drop when the court
    # blocked Capri). The first version used 1/2.
    rebound_kappa: float = 0.75
    kappa_range: tuple[float, float] = (0.5, 1.0)
    # LITE's break value: the two-factor model is the central estimate; its residual band (~14%) matches how far 49
    # AI/semi names strayed from the same model in this crash (scripts/crash_crosssection.py: sd 14.7%, no momentum
    # effect). None = use the factor model's own band.
    standalone_sigma: float | None = None
    fee_tax_rate: float = 0.21               # corporate tax on the regulatory fee (deductible for the payer)
    financing_rate: float | None = None      # opportunity cost of the cash paying for the long; None = Treasury
    stress_standalone_method: str = "worst optical peer"
    expected_close: dt.date | None = None
    vote_date: dt.date = dt.date(2027, 6, 30)
    mc_paths: int = 20000
    policy: mg.SizingPolicy = dataclasses.field(default_factory=mg.SizingPolicy)
    rules: mg.MarginRules = dataclasses.field(default_factory=mg.MarginRules)
    broker: BrokerTerms = dataclasses.field(default_factory=BrokerTerms)
    units_override: int | None = None
    policy_es_cap: float = 0.20               # Monte Carlo 95% expected shortfall <= 20% of equity

    @property
    def p_complete(self) -> float:
        return (1 - self.p_acq_vote_fail) * (1 - self.p_tgt_vote_fail) * (1 - self.p_reg_fail) * (1 - self.p_other_fail)

    @property
    def reg_break_share(self) -> float:
        """P(regulatory termination | break): the fee-bearing share of breaks (votes come first; s8.2(d))."""
        p_reg = (1 - self.p_acq_vote_fail) * (1 - self.p_tgt_vote_fail) * self.p_reg_fail
        return p_reg / max(1 - self.p_complete, 1e-12)


def load_prices(deal: Deal, snapshot=None) -> pd.DataFrame:
    syms = [deal.target.ticker, deal.acquirer.ticker, *PEERS]
    return pd.concat({s: yahoo.load_daily(s, snapshot)["close"] for s in syms}, axis=1).sort_index()


def run(deal: Deal | None = None, inp: Inputs = Inputs(), snapshot=None) -> dict:
    deal = deal or load_deal("mrvl_lite")
    if inp.expected_close:
        deal = deal.with_overrides(expected_close=inp.expected_close)
    T, A, R = deal.target.ticker, deal.acquirer.ticker, deal.exchange_ratio
    px = load_prices(deal, snapshot)
    as_of = px.index[-1]
    start = as_of.date()
    L0, M0 = float(px[T].iloc[-1]), float(px[A].iloc[-1])
    out: dict = {"deal": deal, "as_of": as_of, "L0": L0, "M0": M0}

    # ----- quotes and freshness
    qa = {s: yahoo.load_quote(s, snapshot) for s in (T, A)}
    out["quotes"] = {s: (q, qt.assess(q)) for s, q in qa.items()}

    # ----- spread now and history
    V = deal.implied_value(M0)
    out["implied_value"], out["spread_usd"], out["gross_spread"] = V, V - L0, V / L0 - 1
    hist = sp.spread_history(px[T], px[A], R, deal.cash_per_share, start=deal.announced)
    hist_spec = sp.spread_history(px[T], px[A], R, deal.cash_per_share, start=pd.Timestamp("2026-10-29"))
    out["history"] = hist
    out["history_from_speculation"] = hist_spec
    out["spread_stats"] = sp.stats(hist)
    years = sp.year_fraction(start, deal.expected_close)
    out["years_to_close"] = years
    out["annualized_if_complete"] = sp.annualize(out["gross_spread"], years)
    out["annualized_simple"] = sp.annualize(out["gross_spread"], years, "simple")
    out["sensitivity"] = sp.sensitivity_grid(L0, M0, R, deal.cash_per_share)

    # ----- standalone (break) values and deal discount
    tgt_est, acq_est = sa.standalone_table(px, T, A, deal.unaffected_date, as_of)
    out["standalone_target"], out["standalone_acquirer"] = tgt_est, acq_est
    est2 = next(e for e in tgt_est if e.method.startswith("two-factor"))
    S_base = est2.estimate
    S_sigma = inp.standalone_sigma if inp.standalone_sigma is not None else math.log(est2.estimate / est2.low)
    M_cf = next(e for e in acq_est if e.method.startswith("beta to SMH")).estimate
    peer_rets = {p: float(px[p].iloc[-1] / px.loc[:deal.unaffected_date, p].iloc[-1]) for p in ("COHR", "AAOI", "FN", "CIEN")}
    worst = min(peer_rets, key=peer_rets.get)
    S_stress = deal.unaffected_target * peer_rets[worst]
    S_blend = math.sqrt(S_base * S_stress)       # the first version's base case (geometric midpoint), kept for reference
    delta = sa.deal_discount(M0, M_cf)
    j_half = 0.5 * delta / (1 - delta)
    j_base = inp.rebound_kappa * delta / (1 - delta)
    j_full = delta / (1 - delta)
    out.update(S_base=S_base, S_sigma=S_sigma, S_stress=S_stress, S_blend=S_blend, stress_peer=worst,
               M_counterfactual=M_cf, deal_discount=delta, rebound_half=j_half, rebound_base=j_base,
               rebound_full=j_full, rebound_kappa=inp.rebound_kappa, kappa_range=inp.kappa_range,
               peer_returns=peer_rets)

    # ----- carry, fees, costs and per-unit economics
    fee_ps = deal.regulatory_fee_per_target_share()                 # pre-tax, per LITE share
    fee_acq_ps = deal.regulatory_fee_per_acquirer_share()           # pre-tax, per MRVL share
    fee_L = fee_ps * (1 - inp.fee_tax_rate)
    fee_M = fee_acq_ps * (1 - inp.fee_tax_rate)
    w_reg = inp.reg_break_share
    cb = carry(deal, 1, R, L0, M0, start, deal.expected_close, inp.borrow_fee, inp.equity, inp.broker)
    out["carry_unit"] = cb
    lot = mg.exact_lot(R)
    entry_cost = ((inp.broker.commission(lot, L0) + inp.broker.commission(round(lot * R), M0)) / lot
                  + (L0 + R * M0) * (inp.broker.half_spread_bps + inp.broker.slippage_bps) / 1e4)
    C = pr.completion_pnl(V - L0, cb.acquirer_dividends_paid + cb.borrow_fee)   # before trading costs
    out.update(fee_per_target_share=fee_ps, fee_per_acquirer_share=fee_acq_ps, fee_target_after_tax=fee_L,
               fee_acquirer_after_tax=fee_M, reg_break_share=w_reg, entry_cost_unit=entry_cost)
    CASE_OPT = "optimistic: factor-model standalone, half of MRVL's deal discount reverses"
    CASE_BASE = f"base: factor-model standalone, {inp.rebound_kappa:.0%} of MRVL's deal discount reverses"
    CASE_PESS = f"pessimistic: blended standalone (first version's base), {inp.rebound_kappa:.0%} reversal"
    CASE_STRESS = f"stress: {worst}-like standalone, full MRVL reversal"
    cases = {
        CASE_OPT: pr.BreakCase("optimistic", S_base, j_half),
        CASE_BASE: pr.BreakCase("base", S_base, j_base),
        "factor-model standalone, full reversal": pr.BreakCase("factor_full", S_base, j_full),
        CASE_PESS: pr.BreakCase("pessimistic", S_blend, j_base),
        "blended standalone, half reversal (the first version's base case)": pr.BreakCase("v1_base", S_blend, j_half),
        f"{worst}-like standalone, half reversal": pr.BreakCase("stress_half", S_stress, j_half),
        CASE_STRESS: pr.BreakCase("stress", S_stress, j_full),
        "literal unaffected prices (LITE 913.95, MRVL 251.65)": pr.BreakCase(
            "unaffected", deal.unaffected_target, deal.unaffected_acquirer / M0 - 1),
    }
    out["case_names"] = dict(optimistic=CASE_OPT, base=CASE_BASE, pessimistic=CASE_PESS, stress=CASE_STRESS)
    half = start + (deal.expected_close - start) / 2
    cb_half = carry(deal, 1, R, L0, M0, start, half, inp.borrow_fee, inp.equity, inp.broker)
    carry_b = cb_half.acquirer_dividends_paid + cb_half.borrow_fee
    rf = op.treasury_rate_fn(op.load_treasury_curve()[1])(years)
    fin_rate = rf if inp.financing_rate is None else inp.financing_rate
    out["risk_free"], out["financing_rate"] = rf, fin_rate
    # expected time to a break (MC timing: votes ~vote_date, regulatory failures spread up to the final outside date)
    p_vote = 1 - (1 - inp.p_acq_vote_fail) * (1 - inp.p_tgt_vote_fail)
    t_vote = sp.year_fraction(start, inp.vote_date)
    t_reg = sp.year_fraction(start, dt.date(2027, 9, 1) + (deal.final_outside_date - dt.date(2027, 9, 1)) / 2)
    t_other = sp.year_fraction(start, deal.expected_close) / 2
    p_b = max(1 - inp.p_complete, 1e-12)
    p_oth = (1 - p_vote) * (1 - inp.p_reg_fail) * inp.p_other_fail
    t_break = (p_vote * t_vote + (p_b - p_vote - p_oth) * t_reg + p_oth * t_other) / p_b
    out["years_to_break"] = t_break

    def signal_inputs(p: float, base_case: pr.BreakCase, stress_case: pr.BreakCase, fresh: bool = True,
                      shortable: bool | None = True) -> sg.SignalInputs:
        return sg.SignalInputs(L0, M0, start, deal.expected_close, inp.borrow_fee, p, base_case, stress_case,
                               risk_free=rf, financing_rate=fin_rate, reg_break_share=w_reg, fee_target=fee_L,
                               fee_acquirer=fee_M, entry_cost_unit=entry_cost, exit_cost_unit_break=entry_cost,
                               break_years=t_break, shortable=shortable, data_fresh=fresh)

    rows = []
    for name, case in cases.items():
        e = sg.economics(deal, signal_inputs(inp.p_complete, case, cases[CASE_STRESS]))
        B0 = pr.break_pnl(L0, M0, R, case, carry_b)
        B1 = pr.break_pnl(L0, M0, R, pr.with_regulatory_fee(case, fee_L, fee_M), carry_b)
        Bm = (1 - w_reg) * B0 + w_reg * B1
        rows.append({"case": name, "target_standalone": case.target_standalone, "acquirer_rebound": case.acquirer_rebound,
                     "break_pnl_unit_vote_or_other": B0, "break_pnl_unit_regulatory": B1, "break_pnl_unit": Bm,
                     "break_pnl_pct_of_L": Bm / L0, "breakeven_p": pr.breakeven_probability(C, Bm),
                     "breakeven_p_net": e["p_star"], "ev_unit_at_base_p": pr.expected_pnl(inp.p_complete, C, Bm),
                     "ev_unit_net_at_base_p": e["ev_net"], "excess_per_year_at_base_p": e["excess"]})
    out["completion_pnl_unit"] = C
    out["break_cases"] = pd.DataFrame(rows)
    out["implied_p_grid"] = pr.implied_probability_grid(
        L0, M0, R, C, np.round(np.linspace(380, 700, 17), 0), [0.0, j_half, j_base, j_full, 0.35],
        carry_to_break=carry_b, reg_share=w_reg, fee_target=fee_L, fee_acquirer=fee_M)
    out["market_implied_standalone"] = {
        f"p={p:.0%}, j={j:.0%}": pr.market_implied_standalone(L0, M0, R, p, j, C, fee_L, carry_b, w_reg, fee_M)
        for p in (0.4, round(inp.p_complete, 3), 0.5, 0.64) for j in (j_half, j_base, j_full)}
    out["bottom_up"] = pr.DEFAULT_BOTTOM_UP

    # ----- expected excess return under a probability grid (optimistic / base / pessimistic / stress break)
    cap_unit = inp.rules.regt(L0, R * M0)
    grid = []
    for p in np.round(np.arange(0.30, 0.951, 0.05), 2):
        for nm, key in (("optimistic break", CASE_OPT), ("base break", CASE_BASE),
                        ("pessimistic break", CASE_PESS), ("stress break", CASE_STRESS)):
            e = sg.economics(deal, signal_inputs(float(p), cases[key], cases[CASE_STRESS]))
            r_ = e["ev"] / cap_unit
            ann = (1 + r_) ** (1 / years) - 1 if r_ > -1 else -1
            grid.append({"p_complete": p, "break_case": nm, "ev_unit": e["ev"], "ev_unit_net": e["ev_net"],
                         "return_on_regt_capital": r_, "annualized": ann, "excess_over_rf": e["excess"]})
    out["ev_grid"] = pd.DataFrame(grid)

    # ----- calibration for MC
    curve_date, curve = op.load_treasury_curve()
    rate_fn = op.treasury_rate_fn(curve)
    try:
        ts_m = op.atm_term_structure(A, rate_fn)
        ts_t = op.atm_term_structure(T, rate_fn)
        tgt_exp = ts_m.iloc[(ts_m["expiry"].apply(lambda d: abs((d - deal.expected_close).days))).argmin()]
        acq_vol, acq_vol_src = float(tgt_exp["atm_iv"]), str(tgt_exp["expiry"])
    except FileNotFoundError:
        ts_m = ts_t = pd.DataFrame()
        acq_vol, acq_vol_src = 0.70, "default 0.70 (no option-chain snapshot)"
    out["iv_acquirer"], out["iv_target"] = ts_m, ts_t
    r = np.log(px).diff()
    pre_all = r[r.index <= pd.Timestamp(deal.unaffected_date)]
    vols = {w: float(pre_all[T].tail(w).std() * math.sqrt(252)) for w in (252, 504, 1260)}
    lite_vol = vols[504]   # 2y pre-deal realised; matches LITE's long-dated implied vol (~0.84) - see docs
    pre = pre_all.tail(504)
    rho = float(pre[[T, A]].corr().iloc[0, 1])
    x = np.log1p(hist["gross_spread"])
    spread_vol = float(x.diff().std() * math.sqrt(252))
    out["calibration"] = dict(acq_vol=acq_vol, acq_vol_expiry=acq_vol_src, standalone_vol=lite_vol,
                              standalone_vol_1y=vols[252], standalone_vol_5y=vols[1260], rho=rho,
                              spread_vol=spread_vol, spread_vol_obs=int(x.diff().count()), curve_date=str(curve_date))

    # ----- sizing
    units, binding, size_tab = mg.max_units(inp.equity, L0, M0, R, inp.policy, inp.rules)
    if inp.units_override is not None:
        units = inp.units_override
    pos = mg.units_to_position(max(units, 1), R)
    out["sizing"] = dict(units=units, binding=binding, position=pos, table=size_tab,
                         state=mg.account_state(inp.equity, pos, L0, M0, inp.rules),
                         liquidation_moves=mg.liquidation_moves(inp.equity, pos, L0, M0, inp.rules))

    # ----- scenarios
    specs = sc.default_scenarios(deal, start, L0, M0, out["gross_spread"], S_base, S_stress, j_base, j_full,
                                 inp.vote_date, inp.borrow_fee, inp.fee_tax_rate)
    res = [sc.run_scenario(s, deal, pos, inp.equity, L0, M0, start, inp.borrow_fee, inp.rules, inp.broker)
           for s in specs]
    out["scenarios"] = res
    out["scenario_table"] = sc.results_table(res)
    # same scenarios for a max-leverage Reg T position (to show path risk)
    pos_max = mg.units_to_position(int(inp.equity / inp.rules.regt(2 * L0, 9 * M0)), R)
    res_max = [sc.run_scenario(s, deal, pos_max, inp.equity, L0, M0, start, inp.borrow_fee, inp.rules, inp.broker)
               for s in specs]
    out["scenario_table_max_leverage"] = sc.results_table(res_max)
    out["max_leverage_position"] = pos_max

    # ----- Monte Carlo
    cfg_o = mc.MCConfig(n_paths=inp.mc_paths, acq_vol=acq_vol, standalone0=S_base, standalone_vol=lite_vol, rho=rho,
                        spread_vol=spread_vol, p_acq_vote_fail=inp.p_acq_vote_fail,
                        p_tgt_vote_fail=inp.p_tgt_vote_fail, p_reg_fail=inp.p_reg_fail,
                        p_other_fail=inp.p_other_fail, vote_date=inp.vote_date,
                        completion_mode=deal.expected_close, completion_max=deal.final_outside_date,
                        deal_discount=delta, rebound_kappa=0.5, fee_per_share=fee_ps, acq_fee_per_share=fee_acq_ps,
                        fee_haircut=inp.fee_tax_rate, borrow_fee=inp.borrow_fee, financing_rate=fin_rate)
    # base = integrated over model uncertainty: LITE's starting standalone value lognormal around the factor model
    # (log-sd = the model's residual band), MRVL's reversal fraction uniform in kappa_range
    cfg_b = dataclasses.replace(cfg_o, standalone0_sigma=S_sigma, kappa_low=inp.kappa_range[0],
                                kappa_high=inp.kappa_range[1], seed=cfg_o.seed + 2)
    cfg_s = dataclasses.replace(cfg_o, standalone0=S_stress, rebound_kappa=1.0, seed=cfg_o.seed + 1)
    # the first version's base (log-uniform between the stress and factor values, kappa 0-1), for comparison
    cfg_v1 = dataclasses.replace(cfg_o, standalone0_low=S_stress, standalone0_high=S_base, kappa_low=0.0,
                                 kappa_high=1.0, seed=cfg_o.seed + 2)
    out["mc"] = mc.simulate(deal, pos, inp.equity, L0, M0, start, cfg_b, inp.rules)
    out["mc_optimistic"] = mc.simulate(deal, pos, inp.equity, L0, M0, start, cfg_o, inp.rules)
    out["mc_stress"] = mc.simulate(deal, pos, inp.equity, L0, M0, start, cfg_s, inp.rules)
    out["mc_v1_base"] = mc.simulate(deal, pos, inp.equity, L0, M0, start, cfg_v1, inp.rules)
    out["hedges"] = mc.hedge_comparison(out["mc"].unit_terminal, L0, M0, R)
    out["hedges_optimistic"] = mc.hedge_comparison(out["mc_optimistic"].unit_terminal, L0, M0, R)
    out["hedges_stress"] = mc.hedge_comparison(out["mc_stress"].unit_terminal, L0, M0, R)
    # tail-based size cap: P&L is ~linear in units at these leverages, so ES scales with size
    es_unit = out["mc"].summary["es95"] / max(units, 1)
    es_cap_units = int(inp.policy_es_cap * inp.equity // es_unit) if es_unit > 0 else units   # no tail loss: no cap
    out["sizing"]["es95_per_unit"] = es_unit
    out["sizing"]["es_cap_units"] = es_cap_units
    out["sizing"]["recommended_units"] = min(units, es_cap_units) if inp.units_override is None else units
    # standalone-vol sensitivity of the base model (fewer paths)
    sens = []
    for lab, v in (("5y realised", vols[1260]), ("2y realised (base)", vols[504]), ("1y realised", vols[252])):
        m_ = mc.simulate(deal, pos, inp.equity, L0, M0, start,
                         dataclasses.replace(cfg_b, standalone_vol=v, n_paths=max(inp.mc_paths // 2, 2000)), inp.rules)
        sens.append({"standalone_vol": lab, "vol": v, **{k: m_.summary[k] for k in
                                                          ("mean_pnl", "p_loss", "var95", "es95", "p_liquidation")}})
    out["mc_vol_sensitivity"] = pd.DataFrame(sens)

    # ----- signal
    fresh = all(a.label in ("real-time", "closing print") for _, a in out["quotes"].values())
    if yahoo.data_mode() == "sample":
        fresh = False          # synthetic sample data must never produce a TRADE signal
    out["data_mode"] = yahoo.data_mode()
    si = signal_inputs(inp.p_complete, cases[CASE_BASE], cases[CASE_STRESS], fresh=fresh,
                       shortable=None if inp.shortable_shares is None else inp.shortable_shares > 0)
    out["signal_inputs"] = si
    out["signal"] = sg.evaluate(deal, si)
    # the same rules under the first version's base break case (with the fee and financing corrections applied)
    out["signal_pessimistic"] = sg.evaluate(deal, dataclasses.replace(si, base_break=cases[CASE_PESS]))
    out["inputs"] = inp
    return out
