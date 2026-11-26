"""Pre-flight checks, explicit approval, budgeted leg-managed submission, fill monitoring, cancellation and recovery.

Flow:  plan -> preflight(plan) -> approve(plan, report, typed) -> submit(plan, approval) -> pump() ... -> FILLED

Each leg's approved quantity is a fixed budget. The tool places by itself only the hedge leg against filled short
shares (short_first), inside that budget, and only while the plan is WORKING or PARTIAL. Quantity of an order that
ends unfilled (rejection, DAY expiry, a cancel made outside this tool) is never re-sent automatically: it becomes an
unplaced residual and the plan goes to NEEDS_ACTION until approve_residual() (a fresh typed approval), cancel_all()
or close().

Plan statuses
  WORKING / PARTIAL   orders working (PARTIAL = something filled)
  NEEDS_ACTION        a decision is needed; nothing is placed, monitoring continues
  CANCELLING          cancel requested; nothing is placed, monitored until every order is terminal
  FILLED / CANCELLED / FAILED / CLOSED   terminal, never revived
  DISCONNECTED        connection lost; facts unknown until recover()
  UNRECONCILED        rebuilt from the journal at start-up; recover() reconciles it with the broker first
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pandas as pd

from .broker import READ_ONLY_HINT, BrokerClient, ExecFill, OrderHandle, ShortInfo
from .config import ExecutionConfig
from .journal import Journal
from .orders import OrderLeg, OrderPlan, exact_lot, plan_from_dict, validate_plan, verify_plan_integrity

# NYSE/Nasdaq full-day closures (2026-11 .. 2027-12); the broker's own rejection remains the final backstop
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
            "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}   # 13:00 ET

ACTIVE = ("WORKING", "PARTIAL")
MONITORED = ("WORKING", "PARTIAL", "NEEDS_ACTION", "CANCELLING")
TERMINAL = ("FILLED", "CANCELLED", "FAILED", "CLOSED")
ENDED = ("Cancelled", "ApiCancelled", "Inactive")        # ended at the broker with quantity possibly unfilled
SHORT_NOT_EXECUTED = "short not executed"
EPS = 1e-9


@dataclasses.dataclass
class Check:
    name: str
    status: str      # PASS / WARN / FAIL
    message: str


@dataclasses.dataclass
class PreflightReport:
    plan_hash: str
    checks: list[Check]
    context: dict
    created_utc: str

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "FAIL"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def only_signal_fails(self) -> bool:
        return bool(self.failures) and all(c.name == "model signal" for c in self.failures)


@dataclasses.dataclass(frozen=True)
class Approval:
    plan_hash: str
    account: str
    mode: str
    typed: str
    created_utc: str
    override_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ResidualApproval:
    plan_hash: str
    leg: str
    quantity: int
    limit_price: float | None
    account: str
    mode: str
    typed: str
    created_utc: str


@dataclasses.dataclass
class LegState:
    leg: OrderLeg
    handles: list[OrderHandle] = dataclasses.field(default_factory=list)
    reauthorized: int = 0          # residual shares re-placed under explicit residual approvals

    @property
    def budget(self) -> int:
        return int(self.leg.quantity)

    @property
    def filled(self) -> float:
        return sum(h.filled for h in self.handles)

    @property
    def live(self) -> list[OrderHandle]:
        return [h for h in self.handles if not h.is_done]

    @property
    def live_remaining(self) -> float:
        return sum(h.unfilled for h in self.live)

    @property
    def placed(self) -> float:
        return float(sum(h.quantity for h in self.handles))

    @property
    def unplaced(self) -> float:
        """Shares of orders that ended unfilled and have not been re-approved."""
        return max(sum(h.unfilled for h in self.handles if h.status in ENDED) - self.reauthorized, 0.0)

    @property
    def committed(self) -> float:
        """Budget already used: filled, working, unknown or parked as an unplaced residual."""
        return self.placed - self.reauthorized

    @property
    def avg_price(self) -> float:
        f = self.filled
        return sum(h.filled * h.avg_fill_price for h in self.handles) / f if f else 0.0


@dataclasses.dataclass
class ExecutionState:
    plan: OrderPlan
    approval: Approval | None
    status: str
    legs: dict[str, LegState]
    account: str | None = None
    mode: str = "paper"
    sequencing: str = "short_first"
    alerts: list[str] = dataclasses.field(default_factory=list)
    action_required: list[str] = dataclasses.field(default_factory=list)
    sticky: list[str] = dataclasses.field(default_factory=list)       # reasons only close() clears
    cancel_requested: set[str] = dataclasses.field(default_factory=set)
    cancel_sent: set[str] = dataclasses.field(default_factory=set)
    cancel_reason: str | None = None
    resume_status: str | None = None
    imbalance_since: dt.datetime | None = None
    imbalance_shares: float = 0.0
    imbalance_usd: float = 0.0
    ref_px: float = 0.0
    unhedged: bool = False
    unhedged_episode: int = 0
    disconnects: int = 0
    reconciled: bool = True
    started_utc: str = ""
    next_child: dict[str, int] = dataclasses.field(default_factory=lambda: {"acquirer": 0, "target": 0})
    seen: dict[str, tuple] = dataclasses.field(default_factory=dict)  # order_ref -> last journalled facts
    ended: set[str] = dataclasses.field(default_factory=set)          # order refs whose end was handled
    alert_keys: set[str] = dataclasses.field(default_factory=set)

    def summary(self) -> dict:
        return {"plan_id": self.plan.plan_id, "plan_hash": self.plan.short_hash, "status": self.status,
                "reconciled": self.reconciled, "account": self.account, "mode": self.mode,
                "sequencing": self.sequencing, "action_required": list(self.action_required),
                "unhedged": self.unhedged, "alerts": list(self.alerts),
                "imbalance_acquirer_shares": self.imbalance_shares, "imbalance_usd": round(self.imbalance_usd, 2),
                "residuals": {k: v.unplaced for k, v in self.legs.items() if v.unplaced > EPS},
                "legs": {k: {"symbol": v.leg.symbol, "action": v.leg.action, "target_qty": v.leg.quantity,
                             "budget": v.budget, "placed": v.placed, "filled": v.filled, "working": v.live_remaining,
                             "unplaced": v.unplaced, "reauthorized": v.reauthorized, "avg_price": round(v.avg_price, 4),
                             "orders": [dataclasses.asdict(h) for h in v.handles]} for k, v in self.legs.items()}}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _age_s(iso: str) -> float:
    return (_now() - dt.datetime.fromisoformat(iso)).total_seconds()


def trade_date(now_utc: dt.datetime | None = None) -> str:
    return pd.Timestamp(now_utc or _now()).tz_convert("America/New_York").strftime("%Y-%m-%d")


def market_session(now_utc: dt.datetime | None = None) -> tuple[bool, str]:
    t = pd.Timestamp(now_utc or _now()).tz_convert("America/New_York")
    d = t.strftime("%Y-%m-%d")
    if t.weekday() >= 5 or d in HOLIDAYS:
        return False, f"market closed ({t:%a %Y-%m-%d})"
    close = (13, 0) if d in EARLY_CLOSE else (16, 0)
    if (t.hour, t.minute) < (9, 30) or (t.hour, t.minute) >= close:
        return False, f"outside regular hours ({t:%H:%M} ET; session 09:30-{close[0]}:{close[1]:02d})"
    return True, f"regular session ({t:%H:%M} ET)"


def _is_rate(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def _exec_summary(execs: list[ExecFill], prefix: str) -> dict[str, tuple[float, float]]:
    """Filled quantity and average price per order from executions (a correction replaces the execution it fixes)."""
    groups: dict[str, dict[str, ExecFill]] = {}
    for e in execs:
        if not e.order_ref.startswith(prefix):
            continue
        base = e.exec_id.rpartition(".")[0] or e.exec_id
        g = groups.setdefault(e.order_ref, {})
        if base not in g or e.exec_id > g[base].exec_id:
            g[base] = e
    out = {}
    for ref, g in groups.items():
        fills = list(g.values())
        qty = sum(f.shares for f in fills)
        vwap = sum(f.shares * f.price for f in fills) / qty if qty else 0.0
        top = max(fills, key=lambda f: f.cum_qty)
        out[ref] = (top.cum_qty, top.avg_price or vwap) if top.cum_qty > 0 else (qty, vwap)
    return out


def _merge(h: OrderHandle, ex: tuple[float, float] | None) -> OrderHandle:
    """Broker order facts + executions. Orders that completed while disconnected arrive with filled=0."""
    h = dataclasses.replace(h)
    if ex and ex[0] > h.filled + EPS:
        h.filled, h.avg_fill_price = ex
        h.remaining = max(h.quantity - h.filled, 0.0)
    if h.quantity > 0 and h.filled >= h.quantity - EPS and h.status != "Filled":
        h.status = "Filled"
    return h


def _journal_status(evs: list[dict]) -> tuple[str, str]:
    seq = []
    for r in evs:
        e = r.get("event")
        s = r.get("status") if e == "plan_status" else {"plan_filled": "FILLED", "plan_cancelled": "CANCELLED",
                                                        "plan_failed": "FAILED", "plan_closed": "CLOSED"}.get(e)
        if s:
            seq.append(s)
    last = seq[-1] if seq else "PARTIAL"
    resume = next((s for s in reversed(seq) if s not in ("DISCONNECTED", "UNRECONCILED")), "PARTIAL")
    return last, resume


class PairExecutor:
    def __init__(self, broker: BrokerClient, cfg: ExecutionConfig = ExecutionConfig(), journal: Journal | None = None,
                 deal=None, broker_kind: str | None = None):
        self.broker, self.cfg = broker, cfg
        self.journal = journal or Journal(cfg.journal_path)
        self.deal = deal
        self.broker_kind = broker_kind or type(broker).__name__
        self.account: str | None = None
        self.state: ExecutionState | None = None
        self.backlog: list[ExecutionState] = []

    # ----- connection --------------------------------------------------------------------------------------------
    def connect(self) -> str:
        problems = self.cfg.validate()
        if problems:
            raise RuntimeError("; ".join(problems))
        if self.account and self.broker.is_connected():
            return self.account                          # idempotent: never re-open a working session
        self.broker.connect()
        accts = self.broker.accounts()
        if self.cfg.account:
            if self.cfg.account not in accts:
                raise RuntimeError(f"configured account {self.cfg.account} not among managed accounts")
            acct = self.cfg.account
        elif len(accts) == 1:
            acct = accts[0]
        else:
            raise RuntimeError(f"several managed accounts {accts}; set ExecutionConfig.account explicitly")
        self.account = acct
        self.journal.write("connected", mode=self.cfg.mode, account=acct, port=self.cfg.effective_port,
                           broker=self.broker_kind)
        return acct

    def approval_phrase(self, plan: OrderPlan) -> str:
        return f"{self.cfg.mode.upper()} {self.account} {plan.short_hash}"

    def residual_phrase(self, leg: str, quantity: int, limit_price: float | None) -> str:
        st = self._need_state()
        px = f"{limit_price:.2f}" if limit_price else "MKT"
        return f"{self.cfg.mode.upper()} {self.account} {st.plan.short_hash} {leg.upper()} {int(quantity)} @ {px}"

    def _need_state(self) -> ExecutionState:
        if self.state is None:
            raise RuntimeError("no strategy plan")
        return self.state

    def blocking_reason(self) -> str | None:
        for s in [self.state, *self.backlog]:
            if s is None:
                continue
            if not s.reconciled:
                return (f"plan {s.plan.plan_id[:8]} rebuilt from the journal is not reconciled with the broker yet: "
                        f"connect and recover() first")
            if s.status not in TERMINAL:
                return f"plan {s.plan.plan_id[:8]} is {s.status}: finish, cancel or close it first"
        return None

    def needs_pump(self) -> bool:
        st = self.state
        return bool(st and st.reconciled and st.status in MONITORED)

    def unhedged_cap(self, st: ExecutionState) -> float:
        lot_short_value = exact_lot(st.plan.ratio) * st.plan.ratio * (st.ref_px or 0.0)
        frac = self.cfg.max_unhedged_lot_fraction * lot_short_value
        return min(self.cfg.max_unhedged_usd, frac) if frac > 0 else self.cfg.max_unhedged_usd

    # ----- shared check helpers ----------------------------------------------------------------------------------
    def _account_mode_check(self) -> Check:
        paper_acct = (self.account or "").startswith("DU")
        if self.cfg.mode == "paper":
            return Check("account mode", "PASS" if paper_acct else "FAIL",
                         f"paper mode needs a paper account (DU...); connected to {self.account}")
        ok = self.cfg.live_allowed() and not paper_acct
        return Check("account mode", "PASS" if ok else "FAIL",
                     "live mode enabled by MERGERARB_ALLOW_LIVE=1 on a live account" if ok else
                     "live mode requires MERGERARB_ALLOW_LIVE=1 and a non-paper account")

    def _quote_checks(self, symbol: str, action: str, q, limit: float | None) -> list[Check]:
        ref = q.last or ((q.bid + q.ask) / 2 if q.bid and q.ask else None)
        if ref is None:
            return [Check(f"{symbol} market data", "FAIL", "no price from the broker")]
        out = []
        if q.data_type in ("live", "simulated"):
            out.append(Check(f"{symbol} market data", "PASS", f"{q.data_type} last {q.last} bid {q.bid} ask {q.ask}"))
        else:
            st = "WARN" if (self.cfg.mode == "paper" and self.cfg.allow_delayed_data_in_paper) else "FAIL"
            out.append(Check(f"{symbol} market data", st, f"{q.data_type} data - not live; limits checked against a "
                                                           f"delayed price"))
        if limit:
            dev = limit / ref - 1
            out.append(Check(f"{symbol} limit vs broker price", "PASS" if abs(dev) <= self.cfg.price_band_pct else "FAIL",
                             f"limit {limit} vs {ref} ({dev:+.2%})"))
        if action == "SELL" and q.prev_close and q.last and q.last <= 0.9 * q.prev_close:
            out.append(Check(f"{symbol} Rule 201", "WARN", "down >=10% today: short-sale circuit breaker likely active "
                                                           "(shorts execute only above the best bid)"))
        return out

    def _short_check(self, symbol: str, primary: str, needed: int) -> Check:
        try:
            si = self.broker.short_info(symbol, primary)
        except Exception as e:  # noqa: BLE001
            si = ShortInfo(symbol, None, None)
            self.journal.write("short_info_error", symbol=symbol, error=f"{type(e).__name__}: {e}")
        st, msg = si.status(needed)
        if not si.known and self.cfg.mode == "paper" and self.cfg.allow_unknown_shortability_in_paper:
            st, msg = "WARN", msg + " - accepted in paper mode only (the short goes first, so a rejection leaves no long)"
        return Check("borrow availability", st, msg)

    def _what_ifs(self, items: list[tuple[OrderLeg, int | None, float | None]]):
        out = []
        for leg, qty, lim in items:
            try:
                w = self.broker.what_if(leg, self.account, quantity=qty, limit_price=lim)
            except Exception as e:  # noqa: BLE001 - every failure blocks
                return out, f"{type(e).__name__}: {e}"
            if w is None or w.init_margin_change is None or w.maint_margin_change is None:
                return out, f"what-if for {leg.symbol} returned no margin numbers"
            out.append(w)
        return out, None

    def _margin_checks(self, wi, err, snap, ctx: dict) -> list[Check]:
        if err:
            return [Check("what-if margin", "FAIL", f"{err}. {READ_ONLY_HINT}")]
        ctx["what_if"] = [dataclasses.asdict(w) for w in wi]
        init = sum(w.init_margin_change for w in wi)
        maint = sum(w.maint_margin_change for w in wi)
        warn = "; ".join(w.warning for w in wi if w.warning)
        out = [Check("what-if margin", "PASS", f"initial +{init:,.0f}, maintenance +{maint:,.0f}, commission "
                                               f"{sum(w.commission or 0 for w in wi):.2f}"
                     + (f" | broker warning: {warn}" if warn else ""))]
        if snap is None:
            return out
        out.append(Check("available funds", "PASS" if snap.available_funds >= init else "FAIL",
                         f"available {snap.available_funds:,.0f} vs initial margin needed {init:,.0f}"))
        post_exl = snap.excess_liquidity - maint
        floor = self.cfg.min_excess_liquidity_pct * snap.net_liquidation
        out.append(Check("excess liquidity after trade", "PASS" if post_exl >= floor and snap.net_liquidation > 0
                         else "FAIL", f"{post_exl:,.0f} vs floor {floor:,.0f} ({self.cfg.min_excess_liquidity_pct:.0%} "
                                      f"of NLV)"))
        return out

    def _snapshot(self, C: list[Check], ctx: dict):
        try:
            snap = self.broker.account_snapshot(self.account)
        except Exception as e:  # noqa: BLE001
            C.append(Check("account", "FAIL", f"account summary unavailable: {e}"))
            return None
        ctx["account_snapshot"] = dataclasses.asdict(snap)
        return snap

    def _prefix_clash(self, plan: OrderPlan) -> str | None:
        ids = self.journal.submitted_plan_ids()
        if plan.plan_id in ids:
            return f"plan_id {plan.plan_id} was already submitted"
        if any(i and i[:8] == plan.plan_id[:8] for i in ids):
            return f"another plan already used the order prefix {plan.ref_prefix}"
        return None

    # ----- pre-flight --------------------------------------------------------------------------------------------
    def preflight(self, plan: OrderPlan, borrow_fee: float | None = None, signal_decision: str | None = None,
                  now_utc: dt.datetime | None = None, ssr_flag: bool | None = None) -> PreflightReport:
        C: list[Check] = []
        ctx: dict = {"kind": "plan", "mode": self.cfg.mode, "account": self.account}
        for name, st, msg in validate_plan(plan, self.cfg.mode, self.cfg.max_order_notional, self.cfg.price_band_pct):
            C.append(Check(name, st, msg))
        if self.deal is not None:
            probs = verify_plan_integrity(plan, self.deal, self.cfg.mode)
            C.append(Check("plan integrity", "FAIL" if probs else "PASS", "; ".join(probs) if probs else
                           f"canonical {self.deal.id} plan (ratio {self.deal.exchange_ratio}, DAY, regular hours)"))
        block = self.blocking_reason()
        C.append(Check("execution state", "FAIL" if block else "PASS", block or "no unfinished strategy plan"))
        if signal_decision is None or not str(signal_decision).strip():
            C.append(Check("model signal input", "FAIL", "signal_decision is required (the analysis signal, e.g. "
                                                         "TRADE or WATCH)"))
        else:
            C.append(Check("model signal", "PASS" if signal_decision == "TRADE" else "FAIL",
                           f"analysis signal is {signal_decision}" + ("" if signal_decision == "TRADE" else
                                                                      " - an override needs a written reason")))
        if not _is_rate(borrow_fee):
            C.append(Check("borrow fee", "FAIL", "borrow fee (annual rate, e.g. 0.0032) is required: check IBKR's "
                                                 "Short Stock Availability or iBorrowDesk"))
        else:
            C.append(Check("borrow fee", "PASS" if borrow_fee <= self.cfg.max_borrow_fee else "FAIL",
                           f"{borrow_fee:.2%}/yr (limit {self.cfg.max_borrow_fee:.0%})"))
        if not self.broker.is_connected():
            C.append(Check("connection", "FAIL", "not connected to TWS / IB Gateway"))
            return self._report(plan, C, ctx)
        C.append(Check("connection", "PASS", f"connected ({self.cfg.mode}, port {self.cfg.effective_port})"))
        C.append(self._account_mode_check())
        quotes = {}
        for leg in plan.legs:
            try:
                q = self.broker.quote(leg.symbol, leg.primary_exchange)
            except Exception as e:  # noqa: BLE001
                C.append(Check(f"{leg.symbol} market data", "FAIL", f"no quote: {e}"))
                continue
            quotes[leg.symbol] = q
            C.extend(self._quote_checks(leg.symbol, leg.action, q, leg.limit_price))
        if ssr_flag:
            C.append(Check(f"{plan.acquirer.symbol} Rule 201", "WARN", "external flag: short-sale restriction in effect"))
        ctx["quotes"] = {k: dataclasses.asdict(v) for k, v in quotes.items()}
        C.append(self._short_check(plan.acquirer.symbol, plan.acquirer.primary_exchange, plan.acquirer.quantity))
        snap = self._snapshot(C, ctx)
        wi, err = self._what_ifs([(leg, None, None) for leg in plan.legs])
        C.extend(self._margin_checks(wi, err, snap, ctx))
        try:
            pos = self.broker.positions(self.account)
        except Exception as e:  # noqa: BLE001
            pos = {}
            C.append(Check("positions", "FAIL", f"positions unavailable: {e}"))
        existing = {s: pos.get(s, 0.0) for s in (plan.target.symbol, plan.acquirer.symbol)}
        px = {s: (quotes[s].last or 0.0) for s in quotes}
        gross_now = sum(abs(q) * px.get(s, 0.0) for s, q in existing.items())
        gross_new = gross_now + sum(l.quantity * (l.limit_price or px.get(l.symbol, 0.0)) for l in plan.legs)
        C.append(Check("gross exposure", "PASS" if gross_new <= self.cfg.max_gross_notional else "FAIL",
                       f"{gross_new:,.0f} USD after the trade (cap {self.cfg.max_gross_notional:,.0f})"))
        if any(existing.values()):
            C.append(Check("existing positions", "WARN", f"already holding {existing}; check the combined hedge"))
        try:
            open_ma = [h for h in self.broker.open_orders() if h.symbol in existing]
            C.append(Check("duplicate orders", "FAIL" if open_ma else "PASS",
                           f"{len(open_ma)} open strategy orders in these symbols" if open_ma else "no open strategy orders"))
        except Exception as e:  # noqa: BLE001
            C.append(Check("duplicate orders", "FAIL", f"open orders unavailable: {e}"))
        if plan.idempotency_key(trade_date()) in self.journal.submitted_keys():
            C.append(Check("duplicate plan", "FAIL", "an identical plan was already submitted today"))
        clash = self._prefix_clash(plan)
        if clash:
            C.append(Check("plan id", "FAIL", clash))
        open_, why = market_session(now_utc)
        C.append(Check("session", "PASS" if open_ else ("WARN" if self.cfg.allow_outside_rth else "FAIL"), why))
        return self._report(plan, C, ctx)

    def _report(self, plan: OrderPlan, C: list[Check], ctx: dict) -> PreflightReport:
        r = PreflightReport(plan.plan_hash(), C, ctx, _iso())
        self.journal.write("preflight", plan_id=plan.plan_id, plan_hash=r.plan_hash,
                           result="ok" if r.ok else "blocked", failures=[c.__dict__ for c in r.failures])
        return r

    # ----- approval and submission -------------------------------------------------------------------------------
    def approve(self, plan: OrderPlan, report: PreflightReport, typed: str,
                override_reason: str | None = None) -> Approval:
        if report.context.get("kind", "plan") != "plan":
            raise PermissionError("not a plan pre-flight report")
        if report.plan_hash != plan.plan_hash():
            raise PermissionError("pre-flight report belongs to a different plan")
        age = _age_s(report.created_utc)
        if age > self.cfg.report_ttl_s:
            raise PermissionError(f"pre-flight report expired ({age:.0f}s old); run pre-flight again")
        if not report.ok:
            if not (report.only_signal_fails() and override_reason and len(override_reason.strip()) >= 10):
                raise PermissionError("pre-flight has failures: " + "; ".join(c.name for c in report.failures))
        if self.cfg.mode == "live" and not self.cfg.live_allowed():
            raise PermissionError("live trading is not enabled (MERGERARB_ALLOW_LIVE=1)")
        if (typed or "").strip() != self.approval_phrase(plan):
            raise PermissionError(f"confirmation phrase mismatch; type exactly: {self.approval_phrase(plan)}")
        a = Approval(plan.plan_hash(), self.account, self.cfg.mode, typed.strip(), _iso(), override_reason)
        self.journal.write("approved", plan_id=plan.plan_id, plan_hash=a.plan_hash, account=a.account, mode=a.mode,
                           override_reason=override_reason)
        return a

    def submit(self, plan: OrderPlan, approval: Approval) -> ExecutionState:
        if approval.plan_hash != plan.plan_hash() or approval.account != self.account or approval.mode != self.cfg.mode:
            raise PermissionError("approval does not match this plan / account / mode")
        age = _age_s(approval.created_utc)
        if age > self.cfg.approval_ttl_s:
            raise PermissionError(f"approval expired ({age:.0f}s old); run pre-flight and approve again")
        block = self.blocking_reason()
        if block:
            raise RuntimeError(block)
        key = plan.idempotency_key(trade_date())
        if key in self.journal.submitted_keys():
            raise RuntimeError("duplicate: this plan was already submitted today")
        clash = self._prefix_clash(plan)
        if clash:
            raise RuntimeError(clash)
        if self.deal is not None:
            probs = verify_plan_integrity(plan, self.deal, self.cfg.mode)
            if probs:
                raise PermissionError("plan rejected: " + "; ".join(probs))
        if not self.broker.is_connected():
            raise ConnectionError("not connected")
        st = ExecutionState(plan, approval, "WORKING", {"acquirer": LegState(plan.acquirer),
                                                         "target": LegState(plan.target)},
                            account=self.account, mode=self.cfg.mode, sequencing=self.cfg.sequencing,
                            started_utc=_iso())
        self.state = st
        self.journal.write("plan_submitted", plan_id=plan.plan_id, idempotency_key=key, plan=plan.to_dict(),
                           sequencing=self.cfg.sequencing, account=self.account, mode=self.cfg.mode,
                           broker=self.broker_kind, trade_date=trade_date())
        self.journal.write("plan_status", plan_id=plan.plan_id, status="WORKING", previous=None, note="submitted")
        self._place(st, "acquirer", plan.acquirer.quantity)
        if self.cfg.sequencing == "simultaneous":
            self._place(st, "target", plan.target.quantity)
        return self.pump()

    def _place(self, st: ExecutionState, leg_key: str, qty: int, limit: float | None = None,
               residual: bool = False) -> OrderHandle:
        ls = st.legs[leg_key]
        if not residual:
            if st.status not in ACTIVE:
                raise RuntimeError(f"no automatic placement while the plan is {st.status}")
            if ls.committed + qty > ls.budget + EPS:
                raise RuntimeError(f"{leg_key} budget exceeded: {ls.committed:g} + {qty} > {ls.budget}")
        child = st.next_child[leg_key]
        st.next_child[leg_key] = child + 1
        ref = st.plan.order_ref(leg_key, child)
        self.journal.write("order_attempt", plan_id=st.plan.plan_id, order_ref=ref, leg=leg_key, quantity=qty,
                           limit=limit or ls.leg.limit_price, residual=residual)
        h = self.broker.place(ls.leg, ref, self.account, quantity=qty, limit_price=limit)
        ls.handles.append(h)
        st.seen[ref] = (h.status, 0.0, "")
        self.journal.write("order_submitted", plan_id=st.plan.plan_id, order_ref=ref, leg=leg_key, symbol=ls.leg.symbol,
                           action=ls.leg.action, quantity=qty, limit=limit or ls.leg.limit_price, broker_status=h.status,
                           order_id=h.order_id, error=h.error, residual=residual)
        self._journal_changes(st)        # a marketable order can fill inside the placement window
        return h

    # ----- monitoring ------------------------------------------------------------------------------------------
    def pump(self, acquirer_price: float | None = None) -> ExecutionState:
        st = self.state
        if st is None:
            raise RuntimeError("nothing submitted")
        if not st.reconciled or st.status == "DISCONNECTED":
            return st                                    # recover() first
        if not self.broker.is_connected():
            self._to_disconnected(st)
            return st
        try:
            self._sync(st)
            if self._evaluate(st, True, acquirer_price):
                self._sync(st)
                self._evaluate(st, False, acquirer_price)
        except ConnectionError:
            self._to_disconnected(st)
        return st

    def _to_disconnected(self, st: ExecutionState) -> None:
        if st.status in TERMINAL or st.status == "DISCONNECTED":
            return
        st.resume_status = st.status
        self._set_status(st, "DISCONNECTED")
        st.disconnects += 1
        self._alert(st, f"DISCONNECTED:{st.disconnects}", "DISCONNECTED: connection to TWS / IB Gateway lost - order "
                                                          "states unknown until reconnect + recover()")

    def _sync(self, st: ExecutionState, facts: list[OrderHandle] | None = None) -> None:
        handles = facts if facts is not None else self.broker.refresh()
        prefix = st.plan.ref_prefix
        by_ref = {h.order_ref: h for h in handles if h.order_ref.startswith(prefix)}
        ex = _exec_summary(self.broker.executions(), prefix)
        known = set()
        for ls in st.legs.values():
            for i, h in enumerate(ls.handles):
                known.add(h.order_ref)
                new = by_ref.get(h.order_ref)
                if new is not None or h.order_ref in ex:
                    ls.handles[i] = _merge(new if new is not None else h, ex.get(h.order_ref))
        for ref, h in by_ref.items():
            parts = ref.split(":")
            if ref in known or len(parts) != 4 or parts[2] not in st.legs:
                continue
            st.legs[parts[2]].handles.append(_merge(h, ex.get(ref)))
            if parts[3].isdigit():
                st.next_child[parts[2]] = max(st.next_child[parts[2]], int(parts[3]) + 1)
            self.journal.write("recovered_order", plan_id=st.plan.plan_id, order_ref=ref, leg=parts[2], symbol=h.symbol,
                               action=h.action, quantity=h.quantity, limit=h.limit_price, order_id=h.order_id,
                               status=h.status, filled=h.filled)
        self._journal_changes(st)

    def _journal_changes(self, st: ExecutionState) -> None:
        for ls in st.legs.values():
            for h in ls.handles:
                cur = (h.status, round(h.filled, 6), h.error)
                prev = st.seen.get(h.order_ref)
                if prev == cur:
                    continue
                p_status, p_filled, p_err = prev if prev else (None, 0.0, "")
                if h.status != p_status or abs(h.filled - p_filled) > EPS:
                    self.journal.write("order_status", plan_id=st.plan.plan_id, order_ref=h.order_ref, symbol=h.symbol,
                                       status=h.status, filled=h.filled, remaining=h.remaining,
                                       avg_fill_price=h.avg_fill_price, error=h.error)
                if h.filled > p_filled + EPS:
                    self.journal.write("fill", plan_id=st.plan.plan_id, order_ref=h.order_ref, symbol=h.symbol,
                                       side=h.action, quantity=h.filled - p_filled, cumulative=h.filled,
                                       avg_fill_price=h.avg_fill_price)
                if h.error and h.error != p_err:
                    self.journal.write("order_error", plan_id=st.plan.plan_id, order_ref=h.order_ref, symbol=h.symbol,
                                       status=h.status, error=h.error)
                st.seen[h.order_ref] = cur

    def _set_status(self, st: ExecutionState, status: str, note: str = "") -> None:
        if st.status == status:
            return
        prev, st.status = st.status, status
        self.journal.write("plan_status", plan_id=st.plan.plan_id, status=status, previous=prev, note=note)

    def _alert(self, st: ExecutionState, key: str, msg: str) -> None:
        if key in st.alert_keys:
            return
        st.alert_keys.add(key)
        st.alerts.append(msg)
        self.journal.write("alert", plan_id=st.plan.plan_id, key=key, message=msg)

    def _needs_action(self, st: ExecutionState, reasons: list[str]) -> None:
        changed = reasons != st.action_required
        st.action_required = list(reasons)
        self._set_status(st, "NEEDS_ACTION", reasons[0] if reasons else "")
        if changed:
            self.journal.write("action_required", plan_id=st.plan.plan_id, reasons=reasons)
            for r in reasons:
                self._alert(st, "NA:" + r[:90], "NEEDS ACTION: " + r)

    def _update_imbalance(self, st: ExecutionState, acquirer_price: float | None = None) -> None:
        acq, tgt = st.legs["acquirer"], st.legs["target"]
        st.imbalance_shares = tgt.filled * st.plan.ratio - acq.filled
        st.ref_px = float(acquirer_price or acq.avg_price or st.plan.reference_prices.get(acq.leg.symbol) or 0.0
                          or acq.leg.limit_price or 0.0)
        st.imbalance_usd = abs(st.imbalance_shares) * st.ref_px

    def _unhedged_check(self, st: ExecutionState) -> None:
        cap = self.unhedged_cap(st)
        if st.imbalance_usd > cap + EPS:
            now = _now()
            st.imbalance_since = st.imbalance_since or now
            if not st.unhedged and (now - st.imbalance_since).total_seconds() >= self.cfg.unhedged_timeout_s:
                st.unhedged = True
                self._alert(st, f"UNHEDGED:{st.unhedged_episode}",
                            f"UNHEDGED: {st.imbalance_shares:+.1f} {st.plan.acquirer.symbol}-equivalent shares "
                            f"(~${st.imbalance_usd:,.0f}, cap ${cap:,.0f}) for {self.cfg.unhedged_timeout_s:.0f}s+. "
                            f"Options: wait for the lagging leg, approve a residual, cancel_all() and unwind, or hold.")
        else:
            st.imbalance_since = None
            if st.unhedged:
                st.unhedged = False
                st.unhedged_episode += 1
                self.journal.write("hedged_again", plan_id=st.plan.plan_id, imbalance_shares=st.imbalance_shares)

    def _note_ended(self, st: ExecutionState) -> None:
        for ls in st.legs.values():
            for h in ls.handles:
                if h.order_ref in st.ended or not h.is_done:
                    continue
                if h.status == "Filled":
                    if h.unfilled > EPS:           # a completed order without matching executions
                        st.ended.add(h.order_ref)
                        r = (f"{h.order_ref} ({h.symbol}) is reported Filled but executions show only {h.filled:g} of "
                             f"{h.quantity}: verify the fill in TWS, then close() with a reason")
                        st.sticky.append(r)
                        self._alert(st, "UNVERIFIED:" + h.order_ref, r)
                    continue
                st.ended.add(h.order_ref)
                if h.status == "Unknown":
                    r = (f"{h.order_ref} ({h.action} {h.quantity} {h.symbol}) is not reported by the broker any more "
                         f"(placed in an earlier TWS session, or by the simulator): check it in TWS, then close() with "
                         f"a reason")
                    st.sticky.append(r)
                    self._alert(st, "UNKNOWN:" + h.order_ref, r)
                    continue
                if h.unfilled <= EPS:
                    continue
                if h.order_ref in st.cancel_requested:
                    self.journal.write("order_cancelled", plan_id=st.plan.plan_id, order_ref=h.order_ref,
                                       symbol=h.symbol, unfilled=h.unfilled)
                else:
                    msg = (f"ORDER ENDED UNFILLED: {h.action} {h.symbol} {h.order_ref} is {h.status} with "
                           f"{h.unfilled:g} of {h.quantity} unfilled ({h.error or 'no error text: DAY expiry or a cancel '
                                                                                    'made outside this tool?'})")
                    self.journal.write("order_ended_unfilled", plan_id=st.plan.plan_id, order_ref=h.order_ref,
                                       symbol=h.symbol, status=h.status, unfilled=h.unfilled, error=h.error)
                    self._alert(st, "ENDED:" + h.order_ref, msg)

    def _short_not_executed(self, st: ExecutionState) -> bool:
        acq = st.legs["acquirer"]
        if not acq.handles or acq.live or acq.filled > EPS:
            return False
        if not all(h.status in ENDED for h in acq.handles):
            return False
        return any(h.order_ref not in st.cancel_requested for h in acq.handles)

    def _check_terminal(self, st: ExecutionState, live: list, over: list) -> None:
        if st.status == "CLOSED":
            return
        problems = []
        if live:
            problems.append(f"{len(live)} order(s) of a {st.status} plan are working at the broker")
        if over:
            problems.append("over-filled: " + ", ".join(f"{ls.leg.symbol} {ls.filled:g} > {ls.budget}" for ls in over))
        if abs(st.imbalance_shares) > EPS:
            problems.append(f"imbalance {st.imbalance_shares:+.1f} appeared after the plan was {st.status}")
        if problems:
            st.sticky.extend(p for p in problems if p not in st.sticky)
            self._needs_action(st, list(st.sticky))

    def _evaluate(self, st: ExecutionState, allow_place: bool = True, acquirer_price: float | None = None) -> bool:
        acq, tgt = st.legs["acquirer"], st.legs["target"]
        self._note_ended(st)
        self._update_imbalance(st, acquirer_price)
        live = acq.live + tgt.live
        over = [ls for ls in (acq, tgt) if ls.filled > ls.budget + EPS]
        if st.status in TERMINAL:
            self._check_terminal(st, live, over)
            return False
        if over:
            self._needs_action(st, [f"OVER-FILLED: {ls.leg.symbol} filled {ls.filled:g} > approved {ls.budget}"
                                    for ls in over] + list(st.sticky))
            self._unhedged_check(st)
            return False
        if not live and not st.sticky and acq.filled >= acq.budget - EPS and tgt.filled >= tgt.budget - EPS:
            self._set_status(st, "FILLED")
            self.journal.write("plan_filled", plan_id=st.plan.plan_id, acquirer_avg=acq.avg_price,
                               target_avg=tgt.avg_price,
                               achieved_gross_spread=st.plan.ratio * acq.avg_price / tgt.avg_price - 1
                               if tgt.avg_price else None)
            return False
        if st.status == "CANCELLING":
            self._send_cancels(st)
            if acq.live or tgt.live:
                self._unhedged_check(st)
                return False
            self._finish_cancel(st)
            return False
        if self._short_not_executed(st):
            err = next((h.error for h in reversed(acq.handles) if h.error), "")
            msg = (f"{SHORT_NOT_EXECUTED}: the {acq.leg.symbol} short ended with nothing filled "
                   f"({err or 'no error text'}) - rejected / no borrow?")
            self._alert(st, "SHORT_NOT_EXECUTED", msg)
            if not tgt.handles:
                self._set_status(st, "FAILED", msg)
                self.journal.write("plan_failed", plan_id=st.plan.plan_id, reason=msg)
                return False
            st.cancel_reason = SHORT_NOT_EXECUTED
            self._begin_cancel(st, f"{SHORT_NOT_EXECUTED}: cancelling the {tgt.leg.symbol} leg")
            if not (acq.live or tgt.live):
                self._finish_cancel(st)
            return False
        reasons = list(st.sticky)
        for ls in (acq, tgt):
            if ls.unplaced > EPS:
                reasons.append(f"{ls.leg.leg} {ls.leg.symbol}: {ls.unplaced:g} of {ls.budget} shares ended unfilled and "
                               f"are not re-sent automatically - approve_residual() to re-place them, or cancel_all() / "
                               f"close()")
        placed = False
        if reasons:
            self._needs_action(st, reasons)
        else:
            if st.status == "NEEDS_ACTION":
                st.action_required = []
                self.journal.write("action_cleared", plan_id=st.plan.plan_id)
            self._set_status(st, "PARTIAL" if (acq.filled > EPS or tgt.filled > EPS) else "WORKING")
            if allow_place and st.sequencing == "short_first":
                need = min(tgt.budget, math.floor(acq.filled / st.plan.ratio + EPS))
                gap = int(math.floor(need - tgt.committed + EPS))
                if gap >= 1:
                    self._place(st, "target", gap)
                    placed = True
        self._update_imbalance(st, acquirer_price)
        self._unhedged_check(st)
        return placed

    # ----- cancellation, recovery, closing ---------------------------------------------------------------------
    def _begin_cancel(self, st: ExecutionState, reason: str) -> None:
        if st.status != "CANCELLING":
            st.cancel_reason = st.cancel_reason or reason
            self._set_status(st, "CANCELLING", reason)
            self.journal.write("cancel_all", plan_id=st.plan.plan_id, reason=reason)
        self._send_cancels(st)

    def _send_cancels(self, st: ExecutionState) -> None:
        for ls in st.legs.values():
            for h in ls.live:
                if h.order_ref in st.cancel_sent:
                    continue
                st.cancel_requested.add(h.order_ref)
                self.journal.write("cancel_requested", plan_id=st.plan.plan_id, order_ref=h.order_ref,
                                   reason=st.cancel_reason)
                try:
                    self.broker.cancel(h)               # ConnectionError propagates; recover() re-sends
                except LookupError as e:
                    self.journal.write("cancel_failed", plan_id=st.plan.plan_id, order_ref=h.order_ref, error=str(e))
                    continue
                st.cancel_sent.add(h.order_ref)

    def _finish_cancel(self, st: ExecutionState) -> None:
        imb = st.imbalance_shares
        acq, tgt = st.legs["acquirer"], st.legs["target"]
        if abs(imb) <= EPS and not st.sticky:
            if st.cancel_reason == SHORT_NOT_EXECUTED and acq.filled <= EPS and tgt.filled <= EPS:
                self._set_status(st, "FAILED", SHORT_NOT_EXECUTED)
                self.journal.write("plan_failed", plan_id=st.plan.plan_id, reason=SHORT_NOT_EXECUTED)
            else:
                self._set_status(st, "CANCELLED", st.cancel_reason or "")
                self.journal.write("plan_cancelled", plan_id=st.plan.plan_id, reason=st.cancel_reason,
                                   imbalance_shares=0.0)
            return
        reasons = list(st.sticky)
        if abs(imb) > EPS:
            st.unhedged = True
            reasons.insert(0, f"UNHEDGED after cancel: {imb:+.1f} {acq.leg.symbol}-equivalent shares "
                              f"(~${st.imbalance_usd:,.0f}) left open - unwind or hedge it (approve_residual() can "
                              f"complete the hedge), then close() with a reason")
        self.journal.write("cancel_finished", plan_id=st.plan.plan_id, imbalance_shares=imb)
        self._needs_action(st, reasons)

    def cancel_all(self, reason: str = "user request") -> ExecutionState:
        st = self.state
        if st is None:
            raise RuntimeError("nothing to cancel")
        if st.status in TERMINAL:
            return st
        if not st.reconciled:
            raise RuntimeError("recover() first: the plan was rebuilt from the journal")
        if st.status == "DISCONNECTED":
            st.resume_status, st.cancel_reason = "CANCELLING", st.cancel_reason or reason
            self.journal.write("cancel_all", plan_id=st.plan.plan_id, reason=reason, deferred=True)
            return st
        try:
            self._begin_cancel(st, reason)
        except ConnectionError:
            self._to_disconnected(st)
            return st
        return self.pump()

    def recover(self) -> ExecutionState | None:
        """Reconcile every known plan with broker facts (orders by orderRef + executions). Never places an order and
        never revives a terminal plan; a plan that was CANCELLING has its cancels re-sent."""
        states = [s for s in [self.state, *self.backlog] if s is not None]
        if not states:
            return None
        if not self.broker.is_connected():
            self.broker.connect()
        accts = self.broker.accounts()
        if self.account is None:
            self.account = next((s.account for s in states if s.account in accts), None) or self.cfg.account or \
                (accts[0] if len(accts) == 1 else None)
        facts = {h.order_ref: h for h in self.broker.open_orders()}
        facts.update({h.order_ref: h for h in self.broker.refresh()})
        for st in states:
            if st.account and st.account not in accts:
                self._alert(st, "ACCOUNT:" + st.account, f"plan {st.plan.plan_id[:8]} was placed on account {st.account};"
                                                         f" connected to {accts}: connect to {st.account} to reconcile it")
                continue
            self._reconcile(st, list(facts.values()))
        self._promote_backlog()
        return self.state

    def _reconcile(self, st: ExecutionState, facts: list[OrderHandle]) -> None:
        prior = st.resume_status or st.status
        if prior in ("DISCONNECTED", "UNRECONCILED"):
            prior = "PARTIAL"
        found = {h.order_ref for h in facts}
        self._sync(st, facts=facts)
        for ls in st.legs.values():
            for i, h in enumerate(ls.handles):
                if h.order_ref not in found and not h.is_done:
                    ls.handles[i] = dataclasses.replace(h, status="Unknown", error=(h.error + "; " if h.error else "")
                                                        + "not reported by the broker after reconnect")
        self._journal_changes(st)
        st.cancel_sent.clear()
        st.reconciled, st.resume_status = True, None
        if st.status != prior:
            prev, st.status = st.status, prior
            self.journal.write("plan_status", plan_id=st.plan.plan_id, status=prior, previous=prev, note="recovered")
        self.journal.write("recovered", plan_id=st.plan.plan_id, status=prior,
                           orders=[h.order_ref for ls in st.legs.values() for h in ls.handles])
        self._evaluate(st, allow_place=False)

    def _promote_backlog(self) -> None:
        while self.state is not None and self.state.status in TERMINAL and self.backlog:
            self.state = self.backlog.pop(0)

    def close(self, reason: str) -> ExecutionState:
        """Acknowledge an unfinished plan (residuals, imbalance, unknown orders) and take it over manually."""
        st = self.state
        if st is None:
            raise RuntimeError("nothing to close")
        if st.status in TERMINAL:
            return st
        if not st.reconciled or st.status == "DISCONNECTED":
            raise RuntimeError("order states are unknown: reconnect and recover() first")
        if not reason or len(str(reason).strip()) < 10:
            raise PermissionError("closing a plan needs a written reason (at least 10 characters)")
        if self.broker.is_connected():
            self._sync(st)
        live = [h for ls in st.legs.values() for h in ls.live]
        if live:
            raise RuntimeError(f"{len(live)} order(s) still working - cancel_all() first")
        self._update_imbalance(st)
        self._set_status(st, "CLOSED", reason)
        self.journal.write("plan_closed", plan_id=st.plan.plan_id, reason=reason, imbalance_shares=st.imbalance_shares,
                           residuals={k: v.unplaced for k, v in st.legs.items()})
        self._promote_backlog()
        return st

    # ----- residuals: re-placing unfilled quantity needs a fresh typed approval ------------------------------------
    def residual_preflight(self, leg: str, quantity: int, limit_price: float | None = None,
                           now_utc: dt.datetime | None = None) -> PreflightReport:
        st = self._need_state()
        if leg not in st.legs:
            raise ValueError("leg must be 'acquirer' or 'target'")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or int(quantity) != quantity \
                or quantity < 1:
            raise ValueError("quantity must be a positive whole number")
        qty, ls = int(quantity), st.legs[leg]
        lim = round(float(limit_price), 2) if limit_price is not None else ls.leg.limit_price
        ctx: dict = {"kind": "residual", "leg": leg, "quantity": qty, "limit_price": lim}
        C: list[Check] = []
        ok_state = st.reconciled and st.status == "NEEDS_ACTION"
        C.append(Check("plan status", "PASS" if ok_state else "FAIL", f"plan is {st.status}" +
                       ("" if ok_state else " - residual approvals are only for a NEEDS_ACTION plan")))
        if ok_state and self.broker.is_connected():
            try:
                self._sync(st)
            except ConnectionError:
                pass
        C.append(Check("residual quantity", "PASS" if qty <= ls.unplaced + EPS else "FAIL",
                       f"{qty} vs {ls.unplaced:g} unplaced {ls.leg.symbol} shares (budget {ls.budget}, filled "
                       f"{ls.filled:g}, working {ls.live_remaining:g})"))
        if leg == "target" and st.sequencing == "short_first":
            acq = st.legs["acquirer"]
            room = math.floor(acq.filled / st.plan.ratio + EPS) - (ls.filled + ls.live_remaining)
            C.append(Check("hedge order", "PASS" if qty <= room + EPS else "FAIL",
                           f"{ls.leg.symbol} is only bought against filled {acq.leg.symbol} shares: room {room:g}"))
        if ls.leg.order_type == "LMT" and not (lim and lim > 0):
            C.append(Check("limit price", "FAIL", "a positive limit price is required"))
        if ls.leg.order_type == "MKT" and self.cfg.mode == "live":
            C.append(Check("order type", "FAIL", "market orders are disabled in live mode"))
        if not self.broker.is_connected():
            C.append(Check("connection", "FAIL", "not connected to TWS / IB Gateway"))
        else:
            C.append(Check("connection", "PASS", f"connected ({self.cfg.mode})"))
            C.append(self._account_mode_check())
            C.append(Check("account", "PASS" if self.account == st.account else "FAIL",
                           f"plan account {st.account}, connected {self.account}"))
            try:
                C.extend(self._quote_checks(ls.leg.symbol, ls.leg.action, self.broker.quote(ls.leg.symbol,
                                                                                         ls.leg.primary_exchange), lim))
            except Exception as e:  # noqa: BLE001
                C.append(Check(f"{ls.leg.symbol} market data", "FAIL", f"no quote: {e}"))
            if leg == "acquirer":
                C.append(self._short_check(ls.leg.symbol, ls.leg.primary_exchange, qty))
            snap = self._snapshot(C, ctx)
            wi, err = self._what_ifs([(ls.leg, qty, lim)])
            C.extend(self._margin_checks(wi, err, snap, ctx))
        open_, why = market_session(now_utc)
        C.append(Check("session", "PASS" if open_ else ("WARN" if self.cfg.allow_outside_rth else "FAIL"), why))
        ctx["phrase"] = self.residual_phrase(leg, qty, lim)
        r = PreflightReport(st.plan.plan_hash(), C, ctx, _iso())
        self.journal.write("residual_preflight", plan_id=st.plan.plan_id, leg=leg, quantity=qty, limit=lim,
                           result="ok" if r.ok else "blocked", failures=[c.__dict__ for c in r.failures])
        return r

    def approve_residual(self, report: PreflightReport, typed: str) -> ResidualApproval:
        st = self._need_state()
        ctx = report.context
        if ctx.get("kind") != "residual":
            raise PermissionError("not a residual pre-flight report")
        if report.plan_hash != st.plan.plan_hash():
            raise PermissionError("residual report belongs to a different plan")
        age = _age_s(report.created_utc)
        if age > self.cfg.report_ttl_s:
            raise PermissionError(f"residual pre-flight expired ({age:.0f}s old); run it again")
        if not report.ok:
            raise PermissionError("residual pre-flight has failures: " + "; ".join(c.name for c in report.failures))
        if self.cfg.mode == "live" and not self.cfg.live_allowed():
            raise PermissionError("live trading is not enabled (MERGERARB_ALLOW_LIVE=1)")
        phrase = self.residual_phrase(ctx["leg"], ctx["quantity"], ctx["limit_price"])
        if (typed or "").strip() != phrase:
            raise PermissionError(f"confirmation phrase mismatch; type exactly: {phrase}")
        a = ResidualApproval(st.plan.plan_hash(), ctx["leg"], ctx["quantity"], ctx["limit_price"], self.account,
                             self.cfg.mode, typed.strip(), _iso())
        self.journal.write("residual_approved", plan_id=st.plan.plan_id, leg=a.leg, quantity=a.quantity,
                           limit=a.limit_price, account=a.account, mode=a.mode)
        return a

    def submit_residual(self, approval: ResidualApproval) -> ExecutionState:
        st = self._need_state()
        if approval.plan_hash != st.plan.plan_hash() or approval.account != self.account \
                or approval.mode != self.cfg.mode:
            raise PermissionError("residual approval does not match this plan / account / mode")
        age = _age_s(approval.created_utc)
        if age > self.cfg.approval_ttl_s:
            raise PermissionError(f"residual approval expired ({age:.0f}s old); run the residual pre-flight again")
        if not st.reconciled or st.status != "NEEDS_ACTION":
            raise RuntimeError(f"plan is {st.status}: residuals can only be re-placed from NEEDS_ACTION")
        if not self.broker.is_connected():
            raise ConnectionError("not connected")
        self._sync(st)
        ls = st.legs[approval.leg]
        if approval.quantity > ls.unplaced + EPS:
            raise RuntimeError(f"only {ls.unplaced:g} {ls.leg.symbol} shares are unplaced now; run the residual "
                               f"pre-flight again")
        if approval.leg == "target" and st.sequencing == "short_first":
            acq = st.legs["acquirer"]
            if approval.quantity > math.floor(acq.filled / st.plan.ratio + EPS) - (ls.filled + ls.live_remaining) + EPS:
                raise RuntimeError("that would buy the target ahead of the filled short")
        ls.reauthorized += approval.quantity
        self.journal.write("residual_submitted", plan_id=st.plan.plan_id, leg=approval.leg, quantity=approval.quantity,
                           limit=approval.limit_price)
        self._place(st, approval.leg, approval.quantity, limit=approval.limit_price, residual=True)
        return self.pump()

    # ----- start-up: rebuild unfinished plans from the journal ---------------------------------------------------
    def load_unfinished(self) -> list[str]:
        by_plan: dict[str, list[dict]] = {}
        for r in self.journal.read():
            if r.get("plan_id"):
                by_plan.setdefault(r["plan_id"], []).append(r)
        rebuilt = []
        for pid, evs in by_plan.items():
            sub = next((r for r in evs if r.get("event") == "plan_submitted"), None)
            if sub is None:
                continue
            last, resume = _journal_status(evs)
            if last in TERMINAL:
                continue
            try:
                rebuilt.append(self._rebuild(sub, evs, resume))
            except Exception as e:  # noqa: BLE001
                self.journal.write("rebuild_failed", plan_id=pid, error=f"{type(e).__name__}: {e}")
        if rebuilt:
            self.state, self.backlog = rebuilt[-1], rebuilt[:-1]
            self.journal.write("journal_rebuilt", plans=[s.plan.plan_id for s in rebuilt])
        return [s.plan.plan_id for s in rebuilt]

    def _rebuild(self, sub: dict, evs: list[dict], resume: str) -> ExecutionState:
        plan = plan_from_dict(sub["plan"], require_hash=False)
        appr = next((r for r in evs if r.get("event") == "approved"), {})
        st = ExecutionState(plan, None, "UNRECONCILED", {"acquirer": LegState(plan.acquirer),
                                                          "target": LegState(plan.target)},
                            account=sub.get("account") or appr.get("account"),
                            mode=sub.get("mode") or appr.get("mode") or self.cfg.mode,
                            sequencing=sub.get("sequencing") or self.cfg.sequencing, reconciled=False,
                            started_utc=sub.get("ts_utc", ""))
        idx: dict[str, OrderHandle] = {}
        for r in evs:
            e, ref = r.get("event"), r.get("order_ref")
            parts = ref.split(":") if ref else []
            if e in ("order_attempt", "order_submitted", "recovered_order") and len(parts) == 4 \
                    and parts[2] in st.next_child and parts[3].isdigit():
                st.next_child[parts[2]] = max(st.next_child[parts[2]], int(parts[3]) + 1)
            if e in ("order_submitted", "recovered_order") and ref and ref not in idx and len(parts) == 4 \
                    and parts[2] in st.legs:
                ls = st.legs[parts[2]]
                h = OrderHandle(int(r.get("order_id") or 0), ref, r.get("symbol") or ls.leg.symbol,
                                r.get("action") or ls.leg.action, int(r.get("quantity") or 0), r.get("limit"),
                                r.get("broker_status") or r.get("status") or "PendingSubmit",
                                float(r.get("filled") or 0.0), 0.0, 0.0, r.get("ts_utc", ""), r.get("error") or "")
                ls.handles.append(h)
                idx[ref] = h
            elif e == "order_status" and ref in idx:
                h = idx[ref]
                h.status = r.get("status") or h.status
                h.filled = float(r.get("filled") or 0.0)
                h.avg_fill_price = float(r.get("avg_fill_price") or h.avg_fill_price)
                h.error = r.get("error") or h.error
            elif e == "fill" and ref in idx:
                h = idx[ref]
                h.filled = max(h.filled, float(r.get("cumulative") or 0.0))
                h.avg_fill_price = float(r.get("avg_fill_price") or h.avg_fill_price)
            elif e == "order_error" and ref in idx:
                idx[ref].error = r.get("error") or idx[ref].error
            elif e == "cancel_requested" and ref:
                st.cancel_requested.add(ref)
            elif e == "cancel_all":
                st.cancel_reason = st.cancel_reason or r.get("reason")
            elif e == "residual_submitted" and r.get("leg") in st.legs:
                st.legs[r["leg"]].reauthorized += int(r.get("quantity") or 0)
            elif e == "alert":
                st.alerts.append(r.get("message", ""))
                st.alert_keys.add(r.get("key", ""))
            elif e in ("order_ended_unfilled", "order_cancelled") and ref:
                st.ended.add(ref)
        for ls in st.legs.values():
            for h in ls.handles:
                h.remaining = max(h.quantity - h.filled, 0.0)
                st.seen[h.order_ref] = (h.status, round(h.filled, 6), h.error)
        st.resume_status = resume
        return st
