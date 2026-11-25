"""Headless smoke test of the Streamlit app (needs the local Yahoo snapshot; skipped without it)."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HAS_DATA = (ROOT / "data" / "raw" / "yahoo").exists() and any((ROOT / "data" / "raw" / "yahoo").iterdir())


@pytest.mark.skipif(not HAS_DATA, reason="no local market-data snapshot")
def test_app_runs_without_exceptions(monkeypatch):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("MERGERARB_ENABLE_EXECUTION", "1")
    at = st_testing.AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    assert any("Signal" in m.value for m in at.markdown)
