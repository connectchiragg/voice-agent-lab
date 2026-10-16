"""The worker's writable data: performance.json is written to VALR_PERFORMANCE_PATH (on the VM
/data-live, the worker's own mount) and, while that file does not exist yet, starts from the
read-only copy at VALR_PERFORMANCE_FALLBACK (/data/performance.json). Synthetic data only.
"""
import dataclasses
import json

from app.profile import config, post_call
from tests.test_profile_evaluate import COMP
from tests.test_profile_insights import CueLLM, meeting, scored_iv, settings, write_perf


def test_settings_read_the_fallback_env(monkeypatch, tmp_path):
    monkeypatch.delenv("VALR_PERFORMANCE_FALLBACK", raising=False)
    assert config.settings().performance_fallback is None
    monkeypatch.setenv("VALR_PERFORMANCE_PATH", str(tmp_path / "live" / "performance.json"))
    monkeypatch.setenv("VALR_PERFORMANCE_FALLBACK", str(tmp_path / "ro" / "performance.json"))
    cfg = config.settings()
    assert cfg.performance_path == tmp_path / "live" / "performance.json"
    assert cfg.performance_fallback == tmp_path / "ro" / "performance.json"


def test_first_write_starts_from_the_read_only_copy(tmp_path):
    seed = settings(tmp_path)
    write_perf(seed, [scored_iv("2026-10-01", 5, "fathom:800")])
    live = tmp_path / "live"
    cfg = dataclasses.replace(seed, performance_path=live / "performance.json",
                              performance_csv=live / "performance_scores.csv",
                              performance_fallback=seed.performance_path)
    before = seed.performance_path.read_text()
    out = post_call.process(meeting(), cfg, CueLLM(), COMP)
    assert out["status"] == "ready" and out["performance_updated"]
    perf = json.loads(cfg.performance_path.read_text())
    assert {iv["interview_id"] for iv in perf["interviews"]} == {"fathom:800", "fathom:901"}
    assert cfg.performance_csv.exists()
    assert seed.performance_path.read_text() == before  # the read-only copy is never written


def test_an_existing_live_file_wins_over_the_fallback(tmp_path):
    seed = settings(tmp_path)
    write_perf(seed, [scored_iv("2026-10-01", 5, "fathom:800")])
    live = dataclasses.replace(seed, performance_path=tmp_path / "live" / "performance.json")
    live.performance_path.parent.mkdir()
    write_perf(live, [scored_iv("2026-10-05", 6, "fathom:850")])
    cfg = dataclasses.replace(live, performance_csv=tmp_path / "live" / "performance_scores.csv",
                              performance_fallback=seed.performance_path)  # exists, must be ignored
    assert post_call.process(meeting(), cfg, CueLLM(), COMP)["performance_updated"]
    ids = {iv["interview_id"] for iv in json.loads(cfg.performance_path.read_text())["interviews"]}
    assert ids == {"fathom:850", "fathom:901"}
