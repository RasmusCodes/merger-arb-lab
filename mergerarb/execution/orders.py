"""Order plans: the exportable specification of a ratio-hedged pair (JSON / CSV), with validation."""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
import uuid

from ..deal import Deal


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

    def idempotency_key(self) -> str:
        """Same legs on the same trading day = same key -> a second submission is refused."""
        return hashlib.sha256(f"{self.plan_hash()}|{self.created_utc[:10]}".encode()).hexdigest()[:16]

    def order_ref(self, leg: str, child: int = 0) -> str:
        return f"MA:{self.plan_id[:8]}:{leg}:{child}"

    def hedge_residual(self) -> float:
        return self.acquirer.quantity - self.ratio * self.target.quantity

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["plan_hash"] = self.plan_hash()
        d["idempotency_key"] = self.idempotency_key()
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
                    acquirer_shares: int | None = None, note: str = "") -> OrderPlan:
    """Long ``target_shares`` of the target, short ratio x that many acquirer shares (exact when possible)."""
    if target_shares <= 0:
        raise ValueError("target_shares must be positive")
    if order_type not in ("LMT", "MKT"):
        raise ValueError("order_type must be LMT or MKT")
    if order_type == "LMT" and (not target_limit or not acquirer_limit):
        raise ValueError("limit orders need both limit prices")
    qa = acquirer_shares if acquirer_shares is not None else int(round(target_shares * deal.exchange_ratio))
    legs = (
        OrderLeg("acquirer", deal.acquirer.ticker, "SELL", qa, order_type,
                 round(acquirer_limit, 2) if acquirer_limit else None, tif, outside_rth,
                 primary_exchange=deal.acquirer.exchange or "NASDAQ"),
        OrderLeg("target", deal.target.ticker, "BUY", int(target_shares), order_type,
                 round(target_limit, 2) if target_limit else None, tif, outside_rth,
                 primary_exchange=deal.target.exchange or "NASDAQ"),
    )
    return OrderPlan(str(uuid.uuid4()), deal.id, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                     deal.exchange_ratio, legs, dict(reference_prices), note)


def plan_from_dict(d: dict) -> OrderPlan:
    legs = tuple(OrderLeg(**l) for l in d["legs"])
    p = OrderPlan(d["plan_id"], d["deal_id"], d["created_utc"], float(d["ratio"]), legs, d.get("reference_prices", {}),
                  d.get("note", ""))
    if "plan_hash" in d and d["plan_hash"] != p.plan_hash():
        raise ValueError("plan hash mismatch: the plan was modified after export")
    return p


def validate_plan(plan: OrderPlan, mode: str, max_order_notional: float, band: float) -> list[tuple[str, str, str]]:
    """Static checks that need no broker: (check, status PASS/WARN/FAIL, message)."""
    out = []
    t, a = plan.target, plan.acquirer
    out.append(("legs", "PASS" if (t.action, a.action) == ("BUY", "SELL") else "FAIL",
                f"{t.action} {t.quantity} {t.symbol} / {a.action} {a.quantity} {a.symbol}"))
    res = plan.hedge_residual()
    out.append(("exact ratio", "PASS" if abs(res) < 1e-9 else "WARN",
                "exact contractual hedge" if abs(res) < 1e-9 else
                f"hedge residual {res:+.2f} {a.symbol} shares vs {plan.ratio} x {t.quantity} (fractional shorts are not "
                f"available; use target quantities that make the hedge whole)"))
    for l in plan.legs:
        if l.order_type == "MKT" and mode == "live":
            out.append((f"{l.symbol} order type", "FAIL", "market orders are disabled in live mode; use limits"))
        ref = plan.reference_prices.get(l.symbol)
        if l.limit_price and ref:
            dev = l.limit_price / ref - 1
            out.append((f"{l.symbol} price band", "PASS" if abs(dev) <= band else "FAIL",
                        f"limit {l.limit_price} is {dev:+.2%} from reference {ref} (band +/-{band:.0%})"))
        n = l.notional(ref)
        out.append((f"{l.symbol} notional", "PASS" if n <= max_order_notional else "FAIL",
                    f"{n:,.0f} USD (cap {max_order_notional:,.0f})"))
    if t.outside_rth or a.outside_rth:
        out.append(("session", "WARN", "outside-RTH flag set: thin liquidity, wide spreads"))
    return out
