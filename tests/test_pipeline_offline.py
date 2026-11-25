"""End-to-end pipeline on the SYNTHETIC offline sample (no network, no licensed data)."""
import pytest

from mergerarb.pipeline import Inputs, run


@pytest.fixture()
def sample_mode(monkeypatch):
    monkeypatch.setenv("MERGERARB_DATA", "sample")


def test_pipeline_runs_offline_on_synthetic_sample(sample_mode):
    res = run(inp=Inputs(mc_paths=800))
    assert res["data_mode"] == "sample"
    assert res["implied_value"] == pytest.approx(4.5 * res["M0"])
    assert res["gross_spread"] == pytest.approx(4.5 * res["M0"] / res["L0"] - 1)
    assert {"break_cases", "scenario_table", "mc", "signal", "sizing"} <= set(res)
    assert len(res["scenario_table"]) == 12
    # synthetic data can never produce a TRADE signal
    assert res["signal"].decision == "NO TRADE"
    assert any("stale" in r for r in res["signal"].reasons)
    assert res["calibration"]["acq_vol_expiry"].startswith("default")
    for _, (q, a) in res["quotes"].items():
        assert "SYNTHETIC" in q.source and not a.is_live
