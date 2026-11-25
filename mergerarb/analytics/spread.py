"""Merger-spread arithmetic and history statistics.

Conventions (per ONE target share):
    implied value  V = ratio * acquirer_price + cash
    spread ($)     D = V - target_price
    gross spread   g = V / target_price - 1                     (return on the long leg if the deal closes)
    discount       s = 1 - target_price / V                     (fraction of deal value the market withholds)
A ratio-hedged arbitrage (long 1 target, short `ratio` acquirer shares) has daily mark-to-market P&L equal to
minus the change in the dollar spread D (before carry).
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd


def implied_value(acquirer_price, ratio: float, cash: float = 0.0):
    return ratio * np.asarray(acquirer_price, dtype=float) + cash


def spread_dollars(target_price, acquirer_price, ratio: float, cash: float = 0.0):
    return implied_value(acquirer_price, ratio, cash) - np.asarray(target_price, dtype=float)


def gross_spread(target_price, acquirer_price, ratio: float, cash: float = 0.0):
    return implied_value(acquirer_price, ratio, cash) / np.asarray(target_price, dtype=float) - 1.0


def annualize(total_return: float, years: float, method: str = "compound") -> float:
    """Annualise a holding-period return. ``compound``: (1+r)^(1/T)-1; ``simple``: r/T."""
    if years <= 0:
        raise ValueError("years must be positive")
    if method == "simple":
        return total_return / years
    if total_return <= -1:
        return -1.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def year_fraction(start, end) -> float:
    return (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25


@dataclasses.dataclass(frozen=True)
class SpreadStats:
    current: float               # current gross spread
    mean: float
    std: float
    min: float
    max: float
    percentile: float            # percentile rank of the current value within the window (0-100)
    zscore: float
    daily_change_vol: float      # stdev of daily changes in gross spread (in spread units)
    n_obs: int
    window_start: pd.Timestamp
    window_end: pd.Timestamp


def spread_history(target: pd.Series, acquirer: pd.Series, ratio: float, cash: float = 0.0,
                   start=None) -> pd.DataFrame:
    """Daily spread table from aligned close series (inner join on dates)."""
    df = pd.concat({"target": target, "acquirer": acquirer}, axis=1, join="inner").dropna()
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    df["implied"] = implied_value(df["acquirer"], ratio, cash)
    df["spread_usd"] = df["implied"] - df["target"]
    df["gross_spread"] = df["implied"] / df["target"] - 1.0
    df["discount"] = 1.0 - df["target"] / df["implied"]
    return df


def stats(hist: pd.DataFrame, col: str = "gross_spread") -> SpreadStats:
    x = hist[col].dropna()
    cur = float(x.iloc[-1])
    sd = float(x.std(ddof=1)) if len(x) > 1 else float("nan")
    pct = float((x <= cur).mean() * 100.0)
    z = (cur - float(x.mean())) / sd if sd and not math.isnan(sd) and sd > 0 else float("nan")
    dv = float(x.diff().std(ddof=1)) if len(x) > 2 else float("nan")
    return SpreadStats(cur, float(x.mean()), sd, float(x.min()), float(x.max()), pct, z, dv, len(x),
                       x.index[0], x.index[-1])


def sensitivity_grid(target_price: float, acquirer_price: float, ratio: float, cash: float = 0.0,
                     acq_moves=np.linspace(-0.3, 0.3, 13), tgt_moves=np.linspace(-0.3, 0.3, 13)) -> pd.DataFrame:
    """Gross spread for joint percentage moves in the acquirer (columns) and target (rows)."""
    rows = {}
    for tm in tgt_moves:
        rows[round(float(tm), 4)] = {round(float(am), 4): float(gross_spread(target_price * (1 + tm),
                                                                             acquirer_price * (1 + am), ratio, cash))
                                     for am in acq_moves}
    out = pd.DataFrame(rows).T
    out.index.name = "target_move"
    out.columns.name = "acquirer_move"
    return out
