import datetime as dt

import pytest

from mergerarb.analytics import margin as mg
from mergerarb.analytics import scenarios as sc
from mergerarb.analytics import signal as sg
from mergerarb.analytics.probability import BreakCase
from mergerarb.deal import load_deal

DEAL = load_deal("mrvl_lite")
L0, M0 = 487.12, 131.57


def test_units_to_position_exact_ratio():
    p = mg.units_to_position(1, 4.5)
    assert (p.target_shares, p.acquirer_short_shares) == (2, 9)
    p = mg.units_to_position(3, 4.5)
    assert (p.target_shares, p.acquirer_short_shares) == (6, 27)
    assert mg.units_to_position(2, 2.0).acquirer_short_shares == 4
    assert mg.units_to_position(1, 0.2838).target_shares == 5000  # ratio 0.2838 needs 5000 target shares per exact unit


def test_ibkr_reg_t_requirements():
    r = mg.MarginRules()
    pos = mg.Position(4, 18)
    st = mg.account_state(10_000, pos, L0, M0, r)
    lv, sv = 4 * L0, 18 * M0
    assert st.regt_req == pytest.approx(0.5 * (lv + sv))
    assert st.maint_req == pytest.approx(0.25 * lv + 0.30 * sv)
    assert st.initial_req == pytest.approx(0.2625 * lv + 0.315 * sv)
    assert st.excess_liquidity == pytest.approx(10_000 - st.maint_req)
    assert not st.margin_call


def test_short_maintenance_price_buckets():
    r = mg.MarginRules()
    assert r.short_maint_per_share(100) == pytest.approx(30.0)
    assert r.short_maint_per_share(10) == 5.0
    assert r.short_maint_per_share(4) == 4.0
    assert r.short_maint_per_share(1) == 2.5


def test_margin_call_detection():
    pos = mg.Position(18, 81)                # ~2x gross on 10k
    st = mg.account_state(10_000, pos, L0, M0)
    assert st.regt_excess > -1                # just inside Reg T at entry
    eq = 10_000 + 18 * (L0 * 1.6 / 1.35 * 1.2155 - L0) - 81 * (M0 * 0.6)  # MRVL +60%, spread to 35%
    st2 = mg.account_state(eq, pos, 4.5 * M0 * 1.6 / 1.35, M0 * 1.6)
    assert st2.margin_call


def test_stress_loss_sign_and_survival():
    pos = mg.Position(4, 18)
    s = mg.Stress("break", -0.124, 0.27)
    loss = mg.stress_loss(pos, L0, M0, s)
    assert loss == pytest.approx(4 * L0 * -0.124 - 18 * M0 * 0.27)
    assert mg.survives(10_000, pos, L0, M0, s, mg.MarginRules())


def test_max_units_respects_policy():
    units, binding, tab = mg.max_units(10_000, L0, M0, 4.5)
    assert units >= 1
    row = tab[tab.units == units].iloc[0]
    assert row.worst_stress_loss_pct >= -0.10
    nxt = tab[tab.units == units + 1].iloc[0]
    assert nxt.worst_stress_loss_pct < -0.10 or nxt.excess_liquidity < 5_000 or "survival" in binding


def test_liquidation_moves_monotone_in_size():
    small = mg.liquidation_moves(10_000, mg.Position(4, 18), L0, M0)
    big = mg.liquidation_moves(10_000, mg.Position(18, 81), L0, M0)
    assert big["acquirer_up_target_flat"] is not None
    assert small["acquirer_up_target_flat"] is None or small["acquirer_up_target_flat"] > big["acquirer_up_target_flat"]


def _specs():
    return sc.default_scenarios(DEAL, dt.date(2026, 11, 24), L0, M0, 4.5 * M0 / L0 - 1, 612.95, 426.85, 0.135, 0.27)


def test_on_schedule_scenario_pnl_equals_spread_minus_carry():
    spec = next(s for s in _specs() if s.key == "1_on_schedule")
    pos = mg.Position(4, 18)
    r = sc.run_scenario(spec, DEAL, pos, 10_000, L0, M0, dt.date(2026, 11, 24), 0.0032)
    gross = 4 * (4.5 * M0 - L0)
    assert r.outcome == "complete"
    assert r.pnl_long + r.pnl_short == pytest.approx(gross, rel=1e-9)
    assert r.net_pnl == pytest.approx(gross - r.dividends_paid - r.borrow_cost + r.interest_net - r.commissions_slippage)
    assert r.dividends_paid == pytest.approx(5 * 0.06 * 18)


def test_mrvl_rally_liquidates_only_the_levered_book():
    spec = next(s for s in _specs() if s.key == "6_mrvl_up60_lite_lags")
    small = sc.run_scenario(spec, DEAL, mg.Position(4, 18), 10_000, L0, M0, dt.date(2026, 11, 24), 0.0032)
    big = sc.run_scenario(spec, DEAL, mg.Position(18, 81), 10_000, L0, M0, dt.date(2026, 11, 24), 0.0032)
    assert not small.liquidated and small.outcome == "complete"
    assert big.liquidated and big.net_pnl < 0


def test_six_month_delay_flags_outside_date():
    spec = next(s for s in _specs() if s.key == "3_delay_6m")
    assert spec.end_date > DEAL.final_outside_date
    assert "outside date" in spec.note


def test_signal_rules():
    base = BreakCase("base", 511.5, 0.135)
    stress = BreakCase("stress", 426.85, 0.27)
    x = sg.SignalInputs(L0, M0, dt.date(2026, 11, 24), DEAL.expected_close, 0.0032, 0.63, base, stress)
    s = sg.evaluate(DEAL, x)
    assert s.decision in {"WATCH", "NO TRADE", "TRADE"}
    assert s.metrics["breakeven_p_stress"] > s.metrics["breakeven_p_base"]
    # a much wider spread at the same assumptions clears the hurdle
    x2 = sg.SignalInputs(380.0, M0, dt.date(2026, 11, 24), DEAL.expected_close, 0.0032, 0.75, base, stress)
    assert sg.evaluate(DEAL, x2).metrics["expected_excess_annual"] > s.metrics["expected_excess_annual"]
    # expensive borrow is a hard stop
    x3 = sg.SignalInputs(L0, M0, dt.date(2026, 11, 24), DEAL.expected_close, 0.08, 0.63, base, stress)
    assert sg.evaluate(DEAL, x3).decision == "NO TRADE"
    # stale data is a hard stop
    x4 = sg.SignalInputs(L0, M0, dt.date(2026, 11, 24), DEAL.expected_close, 0.0032, 0.63, base, stress,
                         data_fresh=False)
    assert sg.evaluate(DEAL, x4).decision == "NO TRADE"
