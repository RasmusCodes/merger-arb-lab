"""Carry and transaction costs of a ratio-hedged stock-for-stock arbitrage held through closing.

All rates are annual decimals. Broker parameters default to IBKR Pro (US) as published on interactivebrokers.com on
2026-11-24 (see docs/SOURCES.md); every number is a parameter, so a different broker or a later schedule is a
config change, not a code change.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from ..deal import Deal


@dataclasses.dataclass(frozen=True)
class BrokerTerms:
    # commissions (IBKR Pro Fixed, US stocks)
    commission_per_share: float = 0.005
    commission_min: float = 1.00
    commission_max_pct: float = 0.01
    # interest
    benchmark_rate: float = 0.0388          # IBKR USD benchmark implied by 5.38% = BM + 1.5% (2026-11-24)
    debit_spread_tier1: float = 0.015       # margin loans 0-100k at BM + 1.5%
    credit_threshold: float = 10_000.0      # no credit interest on the first USD 10k of cash
    credit_spread: float = -0.005           # credit interest BM - 0.5% (NAV >= 100k), scaled by NAV/100k below that
    short_proceeds_zero_tier: float = 100_000.0  # 0% on the first USD 100k of short proceeds
    short_proceeds_spread_tier2: float = -0.0125  # BM - 1.25% on 100k-1M
    nav_full_rate: float = 100_000.0        # NAV at which full rates apply; proportionally less below
    # execution
    half_spread_bps: float = 5.0            # assumed half bid-ask paid on entry per leg (configurable)
    slippage_bps: float = 5.0               # assumed impact/slippage per leg

    def commission(self, shares: float, price: float) -> float:
        if shares <= 0:
            return 0.0
        c = max(self.commission_min, self.commission_per_share * shares)
        return min(c, self.commission_max_pct * shares * price)

    def credit_rate(self, cash_balance: float, nav: float) -> float:
        """Blended annual rate paid on a positive cash balance (IBKR-style threshold + NAV scaling)."""
        if cash_balance <= self.credit_threshold:
            return 0.0
        full = max(self.benchmark_rate + self.credit_spread, 0.0)
        scale = min(1.0, max(nav, 0.0) / self.nav_full_rate)
        return full * scale * (cash_balance - self.credit_threshold) / cash_balance

    def short_proceeds_rate(self, proceeds: float, nav: float) -> float:
        """Blended annual rate paid on short-sale proceeds (0% on the first 100k, BM-1.25% above, NAV-scaled)."""
        if proceeds <= self.short_proceeds_zero_tier:
            return 0.0
        tier = max(self.benchmark_rate + self.short_proceeds_spread_tier2, 0.0)
        scale = min(1.0, max(nav, 0.0) / self.nav_full_rate)
        return tier * scale * (proceeds - self.short_proceeds_zero_tier) / proceeds

    def debit_rate(self, loan: float) -> float:
        return self.benchmark_rate + self.debit_spread_tier1 if loan > 0 else 0.0


@dataclasses.dataclass(frozen=True)
class CarryBreakdown:
    """Costs (+) and income (-) per position over the holding period, in USD."""
    acquirer_dividends_paid: float     # dividends owed on the short acquirer shares
    target_dividends_received: float   # dividends received on the long target (0 for LITE)
    borrow_fee: float
    short_rebate_income: float
    cash_interest_income: float
    margin_interest: float
    commissions: float
    spread_and_slippage: float
    n_dividends: int
    years: float

    @property
    def total_cost(self) -> float:
        return (self.acquirer_dividends_paid - self.target_dividends_received + self.borrow_fee
                - self.short_rebate_income - self.cash_interest_income + self.margin_interest
                + self.commissions + self.spread_and_slippage)


def carry(deal: Deal, target_shares: float, acquirer_short_shares: float, target_price: float,
          acquirer_price: float, start: dt.date, end: dt.date, borrow_fee: float, equity: float,
          broker: BrokerTerms = BrokerTerms(), include_exit_costs: bool = False) -> CarryBreakdown:
    """Carry from ``start`` to ``end`` at constant prices (path-dependent carry is handled in scenarios/MC).

    Cash mechanics of a margin account: buying the target uses cash; the short sale credits proceeds.
    cash = equity - target notional + short proceeds. A positive cash balance earns the IBKR blended credit rate,
    the short proceeds earn the short-proceeds rate (0% under USD 100k), a negative balance pays margin interest.
    """
    years = max((end - start).days / 365.25, 0.0)
    long_val = target_shares * target_price
    short_val = acquirer_short_shares * acquirer_price
    exd = deal.acquirer_dividend.ex_dates_between(start, end)
    div_paid = len(exd) * deal.acquirer_dividend.amount * acquirer_short_shares
    tex = deal.target_dividend.ex_dates_between(start, end)
    div_recv = len(tex) * deal.target_dividend.amount * target_shares
    borrow = borrow_fee * short_val * years
    nav = equity
    rebate = broker.short_proceeds_rate(short_val, nav) * short_val * years
    cash_ex_proceeds = equity - long_val           # IBKR pays short-proceeds interest separately from free cash
    cash_income = broker.credit_rate(cash_ex_proceeds, nav) * max(cash_ex_proceeds, 0.0) * years
    loan = max(-(equity - long_val + short_val), 0.0)  # proceeds offset debits in the cash balance
    margin_int = broker.debit_rate(loan) * loan * years
    legs = [(target_shares, target_price), (acquirer_short_shares, acquirer_price)]
    n_trips = 2 if include_exit_costs else 1
    comm = n_trips * sum(broker.commission(q, p) for q, p in legs)
    slip = n_trips * sum(q * p for q, p in legs) * (broker.half_spread_bps + broker.slippage_bps) / 1e4
    return CarryBreakdown(div_paid, div_recv, borrow, rebate, cash_income, margin_int, comm, slip,
                          len(exd), years)
