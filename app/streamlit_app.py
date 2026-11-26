"""Local dashboard (Streamlit). Research views work offline from the saved snapshot; the Execution tab talks only to
the local execution service on 127.0.0.1 (python -m mergerarb.execution.service) and is hidden unless enabled.

    streamlit run app/streamlit_app.py
    MERGERARB_ENABLE_EXECUTION=1 streamlit run app/streamlit_app.py      # show the Execution tab
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from mergerarb.analytics import margin as mg
from mergerarb.analytics import montecarlo as mc
from mergerarb.analytics import probability as pr
from mergerarb.analytics import scenarios as sc
from mergerarb.analytics import signal as sg
from mergerarb.analytics import spread as sp
from mergerarb.analytics import standalone as sa
from mergerarb.analytics.carry import BrokerTerms
from mergerarb.data import options as op
from mergerarb.data import quotes as qt
from mergerarb.data import yahoo
from mergerarb.deal import load_deal
from mergerarb.execution.orders import build_pair_plan, validate_plan

S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
BLUES = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
ROOT = pathlib.Path(__file__).resolve().parents[1]
st.set_page_config(page_title="Merger-arb lab: MRVL / LITE", layout="wide")


@st.cache_data(show_spinner=False)
def base_data():
    deal = load_deal("mrvl_lite")
    syms = [deal.target.ticker, deal.acquirer.ticker, "SMH", "COHR", "AAOI", "FN", "CIEN"]
    px = pd.concat({s: yahoo.load_daily(s)["close"] for s in syms}, axis=1).sort_index()
    tgt, acq = sa.standalone_table(px, deal.target.ticker, deal.acquirer.ticker, deal.unaffected_date, px.index[-1])
    qs = {s: yahoo.load_quote(s) for s in (deal.target.ticker, deal.acquirer.ticker)}
    curve_date, curve = op.load_treasury_curve()
    return deal, px, tgt, acq, qs, curve


deal, px, tgt_est, acq_est, saved_quotes, curve = base_data()
T, A = deal.target.ticker, deal.acquirer.ticker
rate_fn = op.treasury_rate_fn(curve)

# ------------------------------------------------------------------------------------------------ quotes
if "quotes" not in st.session_state:
    st.session_state.quotes = saved_quotes
    st.session_state.quote_src = "saved snapshot"
st.title("Marvell / Lumentum merger arbitrage")
if yahoo.data_mode() == "sample":
    st.error("SYNTHETIC SAMPLE DATA (MERGERARB_DATA=sample): these are generated numbers for offline testing, "
             "not market prices. Do not draw conclusions from them.")
c1, c2, c3 = st.columns([2, 2, 1])
with c3:
    if st.button("Refresh quotes (Yahoo)", help="Unofficial source; quotes are labelled by their real freshness"):
        try:
            st.session_state.quotes = yahoo.fetch_quotes([T, A])
            st.session_state.quote_src = "Yahoo (fetched now)"
        except Exception as e:  # network or rate limit
            st.warning(f"refresh failed: {e}")
qd = st.session_state.quotes
for col, s in ((c1, T), (c2, A)):
    q = qd[s]
    a = qt.assess(q)
    with col:
        st.metric(f"{s} ({a.label})", f"${q.last:,.2f}" if q.last else "n/a",
                  f"{(q.last / q.prev_close - 1):+.2%} vs prev close" if q.last and q.prev_close else None)
        st.caption(f"{q.source} | print time {q.time_utc:%Y-%m-%d %H:%M} UTC | " + ("; ".join(a.warnings) or "no warnings"))
if not all(qt.assess(qd[s]).is_live for s in (T, A)):
    st.info("Quotes are NOT live (closing/delayed prints). Spreads below use these prints.")

# ------------------------------------------------------------------------------------------------ sidebar inputs
sb = st.sidebar
sb.header("Deal terms")
ratio = sb.number_input("Exchange ratio (MRVL per LITE)", value=float(deal.exchange_ratio), step=0.01, format="%.4f",
                        help="Contract: 4.50 (merger agreement s3.1(a)(i)). Change only to test sensitivity.")
cash = sb.number_input("Cash per LITE share", value=float(deal.cash_per_share), step=1.0)
close = sb.date_input("Expected closing date", value=deal.expected_close, help="Guided: first half of 2028")
L0 = sb.number_input(f"{T} price", value=float(qd[T].last), step=0.5)
M0 = sb.number_input(f"{A} price", value=float(qd[A].last), step=0.1)
sb.header("Account and position")
equity = sb.number_input("Account equity (USD)", value=10_000.0, step=1_000.0)
lot = mg.exact_lot(ratio) or 1
lots = sb.number_input(f"Lots ({lot} {T} / {lot * ratio:g} {A} each)", value=1, min_value=0, step=1)
borrow = sb.number_input(f"{A} borrow fee (%/yr)", value=0.32, step=0.05) / 100
sb.header("Completion probability")
pa = sb.slider("MRVL vote passes", 0.5, 1.0, 0.90, 0.01)
pt = sb.slider("LITE vote passes", 0.5, 1.0, 0.90, 0.01)
prg = sb.slider("Regulatory approvals in time", 0.3, 1.0, 0.605, 0.005,
                help="US 0.93 x EU 0.94 x China SAMR 0.69 = 0.605 (base); see the Antitrust tab")
po = sb.slider("No other termination", 0.8, 1.0, 0.97, 0.01)
p = pa * pt * prg * po
sb.metric("P(complete)", f"{p:.0%}")
sb.header("Break assumptions")
S_two = next(e for e in tgt_est if e.method.startswith("two-factor")).estimate
peer = {x: float(px[x].iloc[-1] / px.loc[:deal.unaffected_date, x].iloc[-1]) for x in ("COHR", "AAOI", "FN", "CIEN")}
S_stress = deal.unaffected_target * min(peer.values())
S_blend = float(np.sqrt(S_two * S_stress))
S_break = sb.number_input(f"{T} price if the deal breaks (before any fee)", value=round(S_two, 2), step=5.0,
                          help=f"factor model {S_two:.0f} (base); first version's blend {S_blend:.0f}; worst-peer "
                               f"stress {S_stress:.0f}")
M_cf = next(e for e in acq_est if e.method.startswith("beta to SMH")).estimate
delta = 1 - M0 / M_cf
kappa = sb.slider(f"Share of {A}'s deal discount that reverses on a break", 0.0, 1.0, 0.75, 0.05,
                  help=f"estimated deal discount {delta:.1%} (counterfactual {A} {M_cf:.0f}); it appeared on the "
                       "leak and announcement days, so most should reverse (base 75%)")
j = kappa * delta / (1 - delta)
hurdle = sb.slider("Hurdle: required excess return /yr after financing", 0.0, 0.20, 0.05, 0.01)

# ------------------------------------------------------------------------------------------------ core numbers
d2 = deal.with_overrides(exchange_ratio=ratio, cash_per_share=cash, expected_close=close)
V = d2.implied_value(M0)
gross = V / L0 - 1
years = max((close - px.index[-1].date()).days / 365.25, 1 / 365)
rf = rate_fn(years)
base_case = pr.BreakCase("dashboard", S_break, j)
stress_case = pr.BreakCase("stress", S_stress, delta / (1 - delta))
# the $3.2bn regulatory fee: received by LITE, paid by MRVL (after tax), on the share of breaks that are regulatory
reg_share = pa * pt * (1 - prg) / max(1 - p, 1e-12)
fee_L = d2.regulatory_fee_per_target_share() * 0.79
fee_M = d2.regulatory_fee_per_acquirer_share() * 0.79
entry_cost = ((BrokerTerms().commission(lot, L0) + BrokerTerms().commission(round(lot * ratio), M0)) / lot
              + (L0 + ratio * M0) * 10 / 1e4)
sig = sg.evaluate(d2, sg.SignalInputs(L0, M0, px.index[-1].date(), close, borrow, p, base_case, stress_case,
                                      risk_free=rf, hurdle_excess=hurdle, reg_break_share=reg_share,
                                      fee_target=fee_L, fee_acquirer=fee_M, entry_cost_unit=entry_cost,
                                      exit_cost_unit_break=entry_cost, break_years=0.98,
                                      data_fresh=all(qt.assess(qd[s]).label in ("real-time", "closing print") for s in (T, A))))
m = sig.metrics
pos = mg.units_to_position(int(lots), ratio) if lots else mg.Position(0, 0)

tabs = st.tabs(["Signal", "Spread", "Scenarios & P&L", "Probabilities", "Sizing & margin", "Monte Carlo",
                "Hedge ratios", "Antitrust"]
               + (["Execution (local)"] if os.environ.get("MERGERARB_ENABLE_EXECUTION") == "1" else []))

with tabs[0]:
    colour = {"TRADE": "green", "WATCH": "orange", "NO TRADE": "red"}[sig.decision]
    st.markdown(f"### Signal: :{colour}[{sig.decision}]")
    for r in sig.reasons:
        st.write("- " + r)
    k = st.columns(4)
    k[0].metric("Gross spread", f"{gross:.2%}", f"${V - L0:,.2f} per {T} share")
    k[1].metric("Annualised if it closes", f"{m['annualized_if_complete']:.1%}", f"{years:.2f} years")
    k[2].metric("Expected excess return /yr", f"{m['expected_excess_annual']:+.1%}", f"after financing at {rf:.2%}")
    k[3].metric("Break-even P(complete)", f"{m['breakeven_p_base']:.0%}", f"stress {m['breakeven_p_stress']:.0%}")
    k = st.columns(4)
    k[0].metric("Completion P&L / share", f"${m['completion_pnl_unit']:,.2f}")
    k[1].metric("Break P&L / share (average)", f"${m['break_pnl_unit_base']:,.2f}",
                f"vote ${m['break_pnl_unit_base_nofee']:,.0f} / regulatory ${m['break_pnl_unit_base_regulatory']:,.0f}")
    k[2].metric("Spread needed for hurdle", f"{m['required_spread_for_hurdle']:.1%}")
    k[3].metric("Max borrow fee for hurdle", f"{m['max_borrow_fee_for_hurdle']:.2%}")
    st.caption("The signal never sends orders. It recomputes whenever the spread, borrow fee, closing date or "
               "probabilities change. Research only, not investment advice.")

with tabs[1]:
    hist = sp.spread_history(px[T], px[A], ratio, cash, start="2026-10-29")
    stt = sp.stats(hist[hist.index >= pd.Timestamp(deal.announced)])
    f = go.Figure(go.Scatter(x=hist.index, y=hist.gross_spread, mode="lines+markers", line=dict(color=S1, width=2),
                             hovertemplate="%{x|%b %d}: %{y:.2%}<extra></extra>"))
    f.add_vline(x=pd.Timestamp(deal.announced), line_color="#c3c2b7")
    f.update_layout(title="Gross spread (daily closes; Oct 29-30 = speculation, terms unknown)", yaxis_tickformat=".0%",
                    height=360, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(f, use_container_width=True)
    cc = st.columns(5)
    cc[0].metric("Since announcement: mean", f"{stt.mean:.1%}")
    cc[1].metric("Std dev", f"{stt.std:.1%}")
    cc[2].metric("Percentile today", f"{stt.percentile:.0f}")
    cc[3].metric("z-score", f"{stt.zscore:.2f}")
    cc[4].metric("Daily change sd", f"{stt.daily_change_vol:.2%}")
    g = go.Figure(go.Histogram(x=hist.loc[hist.index >= pd.Timestamp(deal.announced), "gross_spread"], nbinsx=12,
                               marker_color=S1))
    g.update_layout(title="Distribution of daily spreads since announcement", xaxis_tickformat=".0%", height=280,
                    margin=dict(l=10, r=10, t=50, b=10), bargap=0.08)
    st.plotly_chart(g, use_container_width=True)
    st.dataframe(hist[["target", "acquirer", "implied", "spread_usd", "gross_spread"]].style.format(
        {"target": "{:.2f}", "acquirer": "{:.2f}", "implied": "{:.2f}", "spread_usd": "{:.2f}", "gross_spread": "{:.2%}"}))

with tabs[2]:
    st.subheader("Instant price moves: position P&L and gross spread")
    mv = st.columns(2)
    dm = mv[0].slider(f"{A} move", -0.6, 1.0, 0.0, 0.05)
    dl = mv[1].slider(f"{T} move", -0.6, 1.0, 0.0, 0.05)
    pnl = pos.target_shares * L0 * dl - pos.acquirer_short_shares * M0 * dm
    st_after = mg.account_state(equity + pnl, pos, L0 * (1 + dl), M0 * (1 + dm))
    q3 = st.columns(4)
    q3[0].metric("Position P&L", f"${pnl:,.0f}", f"{pnl / equity:+.1%} of equity")
    q3[1].metric("New gross spread", f"{sp.gross_spread(L0 * (1 + dl), M0 * (1 + dm), ratio, cash):.1%}")
    q3[2].metric("Excess liquidity", f"${st_after.excess_liquidity:,.0f}")
    q3[3].metric("Margin call?", "YES" if st_after.margin_call else "no")
    grid_m = np.round(np.linspace(-0.4, 0.8, 13), 2)
    grid_l = np.round(np.linspace(-0.4, 0.8, 13), 2)
    z = [[(pos.target_shares * L0 * gl - pos.acquirer_short_shares * M0 * gm) / equity for gm in grid_m] for gl in grid_l]
    h = go.Figure(go.Heatmap(z=z, x=[f"{x:+.0%}" for x in grid_m], y=[f"{y:+.0%}" for y in grid_l], zmid=0,
                             colorscale=[[0, "#e34948"], [0.5, "#f0efec"], [1, S1]], colorbar_tickformat=".0%",
                             hovertemplate=f"{A} %{{x}} / {T} %{{y}}: %{{z:.1%}} of equity<extra></extra>"))
    h.update_layout(title=f"P&L as % of equity ({pos.target_shares} {T} long / {pos.acquirer_short_shares} {A} short)",
                    xaxis_title=f"{A} move", yaxis_title=f"{T} move", height=440, margin=dict(l=10, r=10, t=50, b=10),
                    xaxis_type="category", yaxis_type="category")
    st.plotly_chart(h, use_container_width=True)
    if lots:
        specs = sc.default_scenarios(d2, px.index[-1].date(), L0, M0, gross, S_two, S_stress, j, delta / (1 - delta))
        res = [sc.run_scenario(s_, d2, pos, equity, L0, M0, px.index[-1].date(), borrow) for s_ in specs]
        tab = sc.results_table(res)
        st.subheader("Nine scenarios (+ break variants), path-simulated through margin")
        st.dataframe(tab[["name", "outcome", "end_date", "pnl_long", "pnl_short", "dividends_paid", "borrow_cost",
                          "net_pnl", "return_on_equity", "annualized_roe", "peak_maint_req", "min_excess_liquidity",
                          "max_drawdown", "liquidated", "note"]].style.format(
            {"pnl_long": "{:,.0f}", "pnl_short": "{:,.0f}", "dividends_paid": "{:,.2f}", "borrow_cost": "{:,.2f}",
             "net_pnl": "{:,.0f}", "return_on_equity": "{:+.1%}", "annualized_roe": "{:+.1%}",
             "peak_maint_req": "{:,.0f}", "min_excess_liquidity": "{:,.0f}", "max_drawdown": "{:.1%}"}),
            use_container_width=True)

with tabs[3]:
    C = m["completion_pnl_unit"]
    ps = np.round(np.arange(0.30, 0.96, 0.05), 2)
    cap = 0.5 * (L0 + ratio * M0)
    fig = go.Figure()
    for name, B, col in (("your break case", m["break_pnl_unit_base"], S1),
                         ("stress break", m["break_pnl_unit_stress"], S2)):
        ev = [(x * C + (1 - x) * B) / cap for x in ps]
        ann = [((1 + e) ** (1 / years) - 1) - rf if e > -1 else -1 for e in ev]
        fig.add_trace(go.Scatter(x=ps, y=ann, name=name, line=dict(color=col, width=2),
                                 hovertemplate="P(complete) %{x:.0%}: %{y:+.1%}/yr<extra></extra>"))
    fig.add_hline(y=0, line_color="#c3c2b7")
    fig.add_hline(y=hurdle, line_color="#898781", annotation_text="hurdle")
    fig.add_vline(x=p, line_color="#c3c2b7", annotation_text=f"your p {p:.0%}")
    fig.update_layout(title="Expected excess return per year vs completion probability", xaxis_tickformat=".0%",
                      yaxis_tickformat=".0%", height=380, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fig, use_container_width=True)
    Ss = np.round(np.linspace(380, 700, 17), 0)
    js = [0.0, 0.05, j, 0.2, delta / (1 - delta), 0.35]
    grid = pr.implied_probability_grid(L0, M0, ratio, C, Ss, sorted(set(np.round(js, 4))))
    hm = go.Figure(go.Heatmap(z=grid.values, x=[f"+{c:.0%}" for c in grid.columns], y=[f"${i:.0f}" for i in grid.index],
                              colorscale=[[i / 6, c] for i, c in enumerate(BLUES)], zmin=0, zmax=1,
                              colorbar_tickformat=".0%", hovertemplate="break-even p %{z:.0%}<extra></extra>"))
    hm.update_layout(title="Break-even completion probability by break assumption", xaxis_title=f"{A} jump on a break",
                     yaxis_title=f"{T} price on a break", height=440, margin=dict(l=10, r=10, t=50, b=10),
                     xaxis_type="category", yaxis_type="category")
    st.plotly_chart(hm, use_container_width=True)
    st.dataframe(pr.DEFAULT_BOTTOM_UP.table(), use_container_width=True)

with tabs[4]:
    units, binding, tab = mg.max_units(equity, L0, M0, ratio)
    st.metric("Largest size passing the survival policy", f"{units} lots", f"binding: {binding}")
    st.dataframe(tab.head(10), use_container_width=True)
    if lots:
        lm = mg.liquidation_moves(equity, pos, L0, M0)
        st.write({k: (f"{v:+.0%}" if isinstance(v, float) else v) for k, v in lm.items()})
    rows = []
    for lab, ps_ in ((f"{int(lots)} lots (yours)", pos), ("max Reg T", mg.units_to_position(max(int(equity / mg.MarginRules().regt(lot * L0, lot * ratio * M0)), 1), ratio))):
        for mvv in np.linspace(0, 1.5, 31):
            Mx, Lx = M0 * (1 + mvv), ratio * M0 * (1 + mvv) / 1.35 if mvv > 0 else L0
            eq = equity + ps_.target_shares * (Lx - L0) - ps_.acquirer_short_shares * (Mx - M0)
            rows.append({"label": lab, "move": mvv, "exl": mg.account_state(eq, ps_, Lx, Mx).excess_liquidity / equity})
    dfm = pd.DataFrame(rows)
    fm = go.Figure()
    for (lab, d_), col in zip(dfm.groupby("label", sort=False), (S1, S2)):
        fm.add_trace(go.Scatter(x=d_.move, y=d_.exl, name=lab, line=dict(color=col, width=2)))
    fm.add_hline(y=0, line_color="#0b0b0b", annotation_text="margin call")
    fm.update_layout(title=f"Excess liquidity if {A} rallies and the spread widens to 35%", xaxis_tickformat=".0%",
                     yaxis_tickformat=".0%", height=360, margin=dict(l=10, r=10, t=50, b=10))
    st.plotly_chart(fm, use_container_width=True)

with tabs[5]:
    n = st.select_slider("Paths", options=[2000, 5000, 10000, 20000], value=5000)
    if st.button("Run Monte Carlo") and lots:
        sig_band = next(e for e in tgt_est if e.method.startswith("two-factor"))
        cfg = mc.MCConfig(n_paths=int(n), standalone0=S_break, deal_discount=delta, rebound_kappa=kappa,
                          standalone0_sigma=float(np.log(sig_band.estimate / sig_band.low)),
                          kappa_low=max(0.0, kappa - 0.25), kappa_high=min(1.0, kappa + 0.25),
                          p_acq_vote_fail=1 - pa, p_tgt_vote_fail=1 - pt, p_reg_fail=1 - prg, p_other_fail=1 - po,
                          completion_mode=close, completion_max=deal.final_outside_date, borrow_fee=borrow,
                          fee_per_share=deal.regulatory_fee_per_target_share(),
                          acq_fee_per_share=deal.regulatory_fee_per_acquirer_share(), financing_rate=rf)
        r = mc.simulate(d2, pos, equity, L0, M0, px.index[-1].date(), cfg)
        st.write({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.summary.items()})
        st.dataframe(r.by_outcome, use_container_width=True)
        x = np.sort(r.pnl) / equity
        fc = go.Figure(go.Scatter(x=x, y=np.arange(1, len(x) + 1) / len(x), line=dict(color=S1, width=2),
                                  hovertemplate="P&L <= %{x:.1%}: %{y:.0%} of paths<extra></extra>"))
        fc.update_layout(title="Cumulative distribution of account P&L at resolution", xaxis_tickformat=".0%",
                         yaxis_tickformat=".0%", height=360, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fc, use_container_width=True)
        st.session_state.mc_unit = r.unit_terminal
    elif not lots:
        st.info("Set at least one lot.")

with tabs[6]:
    if "mc_unit" in st.session_state:
        st.dataframe(mc.hedge_comparison(st.session_state.mc_unit, L0, M0, ratio), use_container_width=True)
        st.caption("Same simulated paths for every hedge. The contract ratio is the only hedge that locks the "
                   "completion payoff; dollar-neutral leaves (ratio - L/M) acquirer shares of net exposure.")
    else:
        st.info("Run the Monte Carlo tab first.")

with tabs[7]:
    from mergerarb.analytics import antitrust as at

    st.subheader("How regulatory news moves LITE, MRVL and the spread")
    st.caption("Two-state pricing: each stock is a mix of its deal / no-deal value weighted by the market's completion "
               "odds q. A headline changes q (Bayesian update from the gate tree); the hedged trade's P&L is dq x G with "
               "G = spread$ / (1 - q). See docs/ANTITRUST_AND_TRADING.md.")
    a1, a2, a3 = st.columns(3)
    g_us_sue = a1.slider("US: P(suit | second request)", 0.0, 0.5, 0.15, 0.01)
    g_court = a1.slider("US: P(parties win in court)", 0.0, 1.0, 0.40, 0.05)
    g_votes = a2.slider("Both votes pass", 0.5, 1.0, 0.81, 0.01)
    g_eu2 = a2.slider("EU: P(phase 2)", 0.0, 1.0, 0.40, 0.05)
    g_samr = a3.slider("China SAMR approves (if accepted on time)", 0.3, 1.0, 0.70, 0.01)
    g_other = a3.slider("Other termination", 0.0, 0.2, 0.03, 0.01)
    tree = at.RegTree(p_us_sue_after_sr=g_us_sue, p_us_clear_after_sr=max(0.0, 0.85 - g_us_sue + 0.15 - 0.35),
                      p_court_win=g_court, p_votes_pass=g_votes, p_eu_phase1=1 - g_eu2, p_samr_approve=g_samr,
                      p_other_break=g_other)
    st.dataframe(tree.gates().style.format({"p_success": "{:.1%}"}), use_container_width=True)
    b1, b2 = st.columns(2)
    mq = b1.slider("Market's completion odds now (q)", 0.2, 0.9, 0.50, 0.01,
                   help="Bloomberg reported arb desks near 50% (snippet)")
    kap_a = b2.slider("Share of MRVL's deal discount that reverses on a break", 0.0, 1.0, 0.75, 0.05, key="kap_at")
    jj = kap_a * delta / (1 - delta)
    ni = at.news_impact(tree, L0, M0, ratio, jj, market_q=mq)
    st.dataframe(ni[["headline", "p_event", "q_before", "market_q_after", "tgt_move", "acq_move", "spread_pts",
                     "gross_spread_after", "arb_pnl_unit", "arb_pnl_unit_day"]].style.format(
        {"p_event": "{:.0%}", "q_before": "{:.1%}", "market_q_after": "{:.1%}", "tgt_move": "{:+.1%}",
         "acq_move": "{:+.1%}", "spread_pts": "{:+.1%}", "gross_spread_after": "{:.1%}", "arb_pnl_unit": "${:+,.0f}",
         "arb_pnl_unit_day": "${:+,.0f}"}, na_rep=""), use_container_width=True)
    st.caption("arb_pnl_unit = change from today once the headline is known; arb_pnl_unit_day = the move on the day "
               "(a court ruling starts from the post-lawsuit odds).")
    st0 = at.from_q_and_rebound(L0, M0, ratio, mq, jj)
    st.write(f"Gap G = ${st0.gap:,.0f} per {T} share: every 10-point drop in q costs the hedged trade "
             f"${0.1 * st0.gap:,.0f} per {T} share. Implied break values: {T} ${st0.S:,.0f}, {A} ${st0.M_B:,.0f}; "
             f"{A} if the deal closes ${st0.M_C:,.0f}.")
    if st.button("Simulate entry rules on news flow (5,000 paths)"):
        mk = at.RegTree()
        rr = at.news_mc(tree, mk, L0, M0, ratio, px.index[-1].date(), max(pos.target_shares, 2),
                        at.NewsMCConfig(n_paths=5000, acq_break_move=jj, you_standalone0=S_break),
                        at.default_strategies(threshold=0.27))
        st.write(f"Your P(complete) {rr['p_complete_you']:.1%} vs market tree {rr['p_complete_market']:.1%}")
        st.dataframe(rr["table"].style.format({"p_trade": "{:.0%}", "mean_pnl_if_traded": "${:,.0f}",
                                               "mean_pnl_all_paths": "${:,.0f}", "p_loss_if_traded": "{:.0%}",
                                               "var95_if_traded": "${:,.0f}", "es95_if_traded": "${:,.0f}",
                                               "mean_entry_spread": "{:.1%}", "mean_years_held": "{:.2f}",
                                               "pnl_per_capital_year": "{:+.1%}"}), use_container_width=True)

if len(tabs) > 8:
    with tabs[8]:
        import requests

        st.warning("Local only. Orders go to TWS / IB Gateway through the local service, after pre-flight checks and "
                   "a typed confirmation. Paper mode unless the service was started with live mode enabled.")
        tok_file = pathlib.Path.home() / ".mergerarb" / "service_token"
        base = f"http://127.0.0.1:{int(os.environ.get('MERGERARB_SERVICE_PORT', '8813'))}"
        token = tok_file.read_text().strip() if tok_file.exists() else None
        hdr = {"Authorization": f"Bearer {token}"} if token else {}

        def call(method: str, path: str, body: dict | None = None):
            r = requests.request(method, base + path, json=body, headers=hdr, timeout=30)
            return r.status_code, r.json()

        tl = st.number_input(f"{T} buy limit", value=round(L0, 2), step=0.05)
        al = st.number_input(f"{A} sell (short) limit", value=round(M0, 2), step=0.01)
        plan = build_pair_plan(d2, max(pos.target_shares, lot), tl, al, {T: L0, A: M0})
        st.write(f"Plan {plan.short_hash}: SELL {plan.acquirer.quantity} {A} @ {al} (short) / BUY {plan.target.quantity} "
                 f"{T} @ {tl}; hedge residual {plan.hedge_residual():+.2f}")
        st.dataframe(pd.DataFrame([dict(name=n_, status=s_, message=msg) for n_, s_, msg in
                                   validate_plan(plan, "paper", 15_000, 0.02)]), use_container_width=True)
        e1, e2 = st.columns(2)
        e1.download_button("Export JSON", plan.to_json(), file_name=f"plan_{plan.short_hash}.json")
        e2.download_button("Export CSV", plan.to_csv(), file_name=f"plan_{plan.short_hash}.csv")
        if not token:
            st.info("Execution service not running. Start it with: python -m mergerarb.execution.service (or --sim).")
        else:
            try:
                code, s_ = call("GET", "/status")
                st.write(s_)
                if st.button("Connect to TWS / IB Gateway"):
                    st.write(call("POST", "/connect"))
                st.markdown("**Spread-trigger ladder** (alerts only; fires on live executable quotes: sell MRVL at bid, "
                            "buy LITE at ask)")
                from mergerarb.execution.trigger import default_ladder
                lad = default_ladder(equity)
                t1, t2 = st.columns(2)
                r_spread = t1.number_input("Rung: gross spread", value=float(lad[0].target_gross_spread), step=0.01,
                                           format="%.2f")
                r_lots = t2.number_input("Rung: lots", value=int(lad[0].lots), min_value=1, step=1)
                if st.button("Arm trigger"):
                    st.write(call("POST", "/triggers", {"rungs": [{"target_gross_spread": r_spread, "lots": r_lots}]}))
                if st.button("Check triggers"):
                    st.write(call("GET", "/triggers"))
                if st.button("Run pre-flight"):
                    code, rep = call("POST", "/preflight", {"plan": plan.to_dict(), "borrow_fee": borrow,
                                                            "signal_decision": sig.decision})
                    st.session_state.preflight = (plan.plan_hash(), rep)
                if "preflight" in st.session_state and st.session_state.preflight[0] == plan.plan_hash():
                    rep = st.session_state.preflight[1]
                    if "checks" in rep:
                        st.dataframe(pd.DataFrame(rep["checks"]), use_container_width=True)
                        st.write(f"To approve, type exactly: `{rep['approval_phrase']}`")
                        typed = st.text_input("Confirmation phrase")
                        reason = st.text_input("Override reason (required only if the model signal is not TRADE)")
                        if st.button("Approve and transmit", type="primary"):
                            st.write(call("POST", "/approve_submit", {"plan": plan.to_dict(), "typed": typed,
                                                                      "override_reason": reason or None}))
                    else:
                        st.error(rep)
                b1, b2, b3 = st.columns(3)
                if b1.button("Refresh order status"):
                    st.write(call("POST", "/pump"))
                if b2.button("Cancel all strategy orders"):
                    st.write(call("POST", "/cancel", {"reason": "dashboard"}))
                if b3.button("Recover after reconnect"):
                    st.write(call("POST", "/recover"))
                st.dataframe(pd.DataFrame(call("GET", "/journal")[1].get("events", [])).tail(50), use_container_width=True)
            except requests.RequestException as e:
                st.error(f"service unreachable: {e}")
