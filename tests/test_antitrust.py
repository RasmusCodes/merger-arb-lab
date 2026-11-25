import dataclasses
import datetime as dt

import numpy as np
import pytest

from mergerarb.analytics import antitrust as at

L0, M0, R = 487.12, 131.57, 4.5


def test_two_state_identities():
    st = at.from_q_and_rebound(L0, M0, R, 0.5, 0.135)
    # spread$ = (1-q) * G, and the mixture reproduces both prices
    assert st.spread_usd == pytest.approx((1 - st.q) * st.gap)
    assert st.q * st.M_C + (1 - st.q) * st.M_B == pytest.approx(M0)
    assert st.q * R * st.M_C + (1 - st.q) * st.S == pytest.approx(L0)
    # arb P&L for a q shock equals dq * G, whatever the split between the legs
    for j in (0.0, 0.135, 0.27):
        s = at.from_q_and_rebound(L0, M0, R, 0.5, j)
        r = s.shock(-0.1)
        pnl = (r["L"] - L0) - R * (r["M"] - M0)
        assert pnl == pytest.approx(r["arb_pnl_unit"]) == pytest.approx(-0.1 * s.gap)


def test_break_values_round_trip():
    st = at.from_q_and_rebound(L0, M0, R, 0.42, 0.2)
    back = at.from_break_values(L0, M0, R, st.S, st.M_B)
    assert back.q == pytest.approx(0.42) and back.M_C == pytest.approx(st.M_C)
    assert at.from_break_values(L0, M0, R, 10_000.0, 100.0) is None   # inconsistent assumptions


def test_completion_and_break_payoffs():
    st = at.from_q_and_rebound(L0, M0, R, 0.5, 0.135)
    up, down = st.shock(1 - st.q), st.shock(-st.q)
    assert up["arb_pnl_unit"] == pytest.approx(st.spread_usd)          # completion earns the spread
    assert down["arb_pnl_unit"] == pytest.approx(-st.q * st.gap)        # break loses q * G
    assert down["gross_spread"] > up["gross_spread"]


def test_tree_probability_and_bayesian_consistency():
    t = at.RegTree()
    P = t.p_complete()
    assert P == pytest.approx(t.p_us() * t.p_votes_pass * t.p_eu() * t.p_samr() * (1 - t.p_other_break))
    nw = t.news().set_index("headline")
    # martingale: the expected update across the outcomes of each gate is zero
    us = nw.loc[["HSR expires, no second request", "HSR second request issued"]]
    assert (us.p_event * us.q_after).sum() == pytest.approx(P)
    eu = nw.loc[["EU clears in phase 1", "EU opens phase 2"]]
    assert (eu.p_event * eu.q_after).sum() == pytest.approx(P)
    v = nw.loc[["Both stockholder votes pass", "A stockholder vote fails"]]
    assert (v.p_event * v.q_after).sum() == pytest.approx(P)
    assert nw.loc["US agency sues to block", "dq"] < nw.loc["EU opens phase 2", "dq"] < 0


def test_news_impact_signs():
    ni = at.news_impact(at.RegTree(), L0, M0, R, 0.135, market_q=0.5).set_index("headline")
    sue = ni.loc["US agency sues to block"]
    assert sue.spread_pts > 0 and sue.acq_move > 0 and sue.arb_pnl_unit < 0
    ok = ni.loc["SAMR approves (conditional)"]
    assert ok.spread_pts < 0 and ok.acq_move < 0 and ok.arb_pnl_unit > 0


def test_news_mc_no_edge_is_roughly_fair_and_edge_helps():
    base = at.RegTree()
    cfg = at.NewsMCConfig(n_paths=6000, seed=3)
    fair = at.news_mc(base, base, L0, M0, R, dt.date(2026, 11, 24), 2, cfg)
    opt = at.news_mc(dataclasses.replace(base, p_samr_approve=0.85), base, L0, M0, R, dt.date(2026, 11, 24), 2, cfg)
    f = fair["table"].set_index("strategy")
    o = opt["table"].set_index("strategy")
    assert o.loc["now_hold", "mean_pnl_if_traded"] > f.loc["now_hold", "mean_pnl_if_traded"]
    # with no edge the mean P&L of holding is small relative to its dispersion
    assert abs(f.loc["now_hold", "mean_pnl_if_traded"]) < 0.1 * f.loc["now_hold", "es95_if_traded"]
    assert 0.7 < f.loc["wait_threshold", "p_trade"] <= 1.0
