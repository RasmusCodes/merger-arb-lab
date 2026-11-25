import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from mergerarb.analytics import probability as pr
from mergerarb.analytics import spread as sp
from mergerarb.analytics.carry import BrokerTerms, carry
from mergerarb.deal import load_deal

DEAL = load_deal("mrvl_lite")


def test_deal_terms_match_merger_agreement():
    assert DEAL.exchange_ratio == 4.5
    assert DEAL.cash_per_share == 0.0
    assert DEAL.final_outside_date == dt.date(2028, 8, 1)
    assert DEAL.outside_date == dt.date(2027, 11, 1)
    assert DEAL.termination_fees["regulatory_reverse_fee"] == 3.2e9
    assert DEAL.validate() == []


def test_announcement_premium_reproduces_press_release():
    # press release: ~$1,132 per LITE share and a ~23.9% premium on Oct-28 unaffected closes
    implied = DEAL.implied_value(DEAL.unaffected_acquirer)
    assert implied == pytest.approx(1132.4, abs=0.5)
    assert implied / DEAL.unaffected_target - 1 == pytest.approx(0.239, abs=0.0015)


def test_spread_definitions_nov24_close():
    L, M = 487.12, 131.57
    assert sp.implied_value(M, 4.5) == pytest.approx(592.065)
    assert sp.spread_dollars(L, M, 4.5) == pytest.approx(104.945)
    assert sp.gross_spread(L, M, 4.5) == pytest.approx(592.065 / 487.12 - 1)
    # cash-and-stock consideration adds cash
    assert sp.implied_value(10.0, 0.5, cash=32.0) == pytest.approx(37.0)


def test_annualize():
    assert sp.annualize(0.21, 1.0) == pytest.approx(0.21)
    assert sp.annualize(0.21, 2.0) == pytest.approx(math.sqrt(1.21) - 1)
    assert sp.annualize(0.21, 2.0, "simple") == pytest.approx(0.105)
    with pytest.raises(ValueError):
        sp.annualize(0.1, 0.0)


def test_ratio_hedge_pnl_is_independent_of_acquirer_price_at_completion():
    """Long 1 target + short R acquirer: at completion the target becomes R acquirer shares, so P&L = spread."""
    L0, M0, R = 487.12, 131.57, 4.5
    for MT in (50.0, 131.57, 400.0):
        pnl = (R * MT - L0) - R * (MT - M0)
        assert pnl == pytest.approx(R * M0 - L0)


def test_dollar_neutral_hedge_is_not_risk_neutral():
    L0, M0, R = 487.12, 131.57, 4.5
    h = L0 / M0                       # dollar-neutral short per target share
    pnl = lambda MT: (R * MT - L0) - h * (MT - M0)
    assert pnl(65.0) < pnl(131.57) < pnl(260.0)   # residual long-acquirer exposure of (R - h) shares


def test_sensitivity_grid_centre_equals_current_spread():
    g = sp.sensitivity_grid(487.12, 131.57, 4.5)
    assert g.loc[0.0, 0.0] == pytest.approx(sp.gross_spread(487.12, 131.57, 4.5))
    assert g.loc[0.0, 0.1] > g.loc[0.0, 0.0]       # acquirer up -> spread wider
    assert g.loc[0.1, 0.0] < g.loc[0.0, 0.0]       # target up -> spread tighter


def test_spread_history_stats():
    idx = pd.bdate_range("2026-11-02", periods=5)
    t = pd.Series([100.0, 100, 100, 100, 100], idx)
    a = pd.Series([25.0, 25.5, 26, 26.5, 27], idx)
    h = sp.spread_history(t, a, 4.5)
    st = sp.stats(h)
    assert st.current == pytest.approx(4.5 * 27 / 100 - 1)
    assert st.percentile == 100.0
    assert st.n_obs == 5


def test_dividend_projection_and_carry():
    start, end = dt.date(2026, 11, 24), dt.date(2028, 3, 31)
    exd = DEAL.acquirer_dividend.ex_dates_between(start, end)
    assert len(exd) == 5 and exd[0].month == 1 and exd[0].year == 2027
    cb = carry(DEAL, 1, 4.5, 487.12, 131.57, start, end, 0.0032, 10_000)
    assert cb.acquirer_dividends_paid == pytest.approx(5 * 0.06 * 4.5)
    assert cb.borrow_fee == pytest.approx(0.0032 * 4.5 * 131.57 * (end - start).days / 365.25)
    assert cb.short_rebate_income == 0.0           # IBKR pays 0% on the first USD 100k of short proceeds
    assert DEAL.target_dividend.ex_dates_between(start, end) == []


def test_ibkr_commission_schedule():
    b = BrokerTerms()
    assert b.commission(4, 487.12) == 1.00          # USD 1 minimum
    assert b.commission(1000, 50.0) == 5.00         # 0.005/share
    assert b.commission(10, 0.20) == pytest.approx(0.02)  # 1% of trade value cap


def test_interest_tiers_small_account():
    b = BrokerTerms()
    assert b.credit_rate(9_000, 10_000) == 0.0
    assert b.short_proceeds_rate(50_000, 60_000) == 0.0
    big = b.short_proceeds_rate(500_000, 1_000_000)
    assert 0 < big < b.benchmark_rate


def test_breakeven_probability():
    assert pr.breakeven_probability(100, -100) == pytest.approx(0.5)
    assert pr.breakeven_probability(100, 20) == 0.0
    assert pr.breakeven_probability(-5, -100) == 1.0
    C, B = 101.0, -222.0
    p = pr.breakeven_probability(C, B)
    assert pr.expected_pnl(p, C, B) == pytest.approx(0.0, abs=1e-9)


def test_market_implied_standalone_inverts_break_pnl():
    L0, M0, R, C = 487.12, 131.57, 4.5, 101.0
    for p, j in ((0.5, 0.135), (0.7, 0.27)):
        S = pr.market_implied_standalone(L0, M0, R, p, j, C)
        B = pr.break_pnl(L0, M0, R, pr.BreakCase("x", S, j))
        assert pr.expected_pnl(p, C, B) == pytest.approx(0.0, abs=1e-6)


def test_bottom_up_product():
    bu = pr.DEFAULT_BOTTOM_UP
    assert bu.total("low") < bu.total("base") < bu.total("high")
    assert bu.total("base") == pytest.approx(np.prod([c.p_pass_base for c in bu.components]))
