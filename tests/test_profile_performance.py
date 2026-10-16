"""VALR performance v2: granular rubric normalisation, series/badges, breakdowns, callouts, outputs."""
import csv
import io
import json
import stat
from pathlib import Path

import pytest

from app.profile import performance, rubric
from app.profile.profile import estimate_tokens
from tests.test_profile_evaluate import COMP, FakeLLM, analysis, rubric_reply, settings


def scored(day: str, base: int, company="Acme", round_type="system_design",
           stage="series_a_b", iid=None):
    out = rubric.normalise(rubric_reply(base))
    out.update(interview_id=iid or f"fathom:{day}", source="fathom", id=day, title="t",
               date=f"{day}T10:00:00Z", company=company, round_type=round_type,
               company_stage=stage, transcript_used=True)
    return out


def test_normalise_clamps_caps_and_computes_groups():
    raw = rubric_reply(6)
    items = {i["key"]: i for i in raw["scores"]}
    items["technical.system_design"]["score"] = 14
    items["technical.cs_fundamentals"]["quote"] = " ".join(["w"] * 30)
    raw["scores"] = [i for i in raw["scores"] if i["key"] != "process.questions_at_end"]
    raw["round_type"] = "chat"
    out = rubric.normalise(raw)
    assert out["scores"]["technical"]["system_design"]["score"] == 10
    assert len(out["scores"]["technical"]["cs_fundamentals"]["quote"].rstrip("…").split()) == 20
    assert out["dimension_scores"]["technical"] == pytest.approx((10 + 6 * 4) / 5)
    assert out["dimension_scores"]["negotiation"] is None  # N/A when comp never came up
    assert out["scores"]["process"]["questions_at_end"]["score"] is None  # missing -> N/A
    assert out["round_type"] == "screen" and out["company_stage"] == "series_a_b"
    assert len(out["do_next_time"]) == 3 and len(out["question_feedback"]) == 5


def test_series_moving_average_slope_and_badges():
    ivs = [scored("2026-09-01", 4), scored("2026-09-15", 5), scored("2026-10-01", 7),
           scored("2026-10-15", 8)]
    s = performance.series(ivs, lambda iv: iv["overall_score"])
    assert [p["x"] for p in s["points"]] == ["2026-09-01", "2026-09-15", "2026-10-01",
                                             "2026-10-15"]
    assert [p["ma3"] for p in s["points"]] == [4.0, 4.5, 5.33, 6.67]
    assert s["badge"] == "improving" and s["fitted_change"] == pytest.approx(4.2, abs=0.1)
    flat = performance.series(ivs, lambda iv: 6)
    assert flat["badge"] == "flat" and flat["slope_per_30d"] == 0
    down = performance.series(ivs, lambda iv: 10 - iv["overall_score"])
    assert down["badge"] == "declining"
    na = performance.series(ivs, lambda iv: None)
    assert na["n"] == 0 and na["badge"] == "insufficient_data"
    two = performance.series(ivs[:2], lambda iv: iv["overall_score"])
    assert two["badge"] == "insufficient_data" and two["fitted_change"] == 1.0


def test_build_breakdowns_callouts_and_trend_text():
    ivs = [scored("2026-09-01", 4, round_type="screen", stage="pre_seed_seed"),
           scored("2026-09-15", 5), scored("2026-10-01", 6), scored("2026-10-15", 8)]
    ivs[-1]["scores"]["communication"]["pace_fillers"]["score"] = 2
    ivs[-2]["scores"]["communication"]["pace_fillers"]["score"] = 3
    topics = [{"topic": "Redis durability", "verdict": "improved", "occurrences": [
        {"date": "2026-09-01", "interview_id": "a", "status": "first_seen", "note": "n"},
        {"date": "2026-10-01", "interview_id": "b", "status": "improved", "note": "n"}]}]
    perf = performance.build(list(reversed(ivs)), topics)
    assert perf["schema_version"] == 2
    assert [iv["date"][:10] for iv in perf["interviews"]][0] == "2026-09-01"
    assert perf["by_round_type"]["screen"]["n"] == 1
    assert perf["by_company_stage"]["series_a_b"]["overall"] == pytest.approx(19 / 3, abs=0.01)
    assert perf["trends"]["dimensions"]["technical"]["badge"] == "improving"
    assert perf["callouts"]["most_improved"]["key"].startswith("technical.")
    assert perf["callouts"]["biggest_risk"]["key"] == "communication.pace_fillers"
    assert "Redis durability x2 improved" in perf["trend_text"]
    assert estimate_tokens(perf["trend_text"]) <= performance.TREND_TOKEN_LIMIT
    rows = list(csv.DictReader(io.StringIO(performance.to_csv(perf))))
    per_iv = 1 + len(rubric.RUBRIC) + len(rubric.SUBDIMENSIONS)
    assert len(rows) == 4 * per_iv and {r["level"] for r in rows} == {
        "overall", "dimension", "subdimension"}


def test_score_interview_uses_transcript_when_available():
    llm = FakeLLM()
    a = analysis()
    a.update(source="fathom", id="1", title="Acme SD", date="2026-09-01T10:00:00Z")
    meeting = {"turns": [{"speaker": "Chirag", "text": "I would shard by tenant.", "start": 0}]}
    with_t = rubric.score_interview(meeting, a, llm, "claude-sonnet-5-5", "Chirag")
    without = rubric.score_interview(None, a, llm, "claude-sonnet-5-5", "Chirag")
    assert with_t["transcript_used"] and not without["transcript_used"]
    assert "<transcript>" in llm.calls[0]["user"] and "<transcript>" not in llm.calls[1]["user"]
    assert "1-10" in llm.calls[0]["system"] and "system_design" in llm.calls[0]["system"]


def test_score_performance_writes_v2_and_keeps_v1_keys(tmp_path):
    from app.profile import importer
    cfg = settings(tmp_path)
    a = analysis()
    a.update(source="fathom", id="1", title="Acme", date="2026-09-01T10:00:00Z")
    record = {"interviews": [a], "brief": {"top_strengths": ["x"], "trend": "old"},
              "brief_text": "old", "trends": {"v1": True}, "stats": {}}
    llm = FakeLLM()
    reply = rubric_reply(6, comp_came_up=True)
    next(i for i in reply["scores"] if i["key"] == "negotiation.anchoring")["justification"] = \
        "Said 66-77 LPA first."
    llm.json_orig = llm.json
    llm.json = lambda **kw: reply if "overall_score" in kw["schema"]["properties"] \
        else llm.json_orig(**kw)
    out = importer.score_performance(cfg, record, {}, llm, COMP)
    assert out["schema_version"] == 2 and out["performance_path"] == "performance.json"
    assert out["trends"] == {"v1": True} and "Trend: " in out["brief_text"]
    perf_text = cfg.performance_path.read_text()
    assert stat.S_IMODE(cfg.performance_path.stat().st_mode) == 0o600
    assert "66-77" not in perf_text and "[redacted]" in perf_text
    assert "66-77" not in cfg.performance_csv.read_text()
    perf = json.loads(perf_text)
    assert perf["interviews"][0]["dimension_scores"]["negotiation"] == 6


def test_score_script_prints_no_comp(tmp_path, monkeypatch, capsys):
    import re
    import scripts.score_performance as script
    for name, value in (("VALR_ENV_FILE", tmp_path / "none.env"),
                        ("VALR_EVALUATION_PATH", tmp_path / "e.json"),
                        ("VALR_PERFORMANCE_PATH", tmp_path / "perf.json"),
                        ("VALR_PERFORMANCE_CSV", tmp_path / "perf.csv"),
                        ("VALR_RECORDINGS_CACHE", tmp_path / "raw"),
                        ("VALR_PROFILE_PATH", tmp_path / "p.json")):
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    (tmp_path / "p.json").write_text(json.dumps({"compensation": COMP}))
    a = analysis()
    a.update(source="fathom", id="333", title="Gamma", date="2026-10-01T09:00:00Z")
    (tmp_path / "e.json").write_text(json.dumps({"interviews": [a], "brief": None}))
    fixtures = Path(__file__).parent / "fixtures" / "profile" / "export_meeting.json"
    assert script.main(["--from-export", str(fixtures), "--days", "3650"], llm=FakeLLM()) == 0
    out = capsys.readouterr().out.replace(str(tmp_path), "<tmp>")
    assert "scored 1 interviews (1 with transcripts)" in out
    rest = "\n".join(ln for ln in out.splitlines() if not ln.startswith("token spend"))
    assert not re.search(r"\b(31|35|120|66|77|52|59)\b", rest)
    assert json.loads((tmp_path / "e.json").read_text())["schema_version"] == 2


def test_parse_json_object_tolerates_fences_and_rejects_garbage():
    from app.profile.llm import LLMError, parse_json_object
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json_object("no json here")


def test_rubric_call_is_unconstrained():
    llm = FakeLLM()
    a = analysis()
    a.update(source="fathom", id="1", title="t", date="2026-09-01T10:00:00Z")
    rubric.score_interview(None, a, llm, "claude-sonnet-5-5", "Chirag")
    assert llm.calls[0]["constrained"] is False


def test_llm_retries_dropped_connections_then_succeeds():
    import anthropic
    import httpx
    from app.profile.llm import AnthropicJsonLLM

    class Resp:
        usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": '{"ok": true}'})()]

    llm = AnthropicJsonLLM(client=object(), sleep=lambda s: None)
    attempts = []

    def flaky(kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
        return Resp()
    llm._stream = flaky
    assert llm.json(model="m", system="s", user="u", schema={}, max_tokens=10,
                    effort="low") == {"ok": True}
    assert len(attempts) == 3 and llm.ledger.calls["m"]["calls"] == 1


def test_rubric_reply_off_schema_raises():
    from app.profile.llm import LLMError
    llm = FakeLLM()
    llm.json = lambda **kw: {"interview": {}}
    a = analysis()
    a.update(source="fathom", id="1", title="t", date="2026-09-01T10:00:00Z")
    with pytest.raises(LLMError, match="interview"):
        rubric.score_interview(None, a, llm, "m", "Chirag")


def test_score_performance_resumes_from_partial(tmp_path):
    from app.profile import importer
    cfg = settings(tmp_path)
    a1, a2 = analysis(), analysis()
    a1.update(source="fathom", id="1", title="A", date="2026-09-01T10:00:00Z")
    a2.update(source="fathom", id="2", title="B", date="2026-09-02T10:00:00Z")
    llm = FakeLLM(fail_on="Title: B")
    out = importer.score_performance(cfg, {"interviews": [a1, a2]}, {}, llm, COMP)
    partial = cfg.performance_path.with_suffix(".partial.json")
    assert partial.exists() and list(json.loads(partial.read_text())) == ["fathom:1"]
    llm2 = FakeLLM()
    importer.score_performance(cfg, {"interviews": [a1, a2]}, {}, llm2, COMP)
    assert sum(1 for c in llm2.calls if "Title: A" in c["user"]) == 0
    assert not partial.exists() and out["schema_version"] == 2
