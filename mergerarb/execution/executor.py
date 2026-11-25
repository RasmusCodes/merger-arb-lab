"""Pre-flight checks, explicit approval, leg-managed submission, fill monitoring, cancellation and recovery.

Flow:  plan -> preflight(plan) -> approve(plan, report, typed_phrase) -> submit(plan, approval) -> pump() ... -> FILLED
       (or cancel_all / recover after a disconnect). Nothing reaches the broker without an Approval bound to the plan.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math

import pandas as pd

from .broker import BrokerClient, OrderHandle
from .config import ExecutionConfig
from .journal import Journal
from .orders import OrderLeg, OrderPlan, validate_plan

# NYSE/Nasdaq full-day closures (2026-11 .. 2027-12); the broker's own rejection remains the final backstop
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
            "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}   # 13:00 ET


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


@dataclasses.dataclass
class LegState:
    leg: OrderLeg
    handles: list[OrderHandle] = dataclasses.field(default_factory=list)

    @property
    def filled(self) -> float:
        return sum(h.filled for h in self.handles)

    @property
    def placed(self) -> float:
        return sum(h.quantity for h in self.handles if h.status not in ("Cancelled", "ApiCancelled", "Inactive")
                   or h.filled > 0)

    @property
    def avg_price(self) -> float:
        f = self.filled
        return sum(h.filled * h.avg_fill_price for h in self.handles) / f if f else 0.0

    @property
    def rejected(self) -> bool:
        return bool(self.handles) and all(h.status == "Inactive" and h.filled == 0 for h in self.handles)


@dataclasses.dataclass
class ExecutionState:
    plan: OrderPlan
    approval: Approval
    status: str
    legs: dict[str, LegState]
    alerts: list[str] = dataclasses.field(default_factory=list)
    imbalance_since: dt.datetime | None = None
    imbalance_shares: float = 0.0
    imbalance_usd: float = 0.0
    started_utc: str = ""

    def summary(self) -> dict:
        return {"plan_id": self.plan.plan_id, "status": self.status, "alerts": list(self.alerts),
                "imbalance_acquirer_shares": self.imbalance_shares, "imbalance_usd": round(self.imbalance_usd, 2),
                "legs": {k: {"symbol": v.leg.symbol, "action": v.leg.action, "target_qty": v.leg.quantity,
                             "placed": v.placed, "filled": v.filled, "avg_price": round(v.avg_price, 4),
                             "orders": [dataclasses.asdict(h) for h in v.handles]} for k, v in self.legs.items()}}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def market_session(now_utc: dt.datetime | None = None) -> tuple[bool, str]:
    t = pd.Timestamp(now_utc or _now()).tz_convert("America/New_York")
    d = t.strftime("%Y-%m-%d")
    if t.weekday() >= 5 or d in HOLIDAYS:
        return False, f"market closed ({t:%a %Y-%m-%d})"
    close = (13, 0) if d in EARLY_CLOSE else (16, 0)
    if (t.hour, t.minute) < (9, 30) or (t.hour, t.minute) >= close:
        return False, f"outside regular hours ({t:%H:%M} ET; session 09:30-{close[0]}:{close[1]:02d})"
    return True, f"regular session ({t:%H:%M} ET)"


class PairExecutor:
    def __init__(self, broker: BrokerClient, cfg: ExecutionConfig = ExecutionConfig(), journal: Journal | None = None):
        self.broker, self.cfg = broker, cfg
        self.journal = journal or Journal(cfg.journal_path)
        self.account: str | None = None
        self.state: ExecutionState | None = None

    # ----- connection --------------------------------------------------------------------------------------------
    def connect(self) -> str:
        problems = self.cfg.validate()
        if problems:
            raise RuntimeError("; ".join(problems))
        self.broker.connect()
        accts = self.broker.accounts()
        if self.cfg.account:
            if self.cfg.account not in accts:
                raise RuntimeError(f"configured account {self.cfg.account} not among managed accounts")
            self.account = self.cfg.account
        elif len(accts) == 1:
            self.account = accts[0]
        else:
            raise RuntimeError(f"several managed accounts {accts}; set ExecutionConfig.account explicitly")
        self.journal.write("connected", mode=self.cfg.mode, account=self.account, port=self.cfg.effective_port)
        return self.account

    def approval_phrase(self, plan: OrderPlan) -> str:
        return f"{self.cfg.mode.upper()} {self.account} {plan.short_hash}"

    # ----- pre-flight --------------------------------------------------------------------------------------------
    def preflight(self, plan: OrderPlan, borrow_fee: float | None = None, signal_decision: str | None = None,
                  now_utc: dt.datetime | None = None, ssr_flag: bool | None = None) -> PreflightReport:
        C: list[Check] = []
        ctx: dict = {"mode": self.cfg.mode, "account": self.account}
        for name, st, msg in validate_plan(plan, self.cfg.mode, self.cfg.max_order_notional, self.cfg.price_band_pct):
            C.append(Check(name, st, msg))
        if not self.broker.is_connected():
            C.append(Check("connection", "FAIL", "not connected to TWS / IB Gateway"))
            return self._report(plan, C, ctx)
        C.append(Check("connection", "PASS", f"connected ({self.cfg.mode}, port {self.cfg.effective_port})"))
        paper_acct = (self.account or "").startswith("DU")
        if self.cfg.mode == "paper":
            C.append(Check("account mode", "PASS" if paper_acct else "FAIL",
                           f"paper mode needs a paper account (DU...); connected to {self.account}"))
        else:
            ok = self.cfg.live_allowed() and not paper_acct
            C.append(Check("account mode", "PASS" if ok else "FAIL",
                           "live mode enabled by MERGERARB_ALLOW_LIVE=1 on a live account" if ok else
                           "live mode requires MERGERARB_ALLOW_LIVE=1 and a non-paper account"))
        # market data and price bands against broker quotes
        quotes = {}
        for leg in plan.legs:
            q = self.broker.quote(leg.symbol, leg.primary_exchange)
            quotes[leg.symbol] = q
            ref = q.last or ((q.bid + q.ask) / 2 if q.bid and q.ask else None)
            if ref is None:
                C.append(Check(f"{leg.symbol} market data", "FAIL", "no price from the broker"))
                continue
            delayed = q.data_type.startswith("delayed") or q.data_type == "frozen"
            if delayed:
                st = "WARN" if (self.cfg.mode == "paper" and self.cfg.allow_delayed_data_in_paper) else "FAIL"
                C.append(Check(f"{leg.symbol} market data", st, f"{q.data_type} data - not live; limits checked "
                                                                  f"against a delayed price"))
            else:
                C.append(Check(f"{leg.symbol} market data", "PASS", f"{q.data_type} last {q.last} bid {q.bid} ask {q.ask}"))
            if leg.limit_price:
                dev = leg.limit_price / ref - 1
                C.append(Check(f"{leg.symbol} limit vs broker price", "PASS" if abs(dev) <= self.cfg.price_band_pct else "FAIL",
                               f"limit {leg.limit_price} vs {ref} ({dev:+.2%})"))
            if leg.action == "SELL" and q.prev_close and q.last and q.last <= 0.9 * q.prev_close:
                C.append(Check(f"{leg.symbol} Rule 201", "WARN", "down >=10% today: short-sale circuit breaker likely "
                                                                  "active (shorts execute only above the best bid)"))
        if ssr_flag:
            C.append(Check(f"{plan.acquirer.symbol} Rule 201", "WARN", "external flag: short-sale restriction in effect"))
        ctx["quotes"] = {k: dataclasses.asdict(v) for k, v in quotes.items()}
        # borrow
        si = self.broker.short_info(plan.acquirer.symbol, plan.acquirer.primary_exchange)
        st, msg = si.status(plan.acquirer.quantity)
        C.append(Check("borrow availability", st, msg))
        if borrow_fee is None:
            C.append(Check("borrow fee", "WARN", "fee not supplied (check IBKR's Short Stock Availability or iBorrowDesk)"))
        else:
            C.append(Check("borrow fee", "PASS" if borrow_fee <= self.cfg.max_borrow_fee else "FAIL",
                           f"{borrow_fee:.2%}/yr (limit {self.cfg.max_borrow_fee:.0%})"))
        # account, what-if margin and exposure
        snap = self.broker.account_snapshot(self.account)
        ctx["account_snapshot"] = dataclasses.asdict(snap)
        wi = [self.broker.what_if(leg, self.account) for leg in plan.legs]
        ctx["what_if"] = [dataclasses.asdict(w) for w in wi]
        init_chg = sum(w.init_margin_change or 0 for w in wi)
        maint_chg = sum(w.maint_margin_change or 0 for w in wi)
        warn = "; ".join(w.warning for w in wi if w.warning)
        C.append(Check("what-if margin", "PASS" if all(w.init_margin_change is not None for w in wi) else "WARN",
                       f"initial +{init_chg:,.0f}, maintenance +{maint_chg:,.0f}, commission "
                       f"{sum(w.commission or 0 for w in wi):.2f}" + (f" | broker warning: {warn}" if warn else "")))
        C.append(Check("available funds", "PASS" if snap.available_funds >= init_chg else "FAIL",
                       f"available {snap.available_funds:,.0f} vs initial margin needed {init_chg:,.0f}"))
        post_exl = snap.excess_liquidity - maint_chg
        floor = self.cfg.min_excess_liquidity_pct * snap.net_liquidation
        C.append(Check("excess liquidity after trade", "PASS" if post_exl >= floor else "FAIL",
                       f"{post_exl:,.0f} vs floor {floor:,.0f} ({self.cfg.min_excess_liquidity_pct:.0%} of NLV)"))
        pos = self.broker.positions(self.account)
        existing = {s: pos.get(s, 0.0) for s in (plan.target.symbol, plan.acquirer.symbol)}
        px = {s: (quotes[s].last or 0.0) for s in quotes}
        gross_now = sum(abs(q) * px.get(s, 0.0) for s, q in existing.items())
        gross_new = gross_now + sum(l.quantity * (l.limit_price or px.get(l.symbol, 0.0)) for l in plan.legs)
        C.append(Check("gross exposure", "PASS" if gross_new <= self.cfg.max_gross_notional else "FAIL",
                       f"{gross_new:,.0f} USD after the trade (cap {self.cfg.max_gross_notional:,.0f})"))
        if any(existing.values()):
            C.append(Check("existing positions", "WARN", f"already holding {existing}; check the combined hedge"))
        # duplicates
        open_ma = [h for h in self.broker.open_orders() if h.symbol in existing]
        C.append(Check("duplicate orders", "FAIL" if open_ma else "PASS",
                       f"{len(open_ma)} open strategy orders in these symbols" if open_ma else "no open strategy orders"))
        if plan.idempotency_key() in self.journal.submitted_keys():
            C.append(Check("duplicate plan", "FAIL", "an identical plan was already submitted today"))
        # session
        open_, why = market_session(now_utc)
        C.append(Check("session", "PASS" if open_ else ("WARN" if self.cfg.allow_outside_rth else "FAIL"), why))
        # model signal (overridable only with a written reason at approval)
        if signal_decision is not None:
            C.append(Check("model signal", "PASS" if signal_decision == "TRADE" else "FAIL",
                           f"analysis signal is {signal_decision}" + ("" if signal_decision == "TRADE" else
                                                                      " - an override needs a written reason")))
        return self._report(plan, C, ctx)

    def _report(self, plan: OrderPlan, C: list[Check], ctx: dict) -> PreflightReport:
        r = PreflightReport(plan.plan_hash(), C, ctx, _now().isoformat(timespec="seconds"))
        self.journal.write("preflight", plan_id=plan.plan_id, plan_hash=r.plan_hash,
                           result="ok" if r.ok else "blocked", failures=[c.__dict__ for c in r.failures])
        return r

    # ----- approval ---------------------------------------------------------------------------------------------
    def approve(self, plan: OrderPlan, report: PreflightReport, typed: str,
                override_reason: str | None = None) -> Approval:
        if report.plan_hash != plan.plan_hash():
            raise PermissionError("pre-flight report belongs to a different plan")
        if not report.ok:
            if not (report.only_signal_fails() and override_reason and len(override_reason.strip()) >= 10):
                raise PermissionError("pre-flight has failures: " + "; ".join(c.name for c in report.failures))
        if self.cfg.mode == "live" and not self.cfg.live_allowed():
            raise PermissionError("live trading is not enabled (MERGERARB_ALLOW_LIVE=1)")
        if typed.strip() != self.approval_phrase(plan):
            raise PermissionError(f"confirmation phrase mismatch; type exactly: {self.approval_phrase(plan)}")
        a = Approval(plan.plan_hash(), self.account, self.cfg.mode, typed.strip(), _now().isoformat(timespec="seconds"),
                     override_reason)
        self.journal.write("approved", plan_id=plan.plan_id, plan_hash=a.plan_hash, account=a.account, mode=a.mode,
                           override_reason=override_reason)
        return a

    # ----- submission ------------------------------------------------------------------------------------------
    def submit(self, plan: OrderPlan, approval: Approval) -> ExecutionState:
        if approval.plan_hash != plan.plan_hash() or approval.account != self.account or approval.mode != self.cfg.mode:
            raise PermissionError("approval does not match this plan / account / mode")
        age = (_now() - dt.datetime.fromisoformat(approval.created_utc)).total_seconds()
        if age > self.cfg.approval_ttl_s:
            raise PermissionError(f"approval expired ({age:.0f}s old); run pre-flight and approve again")
        if self.state and self.state.status in ("WORKING", "PARTIAL", "IMBALANCED", "DISCONNECTED"):
            raise RuntimeError("another strategy execution is still active")
        key = plan.idempotency_key()
        if key in self.journal.submitted_keys():
            raise RuntimeError("duplicate: this plan was already submitted")
        if not self.broker.is_connected():
            raise ConnectionError("not connected")
        self.state = ExecutionState(plan, approval, "WORKING", {"acquirer": LegState(plan.acquirer),
                                                                 "target": LegState(plan.target)},
                                    started_utc=_now().isoformat(timespec="seconds"))
        self.journal.write("plan_submitted", plan_id=plan.plan_id, idempotency_key=key, plan=plan.to_dict(),
                           sequencing=self.cfg.sequencing)
        self._place("acquirer", plan.acquirer.quantity)
        if self.cfg.sequencing == "simultaneous":
            self._place("target", plan.target.quantity)
        return self.pump()

    def _place(self, leg_key: str, qty: int, limit: float | None = None) -> OrderHandle:
        st = self.state
        ls = st.legs[leg_key]
        ref = st.plan.order_ref(leg_key, len(ls.handles))
        h = self.broker.place(ls.leg, ref, self.account, quantity=qty, limit_price=limit)
        ls.handles.append(h)
        self.journal.write("order_submitted", plan_id=st.plan.plan_id, order_ref=ref, symbol=ls.leg.symbol,
                           action=ls.leg.action, quantity=qty, limit=limit or ls.leg.limit_price, broker_status=h.status,
                           order_id=h.order_id, error=h.error)
        return h

    # ----- monitoring ------------------------------------------------------------------------------------------
    def pump(self, acquirer_price: float | None = None) -> ExecutionState:
        st = self.state
        if st is None:
            raise RuntimeError("nothing submitted")
        if not self.broker.is_connected():
            if st.status != "DISCONNECTED":
                st.status = "DISCONNECTED"
                st.alerts.append("connection lost: order states unknown until reconnect + recover()")
                self.journal.write("disconnected", plan_id=st.plan.plan_id)
            return st
        try:
            handles = {h.order_ref: h for h in self.broker.refresh()}
        except ConnectionError:
            st.status = "DISCONNECTED"
            self.journal.write("disconnected", plan_id=st.plan.plan_id)
            return st
        for ls in st.legs.values():
            for i, h in enumerate(ls.handles):
                new = handles.get(h.order_ref)
                if new and (new.status, new.filled) != (h.status, h.filled):
                    self.journal.write("order_status", plan_id=st.plan.plan_id, order_ref=h.order_ref,
                                       symbol=new.symbol, status=new.status, filled=new.filled,
                                       remaining=new.remaining, avg_fill_price=new.avg_fill_price, error=new.error)
                    if new.filled > h.filled:
                        self.journal.write("fill", plan_id=st.plan.plan_id, order_ref=h.order_ref, symbol=new.symbol,
                                           quantity=new.filled - h.filled, cumulative=new.filled,
                                           avg_fill_price=new.avg_fill_price)
                if new:
                    ls.handles[i] = new
        acq, tgt = st.legs["acquirer"], st.legs["target"]
        ratio = st.plan.ratio
        if acq.rejected:
            if st.status != "FAILED":
                st.status = "FAILED"
                st.alerts.append(f"short sale rejected ({acq.handles[-1].error}); no target order was sent"
                                 if self.cfg.sequencing == "short_first" else "short sale rejected")
                self.journal.write("alert", plan_id=st.plan.plan_id, message=st.alerts[-1])
            return st
        # short-first: buy the target only against filled short shares (whole target shares)
        if self.cfg.sequencing == "short_first" and st.status not in ("CANCELLED", "FAILED"):
            need = min(st.plan.target.quantity, math.floor(acq.filled / ratio + 1e-9))
            gap = need - tgt.placed
            if gap >= 1:
                self._place("target", int(gap))
                handles = {h.order_ref: h for h in self.broker.refresh()}
                tgt.handles = [handles.get(h.order_ref, h) for h in tgt.handles]
        # imbalance (acquirer-share equivalents): + = under-hedged long target, - = net short acquirer
        st.imbalance_shares = tgt.filled * ratio - acq.filled
        px = acquirer_price or (acq.avg_price or st.plan.reference_prices.get(st.plan.acquirer.symbol, 0.0))
        st.imbalance_usd = abs(st.imbalance_shares) * px
        done = (acq.filled >= st.plan.acquirer.quantity and tgt.filled >= st.plan.target.quantity)
        if done:
            if st.status != "FILLED":
                st.status = "FILLED"
                self.journal.write("plan_filled", plan_id=st.plan.plan_id, acquirer_avg=acq.avg_price,
                                   target_avg=tgt.avg_price,
                                   achieved_gross_spread=ratio * acq.avg_price / tgt.avg_price - 1 if tgt.avg_price else None)
            return st
        if st.status in ("CANCELLED", "FAILED"):
            return st
        if st.imbalance_usd > self.cfg.max_unhedged_usd:
            now = _now()
            st.imbalance_since = st.imbalance_since or now
            if (now - st.imbalance_since).total_seconds() >= self.cfg.unhedged_timeout_s and st.status != "IMBALANCED":
                st.status = "IMBALANCED"
                msg = (f"UNHEDGED: {st.imbalance_shares:+.1f} {st.plan.acquirer.symbol}-equivalent shares "
                       f"(~${st.imbalance_usd:,.0f}) for {self.cfg.unhedged_timeout_s:.0f}s+. Options: complete the "
                       f"lagging leg (re-approve), cancel_all() and unwind the filled leg, or hold.")
                st.alerts.append(msg)
                self.journal.write("alert", plan_id=st.plan.plan_id, message=msg)
        else:
            st.imbalance_since = None
            if st.status == "IMBALANCED":
                st.status = "PARTIAL"
        if st.status not in ("IMBALANCED",):
            st.status = "PARTIAL" if (acq.filled or tgt.filled) else "WORKING"
        return st

    # ----- cancellation and recovery -------------------------------------------------------------------------
    def cancel_all(self, reason: str = "user request") -> ExecutionState:
        st = self.state
        if st is None:
            raise RuntimeError("nothing to cancel")
        for ls in st.legs.values():
            for h in ls.handles:
                if not h.is_done:
                    self.broker.cancel(h)
                    self.journal.write("cancel_requested", plan_id=st.plan.plan_id, order_ref=h.order_ref,
                                       reason=reason)
        self.pump()
        if st.status != "FILLED":
            st.status = "CANCELLED"
            if abs(st.imbalance_shares) > 1e-9:
                st.alerts.append(f"cancelled with an open imbalance of {st.imbalance_shares:+.1f} "
                                 f"{st.plan.acquirer.symbol}-equivalent shares - unwind or hedge manually")
            self.journal.write("plan_cancelled", plan_id=st.plan.plan_id, reason=reason,
                               imbalance_shares=st.imbalance_shares)
        return st

    def recover(self) -> ExecutionState | None:
        """After a reconnect: rebuild order states from the broker by orderRef before allowing any new action."""
        st = self.state
        if st is None:
            return None
        if not self.broker.is_connected():
            self.broker.connect()
        prefix = f"MA:{st.plan.plan_id[:8]}:"
        known = {h.order_ref for ls in st.legs.values() for h in ls.handles}
        for h in self.broker.refresh() + self.broker.open_orders():
            if h.order_ref.startswith(prefix) and h.order_ref not in known:
                leg_key = h.order_ref.split(":")[2]
                st.legs[leg_key].handles.append(h)
                known.add(h.order_ref)
                self.journal.write("recovered_order", plan_id=st.plan.plan_id, order_ref=h.order_ref, status=h.status)
        st.status = "PARTIAL"
        self.journal.write("recovered", plan_id=st.plan.plan_id)
        return self.pump()
