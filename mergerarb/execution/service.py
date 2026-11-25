"""Local trading service: the ONLY process that talks to TWS / IB Gateway.

    python -m mergerarb.execution.service            # paper mode, TWS paper port 7497
    python -m mergerarb.execution.service --sim      # offline simulator (no IBKR needed)
    python -m mergerarb.execution.service --app gateway --port 8813

Security:
  * binds to 127.0.0.1 only; requests from a non-local Host/Origin are refused (blocks browser CSRF);
  * every request needs ``Authorization: Bearer <token>``; the token is random per start (or MERGERARB_TOKEN) and
    written to ~/.mergerarb/service_token for the local dashboard - it is never committed or published;
  * single-threaded: one IBKR client, no concurrent order handling;
  * live mode additionally needs MERGERARB_ALLOW_LIVE=1 and --mode live (see ExecutionConfig.live_allowed).
"""
from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import os
import pathlib
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from ..deal import load_deal
from .broker import IBKRBroker, SimBroker
from .config import ExecutionConfig
from .executor import PairExecutor, PreflightReport
from .orders import build_pair_plan, plan_from_dict
from .trigger import Rung, evaluate_rung

TOKEN_FILE = pathlib.Path.home() / ".mergerarb" / "service_token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Service:
    def __init__(self, executor: PairExecutor, deal_id: str = "mrvl_lite"):
        self.ex = executor
        self.deal = load_deal(deal_id)
        self.reports: dict[str, tuple[float, PreflightReport]] = {}
        self.rungs: list[Rung] = []
        self.hits_logged: set[tuple[str, float]] = set()

    def check_triggers(self) -> list[dict]:
        """Evaluate armed rungs on broker quotes; journal each rung's first hit per day. Never sends orders."""
        if not self.rungs or not self.ex.broker.is_connected():
            return []
        T, A = self.deal.target.ticker, self.deal.acquirer.ticker
        qt, qa = self.ex.broker.quote(T), self.ex.broker.quote(A)
        live = qt.data_type == "live" and qa.data_type == "live"
        out = []
        for r in self.rungs:
            s = evaluate_rung(r, qt.bid, qt.ask, qa.bid, qa.ask, self.deal.exchange_ratio, self.deal.cash_per_share,
                              quotes_live=live)
            key = (s.checked_utc[:10], r.target_gross_spread)
            if s.hit and key not in self.hits_logged:
                self.hits_logged.add(key)
                self.ex.journal.write("trigger_hit", rung=r.target_gross_spread, lots=r.lots,
                                      executable_spread=s.executable_spread, target_limit=s.target_limit,
                                      acquirer_limit=s.acquirer_limit)
            out.append({**dataclasses.asdict(s), "rung": dataclasses.asdict(r), "data_types": [qt.data_type, qa.data_type]})
        return out

    def status(self) -> dict:
        st = self.ex.state
        return {"connected": self.ex.broker.is_connected(), "mode": self.ex.cfg.mode, "account": self.ex.account,
                "port": self.ex.cfg.effective_port, "execution": st.summary() if st else None}

    def handle(self, method: str, path: str, body: dict, query: dict) -> dict:
        ex = self.ex
        if method == "GET" and path == "/status":
            if ex.state and ex.broker.is_connected():
                ex.pump()
            return self.status()
        if method == "GET" and path == "/journal":
            return {"events": ex.journal.read(int(query.get("limit", ["200"])[0]))}
        if method == "POST" and path == "/connect":
            return {"account": ex.connect(), **self.status()}
        if method == "POST" and path == "/plan":
            refs = {}
            for sym in (self.deal.target.ticker, self.deal.acquirer.ticker):
                q = ex.broker.quote(sym)
                refs[sym] = q.last or ((q.bid + q.ask) / 2 if q.bid and q.ask else None)
            plan = build_pair_plan(self.deal, int(body["target_shares"]), body.get("target_limit"),
                                   body.get("acquirer_limit"), refs, body.get("order_type", "LMT"))
            return {"plan": plan.to_dict(), "approval_phrase": ex.approval_phrase(plan)}
        if method == "POST" and path == "/preflight":
            plan = plan_from_dict(body["plan"])
            rep = ex.preflight(plan, body.get("borrow_fee"), body.get("signal_decision"))
            self.reports[rep.plan_hash] = (time.time(), rep)
            return {"ok": rep.ok, "only_signal_fails": rep.only_signal_fails(),
                    "checks": [c.__dict__ for c in rep.checks], "context": rep.context,
                    "approval_phrase": ex.approval_phrase(plan)}
        if method == "POST" and path == "/approve_submit":
            plan = plan_from_dict(body["plan"])
            ts, rep = self.reports.get(plan.plan_hash(), (0, None))
            if rep is None or time.time() - ts > ex.cfg.approval_ttl_s:
                raise PermissionError("run pre-flight for this exact plan first (or the report expired)")
            appr = ex.approve(plan, rep, body.get("typed", ""), body.get("override_reason"))
            st = ex.submit(plan, appr)
            return {"execution": st.summary()}
        if method == "POST" and path == "/triggers":
            self.rungs = [Rung(float(r["target_gross_spread"]), int(r["lots"])) for r in body.get("rungs", [])]
            ex.journal.write("triggers_armed", rungs=[dataclasses.asdict(r) for r in self.rungs])
            return {"armed": [dataclasses.asdict(r) for r in self.rungs]}
        if method == "GET" and path == "/triggers":
            return {"armed": [dataclasses.asdict(r) for r in self.rungs], "status": self.check_triggers()}
        if method == "POST" and path == "/pump":
            return {"execution": ex.pump().summary()}
        if method == "POST" and path == "/cancel":
            return {"execution": ex.cancel_all(body.get("reason", "user request")).summary()}
        if method == "POST" and path == "/recover":
            st = ex.recover()
            return {"execution": st.summary() if st else None}
        raise LookupError(f"no route {method} {path}")


def make_handler(service: Service, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mergerarb-local/0.1"

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
            host = (self.headers.get("Host") or "").split(":")[0]
            origin = self.headers.get("Origin")
            if host not in LOCAL_HOSTS:
                return False
            if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                return False
            auth = self.headers.get("Authorization", "")
            return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token)

        def _dispatch(self, method: str) -> None:
            if not self._authorised():
                return self._send(401, {"error": "unauthorised"})
            u = urlparse(self.path)
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                self._send(200, service.handle(method, u.path, body, parse_qs(u.query)))
            except PermissionError as e:
                self._send(403, {"error": str(e)})
            except LookupError as e:
                self._send(404, {"error": str(e)})
            except (ConnectionError, RuntimeError, ValueError) as e:
                self._send(409, {"error": str(e)})

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
    a = ap.parse_args(argv)
    cfg = ExecutionConfig(mode=a.mode, app=a.app, port=a.ib_port, client_id=a.client_id, account=a.account)
    problems = cfg.validate()
    if problems:
        sys.exit("refusing to start: " + "; ".join(problems))
    if a.sim:
        broker = SimBroker(quotes={"LITE": {"last": 487.12, "prev_close": 544.38}, "MRVL": {"last": 131.57, "prev_close": 144.74}})
    else:
        broker = IBKRBroker(cfg)
    svc = Service(PairExecutor(broker, cfg))
    token = os.environ.get("MERGERARB_TOKEN") or secrets.token_urlsafe(24)
    tf = write_token(token)
    httpd = HTTPServer(("127.0.0.1", a.port), make_handler(svc, token))
    httpd.timeout = 0.5
    print(f"merger-arb execution service on http://127.0.0.1:{a.port} ({'SIMULATOR' if a.sim else 'IBKR ' + cfg.mode})")
    print(f"token written to {tf} (never commit or share it)")
    last_trigger = 0.0
    try:
        while True:
            httpd.handle_request()
            if svc.ex.state and svc.ex.state.status in ("WORKING", "PARTIAL", "IMBALANCED"):
                try:
                    svc.ex.pump()
                except Exception as exc:  # keep serving; the journal has the details
                    svc.ex.journal.write("pump_error", error=str(exc))
            if svc.rungs and time.time() - last_trigger > 5:
                last_trigger = time.time()
                try:
                    svc.check_triggers()
                except Exception as exc:
                    svc.ex.journal.write("trigger_error", error=str(exc))
    except KeyboardInterrupt:
        print("stopping")


if __name__ == "__main__":
    main()
