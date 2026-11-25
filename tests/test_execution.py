import dataclasses
import datetime as dt
import json
import threading
import urllib.request

import pytest

from mergerarb.deal import load_deal
from mergerarb.execution.broker import SimBroker
from mergerarb.execution.config import ExecutionConfig
from mergerarb.execution.executor import PairExecutor, market_session
from mergerarb.execution.journal import Journal
from mergerarb.execution.orders import build_pair_plan, plan_from_dict, validate_plan

DEAL = load_deal("mrvl_lite")
RTH = dt.datetime(2026, 11, 25, 15, 0, tzinfo=dt.timezone.utc)   # Wed 10:00 ET
QUOTES = {"LITE": {"last": 487.12, "prev_close": 490.0}, "MRVL": {"last": 131.57, "prev_close": 132.0}}


def _plan(shares=2, tl=487.0, al=131.6):
    return build_pair_plan(DEAL, shares, tl, al, {"LITE": 487.12, "MRVL": 131.57})


def _ex(tmp_path, broker=None, **cfg):
    b = broker or SimBroker(quotes=QUOTES)
    c = ExecutionConfig(journal_path=tmp_path / "journal.jsonl", **cfg)
    ex = PairExecutor(b, c, Journal(c.journal_path))
    ex.connect()
    return ex


def _approved(ex, plan, signal="TRADE"):
    rep = ex.preflight(plan, borrow_fee=0.0032, signal_decision=signal, now_utc=RTH)
    assert rep.ok, [c.__dict__ for c in rep.failures]
    return ex.approve(plan, rep, ex.approval_phrase(plan))


def test_plan_exact_hedge_and_exports():
    p = _plan()
    assert (p.target.quantity, p.acquirer.quantity) == (2, 9)
    assert p.hedge_residual() == 0
    assert (p.acquirer.action, p.target.action) == ("SELL", "BUY")
    q = plan_from_dict(json.loads(p.to_json()))
    assert q.plan_hash() == p.plan_hash()
    rows = p.to_csv().strip().splitlines()
    assert rows[0].startswith("plan_id,plan_hash") and len(rows) == 3
    d = json.loads(p.to_json())
    d["legs"][0]["quantity"] = 90
    with pytest.raises(ValueError):
        plan_from_dict(d)


def test_static_validation():
    checks = {n: s for n, s, _ in validate_plan(_plan(tl=520.0), "paper", 15_000, 0.02)}
    assert checks["LITE price band"] == "FAIL"
    odd = build_pair_plan(DEAL, 3, 487.0, 131.6, {"LITE": 487.12, "MRVL": 131.57})
    assert dict((n, s) for n, s, _ in validate_plan(odd, "paper", 15_000, 0.02))["exact ratio"] == "WARN"
    mkt = build_pair_plan(DEAL, 2, None, None, {"LITE": 487.12, "MRVL": 131.57}, order_type="MKT")
    assert "FAIL" in {s for n, s, _ in validate_plan(mkt, "live", 15_000, 0.02) if "order type" in n}


def test_market_session():
    assert market_session(RTH)[0]
    assert not market_session(dt.datetime(2026, 11, 26, 15, 0, tzinfo=dt.timezone.utc))[0]   # Thanksgiving
    assert not market_session(dt.datetime(2026, 11, 27, 18, 30, tzinfo=dt.timezone.utc))[0]  # early close 13:00 ET


def test_preflight_blocks_wrong_account_type_and_no_borrow(tmp_path):
    ex = _ex(tmp_path, SimBroker(account="U1234567", quotes=QUOTES))
    rep = ex.preflight(_plan(), 0.0032, "TRADE", now_utc=RTH)
    assert any(c.name == "account mode" and c.status == "FAIL" for c in rep.checks)
    ex2 = _ex(tmp_path, SimBroker(quotes=QUOTES, short_level=1.0))
    rep2 = ex2.preflight(_plan(), 0.0032, "TRADE", now_utc=RTH)
    assert any(c.name == "borrow availability" and c.status == "FAIL" for c in rep2.checks)
    rep3 = ex2.preflight(_plan(), 0.05, "TRADE", now_utc=RTH)
    assert any(c.name == "borrow fee" and c.status == "FAIL" for c in rep3.checks)


def test_approval_phrase_and_signal_override(tmp_path):
    ex = _ex(tmp_path)
    p = _plan()
    rep = ex.preflight(p, 0.0032, "WATCH", now_utc=RTH)
    assert not rep.ok and rep.only_signal_fails()
    with pytest.raises(PermissionError):
        ex.approve(p, rep, ex.approval_phrase(p))                       # signal fail without a reason
    with pytest.raises(PermissionError):
        ex.approve(p, rep, "PAPER wrong", override_reason="testing the paper workflow only")
    a = ex.approve(p, rep, ex.approval_phrase(p), override_reason="testing the paper workflow only")
    assert a.override_reason and a.mode == "paper"


def test_short_first_full_fill_journal(tmp_path):
    ex = _ex(tmp_path)
    p = _plan()
    st = ex.submit(p, _approved(ex, p))
    st = ex.pump()
    assert st.status == "FILLED"
    assert st.legs["acquirer"].filled == 9 and st.legs["target"].filled == 2
    events = [r["event"] for r in ex.journal.read()]
    for e in ("approved", "plan_submitted", "order_submitted", "fill", "plan_filled"):
        assert e in events
    assert events.index("order_submitted") < events.index("fill")


def test_never_filled_on_submission_and_partial_fills(tmp_path):
    ex = _ex(tmp_path, SimBroker(quotes=QUOTES, fill_mode="none"))
    p = _plan()
    st = ex.submit(p, _approved(ex, p))
    assert st.status == "WORKING" and st.legs["acquirer"].filled == 0
    assert not st.legs["target"].handles            # short-first: no target order until the short fills
    ex.broker.fill_mode, ex.broker.partial_fraction = "partial", 0.5
    seen = []
    for _ in range(10):
        st = ex.pump()
        seen.append((st.status, st.legs["acquirer"].filled, st.legs["target"].filled))
        if st.status == "FILLED":
            break
    assert st.status == "FILLED"
    assert all(s != "FILLED" for s, a, t in seen[:-1])
    assert all(t * 4.5 <= a + 1e-9 for _, a, t in seen)   # target never bought ahead of the short


def test_unhedged_alert_simultaneous(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex = _ex(tmp_path, b, sequencing="simultaneous", max_unhedged_usd=500.0, unhedged_timeout_s=0.0)
    p = _plan()
    ex.submit(p, _approved(ex, p))
    st = ex.pump()
    assert st.status == "IMBALANCED"
    assert st.imbalance_shares == -9 and "UNHEDGED" in st.alerts[-1]
    st = ex.cancel_all("test")
    assert st.status == "CANCELLED" and "imbalance" in st.alerts[-1]


def test_short_rejection_sends_no_target(tmp_path):
    b = SimBroker(quotes=QUOTES)
    ex = _ex(tmp_path, b)
    p = _plan()
    a = _approved(ex, p)
    b.short_level = 1.0                                   # borrow disappears between approval and submission
    st = ex.submit(p, a)
    assert st.status == "FAILED" and not st.legs["target"].handles


def test_duplicate_and_expiry(tmp_path):
    ex = _ex(tmp_path)
    p = _plan()
    first = _approved(ex, p)
    ex.submit(p, first)
    ex.pump()
    # layer 1: pre-flight flags the duplicate, so no new approval can be issued
    rep = ex.preflight(p, 0.0032, "TRADE", now_utc=RTH)
    assert any(c.name == "duplicate plan" and c.status == "FAIL" for c in rep.failures)
    with pytest.raises(PermissionError):
        ex.approve(p, rep, ex.approval_phrase(p))
    # layer 2: re-using the earlier (still unexpired) approval is refused at submission
    with pytest.raises(RuntimeError):
        ex.submit(p, first)
    ex2 = _ex(tmp_path / "b", approval_ttl_s=0.0)
    p2 = _plan(4, 487.0, 131.6)
    a2 = _approved(ex2, p2)
    with pytest.raises(PermissionError):
        ex2.submit(p2, a2)


def test_disconnect_and_recover(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode="none")
    ex = _ex(tmp_path, b)
    p = _plan()
    ex.submit(p, _approved(ex, p))
    b.disconnect()
    assert ex.pump().status == "DISCONNECTED"
    b.fill_mode = "immediate"
    st = ex.recover()
    st = ex.pump()
    assert st.status == "FILLED"
    assert "recovered" in [r["event"] for r in ex.journal.read()]


def test_live_mode_needs_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MERGERARB_ALLOW_LIVE", raising=False)
    assert ExecutionConfig(mode="live").validate()
    assert ExecutionConfig(host="10.0.0.5").validate()
    monkeypatch.setenv("MERGERARB_ALLOW_LIVE", "1")
    assert ExecutionConfig(mode="live").validate() == []


def test_service_requires_token_and_local_host(tmp_path):
    from http.server import HTTPServer

    from mergerarb.execution.service import Service, make_handler

    ex = _ex(tmp_path)
    svc = Service(ex)
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(svc, "secret-token"))
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    try:
        def get(headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/status", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None

        assert get({})[0] == 401
        assert get({"Authorization": "Bearer wrong"})[0] == 401
        assert get({"Authorization": "Bearer secret-token", "Origin": "https://evil.example"})[0] == 401
        code, body = get({"Authorization": "Bearer secret-token"})
        assert code == 200 and body["mode"] == "paper" and body["connected"]
    finally:
        httpd.shutdown()
