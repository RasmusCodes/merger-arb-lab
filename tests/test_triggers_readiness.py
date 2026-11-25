import pytest

from mergerarb.execution.broker import SimBroker
from mergerarb.execution.config import ExecutionConfig
from mergerarb.execution.executor import PairExecutor
from mergerarb.execution.journal import Journal
from mergerarb.execution import readiness
from mergerarb.execution.trigger import Rung, evaluate_rung, executable_spread


def test_executable_spread_uses_bid_for_short_and_ask_for_long():
    assert executable_spread(500.0, 140.0, 4.5) == pytest.approx(630.0 / 500.0 - 1)
    s = evaluate_rung(Rung(0.25, 1), 499.0, 500.0, 140.0, 140.2, 4.5)
    assert s.executable_spread == pytest.approx(0.26) and s.hit
    assert s.mid_spread > s.executable_spread
    assert s.target_limit >= 500.0 and s.acquirer_limit <= 140.0      # limits at the touch plus slippage


def test_rung_not_hit_and_bad_quotes_and_non_live():
    assert not evaluate_rung(Rung(0.30, 1), 499.0, 500.0, 140.0, 140.2, 4.5).hit
    bad = evaluate_rung(Rung(0.10, 1), 501.0, 500.0, 140.0, 140.2, 4.5)     # crossed target quote
    assert not bad.hit and bad.executable_spread is None
    stale = evaluate_rung(Rung(0.10, 1), 499.0, 500.0, 140.0, 140.2, 4.5, quotes_live=False)
    assert not stale.hit and "not live" in stale.note


def test_readiness_on_simulator(tmp_path):
    b = SimBroker(quotes={"LITE": {"last": 487.12, "bid": 486.9, "ask": 487.3},
                          "MRVL": {"last": 131.57, "bid": 131.55, "ask": 131.6}})
    rows = readiness.run(b, ExecutionConfig(journal_path=tmp_path / "j.jsonl"))
    names = {r[0]: r[1] for r in rows}
    assert names["API connection"] == "PASS" and names["account type"] == "PASS"
    assert names["MRVL shortability"] == "PASS" and names["what-if margin (1 lot)"] == "PASS"
    assert names["LITE market data"] == "WARN"          # simulated data is never "live"
    assert not [r for r in rows if r[1] == "FAIL"]


def test_readiness_flags_live_account_in_paper_mode(tmp_path):
    b = SimBroker(account="U9999999", quotes={"LITE": {"last": 487.0, "bid": 486.9, "ask": 487.3},
                                              "MRVL": {"last": 131.6, "bid": 131.55, "ask": 131.6}})
    rows = readiness.run(b, ExecutionConfig(journal_path=tmp_path / "j.jsonl"))
    assert dict((r[0], r[1]) for r in rows)["account type"] == "FAIL"


def test_service_trigger_journal(tmp_path):
    from mergerarb.execution.service import Service

    b = SimBroker(quotes={"LITE": {"last": 480.0, "bid": 479.9, "ask": 480.0},
                          "MRVL": {"last": 140.0, "bid": 140.0, "ask": 140.1}}, data_type="live")
    ex = PairExecutor(b, ExecutionConfig(journal_path=tmp_path / "j.jsonl"), Journal(tmp_path / "j.jsonl"))
    ex.connect()
    svc = Service(ex)
    svc.handle("POST", "/triggers", {"rungs": [{"target_gross_spread": 0.30, "lots": 1},
                                               {"target_gross_spread": 0.40, "lots": 1}]}, {})
    st = svc.handle("GET", "/triggers", {}, {})["status"]
    assert [s["hit"] for s in st] == [True, False]          # 4.5*140/480-1 = 31.25%
    svc.check_triggers()
    hits = [r for r in ex.journal.read() if r["event"] == "trigger_hit"]
    assert len(hits) == 1                                    # logged once per rung per day
    assert not [r for r in ex.journal.read() if r["event"] == "order_submitted"]   # triggers never trade
