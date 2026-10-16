"""VALR interview classifier, evaluator, trend and end-to-end import, with a fake LLM."""
import csv
import json
import logging
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.profile import classify, evaluate, importer, recordings, trend
from app.profile.config import Settings
from app.profile.llm import Ledger, LLMError
from app.profile.profile import estimate_tokens, validate_compensation

FIXTURES = Path(__file__).parent / "fixtures" / "profile"
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
COMP = validate_compensation({"current": {"fixed_lpa": 31, "variable_lpa": 4,
                                          "esop_units_per_year": 120},
                              "target_lpa": {"min": 66, "max": 77},
                              "startup_floor_lpa": {"min": 52, "max": 59}})


def scores(base: int, comp=None):
    out = {d: {"score": base, "justification": f"{d} evidence"} for d in trend.DIMENSIONS}
    out["compensation_handling"] = {"score": comp, "justification": "n/a" if comp is None
                                    else "deferred the number"}
    return out


def analysis(company="Acme", base=3, comp=None, quote="I would use a token bucket"):
    return {"company": company, "round_type": "system design",
            "interviewers": [{"name": "Priya Interviewer", "role": "Staff Engineer"},
                             {"name": "Sam Rivera", "role": "candidate"}],
            "questions_asked": ["Design a rate limiter"],
            "strong_answers": [{"topic": "rate limiting", "quote": quote, "why": "clear"}],
            "weak_answers": [{"topic": "consistency", "quote": "uh not sure", "why": "vague"}],
            "technical_topics": ["rate limiting", "Redis"],
            "communication": {"rambling": "some", "filler": "moderate", "clarity": "ok",
                              "pace": "normal", "notes": ""},
            "compensation": {"discussed": True,
                             "summary": "Asked expectation; he said 66-77 LPA. Offer band 45 LPA."},
            "scores": scores(base, comp)}


BRIEF = {"top_strengths": ["Clear system design"], "recurring_weak_topics": ["Consistency"],
         "communication": "Some filler.", "prep_checklist": ["Practise CAP trade-offs"],
         "negotiation_notes": "Anchored at 66-77 LPA too early.",
         "trend": "Communication improved from Sep 1 to Oct 1. Consistency stayed weak."}


class FakeLLM:
    """Answers by schema: classifier, per-interview analysis, or aggregate brief."""

    def __init__(self, label=None, analyses=None, fail_on=None):
        self.ledger = Ledger()
        self.label = label or {"is_interview": True, "owner_is_candidate": True,
                               "company": "Acme", "round_type": "system design",
                               "confidence": 0.9, "reason": "interview"}
        self.analyses = list(analyses or [])
        self.fail_on = fail_on
        self.calls = []
        self.rubric_base = []

    def json(self, *, model, system, user, schema, max_tokens, effort, constrained=True):
        self.calls.append({"model": model, "system": system, "user": user, "effort": effort,
                           "constrained": constrained})
        self.ledger.add(model, len(user) // 4, 50)
        if self.fail_on and self.fail_on in user:
            raise LLMError("declined")
        if "is_interview" in schema["properties"]:
            return dict(self.label)
        if "where_you_stand" in schema["properties"]:
            return coach_reply()
        if "leaned_in" in schema["properties"]:
            return {"cues": [], "leaned_in": "", "disengaged": "", "close": "none"}
        if "overall_score" in schema["properties"]:
            return rubric_reply(self.rubric_base.pop(0) if self.rubric_base else 6)
        if "topics" in schema["properties"]:
            return {"topics": [{"topic": "consistency", "verdict": "improved", "occurrences": [
                {"date": "2026-09-01", "interview_id": "fathom:1", "status": "first_seen",
                 "note": "vague"}]}]}
        if "scores" in schema["properties"]:
            return self.analyses.pop(0) if self.analyses else analysis()
        return dict(BRIEF)


def coach_reply() -> dict:
    return {
        "where_you_stand": "You are close to the bar. Lead with the answer.",
        "whats_working": [{"headline": "Clear designs", "detail": "You name components.",
                           "quote_id": "Q1"}],
        "whats_costing_you": [{"headline": "Burying the answer", "plain_meaning": "Answer first.",
                               "why_it_matters": "They stop listening.", "quote_id": "Q1",
                               "what_happened": "Long preamble.", "do_instead": "Say the answer first.",
                               "model_rephrase": "Short answer: shard by tenant."}] * 2,
        "this_weeks_focus": [{"drill": f"Drill {i}", "how": "Timer", "done_when": "Under 60s"}
                             for i in range(3)],
        "negotiation": {"read": "You share numbers early.",
                        "scripts": [{"moment": "Asked current pay", "say": "I'd rather hear the range first."}]},
        "interviewers": {"leaned_in_when": "You gave numbers.", "disengaged_when": "You rambled.",
                         "quote_ids": ["Q1", "Q999"]},
        "headline": "Close to the bar; lead with the answer."}


def rubric_reply(base: int, comp_came_up: bool = False) -> dict:
    from app.profile.rubric import RUBRIC
    scores = [{"key": f"{g}.{s}", "score": (0 if g == "negotiation" and not comp_came_up else base),
               "justification": f"{s} evidence", "quote": "I would shard by tenant"}
              for g, subs in RUBRIC.items() for s in subs]
    return {"company": "Acme", "company_stage": "series_a_b", "round_type": "system_design",
            "outcome": "advanced", "outcome_evidence": "next round scheduled",
            "overall_score": base, "scores": scores,
            "do_next_time": ["Lead with the answer", "Quantify impact", "Ask about SLOs", "x"],
            "question_feedback": [{"question": f"Q{i}", "went_well": "w", "improve": "i",
                                   "model_answer_hint": "h"} for i in range(7)]}


def meeting(title, turns=None, participants=("Priya", "Sam Rivera"), date="2026-09-01T10:00:00Z"):
    return {"source": "fathom", "id": title, "title": title, "date": date, "duration": 1800,
            "participants": list(participants), "vendor_summary": "summary",
            "turns": turns if turns is not None else
            [{"speaker": "Priya", "text": "Question?", "start": 0.0},
             {"speaker": "Sam Rivera", "text": "Answer here.", "start": 4.0}]}


def test_heuristic():
    assert classify.heuristic(meeting("Acme interview - round 2")) == "likely"
    assert classify.heuristic(meeting("Weekly team sync")) == "unlikely"
    assert classify.heuristic(meeting("Catch up")) == "maybe"
    assert classify.heuristic(meeting("Chat", participants=[str(i) for i in range(12)])) == "unlikely"


def test_classifier_skips_obvious_internal_meetings_and_uses_haiku_otherwise():
    llm = FakeLLM()
    assert classify.classify(meeting("Daily standup"), llm, "claude-haiku-5-5", "Sam")[
        "stage"] == "heuristic"
    assert llm.calls == []
    label = classify.classify(meeting("Catch up"), llm, "claude-haiku-5-5", "Sam")
    assert label["stage"] == "llm" and classify.is_owner_interview(label)
    assert llm.calls[0]["model"] == "claude-haiku-5-5" and llm.calls[0]["effort"] == "low"
    assert "Sam" in llm.calls[0]["system"]


def test_classifier_respects_owner_as_interviewer():
    llm = FakeLLM(label={"is_interview": True, "owner_is_candidate": False, "company": None,
                         "round_type": None, "confidence": 0.9, "reason": "he interviews"})
    label = classify.classify(meeting("Interview: backend candidate"), llm, "m", "Sam")
    assert not classify.is_owner_interview(label)


def test_speaking_metrics_measured_from_turns():
    turns = [{"speaker": "Priya", "text": "Q one", "start": 0.0},
             {"speaker": "Sam Rivera", "text": "um so basically " + "word " * 57, "start": 5.0},
             {"speaker": "Sam Rivera", "text": "more words here", "start": 30.0},
             {"speaker": "Priya", "text": "ok", "start": 65.0}]
    m = evaluate.speaking_metrics(turns, "Sam")
    assert m["turns"] == 1  # merged consecutive turns
    assert m["fillers_per_100_words"] == pytest.approx(3.2, abs=0.1)
    assert m["words_per_minute"] == 63
    assert evaluate.speaking_metrics(turns, "Nobody") is None


def test_analyze_caps_quotes_drops_candidate_and_clamps_scores():
    long_quote = " ".join(f"w{i}" for i in range(30))
    raw = analysis(quote=long_quote)
    raw["scores"]["technical_depth"]["score"] = 9
    llm = FakeLLM(analyses=[raw])
    out = evaluate.analyze_interview(meeting("Acme interview"), {"company": "Acme"}, llm,
                                     "claude-sonnet-5-5", "Sam")
    assert len(out["strong_answers"][0]["quote"].rstrip("…").split()) == 20
    assert [i["name"] for i in out["interviewers"]] == ["Priya Interviewer"]
    assert out["scores"]["technical_depth"]["score"] == 5
    assert out["speaking_metrics"]["turns"] == 1
    assert "1-5" in llm.calls[0]["system"]  # the fixed rubric is in the prompt


def test_trend_directions():
    rows = []
    for day, base in (("2026-08-20", 2), ("2026-09-10", 3), ("2026-10-05", 4)):
        a = analysis(base=base, comp=None)
        a["date"], a["source"], a["id"] = day + "T10:00:00Z", "fathom", day
        rows += trend.score_rows([a])
    rows = [dict(r, score=(3 if r["dimension"] == "confidence" else r["score"])) for r in rows]
    t = trend.trends(rows)
    assert t["technical_depth"]["direction"] == "improving"
    assert t["technical_depth"]["first"] == {"date": "2026-08-20", "score": 2}
    assert t["confidence"]["direction"] == "flat"
    assert t["compensation_handling"]["direction"] == "insufficient data"
    down = [dict(r, score=6 - r["score"]) for r in rows if r["dimension"] == "structure"]
    assert trend.trends(down)["structure"]["direction"] == "declining"


def test_aggregate_brief_budget_and_trend_section():
    brief = dict(BRIEF, prep_checklist=[f"item {i} " + "word " * 15 for i in range(40)],
                 trend="Sentence one is here. " * 60)
    llm = FakeLLM()
    llm.json = lambda **kw: brief  # noqa: E731
    a1, a2 = analysis(base=2), analysis(base=4)
    a1.update(date="2026-08-20T10:00:00Z", source="f", id="1")
    a2.update(date="2026-10-01T10:00:00Z", source="f", id="2")
    result = evaluate.aggregate([a1, a2], llm, "claude-sonnet-5-5", "Sam")
    assert estimate_tokens(result["text"]) <= evaluate.BRIEF_TOKEN_LIMIT
    trend_text = result["text"].split("Trend: ")[1]
    assert estimate_tokens(trend_text) <= trend.TREND_TOKEN_LIMIT
    assert result["trends"]["technical_depth"]["direction"] == "improving"
    assert len(result["score_rows"]) == 2 * len(trend.DIMENSIONS)


def test_aggregate_with_no_interviews():
    assert evaluate.aggregate([], FakeLLM(), "m", "Sam")["brief"] is None


def settings(tmp_path):
    return Settings(profile_path=tmp_path / "profile.json",
                    evaluation_path=tmp_path / "evaluation.json",
                    scores_path=tmp_path / "interview_scores.csv",
                    performance_path=tmp_path / "performance.json",
                    performance_csv=tmp_path / "performance_scores.csv",
                    compensation_path=tmp_path / "comp.json", cache_dir=tmp_path / "raw",
                    candidate_name="Sam", classifier_model="claude-haiku-5-5",
                    evaluator_model="claude-sonnet-5-5")


def fathom_transport():
    pages = [(FIXTURES / "fathom_page1.json").read_bytes(),
             (FIXTURES / "fathom_page2.json").read_bytes()]
    return lambda method, url, headers, body: recordings.HttpResponse(200, {}, pages.pop(0))


def test_end_to_end_stores_only_derived_analysis(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = settings(tmp_path)
    llm = FakeLLM()
    sources = importer.Sources(fathom_key="k", http=recordings.Http(
        transport=fathom_transport(), sleep=lambda s: None))
    record = importer.run(cfg, sources, llm, cache=recordings.RawCache(cfg.cache_dir),
                          comp=COMP, now=NOW)
    stats = record["stats"]
    assert stats["sources"] == {"fathom": "2 meetings", "fireflies": "skipped (no API key)"}
    assert stats["meetings"] == 2 and stats["classified_by_llm"] == 1  # sync skipped by heuristic
    assert stats["interviews"] == 1 and stats["analysed"] == 1
    stored = cfg.evaluation_path.read_text()
    assert stat.S_IMODE(cfg.evaluation_path.stat().st_mode) == 0o600
    assert "token bucket per tenant" not in stored  # no transcript text
    assert "@" not in stored
    assert "66-77" not in stored and "[redacted]" in stored and "45 LPA" in stored
    assert "Trend:" in json.loads(stored)["brief_text"]
    rows = list(csv.DictReader(cfg.scores_path.open()))
    assert len(rows) == len(trend.DIMENSIONS) and rows[0]["date"] == "2026-09-01"
    assert json.loads(cfg.scores_path.with_suffix(".json").read_text())["trends"]
    models = {c["model"] for c in llm.calls}
    assert models == {"claude-haiku-5-5", "claude-sonnet-5-5"}
    assert not re.search(r"\b(31|35|66|77|52|59)\b", caplog.text)


def test_llm_failure_is_recorded_not_fatal(tmp_path):
    cfg = settings(tmp_path)
    llm = FakeLLM(fail_on="<transcript>")
    sources = importer.Sources(fathom_key="k", http=recordings.Http(
        transport=fathom_transport(), sleep=lambda s: None))
    record = importer.run(cfg, sources, llm, now=NOW)
    assert record["stats"]["analysed"] == 0 and "declined" in record["stats"]["errors"][0]


def test_script_without_keys_reports_ready(tmp_path, monkeypatch, capsys):
    import scripts.import_recordings as script
    monkeypatch.setenv("VALR_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    assert script.main([]) == 0
    assert "Ready to run" in capsys.readouterr().out


def test_script_purge(tmp_path, monkeypatch, capsys):
    import scripts.import_recordings as script
    monkeypatch.setenv("VALR_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("VALR_RECORDINGS_CACHE", str(tmp_path / "raw"))
    recordings.RawCache(tmp_path / "raw").save("fathom", "p", {})
    assert script.main(["--purge"]) == 0
    assert not (tmp_path / "raw").exists() and "Purged" in capsys.readouterr().out


def test_script_from_export_never_prints_comp(tmp_path, monkeypatch, capsys, caplog):
    import scripts.import_recordings as script
    caplog.set_level(logging.DEBUG)
    for name, value in (("VALR_ENV_FILE", tmp_path / "none.env"),
                        ("VALR_EVALUATION_PATH", tmp_path / "e.json"),
                        ("VALR_SCORES_PATH", tmp_path / "s.csv"),
                        ("VALR_PERFORMANCE_PATH", tmp_path / "perf.json"),
                        ("VALR_PERFORMANCE_CSV", tmp_path / "perf.csv"),
                        ("VALR_RECORDINGS_CACHE", tmp_path / "raw"),
                        ("VALR_PROFILE_PATH", tmp_path / "p.json")):
        monkeypatch.setenv(name, str(value))
    (tmp_path / "p.json").write_text(json.dumps({"compensation": COMP}))
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    llm = FakeLLM()
    assert script.main(["--from-export", str(FIXTURES / "export_meeting.json"), "--days", "3650"],
                       llm=llm) == 0
    out = (capsys.readouterr().out + caplog.text).replace(str(tmp_path), "<tmp>")
    assert "export: 1 meetings" in out and "analysed: 1" in out and "token spend" in out
    spend = [ln for ln in out.splitlines() if ln.startswith("token spend")]
    rest = "\n".join(ln for ln in out.splitlines() if not ln.startswith("token spend"))
    assert spend and not re.search(r"\b(31|35|120|66|77|52|59)\b", rest)


def test_briefing_prompt_block(tmp_path):
    from app.profile import briefing
    assert briefing.prompt_block(tmp_path / "none.json", tmp_path / "none2.json") == ""
    (tmp_path / "p.json").write_text(json.dumps({"brief": "Sam, Bengaluru."}))
    (tmp_path / "e.json").write_text(json.dumps({"brief_text": "Trend: improving."}))
    block = briefing.prompt_block(tmp_path / "p.json", tmp_path / "e.json")
    assert "## Profile\nSam, Bengaluru." in block and "Trend: improving." in block
