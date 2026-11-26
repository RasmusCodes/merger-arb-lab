"""Regression tests for the 2026-11-25 triple check (each fails on the code before the fixes)."""
from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np
import pytest

from mergerarb.analytics import antitrust as at
from mergerarb.analytics import montecarlo as mc
from mergerarb.analytics import probability as pr
from mergerarb.analytics import scenarios as sc
from mergerarb.analytics import signal as sg
from mergerarb.analytics.margin import units_to_position
from mergerarb.deal import load_deal

DEAL = load_deal("mrvl_lite")
L0, M0, R = 487.12, 131.57, 4.5
START = dt.date(2026, 11, 24)
FEE_L = DEAL.regulatory_fee_per_target_share() * 0.79
FEE_M = DEAL.regulatory_fee_per_acquirer_share() * 0.79


def test_acquirer_fee_share_count_and_amount():
    # 877,987,882 common + 2,000,000 preferred x 10.889 (merger agreement s4.2(c)(i))
    assert DEAL.acquirer_fully_diluted_shares() == pytest.approx(877_987_882 + 2_000_000 * 10.889)
    assert DEAL.regulatory_fee_per_acquirer_share() == pytest.approx(3.2e9 / 899_765_882, rel=1e-6)
    assert FEE_L == pytest.approx(26.24, abs=0.01) and FEE_M == pytest.approx(2.81, abs=0.01)


def test_break_pnl_counts_the_fee_on_both_legs():
    case = pr.BreakCase("x", 600.0, 0.2)
    b0 = pr.break_pnl(L0, M0, R, case)
    b1 = pr.break_pnl(L0, M0, R, pr.with_regulatory_fee(case, FEE_L, FEE_M))
    assert b1 - b0 == pytest.approx(FEE_L + R * FEE_M)          # target receives it, the shorted acquirer pays it
    bm = pr.mixed_break_pnl(L0, M0, R, case, 0.0, 0.61, FEE_L, FEE_M)
    assert bm == pytest.approx(0.39 * b0 + 0.61 * b1)


def _si(**kw):
    base = dict(target_price=L0, acquirer_price=M0, valuation_date=START, expected_close=DEAL.expected_close,
                borrow_fee=0.0032, p_complete=0.4752, base_break=pr.BreakCase("b", 612.95, 0.2024),
                stress_break=pr.BreakCase("s", 426.85, 0.2698), risk_free=0.0459)
    base.update(kw)
    return sg.SignalInputs(**base)


def test_signal_includes_the_regulatory_fee():
    no_fee = sg.economics(DEAL, _si())
    fee = sg.economics(DEAL, _si(reg_break_share=0.61, fee_target=FEE_L, fee_acquirer=FEE_M))
    assert fee["B"] - no_fee["B"] == pytest.approx(0.61 * (FEE_L + R * FEE_M))
    assert fee["ev"] > no_fee["ev"]


def test_financing_is_charged_on_the_long_leg_over_the_holding_time():
    e0 = sg.economics(DEAL, _si(financing_rate=0.0, break_years=1.0))
    e1 = sg.economics(DEAL, _si(financing_rate=0.05, break_years=1.0))
    p, yrs = 0.4752, e1["years"]
    assert e0["ev_net"] - e1["ev_net"] == pytest.approx(0.05 * L0 * (p * yrs + (1 - p) * 1.0))
    assert e1["cap_years"] == pytest.approx(0.5 * (L0 + R * M0) * (p * yrs + (1 - p) * 1.0))


def test_market_implied_standalone_prices_the_arb_at_zero():
    C, carry, w, j, p = 101.0, 2.1, 0.61, 0.2024, 0.48
    S = pr.market_implied_standalone(L0, M0, R, p, j, C, FEE_L, carry, w, FEE_M)
    B = pr.mixed_break_pnl(L0, M0, R, pr.BreakCase("m", S, j), carry, w, FEE_L, FEE_M)
    assert p * C + (1 - p) * B == pytest.approx(0.0, abs=1e-9)


def _cfg(**kw):
    c = mc.MCConfig(n_paths=4000, chunk=2000, acq_vol=0.71, standalone0=612.95, standalone_vol=0.85, rho=0.55,
                    spread_vol=0.2, p_reg_fail=0.3952, deal_discount=0.2125, rebound_kappa=0.75,
                    fee_per_share=DEAL.regulatory_fee_per_target_share(), completion_mode=DEAL.expected_close,
                    completion_max=DEAL.final_outside_date)
    return dataclasses.replace(c, **kw)


def test_mc_acquirer_pays_the_fee_on_regulatory_breaks_only():
    pos = units_to_position(1, R)
    a = mc.simulate(DEAL, pos, 10_000.0, L0, M0, START, _cfg(acq_fee_per_share=0.0))
    b = mc.simulate(DEAL, pos, 10_000.0, L0, M0, START, _cfg(acq_fee_per_share=DEAL.regulatory_fee_per_acquirer_share()))
    ua, ub = a.unit_terminal, b.unit_terminal
    reg = ua["outcome"].to_numpy() == 2
    other = ua["outcome"].isin([1, 3]).to_numpy()
    assert reg.any() and other.any()
    np.testing.assert_allclose((ub["dM"] - ua["dM"]).to_numpy()[reg], -FEE_M, rtol=1e-9)
    np.testing.assert_allclose((ub["dM"] - ua["dM"]).to_numpy()[other], 0.0, atol=1e-12)


def test_mc_lognormal_standalone_and_martingale_acquirer():
    pos = units_to_position(1, R)
    r = mc.simulate(DEAL, pos, 10_000.0, L0, M0, START, _cfg(n_paths=20000, standalone0_sigma=0.15, kappa_low=0.5,
                                                              kappa_high=1.0))
    u = r.unit_terminal
    # completion-state acquirer value makes the acquirer a martingale overall (two-state consistency at our p)
    assert abs((u["dM"].mean() + M0) / M0 - 1) < 0.03
    # an unhedged long target no longer collects the spread on completion (MRVL ends at its deal-state value)
    comp = u["outcome"] == 0
    assert (u.loc[comp, "dL"]).mean() < R * M0 - L0 - 50


def test_news_mc_market_probability_is_a_martingale_inside_the_samr_window():
    tree = at.RegTree()
    sim = at._simulate_tree(tree, np.random.default_rng(3), 60_000, START)
    q = at._q_path(tree, sim, 60_000, 92)
    assert np.abs(q.mean(0) - q[:, 0].mean()).max() < 0.01        # was 0.147 at week 88 before the fix


def test_news_mc_no_edge_world_has_no_drift():
    tree = at.RegTree()
    cfg = at.NewsMCConfig(n_paths=40000, borrow_fee=0.0, div_per_quarter_unit=0.0)
    out = at.news_mc(tree, tree, L0, M0, R, START, 1, cfg)
    hold = out["table"].set_index("strategy").loc["now_hold"]
    assert abs(hold["mean_pnl_if_traded"]) < 5.0                    # +9 per share when the fee was counted twice


def test_headline_table_has_day_of_moves_for_second_stage_news():
    tree = at.RegTree()
    nw = tree.news().set_index("headline")
    P, us = tree.p_complete(), tree.p_us()
    after_suit = P / us * tree.p_court_win
    assert nw.loc["Court blocks the merger", "q_before_event"] == pytest.approx(after_suit)
    assert nw.loc["Court blocks the merger", "dq_day"] == pytest.approx(-after_suit)
    assert nw.loc["Court blocks the merger", "dq"] == pytest.approx(-P)
    ni = at.news_impact(tree, L0, M0, R, 0.2024, market_q=0.5).set_index("headline")
    assert ni.loc["Court rules for the merger", "arb_pnl_unit_day"] > ni.loc["Court rules for the merger", "arb_pnl_unit"]


def test_regulatory_scenario_uses_after_tax_fee_on_both_legs():
    specs = sc.default_scenarios(DEAL, START, L0, M0, R * M0 / L0 - 1, 612.95, 426.85, 0.2024, 0.2698)
    s4d = next(s for s in specs if s.key == "4d_break_regulatory")
    assert s4d.fee_cash_per_share == pytest.approx(FEE_L)
    assert s4d.acquirer_fee_per_share == pytest.approx(FEE_M)
