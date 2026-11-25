"""Deal terms: a typed, validated view of a deal YAML file (see deals/*.yaml).

The YAML holds the *verified* contractual terms (exchange ratio, cash, dates, fees, dividend policy) with citations.
Anything that is an assumption (e.g. the expected closing date inside a disclosed window) is labelled as such in the
YAML and can be overridden at run time, never edited silently.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
from typing import Any

import yaml

DEALS_DIR = pathlib.Path(__file__).resolve().parents[1] / "deals"


def _date(x: Any) -> dt.date | None:
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return dt.date.fromisoformat(str(x))


@dataclasses.dataclass(frozen=True)
class Security:
    ticker: str
    name: str
    cik: int | None = None
    exchange: str | None = None


@dataclasses.dataclass(frozen=True)
class DividendPolicy:
    amount: float                      # per share, per payment
    frequency_per_year: int = 0
    last_ex_dates: tuple[dt.date, ...] = ()
    allowed: bool = True

    def ex_dates_between(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """Project future ex-dates by stepping the last known ex-date forward by 12/frequency months.

        Ex-dates strictly after ``start`` and on/before ``end`` are returned. This is a projection, not a declared
        schedule; MRVL has paid on a stable quarterly cycle (Jan/Apr/Jul/Oct) through 2026.
        """
        if not self.allowed or self.amount <= 0 or self.frequency_per_year <= 0 or not self.last_ex_dates:
            return []
        step = 12 // self.frequency_per_year
        d = max(self.last_ex_dates)
        out = []
        for _ in range(200):
            m = d.month - 1 + step
            y = d.year + m // 12
            m = m % 12 + 1
            day = min(d.day, 28)
            d = dt.date(y, m, day)
            if d > end:
                break
            if d > start:
                out.append(d)
        return out


@dataclasses.dataclass(frozen=True)
class Deal:
    id: str
    name: str
    acquirer: Security
    target: Security
    exchange_ratio: float
    cash_per_share: float
    consideration_type: str
    announced: dt.date
    unaffected_date: dt.date
    expected_close: dt.date
    expected_close_text: str
    outside_date: dt.date
    outside_date_extensions: tuple[dt.date, ...]
    unaffected_target: float
    unaffected_acquirer: float
    acquirer_dividend: DividendPolicy
    target_dividend: DividendPolicy
    termination_fees: dict[str, float]
    shares: dict[str, float]
    approvals: dict[str, str]
    sources: dict[str, str]
    status: str = "pending"
    raw: dict = dataclasses.field(default_factory=dict, repr=False, compare=False)

    # ----- derived -------------------------------------------------------------------------------------------
    @property
    def final_outside_date(self) -> dt.date:
        return max((self.outside_date, *self.outside_date_extensions))

    def implied_value(self, acquirer_price: float) -> float:
        """Value of the merger consideration per target share at a given acquirer price."""
        return self.exchange_ratio * acquirer_price + self.cash_per_share

    def target_fully_diluted_shares(self) -> float:
        return float(self.shares.get("target_common", 0)) + float(self.shares.get("target_series_a_preferred", 0))

    def regulatory_fee_per_target_share(self) -> float:
        n = self.target_fully_diluted_shares()
        fee = float(self.termination_fees.get("regulatory_reverse_fee", 0.0))
        return fee / n if n else 0.0

    def with_overrides(self, **kw: Any) -> "Deal":
        """Return a copy with explicit run-time overrides (e.g. a different expected close or ratio)."""
        return dataclasses.replace(self, **kw)

    def validate(self) -> list[str]:
        """Basic consistency checks. Returns a list of problems (empty = OK)."""
        p = []
        if self.exchange_ratio <= 0 and self.cash_per_share <= 0:
            p.append("consideration has neither a positive ratio nor cash")
        if self.expected_close <= self.announced:
            p.append("expected close is not after announcement")
        if self.expected_close > self.final_outside_date:
            p.append("expected close is after the final outside date (needs a negotiated extension)")
        if self.unaffected_date >= self.announced:
            p.append("unaffected date is not before announcement")
        if self.consideration_type == "stock" and self.cash_per_share:
            p.append("stock deal with a cash component: use type 'cash_and_stock'")
        return p


def load_deal(path_or_id: str | pathlib.Path = "mrvl_lite") -> Deal:
    p = pathlib.Path(path_or_id)
    if not p.suffix:
        p = DEALS_DIR / f"{path_or_id}.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    c, d = raw["consideration"], raw["dates"]
    divs = raw.get("dividends", {})

    def div(x: dict | None) -> DividendPolicy:
        x = x or {}
        return DividendPolicy(
            amount=float(x.get("amount", 0.0)),
            frequency_per_year=int(x.get("frequency_per_year", 0)),
            last_ex_dates=tuple(_date(v) for v in x.get("last_ex_dates", []) or []),
            allowed=bool(x.get("allowed", True)) if not isinstance(x.get("allowed"), str) else True,
        )

    deal = Deal(
        id=raw["id"],
        name=raw["name"],
        acquirer=Security(**{k: raw["acquirer"].get(k) for k in ("ticker", "name", "cik", "exchange")}),
        target=Security(**{k: raw["target"].get(k) for k in ("ticker", "name", "cik", "exchange")}),
        exchange_ratio=float(c.get("exchange_ratio", 0.0)),
        cash_per_share=float(c.get("cash_per_share", 0.0)),
        consideration_type=str(c.get("type", "stock")),
        announced=_date(d["announced"]),
        unaffected_date=_date(d["unaffected_date"]),
        expected_close=_date(d["expected_close"]),
        expected_close_text=str(d.get("expected_close_text", "")),
        outside_date=_date(d["outside_date"]),
        outside_date_extensions=tuple(_date(x) for x in d.get("outside_date_extensions", []) or []),
        unaffected_target=float(raw["unaffected_prices"]["target"]),
        unaffected_acquirer=float(raw["unaffected_prices"]["acquirer"]),
        acquirer_dividend=div(divs.get("acquirer")),
        target_dividend=div(divs.get("target")),
        termination_fees={k: float(v) for k, v in (raw.get("termination_fees_usd") or {}).items()},
        shares={k: float(v) for k, v in (raw.get("shares") or {}).items()},
        approvals=dict(raw.get("approvals") or {}),
        sources=dict(raw.get("sources") or {}),
        status=str(raw.get("status", "pending")),
        raw=raw,
    )
    return deal
