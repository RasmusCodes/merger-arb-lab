"""Where would the stocks trade if the deal broke? ("standalone" / "break" prices)

The textbook downside - "the target returns to its unaffected price" - is wrong after a large market move: the
unaffected price is a snapshot of a market that no longer exists. We therefore project both stocks forward from their
unaffected closes with factor models estimated BEFORE any deal news:

    log P*_t = log P_unaffected + sum_{d in (u, t]} beta' f_d            (no alpha; residuals have mean zero)

and report a residual band (+/- 1 sd of the accumulated idiosyncratic return). The acquirer's counterfactual tells us
how much of its fall is deal-specific ("deal discount"), part of which would plausibly reverse on termination.
Simple peer-ratio variants are reported next to the regressions so the reader can see how model-dependent the answer is.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd


@dataclasses.dataclass(frozen=True)
class StandaloneEstimate:
    symbol: str
    method: str
    unaffected_date: pd.Timestamp
    as_of: pd.Timestamp
    unaffected_price: float
    estimate: float
    low: float                  # -1 residual sd
    high: float                 # +1 residual sd
    betas: dict
    r2: float | None
    resid_vol_daily: float | None
    horizon_days: int


def _logret(px: pd.DataFrame) -> pd.DataFrame:
    return np.log(px).diff()


def factor_projection(prices: pd.DataFrame, symbol: str, factors: list[str], unaffected_date, as_of,
                      window: int = 252, method_name: str | None = None) -> StandaloneEstimate:
    """OLS of daily log returns of ``symbol`` on ``factors`` over ``window`` sessions ending at ``unaffected_date``;
    projection of the unaffected price to ``as_of`` with realised factor returns."""
    u, a = pd.Timestamp(unaffected_date), pd.Timestamp(as_of)
    r = _logret(prices[[symbol, *factors]]).dropna()
    est = r[r.index <= u].tail(window)
    X = np.column_stack([np.ones(len(est)), est[factors].to_numpy()])
    y = est[symbol].to_numpy()
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    r2 = 1 - resid.var() / y.var() if y.var() > 0 else None
    sd = float(resid.std(ddof=X.shape[1]))
    fwd = r[(r.index > u) & (r.index <= a)]
    h = len(fwd)
    drift = float((fwd[factors].to_numpy() @ coef[1:]).sum()) if h else 0.0
    p0 = float(prices.loc[prices.index <= u, symbol].iloc[-1])
    est_px = p0 * math.exp(drift)
    band = sd * math.sqrt(max(h, 1))
    return StandaloneEstimate(symbol, method_name or f"OLS on {'+'.join(factors)} ({window}d)", u, a, p0, est_px,
                              est_px * math.exp(-band), est_px * math.exp(band),
                              {f: float(b) for f, b in zip(factors, coef[1:])}, None if r2 is None else float(r2),
                              sd, h)


def peer_ratio(prices: pd.DataFrame, symbol: str, peers: list[str], unaffected_date, as_of) -> StandaloneEstimate:
    """Unaffected price x equal-weighted simple return of ``peers`` since the unaffected date (beta = 1)."""
    u, a = pd.Timestamp(unaffected_date), pd.Timestamp(as_of)
    p0 = float(prices.loc[prices.index <= u, symbol].iloc[-1])
    rets = []
    for p in peers:
        s = prices[p].dropna()
        rets.append(float(s[s.index <= a].iloc[-1] / s[s.index <= u].iloc[-1]))
    g = float(np.mean(rets))
    lo, hi = min(rets), max(rets)
    h = int(((prices.index > u) & (prices.index <= a)).sum())
    return StandaloneEstimate(symbol, f"peer ratio ({', '.join(peers)})", u, a, p0, p0 * g, p0 * lo, p0 * hi,
                              {p: 1.0 for p in peers}, None, None, h)


def deal_discount(actual_price: float, counterfactual: float) -> float:
    """Fraction of the counterfactual (deal-free) acquirer price that the market has taken off: 1 - actual/cf."""
    return 1.0 - actual_price / counterfactual


def standalone_table(prices: pd.DataFrame, target: str, acquirer: str, unaffected_date, as_of,
                     sector: str = "SMH", optical_peers: tuple[str, ...] = ("COHR", "AAOI", "FN", "CIEN"),
                     window: int = 252) -> tuple[list[StandaloneEstimate], list[StandaloneEstimate]]:
    """All target and acquirer standalone estimates used in the report (several methods, side by side)."""
    px = prices.copy()
    peers = [p for p in optical_peers if p in px.columns]
    px["OPT_EW"] = np.exp(np.log(px[peers]).diff().mean(axis=1).fillna(0).cumsum())  # EW log-return index
    tgt = [
        factor_projection(px, target, [sector], unaffected_date, as_of, window, f"beta to {sector}"),
        factor_projection(px, target, ["OPT_EW"], unaffected_date, as_of, window, "beta to optical peers (EW)"),
        factor_projection(px, target, [sector, "OPT_EW"], unaffected_date, as_of, window,
                          f"two-factor: {sector} + optical peers"),
        factor_projection(px, target, [acquirer], unaffected_date, as_of, window,
                          f"beta to {acquirer} (uses the deal-affected {acquirer} path: biased low)"),
        peer_ratio(px, target, peers, unaffected_date, as_of),
        peer_ratio(px, target, [sector], unaffected_date, as_of),
    ]
    acq = [
        factor_projection(px, acquirer, [sector], unaffected_date, as_of, window, f"beta to {sector}"),
        factor_projection(px, acquirer, [sector, "OPT_EW"], unaffected_date, as_of, window,
                          f"two-factor: {sector} + optical peers"),
        peer_ratio(px, acquirer, [sector], unaffected_date, as_of),
    ]
    return tgt, acq
