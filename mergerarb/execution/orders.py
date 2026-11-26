"""Order plans: the exportable specification of a ratio-hedged pair (JSON / CSV), with validation.

A plan is only valid when the acquirer leg is an exact whole number of shares (target shares x ratio): there is no
rounding of any kind. For MRVL/LITE (ratio 4.5) that means multiples of 2 LITE / 9 MRVL."""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import uuid
from fractions import Fraction

from ..deal import Deal


def _frac(x: float) -> Fraction:
    return Fraction(repr(float(x)))


def exact_lot(ratio: float) -> int:
    """Smallest whole target quantity whose acquirer leg (ratio x quantity) is a whole number of shares."""
    return _frac(ratio).denominator


def exact_acquirer_shares(target_shares: int, ratio: float) -> int:
    q = _frac(ratio) * int(target_shares)
    if q.denominator != 1:
        lot = exact_lot(ratio)
        raise ValueError(f"{target_shares} target shares x ratio {ratio} = {float(q):g} acquirer shares is not a whole "
                         f"number; use multiples of {lot} target shares ({lot} / {int(_frac(ratio) * lot)})")
    return int(q)


def _positive_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _whole(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


@dataclasses.dataclass(frozen=True)
class OrderLeg:
    leg: str                 # "target" | "acquirer"
    symbol: str
    action: str              # "BUY" | "SELL" (SELL of an unowned stock = short sale)
    quantity: int
    order_type: str          # "LMT" | "MKT" (market orders only allowed in paper mode)
    limit_price: float | None
    tif: str = "DAY"
    outside_rth: bool = False
    exchange: str = "SMART"
    primary_exchange: str = "NASDAQ"
    currency: str = "USD"

    def notional(self, ref_price: float | None = None) -> float:
        p = self.limit_price if self.limit_price else (ref_price or 0.0)
        return abs(self.quantity) * p


@dataclasses.dataclass(frozen=True)
class OrderPlan:
    plan_id: str
    deal_id: str
    created_utc: str
    ratio: float
    legs: tuple[OrderLeg, ...]
    reference_prices: dict
    note: str = ""

    @property
    def target(self) -> OrderLeg:
        return next(l for l in self.legs if l.leg == "target")

    @property
    def acquirer(self) -> OrderLeg:
        return next(l for l in self.legs if l.leg == "acquirer")

    def plan_hash(self) -> str:
        body = json.dumps({"deal": self.deal_id, "ratio": self.ratio,
                           "legs": [dataclasses.asdict(l) for l in self.legs]}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()

    @property
    def short_hash(self) -> str:
        return self.plan_hash()[:8]

    def idempotency_key(self, trade_date: str | None = None) -> str:
        """Same legs on the same trading day = same key -> a second submission is refused. The executor passes the
        actual (New York) trade date, so editing ``created_utc`` cannot get around it."""
        return hashlib.sha256(f"{self.plan_hash()}|{trade_date or self.created_utc[:10]}".encode()).hexdigest()[:16]

    @property
    def ref_prefix(self) -> str:
        return f"MA:{self.plan_id[:8]}:"

    def order_ref(self, leg: str, child: int = 0) -> str:
        return f"{self.ref_prefix}{leg}:{child}"

    def hedge_residual(self) -> float:
        return float(self.acquirer.quantity - _frac(self.ratio) * self.target.quantity)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["plan_hash"] = self.plan_hash()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["plan_id", "plan_hash", "deal_id", "leg", "symbol", "exchange", "primary_exchange", "currency",
                    "action", "quantity", "order_type", "limit_price", "tif", "outside_rth", "order_ref", "ratio",
                    "reference_price", "notional"])
        for l in self.legs:
            ref = self.reference_prices.get(l.symbol)
            w.writerow([self.plan_id, self.short_hash, self.deal_id, l.leg, l.symbol, l.exchange, l.primary_exchange,
                        l.currency, l.action, l.quantity, l.order_type, l.limit_price, l.tif, l.outside_rth,
                        self.order_ref(l.leg), self.ratio, ref, round(l.notional(ref), 2)])
        return buf.getvalue()


def build_pair_plan(deal: Deal, target_shares: int, target_limit: float | None, acquirer_limit: float | None,
                    reference_prices: dict, order_type: str = "LMT", tif: str = "DAY", outside_rth: bool = False,
                    acquirer_shares: int | None = None, note: str = "", plan_id: str | None = None,
                    created_utc: str | None = None) -> OrderPlan:
    """Long ``target_shares`` of the target, short exactly ratio x that many acquirer shares (else ValueError)."""
    if isinstance(target_shares, bool) or not isinstance(target_shares, (int, float)) or target_shares <= 0 \
            or int(target_shares) != target_shares:
        raise ValueError("target_shares must be a positive whole number")
    n = int(target_shares)
    if order_type not in ("LMT", "MKT"):
        raise ValueError("order_type must be LMT or MKT")
    if tif != "DAY":
        raise ValueError("only DAY orders are supported")
    if outside_rth:
        raise ValueError("outside-RTH orders are not supported")
    if order_type == "LMT" and not (_positive_number(target_limit) and _positive_number(acquirer_limit)):
        raise ValueError("limit orders need both limit prices (positive numbers)")
    qa = exact_acquirer_shares(n, deal.exchange_ratio)
    if acquirer_shares is not None and int(acquirer_shares) != qa:
        raise ValueError(f"acquirer_shares {acquirer_shares} is not the exact hedge {qa} (= {n} x {deal.exchange_ratio})")
    legs = (
        OrderLeg("acquirer", deal.acquirer.ticker, "SELL", qa, order_type,
                 round(float(acquirer_limit), 2) if order_type == "LMT" else None, "DAY", False,
                 primary_exchange=deal.acquirer.exchange or "NASDAQ"),
        OrderLeg("target", deal.target.ticker, "BUY", n, order_type,
                 round(float(target_limit), 2) if order_type == "LMT" else None, "DAY", False,
                 primary_exchange=deal.target.exchange or "NASDAQ"),
    )
    return OrderPlan(plan_id or str(uuid.uuid4()), deal.id,
                     created_utc or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     float(deal.exchange_ratio), legs, dict(reference_prices or {}), note)


def plan_from_dict(d: dict, require_hash: bool = True) -> OrderPlan:
    if not isinstance(d, dict):
        raise ValueError("plan must be a JSON object")
    try:
        legs = tuple(OrderLeg(**l) for l in d["legs"])
        p = OrderPlan(str(d["plan_id"]), str(d["deal_id"]), str(d["created_utc"]), float(d["ratio"]), legs,
                      dict(d.get("reference_prices") or {}), str(d.get("note", "")))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"malformed plan: {e}") from e
    if "plan_hash" not in d:
        if require_hash:
            raise ValueError("plan_hash missing: send the exported plan with its hash (edits are detected through it)")
    elif d["plan_hash"] != p.plan_hash():
        raise ValueError("plan hash mismatch: the plan was modified after export")
    return p


def verify_plan_integrity(plan: OrderPlan, deal: Deal, mode: str = "paper") -> list[str]:
    """Server-side integrity: the plan must be exactly the canonical plan this deal file produces for its target
    quantity and limits (ratio, actions, symbols, exact hedge, DAY, regular hours). Returns problems (empty = OK)."""
    p: list[str] = []
    try:
        uuid.UUID(str(plan.plan_id))
    except ValueError:
        p.append("plan_id must be a UUID (it names the orders: MA:<plan>:<leg>:<n>)")
    if plan.deal_id != deal.id:
        p.append(f"deal {plan.deal_id!r} is not this service's deal {deal.id!r}")
    if abs(float(plan.ratio) - float(deal.exchange_ratio)) > 1e-12:
        p.append(f"ratio {plan.ratio} differs from the deal file's exchange ratio {deal.exchange_ratio}")
    if sorted(l.leg for l in plan.legs) != ["acquirer", "target"]:
        return p + ["a plan needs exactly one acquirer leg and one target leg"]
    t, a = plan.target, plan.acquirer
    for l in plan.legs:
        if not _whole(l.quantity) or l.quantity <= 0:
            p.append(f"{l.leg} quantity must be a positive whole number, got {l.quantity!r}")
        if l.tif != "DAY":
            p.append(f"{l.leg} tif {l.tif!r} is not allowed (DAY only)")
        if l.outside_rth:
            p.append(f"{l.leg}: outside-RTH orders are not allowed")
        if l.order_type not in ("LMT", "MKT"):
            p.append(f"{l.leg}: order type {l.order_type!r} is not allowed")
        elif l.order_type == "MKT" and mode == "live":
            p.append(f"{l.leg}: market orders are disabled in live mode")
    if (t.action, a.action) != ("BUY", "SELL"):
        p.append(f"actions must be BUY target / SELL acquirer, got {t.action} / {a.action}")
    if (t.symbol, a.symbol) != (deal.target.ticker, deal.acquirer.ticker):
        p.append(f"symbols must be {deal.target.ticker} / {deal.acquirer.ticker}")
    if p:
        return p
    try:
        exact = exact_acquirer_shares(t.quantity, deal.exchange_ratio)
    except ValueError as e:
        return [str(e)]
    if a.quantity != exact:
        return [f"acquirer quantity {a.quantity} is not the exact hedge {exact} (= {t.quantity} x {deal.exchange_ratio})"]
    if t.order_type != a.order_type:
        return ["both legs must use the same order type"]
    try:
        canon = build_pair_plan(deal, t.quantity, t.limit_price, a.limit_price, plan.reference_prices,
                                order_type=t.order_type, plan_id=plan.plan_id, created_utc=plan.created_utc,
                                note=plan.note)
    except ValueError as e:
        return [str(e)]
    if canon.plan_hash() != plan.plan_hash():
        p.append("plan differs from the canonical plan for this deal (exchange, primary exchange, currency or "
                 "limit-price rounding was changed)")
    return p


def validate_plan(plan: OrderPlan, mode: str, max_order_notional: float, band: float) -> list[tuple[str, str, str]]:
    """Static checks that need no broker: (check, status PASS/WARN/FAIL, message)."""
    out = []
    t, a = plan.target, plan.acquirer
    out.append(("legs", "PASS" if (t.action, a.action) == ("BUY", "SELL") else "FAIL",
                f"{t.action} {t.quantity} {t.symbol} / {a.action} {a.quantity} {a.symbol}"))
    whole = all(_whole(l.quantity) and l.quantity > 0 for l in plan.legs)
    exact = whole and _frac(plan.ratio) * t.quantity == a.quantity
    out.append(("exact ratio", "PASS" if exact else "FAIL",
                "exact contractual hedge" if exact else
                f"{a.quantity} {a.symbol} is not exactly {plan.ratio} x {t.quantity} {t.symbol}: whole-share plans "
                f"only (multiples of {exact_lot(plan.ratio)} {t.symbol}); nothing is rounded"))
    for l in plan.legs:
        if l.order_type == "MKT" and mode == "live":
            out.append((f"{l.symbol} order type", "FAIL", "market orders are disabled in live mode; use limits"))
        if l.tif != "DAY" or l.outside_rth:
            out.append((f"{l.symbol} time in force", "FAIL", f"tif={l.tif} outside_rth={l.outside_rth}: DAY orders "
                                                             f"in regular hours only"))
        ref = plan.reference_prices.get(l.symbol)
        if l.limit_price and ref:
            dev = l.limit_price / ref - 1
            out.append((f"{l.symbol} price band", "PASS" if abs(dev) <= band else "FAIL",
                        f"limit {l.limit_price} is {dev:+.2%} from reference {ref} (band +/-{band:.0%})"))
        n = l.notional(ref)
        out.append((f"{l.symbol} notional", "PASS" if n <= max_order_notional else "FAIL",
                    f"{n:,.0f} USD (cap {max_order_notional:,.0f})"))
    return out
