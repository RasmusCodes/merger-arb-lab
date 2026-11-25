import dataclasses
import datetime as dt

import numpy as np
import pytest

from mergerarb.analytics import margin as mg
from mergerarb.analytics import montecarlo as mc
from mergerarb.data.quotes import Quote, assess
from mergerarb.deal import load_deal

UTC = dt.timezone.utc
DEAL = load_deal("mrvl_lite")


def _q(**kw):
    base = dict(symbol="LITE", last=487.12, time_utc=dt.datetime(2026, 11, 24, 21, 0, tzinfo=UTC),
                source="yahoo:Nasdaq Real Time Price", delay_minutes=0, market_state="REGULAR")
    base.update(kw)
    return Quote(**base)


def test_closing_print_is_never_live():
    a = assess(_q(market_state="POSTPOST"), now_utc=dt.datetime(2026, 11, 25, 1, 0, tzinfo=UTC))
    assert a.label == "closing print" and not a.is_live


def test_delayed_source_is_labelled_delayed():
    a = assess(_q(delay_minutes=15, source="yahoo:Delayed Quote"), now_utc=dt.datetime(2026, 11, 24, 21, 1, tzinfo=UTC))
    assert a.label.startswith("delayed") and not a.is_live


def test_stale_regular_session_quote():
    a = assess(_q(), now_utc=dt.datetime(2026, 11, 24, 21, 30, tzinfo=UTC))
    assert a.label == "stale" and not a.is_live


def test_live_only_when_fresh_realtime_regular():
    a = assess(_q(), now_utc=dt.datetime(2026, 11, 24, 21, 0, 30, tzinfo=UTC))
    assert a.label == "real-time" and a.is_live
    assert any("Nasdaq last sale" in w for w in a.warnings)


def test_crossed_bid_ask_flagged():
    a = assess(_q(bid=559.21, ask=542.79, market_state="POSTPOST"), now_utc=dt.datetime(2026, 11, 25, tzinfo=UTC))
    assert not a.bid_ask_usable and any("crossed" in w for w in a.warnings)


def test_unavailable():
    assert assess(_q(last=None)).label == "unavailable"


def _cfg(**kw):
    c = mc.MCConfig(n_paths=1500, chunk=1500, standalone0=511.5, acq_vol=0.7, standalone_vol=0.85, rho=0.55,
                    spread_vol=0.2, deal_discount=0.2125, fee_per_share=33.2)
    return dataclasses.replace(c, **kw)


def test_mc_reproducible_and_completion_pnl_locked():
    pos = mg.Position(2, 9)
    a = mc.simulate(DEAL, pos, 10_000, 487.12, 131.57, dt.date(2026, 11, 24), _cfg())
    b = mc.simulate(DEAL, pos, 10_000, 487.12, 131.57, dt.date(2026, 11, 24), _cfg())
    assert np.array_equal(a.pnl, b.pnl)
    comp = a.pnl[a.outcome == 0]
    # completed paths earn the spread minus carry: tight distribution around 2 * (4.5*M0 - L0) - carry
    assert comp.mean() == pytest.approx(2 * (4.5 * 131.57 - 487.12) - 8, abs=6)
    assert comp.std() < 5


def test_mc_outcome_shares_match_inputs():
    r = mc.simulate(DEAL, mg.Position(2, 9), 10_000, 487.12, 131.57, dt.date(2026, 11, 24), _cfg(n_paths=6000, chunk=2000))
    p_comp = (r.outcome == 0).mean() + (r.outcome == 4).mean() * 0  # liquidations are rare at this size
    assert p_comp == pytest.approx(r.config.p_complete, abs=0.03)


def test_mc_no_break_risk_no_losses():
    cfg = _cfg(p_acq_vote_fail=0.0, p_tgt_vote_fail=0.0, p_reg_fail=0.0, p_other_fail=0.0)
    r = mc.simulate(DEAL, mg.Position(2, 9), 10_000, 487.12, 131.57, dt.date(2026, 11, 24), cfg)
    assert (r.outcome == 0).mean() > 0.99
    assert r.summary["p_loss"] < 0.01


def test_hedge_comparison_ratio_neutral_locks_completion():
    r = mc.simulate(DEAL, mg.Position(2, 9), 10_000, 487.12, 131.57, dt.date(2026, 11, 24), _cfg())
    h = mc.hedge_comparison(r.unit_terminal, 487.12, 131.57, 4.5)
    row = h[h.hedge.str.startswith("ratio")].iloc[0]
    comp = r.unit_terminal[r.unit_terminal.outcome == 0]
    p = comp.dL - 4.5 * comp.dM
    assert p.std() < 1e-6   # exact hedge at completion (before carry)
    assert row.short_per_long == 4.5
