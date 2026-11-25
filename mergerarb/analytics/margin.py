"""Rules-based (Reg T) margin for a long-target / short-acquirer stock pair, IBKR-style, and survival-first sizing.

Defaults are IBKR's published US stock requirements for US residents (read 2026-11-24):
  long : 26.25% initial (time of trade), 25% maintenance, 50% Reg T end-of-day initial
  short: 31.5% initial, 30% maintenance (stock > USD 16.67), 50% Reg T end-of-day initial
  USD 2,000 minimum equity for margin / short trades.
IBKR may apply higher house requirements (concentration, volatility, stocks involved in corporate actions) - the
``house_multiplier`` stresses that. The authoritative number for a real order is IBKR's what-if margin (see
mergerarb.execution), which this module never claims to replace.
"""
from __future__ import annotations

import dataclasses
import math

import pandas as pd


@dataclasses.dataclass(frozen=True)
class MarginRules:
    long_initial: float = 0.2625
    long_maint: float = 0.25
    short_initial: float = 0.315
    short_maint: float = 0.30
    regt_initial: float = 0.50
    min_equity: float = 2000.0
    house_multiplier: float = 1.0          # e.g. 1.5 = 50% higher maintenance than the base rule

    def short_maint_per_share(self, price: float) -> float:
        """IBKR short maintenance schedule by price bucket (per share)."""
        if price > 16.67:
            return self.short_maint * price
        if price > 5.0:
            return 5.0
        if price > 2.5:
            return price
        return 2.5

    def maintenance(self, long_value: float, short_shares: float, short_price: float) -> float:
        return self.house_multiplier * (self.long_maint * long_value
                                        + short_shares * self.short_maint_per_share(short_price))

    def initial(self, long_value: float, short_value: float) -> float:
        return self.house_multiplier * (self.long_initial * long_value + self.short_initial * short_value)

    def regt(self, long_value: float, short_value: float) -> float:
        return self.regt_initial * (long_value + short_value)


@dataclasses.dataclass(frozen=True)
class Position:
    target_shares: int
    acquirer_short_shares: int

    def values(self, L: float, M: float) -> tuple[float, float]:
        return self.target_shares * L, self.acquirer_short_shares * M

    def hedge_ratio(self) -> float:
        return self.acquirer_short_shares / self.target_shares if self.target_shares else float("nan")


@dataclasses.dataclass(frozen=True)
class AccountState:
    equity: float
    long_value: float
    short_value: float
    initial_req: float
    maint_req: float
    regt_req: float
    excess_liquidity: float          # equity - maintenance
    regt_excess: float               # equity - Reg T EOD requirement (SMA-like, simplified)
    gross_leverage: float
    margin_call: bool


def account_state(equity: float, pos: Position, L: float, M: float, rules: MarginRules = MarginRules()) -> AccountState:
    lv, sv = pos.values(L, M)
    mr = rules.maintenance(lv, pos.acquirer_short_shares, M)
    ir = rules.initial(lv, sv)
    rt = rules.regt(lv, sv)
    gl = (lv + sv) / equity if equity > 0 else float("inf")
    return AccountState(equity, lv, sv, ir, mr, rt, equity - mr, equity - rt, gl, equity < mr)


def exact_lot(ratio: float, max_lot: int = 10_000) -> int | None:
    """Smallest target-share count whose hedge (count x ratio) is a whole number of acquirer shares, or None."""
    for lot in range(1, max_lot + 1):
        if abs(lot * ratio - round(lot * ratio)) < 1e-9:
            return lot
    return None


def units_to_position(units: int, ratio: float, lot: int | None = None) -> Position:
    """Position for ``units`` exact hedge lots: for ratio 4.5 a lot is 2 target / 9 acquirer shares.
    If no exact lot exists below 10,000 shares, lots of 1 target share are used with the hedge rounded to whole
    shares (check ``hedge_residual``). Fractional short sales are not available at IBKR."""
    if lot is None:
        lot = exact_lot(ratio) or 1
    return Position(units * lot, int(round(units * lot * ratio)))


def hedge_residual(pos: Position, ratio: float) -> float:
    """Acquirer shares short minus the contractual hedge (positive = over-hedged)."""
    return pos.acquirer_short_shares - pos.target_shares * ratio


# ------------------------------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Stress:
    name: str
    target_move: float      # e.g. -0.30
    acquirer_move: float    # e.g. +0.25


# Loss-budget stresses: plausible-but-severe moves whose loss must stay within the policy's loss budget.
DEFAULT_STRESSES = (
    Stress("break: target to worst-peer standalone (-12%), acquirer +27% (full deal-discount reversal)", -0.124, 0.27),
    Stress("spread blow-out: acquirer +40%, target +10%", 0.10, 0.40),
    Stress("melt-up: both +60%, spread % unchanged", 0.60, 0.60),
)
# Survival stresses: extreme moves that may exceed the loss budget but must NOT trigger a margin call.
SURVIVAL_STRESSES = (
    Stress("extreme break: target -40%, acquirer +50%", -0.40, 0.50),
    Stress("extreme melt-up: both +150%, spread % unchanged", 1.50, 1.50),
    Stress("extreme blow-out: acquirer +100%, target +20%", 0.20, 1.00),
)


def stress_loss(pos: Position, L: float, M: float, s: Stress) -> float:
    """P&L of the pair under an instantaneous move (negative = loss)."""
    return pos.target_shares * L * s.target_move - pos.acquirer_short_shares * M * s.acquirer_move


def survives(equity: float, pos: Position, L: float, M: float, s: Stress, rules: MarginRules,
             buffer: float = 0.0) -> bool:
    eq = equity + stress_loss(pos, L, M, s)
    st = account_state(eq, pos, L * (1 + s.target_move), M * (1 + s.acquirer_move), rules)
    return eq >= st.maint_req * (1 + buffer)


def size_table(equity: float, L: float, M: float, ratio: float, rules: MarginRules = MarginRules(),
               stresses=DEFAULT_STRESSES, survival=SURVIVAL_STRESSES, max_units: int = 200) -> pd.DataFrame:
    rows = []
    for u in range(1, max_units + 1):
        pos = units_to_position(u, ratio)
        st = account_state(equity, pos, L, M, rules)
        if st.long_value + st.short_value > 20 * equity:
            break
        row = {"units": u, "target_shares": pos.target_shares, "acquirer_short": pos.acquirer_short_shares,
               "long_value": st.long_value, "short_value": st.short_value, "gross_leverage": st.gross_leverage,
               "regt_req": st.regt_req, "maint_req": st.maint_req, "excess_liquidity": st.excess_liquidity,
               "regt_ok": st.regt_excess >= 0}
        for i, s in enumerate(stresses):
            row[f"stress{i + 1}_loss"] = stress_loss(pos, L, M, s)
        row["worst_stress_loss_pct"] = min(row[f"stress{i + 1}_loss"] for i in range(len(stresses))) / equity
        for i, s in enumerate(survival):
            row[f"survival{i + 1}_loss"] = stress_loss(pos, L, M, s)
            row[f"survival{i + 1}_ok"] = survives(equity, pos, L, M, s, rules)
        rows.append(row)
    return pd.DataFrame(rows)


@dataclasses.dataclass(frozen=True)
class SizingPolicy:
    max_stress_loss_pct: float = 0.10      # worst loss-budget stress <= 10% of equity
    min_excess_liquidity_pct: float = 0.50  # keep >= 50% of equity as excess liquidity after the trade
    regt_required: bool = True
    survival_buffer: float = 0.25          # after each survival stress: equity >= 125% of maintenance


def max_units(equity: float, L: float, M: float, ratio: float, policy: SizingPolicy = SizingPolicy(),
              rules: MarginRules = MarginRules(), stresses=DEFAULT_STRESSES,
              survival=SURVIVAL_STRESSES) -> tuple[int, str, pd.DataFrame]:
    """Largest unit count satisfying every constraint, the binding constraint, and the full size table."""
    tab = size_table(equity, L, M, ratio, rules, stresses, survival)
    best, binding = 0, "no size passes (equity too small?)"
    for _, r in tab.iterrows():
        pos = Position(int(r.target_shares), int(r.acquirer_short))
        checks = {
            "Reg T end-of-day initial": (not policy.regt_required) or bool(r.regt_ok),
            "minimum equity": equity >= rules.min_equity,
            "excess liquidity floor": r.excess_liquidity >= policy.min_excess_liquidity_pct * equity,
            "loss budget (worst plausible stress)": r.worst_stress_loss_pct >= -policy.max_stress_loss_pct,
            "survival (no margin call in extreme stresses)": all(
                survives(equity, pos, L, M, s, rules, policy.survival_buffer) for s in survival),
        }
        failed = [k for k, ok in checks.items() if not ok]
        if failed:
            binding = failed[0]
            break
        best = int(r.units)
    return best, binding, tab


def liquidation_moves(equity: float, pos: Position, L: float, M: float, rules: MarginRules = MarginRules()) -> dict:
    """How far prices can move before equity < maintenance, along three directions (bisection)."""
    def breach(f) -> float | None:
        lo, hi = 0.0, 5.0
        if not f(hi):
            return None
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if f(mid):
                hi = mid
            else:
                lo = mid
        return hi

    def call_at(dl, dm):
        eq = equity + pos.target_shares * L * dl - pos.acquirer_short_shares * M * dm
        st = account_state(eq, pos, L * (1 + dl), M * (1 + dm), rules)
        return eq < st.maint_req

    out = {
        "acquirer_up_target_flat": breach(lambda x: call_at(0.0, x)),
        "target_down_acquirer_flat": breach(lambda x: call_at(-min(x, 0.999), 0.0)),
        "both_up_same_pct": breach(lambda x: call_at(x, x)),
    }
    td = out["target_down_acquirer_flat"]
    ratio = pos.hedge_ratio()
    # gross spread at which a margin call occurs if only the target moves (acquirer flat)
    out["gross_spread_at_call_acquirer_flat"] = (ratio * M / (L * (1 - td)) - 1) if td is not None and td < 1 else None
    return out
