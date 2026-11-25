"""Monte Carlo of a ratio-hedged stock-for-stock arbitrage held through resolution, in a margin account.

Model (daily steps, business days):
  * Acquirer price M_t: geometric Brownian motion with volatility ``acq_vol`` (default: option-implied vol for the
    expiry nearest the expected close) and drift ``acq_drift``.
  * Target standalone value S_t (what the target would trade at without the deal): lognormal, correlation ``rho`` with
    the acquirer's shocks (pre-deal correlation), volatility ``standalone_vol``.
  * Gross spread g_t = R*M_t/L_t - 1 (so the target price is L_t = R*M_t/(1+g_t)). x_t = log(1+g_t):
      - on paths that complete at T_c, x follows a Brownian bridge from x_0 to 0 (spreads converge),
      - on paths that break at tau, x drifts toward ``break_widen_to`` with the same noise (spreads usually widen
        before a failure is announced; see the CRSP comparables), floored at 0.
  * Resolution: completion -> the target converts into R acquirer shares, closing the hedge at zero spread.
    Break -> target jumps to S_tau (+ after-tax cash fee per share for a regulatory failure), acquirer jumps by
    j = kappa*delta/(1-delta), the reversal of a fraction kappa of the estimated deal discount delta.
  * Outcomes and timing (REAL-WORLD, analyst inputs): vote failure at the vote date (+/- window), regulatory failure
    uniformly in a window ending at the final outside date, other failure uniformly before the expected close,
    completion on a triangular date distribution. Independent draws.
  * Account: borrow fee accrues daily on the short value, acquirer dividends are paid on projected ex-dates, a
    maintenance-margin breach liquidates the pair at that day's prices plus slippage.
Outputs are distributions of account P&L, drawdown and liquidation frequency - not forecasts.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np
import pandas as pd

from ..deal import Deal
from .margin import MarginRules, Position


@dataclasses.dataclass(frozen=True)
class MCConfig:
    n_paths: int = 20000
    seed: int = 20261124
    chunk: int = 2000
    # market dynamics
    acq_vol: float = 0.79
    acq_drift: float = 0.0
    standalone0: float = 600.0
    standalone_vol: float = 0.85
    rho: float = 0.48
    spread_vol: float = 0.30            # annual vol of log(1+g)
    break_widen_to: float = 0.35        # gross spread level a breaking deal drifts toward before the news
    # deal outcome model (real world)
    p_acq_vote_fail: float = 0.10
    p_tgt_vote_fail: float = 0.10
    p_reg_fail: float = 0.20
    p_other_fail: float = 0.03
    vote_date: dt.date = dt.date(2027, 6, 30)
    vote_window_days: int = 60
    reg_fail_start: dt.date = dt.date(2027, 9, 1)
    completion_min: dt.date = dt.date(2027, 11, 1)
    completion_mode: dt.date = dt.date(2028, 3, 31)
    completion_max: dt.date = dt.date(2028, 8, 1)
    # model uncertainty: when set, each path draws its starting standalone value log-uniformly in
    # [standalone0_low, standalone0_high] and its reversal fraction kappa uniformly in [kappa_low, kappa_high]
    standalone0_low: float | None = None
    standalone0_high: float | None = None
    kappa_low: float | None = None
    kappa_high: float | None = None
    # break jumps
    deal_discount: float = 0.21
    rebound_kappa: float = 0.5
    fee_per_share: float = 33.2
    fee_haircut: float = 0.21           # tax / leakage on the cash fee before it reaches the share price
    # costs
    borrow_fee: float = 0.0032
    liquidation_slippage_bps: float = 25.0

    @property
    def p_complete(self) -> float:
        return (1 - self.p_acq_vote_fail) * (1 - self.p_tgt_vote_fail) * (1 - self.p_reg_fail) * (1 - self.p_other_fail)


@dataclasses.dataclass
class MCResult:
    config: MCConfig
    summary: dict
    by_outcome: pd.DataFrame
    pnl: np.ndarray                 # per path, USD, account level
    roe: np.ndarray
    outcome: np.ndarray             # 0 complete, 1 vote break, 2 regulatory break, 3 other break, 4 liquidated
    end_years: np.ndarray
    max_drawdown: np.ndarray
    fan: pd.DataFrame               # equity percentiles over time (from the first chunk)
    unit_terminal: pd.DataFrame     # per-unit terminal dL, dM, carry per path (for hedge-ratio comparison)


OUTCOMES = {0: "complete", 1: "vote failure", 2: "regulatory failure", 3: "other failure", 4: "liquidated"}


def _dates_to_idx(days: pd.DatetimeIndex, dates) -> np.ndarray:
    """Index of the first business day on/after each date (day resolution; unit-safe under pandas 2 and 3)."""
    d = np.asarray(days.values).astype("datetime64[D]")
    if len(dates) == 0:
        return np.array([], dtype=int)
    x = np.asarray(pd.DatetimeIndex(dates).values).astype("datetime64[D]")
    return np.clip(np.searchsorted(d, x), 1, len(d) - 1)


def simulate(deal: Deal, pos: Position, equity0: float, L0: float, M0: float, start: dt.date,
             cfg: MCConfig = MCConfig(), rules: MarginRules = MarginRules()) -> MCResult:
    rng = np.random.default_rng(cfg.seed)
    R = deal.exchange_ratio
    horizon = max(cfg.completion_max, deal.final_outside_date)
    days = pd.bdate_range(start, horizon)
    D = len(days)
    dtau = 1.0 / 252.0
    t_years = np.arange(D) * dtau
    g0 = R * M0 / L0 - 1.0
    x0 = np.log1p(g0)
    xb = np.log1p(cfg.break_widen_to)
    div_idx = set(_dates_to_idx(days, deal.acquirer_dividend.ex_dates_between(start, horizon)).tolist())
    div_mask = np.zeros(D)
    for k in div_idx:
        div_mask[k] = 1.0
    div_amt = deal.acquirer_dividend.amount
    nL, nM = pos.target_shares, pos.acquirer_short_shares

    all_pnl, all_out, all_end, all_dd, all_dl, all_dm, all_carry, all_raw = [], [], [], [], [], [], [], []
    fan = None
    n_done = 0
    while n_done < cfg.n_paths:
        n = min(cfg.chunk, cfg.n_paths - n_done)
        # ----- outcomes and timing
        u = rng.random((n, 4))
        vote_fail = (u[:, 0] < cfg.p_acq_vote_fail) | (u[:, 1] < cfg.p_tgt_vote_fail)
        reg_fail = (~vote_fail) & (u[:, 2] < cfg.p_reg_fail)
        other_fail = (~vote_fail) & (~reg_fail) & (u[:, 3] < cfg.p_other_fail)
        out = np.zeros(n, dtype=int)
        out[vote_fail], out[reg_fail], out[other_fail] = 1, 2, 3
        day_ns = 86_400 * 10 ** 9
        v0 = pd.Timestamp(cfg.vote_date).value
        vote_d = v0 + (rng.uniform(-cfg.vote_window_days, cfg.vote_window_days, n) * day_ns).astype("int64")
        r0, r1 = pd.Timestamp(cfg.reg_fail_start).value, pd.Timestamp(deal.final_outside_date).value
        reg_d = rng.integers(r0, max(r1, r0 + 1), n)
        o0, o1 = pd.Timestamp(start).value, pd.Timestamp(cfg.completion_mode).value
        oth_d = rng.integers(o0, max(o1, o0 + 1), n)
        a, c, b = (float(pd.Timestamp(x).value) for x in (cfg.completion_min, cfg.completion_mode, cfg.completion_max))
        comp_d = rng.triangular(a, c, b, n).astype("int64")
        res_ns = np.where(out == 1, vote_d, np.where(out == 2, reg_d, np.where(out == 3, oth_d, comp_d)))
        res_date = pd.to_datetime(res_ns)
        ridx = _dates_to_idx(days, res_date)
        # ----- price paths
        z1 = rng.standard_normal((n, D - 1))
        z2 = rng.standard_normal((n, D - 1))
        logM = np.concatenate([np.zeros((n, 1)), np.cumsum((cfg.acq_drift - 0.5 * cfg.acq_vol ** 2) * dtau
                                                           + cfg.acq_vol * np.sqrt(dtau) * z1, axis=1)], axis=1)
        M = M0 * np.exp(logM)
        zs = cfg.rho * z1 + np.sqrt(1 - cfg.rho ** 2) * z2
        logS = np.concatenate([np.zeros((n, 1)), np.cumsum(-0.5 * cfg.standalone_vol ** 2 * dtau
                                                           + cfg.standalone_vol * np.sqrt(dtau) * zs, axis=1)], axis=1)
        if cfg.standalone0_low is not None and cfg.standalone0_high is not None:
            lo_, hi_ = sorted((cfg.standalone0_low, cfg.standalone0_high))
            s0 = np.exp(rng.uniform(np.log(lo_), np.log(hi_), n))
        else:
            s0 = np.full(n, cfg.standalone0)
        S = s0[:, None] * np.exp(logS)
        if cfg.kappa_low is not None and cfg.kappa_high is not None:
            kap = rng.uniform(*sorted((cfg.kappa_low, cfg.kappa_high)), n)
        else:
            kap = np.full(n, cfg.rebound_kappa)
        j_path = kap * cfg.deal_discount / (1 - cfg.deal_discount)
        # ----- spread paths: bridge to 0 at completion, drift to xb before a break
        zx = rng.standard_normal((n, D - 1))
        W = np.concatenate([np.zeros((n, 1)), np.cumsum(np.sqrt(dtau) * zx, axis=1)], axis=1)
        T_res = t_years[ridx][:, None]
        tt = t_years[None, :]
        frac = np.clip(tt / np.maximum(T_res, dtau), 0.0, 1.0)
        bridge = x0 * (1 - frac) + cfg.spread_vol * (W - frac * W[np.arange(n), ridx][:, None])
        widen = x0 + (xb - x0) * frac + cfg.spread_vol * W
        x = np.where((out == 0)[:, None], bridge, widen)
        x = np.maximum(x, 0.0)
        L = R * M / np.exp(x)
        L[:, 0] = L0
        # ----- account equity path (pre-resolution)
        sv = nM * M
        borrow = np.concatenate([np.zeros((n, 1)), np.cumsum(cfg.borrow_fee * sv[:, :-1] * (1 / 252.0), axis=1)], axis=1)
        divs = np.cumsum(div_mask[None, :] * div_amt * nM * np.ones((n, 1)), axis=1)
        mtm = nL * (L - L0) - nM * (M - M0)
        eq = equity0 + mtm - borrow - divs
        maint = rules.house_multiplier * (rules.long_maint * nL * L + rules.short_maint * nM * M)
        alive = np.arange(D)[None, :] < ridx[:, None]
        breach = (eq < maint) & alive
        first_breach = np.where(breach.any(axis=1), breach.argmax(axis=1), D + 1)
        liquidated = first_breach < ridx
        end_idx = np.where(liquidated, first_breach, ridx)
        rows = np.arange(n)
        # ----- terminal values
        L_end = L[rows, end_idx].copy()
        M_end = M[rows, end_idx].copy()
        comp = (~liquidated) & (out == 0)
        brk = (~liquidated) & (out > 0)
        L_end[comp] = R * M_end[comp]
        fee = np.where(out == 2, cfg.fee_per_share * (1 - cfg.fee_haircut), 0.0)
        L_end[brk] = S[rows, end_idx][brk] + fee[brk]
        M_end[brk] = M_end[brk] * (1 + j_path[brk])
        carry = borrow[rows, end_idx] + divs[rows, end_idx]
        slip = np.where(liquidated, (nL * L_end + nM * M_end) * cfg.liquidation_slippage_bps / 1e4, 0.0)
        pnl = nL * (L_end - L0) - nM * (M_end - M0) - carry - slip
        # running max drawdown on the equity path up to the end index
        eq_masked = np.where(np.arange(D)[None, :] <= end_idx[:, None], eq, np.nan)
        eq_masked[rows, end_idx] = equity0 + pnl
        peak = np.fmax.accumulate(np.nan_to_num(eq_masked, nan=-np.inf), axis=1)
        dd = np.nanmax(np.where(np.isfinite(eq_masked), (peak - eq_masked) / peak, np.nan), axis=1)
        if fan is None:
            qs = [5, 25, 50, 75, 95]
            fan = pd.DataFrame(np.nanpercentile(eq_masked, qs, axis=0).T, index=days,
                               columns=[f"p{q}" for q in qs])
        o = np.where(liquidated, 4, out)
        all_pnl.append(pnl)
        all_out.append(o)
        all_end.append(t_years[end_idx])
        all_dd.append(dd)
        # per-unit-long terminal changes for hedge comparisons (ratio-neutral path, no margin truncation)
        Lr = L[rows, ridx].copy()
        Mr = M[rows, ridx].copy()
        Lr[out == 0] = R * Mr[out == 0]
        Lr[out > 0] = S[rows, ridx][out > 0] + fee[out > 0]
        Mr[out > 0] = Mr[out > 0] * (1 + j_path[out > 0])
        all_dl.append(Lr - L0)
        all_dm.append(Mr - M0)
        all_carry.append((borrow[rows, ridx] + divs[rows, ridx]) / max(nM, 1))
        all_raw.append(out)
        n_done += n

    pnl = np.concatenate(all_pnl)
    outc = np.concatenate(all_out)
    endy = np.concatenate(all_end)
    dd = np.concatenate(all_dd)
    roe = pnl / equity0
    ann = np.where(roe > -1, (1 + roe) ** (1 / np.maximum(endy, 1 / 252)) - 1, -1.0)
    q05 = np.percentile(pnl, 5)
    summary = {
        "n_paths": int(len(pnl)), "p_complete_input": cfg.p_complete,
        "mean_pnl": float(pnl.mean()), "median_pnl": float(np.median(pnl)), "std_pnl": float(pnl.std()),
        "p_loss": float((pnl < 0).mean()), "var95": float(-q05), "es95": float(-pnl[pnl <= q05].mean()),
        "p_liquidation": float((outc == 4).mean()), "mean_roe": float(roe.mean()),
        "mean_years": float(endy.mean()), "mean_annualized_roe": float(np.mean(ann)),
        "roe_per_year_of_expected": float(roe.mean() / endy.mean()),
        "median_max_drawdown": float(np.median(dd)), "p95_max_drawdown": float(np.percentile(dd, 95)),
    }
    rows = []
    for k, name in OUTCOMES.items():
        m = outc == k
        if m.any():
            rows.append({"outcome": name, "share": float(m.mean()), "mean_pnl": float(pnl[m].mean()),
                         "p5_pnl": float(np.percentile(pnl[m], 5)), "p95_pnl": float(np.percentile(pnl[m], 95)),
                         "mean_years": float(endy[m].mean())})
    unit = pd.DataFrame({"dL": np.concatenate(all_dl), "dM": np.concatenate(all_dm),
                         "carry_per_short_share": np.concatenate(all_carry), "outcome": np.concatenate(all_raw)})
    return MCResult(cfg, summary, pd.DataFrame(rows), pnl, roe, outc, endy, dd, fan, unit)


def hedge_comparison(unit: pd.DataFrame, L0: float, M0: float, ratio: float) -> pd.DataFrame:
    """Per-unit-long terminal P&L for alternative hedge ratios on the SAME simulated paths:
    ratio-neutral (contract), dollar-neutral (L0/M0), and the variance-minimising h* = cov(dL,dM)/var(dM)."""
    dL, dM, c = unit["dL"].to_numpy(), unit["dM"].to_numpy(), unit["carry_per_short_share"].to_numpy()
    h_star = float(np.cov(dL, dM)[0, 1] / np.var(dM, ddof=1))
    rows = []
    for name, h in (("ratio-neutral (contract)", ratio), ("dollar-neutral", L0 / M0), ("variance-minimising", h_star),
                    ("unhedged long target", 0.0)):
        p = dL - h * dM - h * c
        q05 = np.percentile(p, 5)
        rows.append({"hedge": name, "short_per_long": h, "mean": float(p.mean()), "std": float(p.std()),
                     "p_loss": float((p < 0).mean()), "p5": float(q05), "es95": float(-p[p <= q05].mean()),
                     "mean_if_complete": float(p[unit["outcome"] == 0].mean()),
                     "mean_if_break": float(p[unit["outcome"].isin([1, 2, 3])].mean())})
    return pd.DataFrame(rows)
