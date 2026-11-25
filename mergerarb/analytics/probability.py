"""Completion probabilities: what the market implies, what a bottom-up decomposition suggests, and what each implies
for expected returns.

Per-unit convention: one "unit" = long 1 target share + short `ratio` acquirer shares (the exact contractual hedge).
    C = completion P&L per unit  = spread$ - carry to the closing date            (independent of the acquirer price)
    B = break P&L per unit       = (S - L0) + fee_cash - ratio * (M_break - M0) - carry to the break date
where S is the target's standalone price and M_break the acquirer price after a termination.
The break-even completion probability is p* = -B / (C - B) (for B < 0 < C); with timing, values are discounted.
Nothing here is a fact about the future: these are explicit, labelled assumptions with ranges.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd


@dataclasses.dataclass(frozen=True)
class BreakCase:
    name: str
    target_standalone: float          # S: target price if the deal breaks
    acquirer_rebound: float           # j: % jump in the acquirer on termination (e.g. 0.12 = +12%)
    fee_per_share: float = 0.0        # cash reverse-termination fee per target share (regulatory break only)
    note: str = ""


def completion_pnl(spread_usd: float, carry_to_close: float) -> float:
    return spread_usd - carry_to_close


def break_pnl(L0: float, M0: float, ratio: float, case: BreakCase, carry_to_break: float = 0.0,
              fee_haircut: float = 0.0) -> float:
    """Per-unit P&L if the deal terminates (target -> standalone + fee value, acquirer jumps by j)."""
    fee = case.fee_per_share * (1.0 - fee_haircut)
    return (case.target_standalone + fee - L0) - ratio * M0 * case.acquirer_rebound - carry_to_break


def breakeven_probability(C: float, B: float, df_complete: float = 1.0, df_break: float = 1.0) -> float:
    """p* such that p*C*df_c + (1-p)*B*df_b = 0. Returns 0 if B >= 0 (no loss on break), 1 if C <= 0."""
    if C <= 0:
        return 1.0
    if B >= 0:
        return 0.0
    return (-B * df_break) / (C * df_complete - B * df_break)


def expected_pnl(p: float, C: float, B: float) -> float:
    return p * C + (1.0 - p) * B


def market_implied_standalone(L0: float, M0: float, ratio: float, p: float, j: float, C: float,
                              fee_per_share: float = 0.0) -> float:
    """Standalone price S at which the arb is priced at zero expected P&L for a given probability p and rebound j:
    p*C + (1-p)*B(S) = 0  ->  S = L0 - fee + ratio*M0*j - p*C/(1-p)."""
    if p >= 1:
        return float("-inf")
    return L0 - fee_per_share + ratio * M0 * j - p * C / (1.0 - p)


def implied_probability_grid(L0: float, M0: float, ratio: float, C: float, standalones, rebounds) -> pd.DataFrame:
    """Break-even (market-implied, zero-excess-return) completion probability for a grid of break assumptions."""
    out = {}
    for j in rebounds:
        col = {}
        for S in standalones:
            B = break_pnl(L0, M0, ratio, BreakCase("grid", S, j))
            col[round(float(S), 2)] = breakeven_probability(C, B)
        out[round(float(j), 4)] = col
    df = pd.DataFrame(out)
    df.index.name = "target_standalone"
    df.columns.name = "acquirer_rebound"
    return df


# ------------------------------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ProbabilityComponent:
    name: str
    p_pass_low: float
    p_pass_base: float
    p_pass_high: float
    rationale: str


@dataclasses.dataclass(frozen=True)
class BottomUp:
    components: tuple[ProbabilityComponent, ...]

    def total(self, which: str = "base") -> float:
        attr = {"low": "p_pass_low", "base": "p_pass_base", "high": "p_pass_high"}[which]
        return float(np.prod([getattr(c, attr) for c in self.components]))

    def table(self) -> pd.DataFrame:
        rows = [{"component": c.name, "low": c.p_pass_low, "base": c.p_pass_base, "high": c.p_pass_high,
                 "rationale": c.rationale} for c in self.components]
        rows.append({"component": "P(complete) = product", "low": self.total("low"), "base": self.total("base"),
                     "high": self.total("high"), "rationale": "assumes independence (sequential conditions)"})
        return pd.DataFrame(rows)


DEFAULT_BOTTOM_UP = BottomUp((
    ProbabilityComponent(
        "Marvell stockholders approve share issuance + charter amendment", 0.80, 0.90, 0.95,
        "Votes-cast standard (low bar) and both boards unanimous, but MRVL fell ~20% abnormally on the news and "
        "holders cite dilution (33% to LITE holders). No organised opposition or proxy-adviser view found as of "
        "2026-11-24."),
    ProbabilityComponent(
        "Lumentum stockholders adopt the agreement", 0.80, 0.90, 0.96,
        "Majority-of-OUTSTANDING standard (abstentions count as no). The offer is now worth less than LITE's pre-talks "
        "price and below factor-model standalone estimates; CEO says shareholders are 'not happy'. Votes expected "
        "after the S-4 is effective (mid-2027, assumption)."),
    ProbabilityComponent(
        "Required regulatory approvals (HSR, China SAMR, others) before the final outside date", 0.65, 0.80, 0.90,
        "Commentators (Breakingviews, Bloomberg Intelligence) name SAMR as the key risk. SAMR cleared Marvell-Cavium, "
        "Marvell-Inphi, AMD-Xilinx, II-VI-Coherent and Lumentum-NeoPhotonics (several with long delays/conditions), "
        "but let Qualcomm-NXP, Applied-Kokusai and Intel-Tower lapse. Remedies are capped at 'material to the "
        "combined company' (Burdensome Condition), so Marvell can walk from heavy remedies (paying USD 3.2bn)."),
    ProbabilityComponent(
        "No other termination (MAE, superior proposal, renegotiation failure, litigation)", 0.93, 0.97, 0.99,
        "Standard MAE carve-outs exclude industry-wide and stock-price changes; reciprocal no-shops and large fees."),
))
