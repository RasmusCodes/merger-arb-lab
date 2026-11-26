"""Local trading service: the ONLY process that talks to TWS / IB Gateway.

    python -m mergerarb.execution.service            # paper mode, TWS paper port 7497
    python -m mergerarb.execution.service --sim      # offline simulator (no IBKR needed; separate journal)
    python -m mergerarb.execution.service --app gateway --port 8813

Security:
  * binds to 127.0.0.1 only; requests from a non-local Host/Origin are refused (blocks browser CSRF);
  * every request needs ``Authorization: Bearer <token>``; the token is random per start (or MERGERARB_TOKEN) and
    written to ~/.mergerarb/service_token for the local dashboard - it is never committed or published;
  * plans are re-validated server-side against the deal file (hash, ratio, exact whole-share hedge, actions, DAY);
  * malformed requests get 400 JSON answers, internal errors a generic 500 (details only in the journal);
  * single-threaded: one IBKR client, no concurrent order handling;
  * live mode additionally needs MERGERARB_ALLOW_LIVE=1 and --mode live (see ExecutionConfig.live_allowed).
On start it rebuilds unfinished plans from the journal; new plans stay blocked until /recover reconciles them.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hmac
import json
import math
import os
import pathlib
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from ..deal import load_deal
from .broker import IBKRBroker, SimBroker
from .config import ROOT, ExecutionConfig
from .executor import PairExecutor, market_session
from .orders import build_pair_plan, plan_from_dict, verify_plan_integrity
from .trigger import Rung, evaluate_rung

TOKEN_FILE = pathlib.Path.home() / ".mergerarb" / "service_token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 1_000_000


class BadRequest(ValueError):
    """-> HTTP 400"""


class NotFound(LookupError):
    """-> HTTP 404"""


def _num(body: dict, key: str, required: bool = False, positive: bool = False, minimum: float | None = None):
    v = body.get(key)
    if v is None:
        if required:
            raise BadRequest(f"'{key}' is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise BadRequest(f"'{key}' must be a number")
    if positive and v <= 0:
        raise BadRequest(f"'{key}' must be positive")
    if minimum is not None and v < minimum:
        raise BadRequest(f"'{key}' must be >= {minimum}")
    return float(v)


def _int(body: dict, key: str, minimum: int = 1) -> int:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v or v < minimum:
        raise BadRequest(f"'{key}' must be a whole number >= {minimum}")
    return int(v)


def _str(body: dict, key: str, required: bool = False) -> str | None:
    v = body.get(key)
    if v is None:
        if required:
            raise BadRequest(f"'{key}' is required")
        return None
    if not isinstance(v, str):
        raise BadRequest(f"'{key}' must be a string")
    return v


class Service:
    def __init__(self, executor: PairExecutor, deal_id: str = "mrvl_lite", clock=None):
        self.ex = executor
        self.deal = load_deal(deal_id)
        if self.ex.deal is None:
            self.ex.deal = self.deal
        self.reports: dict[str, tuple[float, object]] = {}
        self.residual_reports: dict[tuple[str, int], tuple[float, object]] = {}
        self.rungs: list[Rung] = []
        self.hits_logged: set[tuple[str, float]] = set()
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def check_triggers(self) -> list[dict]:
        """Evaluate armed rungs on fresh live broker quotes in the regular session; journal each rung's first hit per
        day. Never sends orders."""
        if not self.rungs or not self.ex.broker.is_connected():
            return []
        T, A = self.deal.target.ticker, self.deal.acquirer.ticker
        qt, qa = self.ex.broker.quote(T), self.ex.broker.quote(A)
        now = self.clock()
        live = qt.data_type == "live" and qa.data_type == "live"
        age = None
        if qt.time_utc and qa.time_utc:
            age = max((now - qt.time_utc).total_seconds(), (now - qa.time_utc).total_seconds())
        session_open = market_session(now)[0]
        out = []
        for r in self.rungs:
            s = evaluate_rung(r, qt.bid, qt.ask, qa.bid, qa.ask, self.deal.exchange_ratio, self.deal.cash_per_share,
                              quotes_live=live, quote_age_s=age, max_quote_age_s=self.ex.cfg.max_quote_age_s,
                              session_open=session_open, now_utc=now)
            key = (s.checked_utc[:10], r.target_gross_spread)
            if s.hit and key not in self.hits_logged:
                self.hits_logged.add(key)
                self.ex.journal.write("trigger_hit", rung=r.target_gross_spread, lots=r.lots,
                                      executable_spread=s.executable_spread, target_limit=s.target_limit,
                                      acquirer_limit=s.acquirer_limit, quote_age_s=age)
            out.append({**dataclasses.asdict(s), "rung": dataclasses.asdict(r), "data_types": [qt.data_type, qa.data_type]})
        return out

    def status(self) -> dict:
        st = self.ex.state
        alerts = list(reversed(st.alerts[-25:])) if st else []
        attention = None
        if st is not None:
            if not st.reconciled:
                attention = "UNRECONCILED: plan rebuilt from the journal - connect, then POST /recover"
            elif st.status == "DISCONNECTED":
                attention = "DISCONNECTED: order states unknown - reconnect, then POST /recover"
            elif st.status == "NEEDS_ACTION":
                attention = "NEEDS ACTION: " + (st.action_required[0] if st.action_required else "see alerts")
            elif st.unhedged:
                attention = alerts[0] if alerts else "UNHEDGED"
        phrases = {}
        if st is not None and st.status == "NEEDS_ACTION":
            for k, ls in st.legs.items():
                if ls.unplaced > 0:
                    phrases[k] = self.ex.residual_phrase(k, int(ls.unplaced), ls.leg.limit_price)
        return {"attention": attention, "alerts": alerts, "action_required": list(st.action_required) if st else [],
                "blocked": self.ex.blocking_reason(), "residual_phrases": phrases,
                "connected": self.ex.broker.is_connected(), "mode": self.ex.cfg.mode, "account": self.ex.account,
                "port": self.ex.cfg.effective_port, "execution": st.summary() if st else None,
                "backlog": [s.plan.plan_id for s in self.ex.backlog]}

    def _plan(self, body: dict):
        d = body.get("plan")
        if not isinstance(d, dict):
            raise BadRequest("'plan' (the exported plan JSON including plan_hash) is required")
        try:
            plan = plan_from_dict(d, require_hash=True)
        except ValueError as e:
            raise BadRequest(str(e)) from e
        problems = verify_plan_integrity(plan, self.deal, self.ex.cfg.mode)
        if problems:
            raise BadRequest("plan rejected: " + "; ".join(problems))
        return plan

    def _residual_args(self, body: dict) -> tuple[str, int, float | None]:
        leg = _str(body, "leg", required=True)
        if leg not in ("acquirer", "target"):
            raise BadRequest("'leg' must be 'acquirer' or 'target'")
        return leg, _int(body, "quantity"), _num(body, "limit_price", positive=True)

    def handle(self, method: str, path: str, body: dict, query: dict) -> dict:
        ex = self.ex
        if method == "GET" and path == "/status":
            if ex.needs_pump() and ex.broker.is_connected():
                ex.pump()
            return self.status()
        if method == "GET" and path == "/journal":
            raw = query.get("limit", ["200"])[0]
            if not raw.isdigit():
                raise BadRequest("'limit' must be a whole number")
            return {"events": ex.journal.read(min(int(raw), 5000))}
        if method == "POST" and path == "/connect":
            return {"account": ex.connect(), **self.status()}
        if method == "POST" and path == "/plan":
            order_type = _str(body, "order_type") or "LMT"
            if order_type not in ("LMT", "MKT"):
                raise BadRequest("'order_type' must be LMT or MKT")
            n = _int(body, "target_shares")
            tl = _num(body, "target_limit", required=order_type == "LMT", positive=True)
            al = _num(body, "acquirer_limit", required=order_type == "LMT", positive=True)
            refs = {}
            for sym in (self.deal.target.ticker, self.deal.acquirer.ticker):
                q = ex.broker.quote(sym)
                refs[sym] = q.last or ((q.bid + q.ask) / 2 if q.bid and q.ask else None)
            try:
                plan = build_pair_plan(self.deal, n, tl, al, refs, order_type)
            except ValueError as e:
                raise BadRequest(str(e)) from e
            return {"plan": plan.to_dict(), "approval_phrase": ex.approval_phrase(plan)}
        if method == "POST" and path == "/preflight":
            plan = self._plan(body)
            rep = ex.preflight(plan, _num(body, "borrow_fee", minimum=0.0), _str(body, "signal_decision"))
            self.reports[rep.plan_hash] = (time.time(), rep)
            return {"ok": rep.ok, "only_signal_fails": rep.only_signal_fails(),
                    "checks": [c.__dict__ for c in rep.checks], "context": rep.context,
                    "approval_phrase": ex.approval_phrase(plan)}
        if method == "POST" and path == "/approve_submit":
            plan = self._plan(body)
            ts, rep = self.reports.get(plan.plan_hash(), (0, None))
            if rep is None or time.time() - ts > ex.cfg.report_ttl_s:
                raise PermissionError("run pre-flight for this exact plan first (or the report expired)")
            appr = ex.approve(plan, rep, _str(body, "typed", required=True), _str(body, "override_reason"))
            st = ex.submit(plan, appr)
            self.reports.pop(plan.plan_hash(), None)
            return {"execution": st.summary()}
        if method == "POST" and path == "/residual_preflight":
            leg, qty, lim = self._residual_args(body)
            rep = ex.residual_preflight(leg, qty, lim)
            self.residual_reports[(leg, qty)] = (time.time(), rep)
            return {"ok": rep.ok, "checks": [c.__dict__ for c in rep.checks], "context": rep.context,
                    "approval_phrase": rep.context["phrase"]}
        if method == "POST" and path == "/approve_residual":
            leg, qty, lim = self._residual_args(body)
            ts, rep = self.residual_reports.get((leg, qty), (0, None))
            if rep is None or time.time() - ts > ex.cfg.report_ttl_s:
                raise PermissionError("run the residual pre-flight for this leg and quantity first (or it expired)")
            if lim is not None and round(lim, 2) != rep.context["limit_price"]:
                raise PermissionError("limit price differs from the residual pre-flight")
            appr = ex.approve_residual(rep, _str(body, "typed", required=True))
            st = ex.submit_residual(appr)
            self.residual_reports.pop((leg, qty), None)
            return {"execution": st.summary()}
        if method == "POST" and path == "/triggers":
            rungs = body.get("rungs", [])
            if not isinstance(rungs, list) or not all(isinstance(r, dict) for r in rungs):
                raise BadRequest("'rungs' must be a list of {target_gross_spread, lots}")
            self.rungs = [Rung(_num(r, "target_gross_spread", required=True), _int(r, "lots")) for r in rungs]
            ex.journal.write("triggers_armed", rungs=[dataclasses.asdict(r) for r in self.rungs])
            return {"armed": [dataclasses.asdict(r) for r in self.rungs]}
        if method == "GET" and path == "/triggers":
            return {"armed": [dataclasses.asdict(r) for r in self.rungs], "status": self.check_triggers()}
        if method == "POST" and path == "/pump":
            return {"execution": ex.pump().summary()}
        if method == "POST" and path == "/cancel":
            return {"execution": ex.cancel_all(_str(body, "reason") or "user request").summary()}
        if method == "POST" and path == "/recover":
            st = ex.recover()
            return {"execution": st.summary() if st else None, **self.status()}
        if method == "POST" and path == "/close":
            return {"execution": ex.close(_str(body, "reason", required=True)).summary()}
        raise NotFound(f"no route {method} {path}")


def make_handler(service: Service, token: str):
    tok = token.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "mergerarb-local/0.2"

        def log_message(self, fmt, *args):  # keep console quiet; the journal records actions
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            host = self.headers.get("Host") or ""
            name = host[: host.find("]") + 1] if host.startswith("[") else host.split(":")[0]
            if name not in LOCAL_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin is not None:
                try:
                    if urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                        return False
                except ValueError:
                    return False
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return False
            return hmac.compare_digest(auth[7:].encode("utf-8", "surrogateescape"), tok)   # bytes: no TypeError

        def _dispatch(self, method: str) -> None:
            try:
                if not self._authorised():
                    return self._send(401, {"error": "unauthorised"})
                u = urlparse(self.path)
                body: dict = {}
                if method == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        return self._send(400, {"error": "bad Content-Length"})
                    if n < 0 or n > MAX_BODY:
                        return self._send(413, {"error": "request body too large"})
                    raw = self.rfile.read(n) if n else b""
                    if raw.strip():
                        try:
                            body = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as e:
                            return self._send(400, {"error": f"malformed JSON body: {e}"})
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "JSON body must be an object"})
                self._send(200, service.handle(method, u.path, body, parse_qs(u.query)))
            except BadRequest as e:
                self._send(400, {"error": str(e)})
            except PermissionError as e:
                self._send(403, {"error": str(e)})
            except NotFound as e:
                self._send(404, {"error": str(e)})
            except (ConnectionError, RuntimeError) as e:
                self._send(409, {"error": str(e)})
            except (ValueError, TypeError, KeyError) as e:
                self._send(400, {"error": f"bad request: {type(e).__name__}: {e}"})
            except Exception as e:  # noqa: BLE001 - never leak internals; the journal has the details
                try:
                    service.ex.journal.write("service_error", path=self.path, error=f"{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
                self._send(500, {"error": "internal error - details in the journal (service_error)"})

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

    return Handler


def write_token(token: str) -> pathlib.Path:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return TOKEN_FILE


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="local merger-arb execution service (127.0.0.1 only)")
    ap.add_argument("--sim", action="store_true", help="use the offline simulator instead of IBKR")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--app", default="tws", choices=["tws", "gateway"])
    ap.add_argument("--ib-port", type=int, default=None, help="override the TWS/Gateway API port")
    ap.add_argument("--port", type=int, default=8813, help="port of THIS service")
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--account", default=None)
    ap.add_argument("--journal", default=None, help="journal path (default data/execution/journal[_sim].jsonl)")
    a = ap.parse_args(argv)
    journal = pathlib.Path(a.journal) if a.journal else \
        ROOT / "data" / "execution" / ("journal_sim.jsonl" if a.sim else "journal.jsonl")
    cfg = ExecutionConfig(mode=a.mode, app=a.app, port=a.ib_port, client_id=a.client_id, account=a.account,
                          journal_path=journal)
    problems = cfg.validate()
    if problems:
        sys.exit("refusing to start: " + "; ".join(problems))
    if a.sim:
        broker = SimBroker(quotes={"LITE": {"last": 487.12, "bid": 486.9, "ask": 487.3, "prev_close": 544.38},
                                   "MRVL": {"last": 131.57, "bid": 131.55, "ask": 131.6, "prev_close": 144.74}})
    else:
        broker = IBKRBroker(cfg)
    ex = PairExecutor(broker, cfg, deal=load_deal("mrvl_lite"), broker_kind="sim" if a.sim else "ibkr")
    pending = ex.load_unfinished()
    svc = Service(ex)
    token = os.environ.get("MERGERARB_TOKEN") or secrets.token_urlsafe(24)
    tf = write_token(token)
    httpd = HTTPServer(("127.0.0.1", a.port), make_handler(svc, token))
    httpd.timeout = 0.5
    print(f"merger-arb execution service on http://127.0.0.1:{a.port} ({'SIMULATOR' if a.sim else 'IBKR ' + cfg.mode})")
    print(f"token written to {tf} (never commit or share it); journal {journal}")
    if pending:
        print(f"journal has {len(pending)} unfinished plan(s) {pending}: new plans are blocked until POST /connect "
              f"and POST /recover reconcile them with the broker")
    last_trigger = 0.0
    try:
        while True:
            httpd.handle_request()
            if svc.ex.needs_pump():
                try:
                    svc.ex.pump()
                except Exception as exc:  # noqa: BLE001 - keep serving; the journal has the details
                    svc.ex.journal.write("pump_error", error=f"{type(exc).__name__}: {exc}")
            if svc.rungs and time.time() - last_trigger > 5:
                last_trigger = time.time()
                try:
                    svc.check_triggers()
                except Exception as exc:  # noqa: BLE001
                    svc.ex.journal.write("trigger_error", error=f"{type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        print("stopping")


if __name__ == "__main__":
    main()
