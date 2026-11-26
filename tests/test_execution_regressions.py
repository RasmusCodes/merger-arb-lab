"""Regression tests for the execution-layer audit of 2026-11-25. Every test here failed on the pre-fix code."""
import asyncio
import dataclasses
import datetime as dt
import http.client
import json
import threading
import uuid
from http.server import HTTPServer

import pytest

from mergerarb.deal import load_deal
from mergerarb.execution import readiness
from mergerarb.execution.broker import IBKRBroker, SimBroker, WhatIfError
from mergerarb.execution.config import ExecutionConfig
from mergerarb.execution.executor import PairExecutor, _exec_summary, _merge
from mergerarb.execution.journal import Journal
from mergerarb.execution.orders import build_pair_plan, plan_from_dict, validate_plan
from mergerarb.execution.service import Service, make_handler
from mergerarb.execution.trigger import Rung, evaluate_rung

DEAL = load_deal("mrvl_lite")
RTH = dt.datetime(2026, 11, 25, 15, 0, tzinfo=dt.timezone.utc)   # Wed 10:00 ET
QUOTES = {"LITE": {"last": 487.12, "bid": 486.9, "ask": 487.3, "prev_close": 490.0},
          "MRVL": {"last": 131.57, "bid": 131.55, "ask": 131.6, "prev_close": 132.0}}


def _plan(n=2, tl=487.0, al=131.6):
    return build_pair_plan(DEAL, n, tl, al, {"LITE": 487.12, "MRVL": 131.57})


def _ex(tmp_path, broker=None, **cfg):
    b = broker or SimBroker(quotes=QUOTES)
    c = ExecutionConfig(journal_path=tmp_path / "journal.jsonl", **cfg)
    ex = PairExecutor(b, c, Journal(c.journal_path))
    ex.connect()
    return ex


def _approve(ex, p):
    rep = ex.preflight(p, borrow_fee=0.0032, signal_decision="TRADE", now_utc=RTH)
    assert rep.ok, [(c.name, c.message) for c in rep.failures]
    return ex.approve(p, rep, ex.approval_phrase(p))


def _go(ex, p):
    return ex.submit(p, _approve(ex, p))


def _buys(b):
    return [h for h in b._orders.values() if h.action == "BUY"]


def _events(ex, name):
    return [r for r in ex.journal.read() if r["event"] == name]


@pytest.fixture
def ibkr():
    asyncio.set_event_loop(asyncio.new_event_loop())
    return IBKRBroker(ExecutionConfig())


# ----- A / B / B2 / C / H / I: consumption model, cancel and recover --------------------------------------------
def test_A_cancel_all_never_places_and_flags_final_imbalance(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex = _ex(tmp_path, b, unhedged_timeout_s=3600)
    st = _go(ex, _plan())
    assert st.legs["acquirer"].filled == 9 and len(_buys(b)) == 1
    ex.cancel_all("user")
    for _ in range(3):
        st = ex.pump()
    assert len(_buys(b)) == 1 and b.open_orders() == []          # old code sent a new LITE BUY (target:1)
    assert st.status == "NEEDS_ACTION" and st.imbalance_shares == -9
    assert any("UNHEDGED after cancel" in a for a in st.alerts)
    statuses = [r["status"] for r in _events(ex, "plan_status")]
    assert statuses.index("CANCELLING") < statuses.index("NEEDS_ACTION")


def test_B_rejected_target_is_not_resent(tmp_path):
    b = SimBroker(quotes=QUOTES, reject_symbols={"LITE"})
    ex = _ex(tmp_path, b)
    _go(ex, _plan())
    for _ in range(10):
        st = ex.pump()
    assert len(_buys(b)) == 1                                    # old code: 11 orders in 10 pumps
    assert st.status == "NEEDS_ACTION" and st.legs["target"].unplaced == 2
    assert any("ORDER ENDED UNFILLED" in a and "201" in a for a in st.alerts)
    assert _events(ex, "order_error") and _events(ex, "order_ended_unfilled")


def test_B2_expired_target_needs_a_fresh_residual_approval(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex = _ex(tmp_path, b)
    _go(ex, _plan())
    b.expire("LITE")                                             # DAY expiry / cancelled in TWS
    st = ex.pump()
    assert len(_buys(b)) == 1 and st.status == "NEEDS_ACTION"    # old code re-sent it at the stale limit
    with pytest.raises(PermissionError):
        ex.approve_residual(ex.residual_preflight("target", 2, now_utc=RTH), "PAPER yes")
    assert not ex.residual_preflight("target", 3, now_utc=RTH).ok          # more than the residual
    rep = ex.residual_preflight("target", 2, 487.5, now_utc=RTH)
    assert rep.ok, [(c.name, c.message) for c in rep.failures]
    phrase = ex.residual_phrase("target", 2, 487.5)
    assert phrase.endswith("TARGET 2 @ 487.50") and rep.context["phrase"] == phrase
    appr = ex.approve_residual(rep, phrase)
    b.fill_mode_by_symbol["LITE"] = "immediate"
    st = ex.submit_residual(appr)
    st = ex.pump()
    assert st.status == "FILLED" and len(_buys(b)) == 2 and _buys(b)[-1].limit_price == 487.5
    with pytest.raises(RuntimeError):
        ex.submit_residual(appr)                                 # one approval, one order
    expired = dataclasses.replace(appr, created_utc="2000-01-01T00:00:00+00:00")
    with pytest.raises(PermissionError):
        ex.submit_residual(expired)


def test_C_recover_never_revives_a_finished_plan(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode="none")
    ex = _ex(tmp_path, b)
    _go(ex, _plan())
    st = ex.cancel_all("user")
    assert st.status == "CANCELLED"
    n = len(b._orders)
    st = ex.recover()
    assert st.status == "CANCELLED" and len(b._orders) == n     # old code: PARTIAL plus new orders
    b2 = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex2 = _ex(tmp_path / "b", b2)
    _go(ex2, _plan())
    ex2.cancel_all("user")
    st2 = ex2.recover()
    assert st2.status == "NEEDS_ACTION" and len(_buys(b2)) == 1


def test_H_partial_fill_then_cancel_leaves_a_flagged_residual(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"MRVL": "immediate", "LITE": "partial"}, partial_fraction=0.5)
    ex = _ex(tmp_path, b)
    st = _go(ex, _plan(4))
    assert st.legs["target"].filled == 2
    b.expire("LITE")
    st = ex.pump()
    assert st.status == "NEEDS_ACTION" and st.legs["target"].unplaced == 2   # old code: silent PARTIAL forever
    assert len(_buys(b)) == 1


def test_I_late_fill_while_cancelling_is_monitored_and_flagged(tmp_path):
    class SlowCancel(SimBroker):
        def cancel(self, handle):            # the cancel is still in flight at the broker
            pass

    b = SlowCancel(quotes=QUOTES, fill_mode="none")
    ex = _ex(tmp_path, b)
    _go(ex, _plan())
    st = ex.cancel_all("user")
    assert st.status == "CANCELLING" and ex.needs_pump()         # the service loop keeps pumping it
    b.fill_mode = "immediate"                                     # ...and the short fills late
    st = ex.pump()
    assert st.status == "NEEDS_ACTION" and st.imbalance_shares == -9 and st.unhedged
    assert not _buys(b)
    assert any("UNHEDGED after cancel" in a for a in st.alerts)


# ----- D / F: rejections ----------------------------------------------------------------------------------------
def test_D_simultaneous_short_rejection_cancels_the_target(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none"})
    ex = _ex(tmp_path, b, sequencing="simultaneous")
    p = _plan()
    a = _approve(ex, p)
    b.short_level = 1.0
    st = ex.submit(p, a)
    st = ex.pump()
    assert st.status == "FAILED" and b.open_orders() == []      # old code left the LITE order working
    assert any("short not executed" in x for x in st.alerts)
    b2 = SimBroker(quotes=QUOTES)                                # LITE fills before the cancel can land
    ex2 = _ex(tmp_path / "b", b2, sequencing="simultaneous")
    a2 = _approve(ex2, p)
    b2.short_level = 1.0
    st2 = ex2.submit(p, a2)
    assert st2.status == "NEEDS_ACTION" and st2.imbalance_shares == 9 and st2.unhedged


def test_F_short_rejected_as_cancelled_means_failed(tmp_path):
    b = SimBroker(quotes=QUOTES)
    ex = _ex(tmp_path, b)
    p = _plan()
    a = _approve(ex, p)
    b.short_level = 1.0
    st = ex.submit(p, a)
    assert b._orders[p.order_ref("acquirer", 0)].status == "Cancelled"   # ib_async semantics, not 'Inactive'
    assert st.status == "FAILED" and not st.legs["target"].handles


def test_mismarked_cancel_is_not_resent(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex = _ex(tmp_path, b)
    p = _plan()
    _go(ex, p)
    b.mismark_cancelled(p.order_ref("target", 0))   # ib_async says Cancelled after an error, TWS still works it
    st = ex.pump()
    assert st.status == "NEEDS_ACTION" and len(_buys(b)) == 1
    b.fill_mode_by_symbol["LITE"] = "immediate"
    st = ex.pump()
    assert st.status == "FILLED" and len(_buys(b)) == 1 and st.legs["target"].filled == 2


# ----- K: what-if ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["error", "timeout", "empty"])
def test_K_failed_what_if_blocks_preflight_and_readiness(tmp_path, mode):
    ex = _ex(tmp_path, SimBroker(quotes=QUOTES, what_if_mode=mode, net_liq=3000.0))
    rep = ex.preflight(_plan(), 0.0032, "TRADE", now_utc=RTH)
    wi = next(c for c in rep.checks if c.name == "what-if margin")
    assert wi.status == "FAIL" and "Read-Only API" in wi.message and not rep.ok
    rows = {r[0]: r for r in readiness.run(SimBroker(quotes=QUOTES, what_if_mode=mode),
                                           ExecutionConfig(journal_path=tmp_path / "r.jsonl"))}
    assert rows["what-if margin (1 lot)"][1] == "FAIL" and "Read-Only API" in rows["what-if margin (1 lot)"][2]


def test_K_ibkr_what_if_timeout_and_empty_answer(ibkr, monkeypatch):
    monkeypatch.setattr(ibkr.ib, "isConnected", lambda: True)
    ibkr._contracts["MRVL"] = object()
    assert ibkr.ib.RequestTimeout == 10.0                         # was 0 = wait forever
    leg = _plan().acquirer
    monkeypatch.setattr(ibkr.ib, "whatIfOrder", lambda c, o: [])
    with pytest.raises(WhatIfError, match="rejected"):
        ibkr.what_if(leg, "DU1")

    def never(c, o):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(ibkr.ib, "whatIfOrder", never)
    with pytest.raises(WhatIfError, match="not answered"):
        ibkr.what_if(leg, "DU1")


# ----- reconnect: fresh library objects, fills from executions ---------------------------------------------------
def test_reconnect_takes_fills_from_executions(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode="none")
    ex = _ex(tmp_path, b)
    p = _plan()
    _go(ex, p)
    b.disconnect()
    assert ex.pump().status == "DISCONNECTED"
    b.fill_mode = "immediate"
    b.step()                                                     # the short fills while the API is down
    st = ex.recover()
    view = b._view(b._orders[p.order_ref("acquirer", 0)])
    assert (view.status, view.filled) == ("Filled", 0.0)          # completed order: no fill numbers
    assert st.legs["acquirer"].filled == 9 and not _buys(b)       # fills from executions; recover places nothing
    assert ex.pump().status == "FILLED" and len(_buys(b)) == 1


def test_ibkr_adapter_reads_fresh_trades_and_executions(ibkr, monkeypatch):
    from ib_async import Execution, Order, OrderState, Stock

    monkeypatch.setattr(ibkr.ib, "isConnected", lambda: True)
    monkeypatch.setattr(ibkr.ib, "sleep", lambda *a, **k: None)
    w = ibkr.ib.wrapper
    w.clientId = 17
    mrvl = Stock("MRVL", "SMART", "USD", primaryExchange="NASDAQ")
    ref = "MA:abcd1234:acquirer:0"
    o = Order(orderId=6, clientId=17, permId=9006, action="SELL", totalQuantity=9, orderType="LMT", lmtPrice=131.6,
              orderRef=ref)
    w.openOrder(6, mrvl, o, OrderState(status="Submitted"))
    w.error(6, 404, "Shares for this order are not immediately available for short sale", "")
    h = {x.order_ref: x for x in ibkr.refresh()}[ref]
    assert h.status == "ValidationError" and not h.is_done and "404" in h.error
    w.reset()                                                    # disconnect: ib_async drops every Trade
    w.clientId = 17
    w._results["completedOrders"] = []
    w.completedOrder(mrvl, dataclasses.replace(o), OrderState(status="Filled"))
    w.execDetails(-1, mrvl, Execution(execId="0001f4e8.57427bc3.01.01", side="SLD", shares=9.0, price=131.6,
                                      permId=9006, clientId=17, orderId=6, cumQty=9.0, avgPrice=131.6, orderRef=ref))
    h = {x.order_ref: x for x in ibkr.refresh()}[ref]
    assert (h.status, h.filled) == ("Filled", 0.0)                # what the library reports after the reconnect
    assert _merge(h, _exec_summary(ibkr.executions(), "MA:abcd1234:")[ref]).filled == 9.0


def test_ibkr_connect_is_idempotent_and_drops_stale_tickers(ibkr, monkeypatch, tmp_path):
    up = {"on": False, "calls": 0}
    monkeypatch.setattr(ibkr.ib, "isConnected", lambda: up["on"])

    def fake_connect(*a, **k):
        up["on"], up["calls"] = True, up["calls"] + 1

    monkeypatch.setattr(ibkr.ib, "connect", fake_connect)
    monkeypatch.setattr(ibkr.ib, "reqMarketDataType", lambda *a: None)
    monkeypatch.setattr(ibkr.ib, "reqExecutions", lambda *a: [])
    ibkr.connect()
    ibkr._tickers["MRVL"] = "subscription of the first session"
    ibkr.connect()
    assert up["calls"] == 1 and "MRVL" in ibkr._tickers           # a second connect is a no-op
    up["on"] = False                                              # the socket dropped
    ibkr.connect()
    assert up["calls"] == 2 and not ibkr._tickers                 # market data is requested afresh
    monkeypatch.setattr(ibkr.ib, "qualifyContracts", lambda c: [None])
    with pytest.raises(RuntimeError, match="qualify"):
        ibkr._contract("NOPE", "NASDAQ")
    ex = _ex(tmp_path)
    svc = Service(ex)
    svc.handle("POST", "/connect", {}, {})
    svc.handle("POST", "/connect", {}, {})
    assert ex.broker.connect_calls == 1


# ----- plan integrity, sizes, duplicates ---------------------------------------------------------------------------
def test_odd_sizes_are_refused_not_rounded():
    for n in (1, 3, 5, 9):
        with pytest.raises(ValueError, match="whole number"):
            build_pair_plan(DEAL, n, 487.0, 131.6, {})
    assert [build_pair_plan(DEAL, n, 487.0, 131.6, {}).acquirer.quantity for n in (2, 4, 6)] == [9, 18, 27]
    p = _plan()
    bad = dataclasses.replace(p, legs=(dataclasses.replace(p.acquirer, quantity=4), dataclasses.replace(p.target, quantity=1)))
    assert dict((n, s) for n, s, _ in validate_plan(bad, "paper", 15_000, 0.02))["exact ratio"] == "FAIL"


def test_duplicate_key_uses_the_trade_date_not_the_plan_timestamp(tmp_path):
    ex = _ex(tmp_path)
    p = _plan()
    _go(ex, p)
    p2 = dataclasses.replace(p, plan_id=str(uuid.uuid4()), created_utc="2020-01-01T00:00:00+00:00")
    rep = ex.preflight(p2, 0.0032, "TRADE", now_utc=RTH)
    assert any(c.name == "duplicate plan" and c.status == "FAIL" for c in rep.checks)


def _req(port, method, path, body=None, raw=None, auth="Bearer tok"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=data, headers={"Authorization": auth})
    r = c.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    c.close()
    return out


def test_service_rejects_edited_plans_and_bad_requests(tmp_path):
    ex = _ex(tmp_path)
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(Service(ex), "tok"))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        good = _plan().to_dict()
        args = {"borrow_fee": 0.003, "signal_decision": "TRADE"}
        nohash = dict(good)
        nohash.pop("plan_hash")
        assert _req(port, "POST", "/preflight", {"plan": nohash, **args})[0] == 400
        p = plan_from_dict(good)
        ratio1 = dataclasses.replace(p, ratio=1.0, legs=(p.acquirer, dataclasses.replace(p.target, quantity=9)))
        code, body = _req(port, "POST", "/preflight", {"plan": ratio1.to_dict(), **args})
        assert code == 400 and "ratio" in body["error"]               # old code: pre-flight ok, executed 9 vs 9
        gtc = dataclasses.replace(p, legs=tuple(dataclasses.replace(l, tif="GTC") for l in p.legs))
        code, body = _req(port, "POST", "/preflight", {"plan": gtc.to_dict(), **args})
        assert code == 400 and "GTC" in body["error"]
        code, body = _req(port, "POST", "/plan", {"target_shares": 3, "target_limit": 487.0, "acquirer_limit": 131.6})
        assert code == 400 and "whole number" in body["error"]
        assert _req(port, "POST", "/plan", {"target_shares": 2, "target_limit": "487", "acquirer_limit": 131.6})[0] == 400
        assert _req(port, "POST", "/plan", raw=b"{not json")[0] == 400
        assert _req(port, "GET", "/status", auth="Bearer t\xe9k")[0] == 401
        code, body = _req(port, "POST", "/preflight", {"plan": good})            # signal and fee missing
        fails = {c["name"] for c in body["checks"] if c["status"] == "FAIL"}
        assert code == 200 and {"model signal input", "borrow fee"} <= fails
        code, body = _req(port, "POST", "/preflight", {"plan": good, **args})
        assert {c["name"]: c["status"] for c in body["checks"]}["plan integrity"] == "PASS"
    finally:
        httpd.shutdown()


# ----- alerts, journal rebuild, triggers, readiness ---------------------------------------------------------------
def test_unhedged_cap_fires_for_one_lot_and_shows_in_status(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex = _ex(tmp_path, b, unhedged_timeout_s=0.0)
    st = _go(ex, _plan())
    assert ex.unhedged_cap(st) == pytest.approx(0.25 * 9 * 131.6)    # below $500; the old $1,500 never fired
    assert st.unhedged and st.status == "PARTIAL" and any(a.startswith("UNHEDGED") for a in st.alerts)
    s = Service(ex).status()
    assert s["alerts"][0].startswith("UNHEDGED") and s["attention"]


def test_fill_inside_the_placement_window_is_journalled(tmp_path):
    ex = _ex(tmp_path, SimBroker(quotes=QUOTES, fill_on_place=True))
    st = _go(ex, _plan())
    assert st.status == "FILLED"
    assert sorted(f["symbol"] for f in _events(ex, "fill")) == ["LITE", "MRVL"]
    ex.recover()
    assert len(_events(ex, "plan_filled")) == 1


def test_service_start_rebuilds_unfinished_plans_from_the_journal(tmp_path):
    b = SimBroker(quotes=QUOTES, fill_mode_by_symbol={"LITE": "none", "MRVL": "immediate"})
    ex = _ex(tmp_path, b)
    p = _plan()
    _go(ex, p)                                                   # PARTIAL: short filled, LITE working
    ex2 = PairExecutor(b, ex.cfg, Journal(ex.cfg.journal_path))    # restart; the broker still has the orders
    assert ex2.load_unfinished() == [p.plan_id]
    assert ex2.state.status == "UNRECONCILED" and ex2.blocking_reason()
    ex2.connect()
    rep = ex2.preflight(_plan(4), 0.0032, "TRADE", now_utc=RTH)
    assert any(c.name == "execution state" and c.status == "FAIL" for c in rep.checks)
    st = ex2.recover()
    assert st.status == "PARTIAL" and st.legs["acquirer"].filled == 9 and len(st.legs["target"].handles) == 1
    b.fill_mode_by_symbol["LITE"] = "immediate"
    assert ex2.pump().status == "FILLED" and len(_buys(b)) == 1
    b3 = SimBroker(quotes=QUOTES, fill_mode="none")
    ex3 = _ex(tmp_path / "c", b3)
    _go(ex3, _plan())
    ex4 = PairExecutor(SimBroker(quotes=QUOTES), ex3.cfg, Journal(ex3.cfg.journal_path))   # broker forgot them
    ex4.load_unfinished()
    ex4.connect()
    with pytest.raises(RuntimeError):
        ex4.close("taking it over manually")                    # not reconciled yet
    st = ex4.recover()
    assert st.status == "NEEDS_ACTION" and any("not reported by the broker" in r for r in st.action_required)
    with pytest.raises(PermissionError):
        ex4.close("short")
    assert ex4.close("checked in TWS: the order is gone").status == "CLOSED" and ex4.blocking_reason() is None


def test_triggers_need_fresh_quotes_and_the_regular_session(tmp_path):
    args = (Rung(0.25, 1), 499.0, 500.0, 140.0, 140.2, 4.5)
    assert evaluate_rung(*args, quote_age_s=2.0, session_open=True).hit
    assert not evaluate_rung(*args, quote_age_s=60.0, session_open=True).hit
    assert not evaluate_rung(*args, quote_age_s=None, session_open=True).hit
    closed = evaluate_rung(*args, quote_age_s=2.0, session_open=False)
    assert not closed.hit and "session" in closed.note
    old = RTH - dt.timedelta(minutes=5)
    b = SimBroker(quotes={"LITE": {"last": 480.0, "bid": 479.9, "ask": 480.0, "time_utc": old},
                          "MRVL": {"last": 140.0, "bid": 140.0, "ask": 140.1, "time_utc": old}}, data_type="live")
    svc = Service(_ex(tmp_path, b), clock=lambda: RTH)
    svc.handle("POST", "/triggers", {"rungs": [{"target_gross_spread": 0.30, "lots": 1}]}, {})
    assert [s["hit"] for s in svc.check_triggers()] == [False]        # 31% spread, but quotes 5 minutes old


def test_readiness_blocks_on_missing_data_delayed_live_data_and_unknown_borrow(tmp_path, monkeypatch):
    cfg = ExecutionConfig(journal_path=tmp_path / "r.jsonl")
    none = {"LITE": {"last": None, "bid": None, "ask": None}, "MRVL": {"last": None, "bid": None, "ask": None}}
    rows = {r[0]: r[1] for r in readiness.run(SimBroker(quotes=none), cfg)}       # old code: ValueError crash
    assert rows["LITE market data"] == "FAIL" and rows["what-if margin (1 lot)"] == "FAIL"
    monkeypatch.setenv("MERGERARB_ALLOW_LIVE", "1")
    live = readiness.run(SimBroker(account="U7654321", quotes=QUOTES, data_type="delayed"),
                         ExecutionConfig(mode="live", journal_path=tmp_path / "r2.jsonl"))
    assert dict((r[0], r[1]) for r in live)["LITE market data"] == "FAIL"
    unk = readiness.run(SimBroker(quotes=QUOTES, short_level=float("nan")), cfg)
    assert dict((r[0], r[1]) for r in unk)["MRVL shortability"] == "FAIL"
