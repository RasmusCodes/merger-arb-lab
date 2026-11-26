"""How antitrust / regulatory news moves the target, the acquirer and the spread - and how to trade around it.

1. TWO-STATE PRICING. Each stock is a probability-weighted mix of its value if the deal completes (C) and if it
   breaks (B), with q the market's (risk-neutral) completion probability:
        M = q*M_C + (1-q)*M_B            L = q*R*M_C + (1-q)*S
   Define the break gap G = R*M_B - S (how much the R acquirer shares would be worth more than the target if the deal
   broke). Then, exactly:   spread$ = R*M - L = (1-q)*G,   completion P&L = (1-q)*G,   break P&L = -q*G.
   A regulatory shock is a change dq in q with state values fixed:
        dM = dq*(M_C - M_B)      dL = dq*(R*M_C - S)      d(spread$) = -dq*G      arb P&L per unit = dq*G
   So the ARB's exposure to deal odds is pinned by the spread and q alone (G = spread$/(1-q)); how the hit splits
   between "target falls" and "acquirer rallies" depends on M_B, i.e. how much of the acquirer's announcement drop
   is deal-specific and would reverse on a break.
2. REGULATORY EVENT TREE. Sequential gates (US HSR/second request/litigation, shareholder votes, EU, China SAMR,
   other) with branch probabilities and dates. Completion probability is exact; every possible headline maps to a
   Bayesian update of q - which is precisely how much "antitrust worries" move the stocks.
3. NEWS-FLOW MONTE CARLO. Paths draw outcomes and dates from YOUR tree while prices follow the MARKET's tree (they
   may disagree gate by gate - the only possible source of edge); state values diffuse; entry/exit rules are tested.
All probabilities are labelled judgments with ranges, never facts.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import numpy as np
import pandas as pd


# ------------------------------------------------------------------------------------------------------------------
# 1. Two-state pricing
@dataclasses.dataclass(frozen=True)
class TwoState:
    L: float
    M: float
    R: float
    q: float          # market completion probability
    S: float          # target break value
    M_B: float        # acquirer break value
    M_C: float        # acquirer completion value

    @property
    def gap(self) -> float:
        return self.R * self.M_B - self.S

    @property
    def spread_usd(self) -> float:
        return self.R * self.M - self.L

    def shock(self, dq: float) -> dict:
        q2 = min(max(self.q + dq, 0.0), 1.0)
        d = q2 - self.q
        M2 = self.M + d * (self.M_C - self.M_B)
        L2 = self.L + d * (self.R * self.M_C - self.S)
        return {"dq": d, "q": q2, "M": M2, "L": L2, "acq_move": M2 / self.M - 1, "tgt_move": L2 / self.L - 1,
                "gross_spread": self.R * M2 / L2 - 1, "spread_pts": (self.R * M2 / L2) - (self.R * self.M / self.L),
                "arb_pnl_unit": d * self.gap}


def from_q_and_rebound(L: float, M: float, R: float, q: float, acq_break_move: float) -> TwoState:
    """Identify the state given a market completion probability q and the acquirer's break-state move
    (M_B = M*(1+acq_break_move)). Then G = spread$/(1-q) and S = R*M_B - G, M_C = (M - (1-q)*M_B)/q."""
    M_B = M * (1 + acq_break_move)
    G = (R * M - L) / (1 - q)
    S = R * M_B - G
    M_C = (M - (1 - q) * M_B) / q
    return TwoState(L, M, R, q, S, M_B, M_C)


def from_break_values(L: float, M: float, R: float, S: float, M_B: float) -> TwoState | None:
    """Identify q and M_C from assumed break values; None if inconsistent (implied q outside (0,1))."""
    G = R * M_B - S
    if G <= 0:
        return None
    q = 1 - (R * M - L) / G
    if not 0 < q < 1:
        return None
    return TwoState(L, M, R, q, S, M_B, (M - (1 - q) * M_B) / q)


def shock_table(L: float, M: float, R: float, qs=(0.35, 0.5, 0.65), rebounds=(0.0, 0.135, 0.27),
                dqs=(-0.30, -0.20, -0.10, -0.05, 0.05, 0.10, 0.20)) -> pd.DataFrame:
    rows = []
    for q in qs:
        for j in rebounds:
            st = from_q_and_rebound(L, M, R, q, j)
            for dq in dqs:
                r = st.shock(dq)
                rows.append({"market_q": q, "acq_break_move": j, "implied_S": st.S, "implied_M_C": st.M_C,
                             "gap_G": st.gap, **{k: r[k] for k in ("dq", "tgt_move", "acq_move", "spread_pts",
                                                                  "gross_spread", "arb_pnl_unit")}})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------------------------
# 2. Regulatory event tree
@dataclasses.dataclass(frozen=True)
class RegTree:
    """Judgmental branch probabilities (YOUR view or the MARKET's). Dates are central estimates."""
    p_hsr_clear_initial: float = 0.25        # HSR waiting period expires without a second request
    p_us_clear_after_sr: float = 0.50        # after a second request: clear without remedies
    p_us_consent_after_sr: float = 0.35      # ... clear with a consent decree (behavioural / divestiture)
    p_us_sue_after_sr: float = 0.15          # ... agency sues to block (remainder of the three)
    p_court_win: float = 0.40                # merging parties win in court (else the deal dies)
    p_votes_pass: float = 0.81               # both stockholder votes pass (MRVL votes cast, LITE outstanding)
    p_eu_phase1: float = 0.60                # EU clears in phase 1 (possibly with commitments)
    p_eu_phase2_clear: float = 0.85          # EU clears after phase 2 (else prohibition / abandonment)
    p_samr_accept: float = 0.95              # SAMR formally accepts the filing on time
    p_samr_approve: float = 0.70             # SAMR approves (mostly conditional) before the final outside date
    p_samr_approve_if_late_accept: float = 0.50
    p_other_break: float = 0.03              # MAE, superior proposal, litigation, renegotiation failure
    # dates
    d_hsr: dt.date = dt.date(2027, 1, 20)
    d_samr_accept: dt.date = dt.date(2027, 3, 15)
    d_eu_phase1: dt.date = dt.date(2027, 5, 15)
    d_votes: dt.date = dt.date(2027, 6, 30)
    d_us_after_sr: dt.date = dt.date(2027, 8, 15)
    d_eu_phase2: dt.date = dt.date(2027, 10, 31)
    d_court: dt.date = dt.date(2028, 1, 31)
    samr_window: tuple[dt.date, dt.date] = (dt.date(2027, 10, 1), dt.date(2028, 7, 31))

    # ----- exact gate success probabilities
    def p_us(self) -> float:
        return self.p_hsr_clear_initial + (1 - self.p_hsr_clear_initial) * (
            self.p_us_clear_after_sr + self.p_us_consent_after_sr + self.p_us_sue_after_sr * self.p_court_win)

    def p_eu(self) -> float:
        return self.p_eu_phase1 + (1 - self.p_eu_phase1) * self.p_eu_phase2_clear

    def p_samr(self) -> float:
        return self.p_samr_accept * self.p_samr_approve + (1 - self.p_samr_accept) * self.p_samr_approve_if_late_accept

    def p_complete(self) -> float:
        return self.p_us() * self.p_votes_pass * self.p_eu() * self.p_samr() * (1 - self.p_other_break)

    def gates(self) -> pd.DataFrame:
        return pd.DataFrame([
            {"gate": "US (HSR, second request, consent or litigation)", "p_success": self.p_us()},
            {"gate": "Stockholder votes (both)", "p_success": self.p_votes_pass},
            {"gate": "EU", "p_success": self.p_eu()},
            {"gate": "China SAMR", "p_success": self.p_samr()},
            {"gate": "No other termination", "p_success": 1 - self.p_other_break},
            {"gate": "P(complete)", "p_success": self.p_complete()},
        ])

    # ----- news impact: Bayesian update of completion probability for each possible headline
    def news(self) -> pd.DataFrame:
        """q_before = today's P(complete); q_after = P(complete) once the headline is known, so q_after - q_before is
        the CUMULATIVE change from today. q_before_event is the probability just before the headline, given the news
        that must precede it (a court ruling follows a lawsuit; a SAMR decision follows on-time acceptance), so
        q_after - q_before_event is the move ON THE DAY. (Inside SAMR's decision window the probability also decays
        week by week while no decision arrives; the news simulation models that, this table does not.)"""
        P = self.p_complete()
        us, eu, samr = self.p_us(), self.p_eu(), self.p_samr()
        rest_us, rest_eu, rest_samr = P / us, P / eu, P / samr
        sr_cont = self.p_us_clear_after_sr + self.p_us_consent_after_sr + self.p_us_sue_after_sr * self.p_court_win
        after_sr, after_suit = rest_us * sr_cont, rest_us * self.p_court_win
        after_accept = rest_samr * self.p_samr_approve
        ev = [
            ("HSR expires, no second request", "US", self.p_hsr_clear_initial, rest_us * 1.0, P),
            ("HSR second request issued", "US", 1 - self.p_hsr_clear_initial, after_sr, P),
            ("US clears after second request (with or without consent decree)", "US",
             (1 - self.p_hsr_clear_initial) * (self.p_us_clear_after_sr + self.p_us_consent_after_sr), rest_us,
             after_sr),
            ("US agency sues to block", "US", (1 - self.p_hsr_clear_initial) * self.p_us_sue_after_sr, after_suit,
             after_sr),
            ("Court rules for the merger", "US", None, rest_us, after_suit),
            ("Court blocks the merger", "US", None, 0.0, after_suit),
            ("EU clears in phase 1", "EU", self.p_eu_phase1, rest_eu, P),
            ("EU opens phase 2", "EU", 1 - self.p_eu_phase1, rest_eu * self.p_eu_phase2_clear, P),
            ("SAMR accepts filing on time", "China", self.p_samr_accept, after_accept, P),
            ("SAMR acceptance delayed / pull-and-refile", "China", 1 - self.p_samr_accept,
             rest_samr * self.p_samr_approve_if_late_accept, P),
            ("SAMR approves (conditional)", "China", None, rest_samr, after_accept),
            ("SAMR no decision by outside date -> termination", "China", None, 0.0, after_accept),
            ("Both stockholder votes pass", "Votes", self.p_votes_pass, P / self.p_votes_pass, P),
            ("A stockholder vote fails", "Votes", 1 - self.p_votes_pass, 0.0, P),
        ]
        return pd.DataFrame([{"headline": h, "jurisdiction": j, "p_event": pe, "q_before": P, "q_after": qa,
                              "dq": qa - P, "q_before_event": qb, "dq_day": qa - qb} for h, j, pe, qa, qb in ev])


def news_impact(tree: RegTree, L: float, M: float, R: float, acq_break_move: float,
                market_q: float | None = None) -> pd.DataFrame:
    """Price impact of every headline: the tree gives dq (scaled to the market's q level if given), the two-state
    model turns dq into target/acquirer moves, spread change and arb P&L per unit."""
    nw = tree.news()
    q0 = market_q if market_q is not None else tree.p_complete()
    st = from_q_and_rebound(L, M, R, q0, acq_break_move)
    rows = []
    for _, r in nw.iterrows():
        rel = r.q_after / r.q_before if r.q_before > 0 else 0.0   # relative update applied at the market's q level
        dq = min(q0 * rel, 1.0) - q0
        s = st.shock(dq)
        # on the day: start from the state just before the headline (same state values, market q scaled the same way)
        qb = min(q0 * (r.q_before_event / r.q_before), 1.0) if r.q_before > 0 else q0
        stb = TwoState(st.L + (qb - q0) * (R * st.M_C - st.S), st.M + (qb - q0) * (st.M_C - st.M_B), R, qb, st.S,
                       st.M_B, st.M_C)
        sd = stb.shock(min(q0 * rel, 1.0) - qb)
        rows.append({**r.to_dict(), "market_q_after": q0 + s["dq"], "tgt_move": s["tgt_move"], "acq_move": s["acq_move"],
                     "spread_pts": s["spread_pts"], "gross_spread_after": s["gross_spread"],
                     "arb_pnl_unit": s["arb_pnl_unit"], "market_q_before_event": qb, "tgt_move_day": sd["tgt_move"],
                     "acq_move_day": sd["acq_move"], "spread_pts_day": sd["spread_pts"],
                     "arb_pnl_unit_day": sd["arb_pnl_unit"]})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------------------------
# 3. News-flow Monte Carlo with entry / exit rules
@dataclasses.dataclass(frozen=True)
class NewsMCConfig:
    n_paths: int = 20000
    seed: int = 1125
    weeks: int = 92                       # to 2028-08 from 2026-11-24
    acq_vol: float = 0.71
    standalone_vol: float = 0.85
    rho: float = 0.55
    acq_break_move: float = 0.2024        # M_B / M - 1 today (3/4 of MRVL's 21% deal discount reverses)
    regulatory_fee_ps: float = 26.2       # after tax, per target share, received on a regulatory failure
    acq_fee_ps: float = 2.81              # after tax, per acquirer share, paid on a regulatory failure
    borrow_fee: float = 0.0032
    div_per_quarter_unit: float = 0.27    # R x MRVL dividend
    # YOUR break values, if they differ from the market's (the market's are backed out of today's prices): on a break
    # the target goes to your standalone path (+ fee) and the acquirer to your rebound. None = agree with the market.
    you_standalone0: float | None = None
    you_acq_break_move: float | None = None


def _simulate_tree(tree: RegTree, rng: np.random.Generator, n: int, start: dt.date):
    """Draw gate outcomes and dates. Returns dict of arrays: break_week (or -1), break_kind, complete_week, and the
    list of (week, event_code) news per path encoded as arrays of event times for each gate outcome."""
    wk = lambda d: max(1, int(round((d - start).days / 7)))  # noqa: E731
    W_hsr, W_sa, W_eu1, W_v, W_us2, W_eu2, W_ct = (wk(tree.d_hsr), wk(tree.d_samr_accept), wk(tree.d_eu_phase1),
                                                   wk(tree.d_votes), wk(tree.d_us_after_sr), wk(tree.d_eu_phase2),
                                                   wk(tree.d_court))
    s0, s1 = wk(tree.samr_window[0]), wk(tree.samr_window[1])
    u = rng.random((n, 9))
    hsr_sr = u[:, 0] >= tree.p_hsr_clear_initial
    us_r = u[:, 1]
    us_sue = hsr_sr & (us_r >= tree.p_us_clear_after_sr + tree.p_us_consent_after_sr)
    court_lose = us_sue & (u[:, 2] >= tree.p_court_win)
    votes_fail = u[:, 3] >= tree.p_votes_pass
    eu_p2 = u[:, 4] >= tree.p_eu_phase1
    eu_block = eu_p2 & (u[:, 5] >= tree.p_eu_phase2_clear)
    samr_late = u[:, 6] >= tree.p_samr_accept
    p_ap = np.where(samr_late, tree.p_samr_approve_if_late_accept, tree.p_samr_approve)
    samr_fail = u[:, 7] >= p_ap
    samr_week = rng.integers(s0, s1 + 1, n)
    samr_week = np.where(samr_fail, s1 + 1, samr_week)             # no decision by the outside date
    other = u[:, 8] < tree.p_other_break
    other_week = rng.integers(1, s1, n)
    # earliest break among failing gates
    INF = 10 ** 6
    cand = np.stack([np.where(court_lose, W_ct, INF), np.where(votes_fail, W_v, INF), np.where(eu_block, W_eu2, INF),
                     np.where(samr_fail, s1 + 1, INF), np.where(other, other_week, INF)], axis=1)
    bw = cand.min(axis=1)
    kind = cand.argmin(axis=1)          # 0 court, 1 votes, 2 EU, 3 SAMR (regulatory fee), 4 other
    broke = bw < INF
    us_done = np.where(~hsr_sr, W_hsr, np.where(us_sue, W_ct, W_us2))
    eu_done = np.where(eu_p2, W_eu2, W_eu1)
    complete_week = np.maximum.reduce([us_done, eu_done, np.full(n, W_v), samr_week]) + 1
    return {"hsr_sr": hsr_sr, "us_sue": us_sue, "court_lose": court_lose, "votes_fail": votes_fail, "eu_p2": eu_p2,
            "eu_block": eu_block, "samr_late": samr_late, "samr_fail": samr_fail, "samr_week": samr_week,
            "broke": broke, "break_week": np.where(broke, bw, -1), "break_kind": np.where(broke, kind, -1),
            "complete_week": np.where(broke, -1, complete_week),
            "W": dict(hsr=W_hsr, sa=W_sa, eu1=W_eu1, v=W_v, us2=W_us2, eu2=W_eu2, ct=W_ct, s0=s0, s1=s1)}


def _fee_share(tree: RegTree, start: dt.date, seed: int, n: int = 200_000) -> tuple[float, float]:
    """P(fee-bearing regulatory break | break) under ``tree``: unconditionally, and given that the votes passed."""
    s = _simulate_tree(tree, np.random.default_rng(seed), n, start)
    reg = np.isin(s["break_kind"], (0, 2, 3))
    b = s["broke"]
    bp = b & ~s["votes_fail"]
    w_pre = float(reg[b].mean()) if b.any() else 0.0
    w_post = float(reg[bp].mean()) if bp.any() else 0.0
    return w_pre, w_post


def _q_path(tree: RegTree, sim: dict, n: int, weeks: int) -> np.ndarray:
    """Exact conditional completion probability under ``tree`` given the news revealed by each week."""
    W = sim["W"]
    us0, eu0, sa0 = tree.p_us(), tree.p_eu(), tree.p_samr()
    v0, o0 = tree.p_votes_pass, 1 - tree.p_other_break
    sr_cont = tree.p_us_clear_after_sr + tree.p_us_consent_after_sr + tree.p_us_sue_after_sr * tree.p_court_win
    t = np.arange(weeks + 1)[None, :]
    # US gate success probability given information at t
    us = np.full((n, weeks + 1), us0)
    sr = sim["hsr_sr"][:, None]
    us = np.where(t >= W["hsr"], np.where(sr, sr_cont, 1.0), us)
    sue = sim["us_sue"][:, None]
    us = np.where((t >= W["us2"]) & sr, np.where(sue, tree.p_court_win, 1.0), us)
    us = np.where((t >= W["ct"]) & sue, np.where(sim["court_lose"][:, None], 0.0, 1.0), us)
    eu = np.full((n, weeks + 1), eu0)
    p2 = sim["eu_p2"][:, None]
    eu = np.where(t >= W["eu1"], np.where(p2, tree.p_eu_phase2_clear, 1.0), eu)
    eu = np.where((t >= W["eu2"]) & p2, np.where(sim["eu_block"][:, None], 0.0, 1.0), eu)
    sa = np.full((n, weeks + 1), sa0)
    late = sim["samr_late"][:, None]
    p_ap = np.where(late, tree.p_samr_approve_if_late_accept, tree.p_samr_approve)
    # approvals arrive uniformly over the decision window [s0, s1]; a failure only shows after s1. So while no decision
    # has arrived, the chance of approval decays: P(approve | none by t) = p*f / (p*f + 1 - p), f = P(week > t | approve)
    f = np.clip((W["s1"] - t) / max(W["s1"] - W["s0"] + 1, 1), 0.0, 1.0)
    haz = p_ap * f / np.maximum(p_ap * f + (1 - p_ap), 1e-12)
    sa = np.where(t >= W["sa"], haz, sa)
    sw = sim["samr_week"][:, None]
    sa = np.where(t >= sw, np.where(sim["samr_fail"][:, None], 0.0, 1.0), sa)
    vt = np.where(t >= W["v"], np.where(sim["votes_fail"][:, None], 0.0, 1.0), v0)
    # other-break hazard decays linearly to the end of the window
    frac_left = np.clip(1 - t / max(W["s1"], 1), 0, 1)
    ot = 1 - tree.p_other_break * frac_left
    q = us * eu * sa * vt * ot
    bw = sim["break_week"][:, None]
    q = np.where((bw >= 0) & (t >= bw), 0.0, q)
    cw = sim["complete_week"][:, None]
    q = np.where((cw >= 0) & (t >= cw), 1.0, q)
    return q


def news_mc(you: RegTree, market: RegTree, L0: float, M0: float, R: float, start: dt.date, units_target: int,
            cfg: NewsMCConfig = NewsMCConfig(), strategies: dict | None = None) -> dict:
    """Outcomes follow YOUR tree; prices follow the MARKET's conditional probability (two-state mixture with
    diffusing break values). Strategies decide when to enter/exit; P&L is per position of ``units_target`` target
    shares (hedged at the contract ratio)."""
    rng = np.random.default_rng(cfg.seed)
    n, Wk = cfg.n_paths, cfg.weeks
    sim = _simulate_tree(you, rng, n, start)
    qm = _q_path(market, sim, n, Wk)
    qm0 = market.p_complete()
    st0 = from_q_and_rebound(L0, M0, R, qm0, cfg.acq_break_move)
    dt_ = 1 / 52
    z1 = rng.standard_normal((n, Wk))
    z2 = cfg.rho * z1 + math.sqrt(1 - cfg.rho ** 2) * rng.standard_normal((n, Wk))
    gM = np.concatenate([np.zeros((n, 1)), np.cumsum(-0.5 * cfg.acq_vol ** 2 * dt_ + cfg.acq_vol * math.sqrt(dt_) * z1, 1)], 1)
    gS = np.concatenate([np.zeros((n, 1)), np.cumsum(-0.5 * cfg.standalone_vol ** 2 * dt_ + cfg.standalone_vol * math.sqrt(dt_) * z2, 1)], 1)
    # Marvell owes the USD 3.2bn regulatory fee on a regulatory failure (court block, EU block, SAMR timeout); not on a
    # failed vote or an "other" termination (merger agreement s8.2(d)). The break values backed out of today's prices
    # (st0.S, st0.M_B) are the market's AVERAGE over break types, so they already contain the expected fee: split it
    # out (w = the market's P(fee-bearing break | break), before and after the votes) and add the actual fee only when
    # a regulatory break happens. Without this the fee was counted twice and a no-edge world showed a profit.
    w_pre, w_post = _fee_share(market, start, cfg.seed + 7)
    t_w = np.arange(Wk + 1)[None, :]
    votes_in = (t_w >= sim["W"]["v"]) & ~sim["votes_fail"][:, None]
    w_t = np.where(votes_in, w_post, w_pre)
    after = sim["broke"][:, None] & (t_w >= np.maximum(sim["break_week"], 0)[:, None])
    is_reg = np.isin(sim["break_kind"], (0, 2, 3))[:, None]
    fee_L = np.where(after, cfg.regulatory_fee_ps * is_reg, w_t * cfg.regulatory_fee_ps)
    fee_M = np.where(after, cfg.acq_fee_ps * is_reg, w_t * cfg.acq_fee_ps)
    MB = (st0.M_B + w_pre * cfg.acq_fee_ps) * np.exp(gM)     # ex-fee break value of the acquirer
    MC = st0.M_C * np.exp(gM)            # completion- and break-state acquirer values share the common shocks
    S = (st0.S - w_pre * cfg.regulatory_fee_ps) * np.exp(gS)  # ex-fee break value of the target
    M = qm * MC + (1 - qm) * (MB - fee_M)
    L = qm * R * MC + (1 - qm) * (S + fee_L)
    if cfg.you_standalone0 is not None:          # after a break, prices are YOUR break values (if you are right)
        L = np.where(after, cfg.you_standalone0 * np.exp(gS) + fee_L, L)
    if cfg.you_acq_break_move is not None:
        M = np.where(after, M0 * (1 + cfg.you_acq_break_move) * np.exp(gM) - fee_M, M)
    gross = R * M / L - 1
    strategies = strategies or default_strategies()
    out = {"q_market_path_mean": qm.mean(0), "gross_spread_pct": np.percentile(gross, [5, 25, 50, 75, 95], axis=0),
           "p_complete_you": you.p_complete(), "p_complete_market": qm0, "state0": st0, "strategies": {}}
    end = np.where(sim["broke"], sim["break_week"], np.minimum(sim["complete_week"], Wk))
    for name, rule in strategies.items():
        ent, ext = rule(sim, qm, gross)                     # entry week (or -1) and exit week (or -1 = hold)
        ent = np.asarray(ent)
        ext = np.asarray(ext)
        stop = np.where(ext >= 0, np.minimum(ext, end), end)
        has = (ent >= 0) & (ent < stop)
        rows = np.arange(n)
        e_i, s_i = np.clip(ent, 0, Wk), np.clip(stop, 0, Wk)
        Le, Me = L[rows, e_i], M[rows, e_i]
        Lx, Mx = L[rows, s_i].copy(), M[rows, s_i].copy()
        comp = has & ~sim["broke"] & (stop == end)
        Lx[comp] = R * Mx[comp]                             # conversion at completion
        held_w = np.maximum(s_i - e_i, 0)
        carry = units_target * (cfg.div_per_quarter_unit * held_w / 13 + cfg.borrow_fee * R * Me * held_w / 52)
        pnl = np.where(has, units_target * ((Lx - Le) - R * (Mx - Me)) - carry, 0.0)
        cap = 0.5 * units_target * (Le + R * Me)
        yrs = np.maximum(held_w / 52, 1 / 52)
        traded = has
        p = pnl[traded]
        q05 = np.percentile(p, 5) if p.size else 0.0
        out["strategies"][name] = {
            "p_trade": float(traded.mean()), "mean_pnl_if_traded": float(p.mean()) if p.size else 0.0,
            "mean_pnl_all_paths": float(pnl.mean()), "p_loss_if_traded": float((p < 0).mean()) if p.size else 0.0,
            "var95_if_traded": float(-q05), "es95_if_traded": float(-p[p <= q05].mean()) if p.size else 0.0,
            "mean_entry_spread": float(gross[rows, e_i][traded].mean()) if p.size else float("nan"),
            "mean_years_held": float(yrs[traded].mean()) if p.size else 0.0,
            # aggregate (not path-averaged) return per unit of capital-time: sum P&L / sum(capital x years)
            "pnl_per_capital_year": float(pnl[traded].sum() / (cap[traded] * yrs[traded]).sum()) if p.size else 0.0,
        }
    out["table"] = pd.DataFrame([{"strategy": k, **v} for k, v in out["strategies"].items()])
    return out


def default_strategies(threshold: float = 0.287, lawsuit_exit: bool = True) -> dict:
    """Entry/exit rules evaluated on the same paths:
      now_hold          enter today, hold to resolution
      now_exit_on_suit  enter today, exit the week a US agency sues or SAMR acceptance slips
      wait_threshold    enter the first week the gross spread >= threshold (the hurdle spread), hold
      after_us_clear    enter the week the US gate clears (HSR expiry or post-second-request clearance), hold
      after_votes       enter after both votes pass, hold
    """
    def now_hold(sim, q, g):
        n = q.shape[0]
        return np.zeros(n, int), np.full(n, -1)

    def now_exit(sim, q, g):
        n = q.shape[0]
        W = sim["W"]
        ext = np.where(sim["us_sue"], W["us2"], np.where(sim["samr_late"], W["sa"], -1))
        return np.zeros(n, int), ext

    def wait_thr(sim, q, g):
        hit = (g >= threshold) & (q > 0.02) & (q < 0.999)
        first = np.where(hit.any(1), hit.argmax(1), -1)
        return first, np.full(q.shape[0], -1)

    def after_us(sim, q, g):
        W = sim["W"]
        w = np.where(~sim["hsr_sr"], W["hsr"], np.where(~sim["us_sue"], W["us2"], -1))
        alive = q[np.arange(q.shape[0]), np.clip(w, 0, q.shape[1] - 1)] > 0.0
        return np.where(alive & (w >= 0), w, -1), np.full(q.shape[0], -1)

    def after_votes(sim, q, g):
        W = sim["W"]
        w = np.full(q.shape[0], W["v"] + 1)
        alive = q[np.arange(q.shape[0]), np.clip(w, 0, q.shape[1] - 1)] > 0.0
        return np.where(alive, w, -1), np.full(q.shape[0], -1)

    rules = {"now_hold": now_hold, "now_exit_on_suit": now_exit, "wait_threshold": wait_thr,
             "after_us_clear": after_us, "after_votes": after_votes}
    if not lawsuit_exit:
        rules.pop("now_exit_on_suit")
    return rules
