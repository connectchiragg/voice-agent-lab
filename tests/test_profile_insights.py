"""VALR performance v3: interviewer impression cues, the coach's read, the post-call report.

Synthetic fixtures only: candidate "Sam Rivera", interviewers "Dana" and "Lee", fictional
companies (Northwind Labs, Bluefin Systems), example.com addresses.
"""
import asyncio
import dataclasses
import json
import re
import stat

import pytest

from app import post_call as post_call_app
from app import telegram_api, valr_page, valr_view
from app.profile import coach, importer, impressions, performance, post_call, rubric
from app.profile.llm import Ledger, LLMError
from tests.test_profile_evaluate import COMP, rubric_reply, settings as base_settings

CANDIDATE = "Sam"


def settings(tmp_path):
    return dataclasses.replace(base_settings(tmp_path), candidate_name=CANDIDATE)


def turns() -> list[dict]:
    """A 30-minute technical round: warm probing early, rushing and a brush-off at the end."""
    long = ("I would put a token bucket per tenant in Redis, refill it with a Lua script, and keep a local "
            "cache so the hot path stays under a millisecond even when Redis is slow, and we measured that")
    rows = [("Dana", "Hi Sam, thanks for joining. Can you walk me through a rate limiter you built?", 0),
            ("Sam Rivera", long + ".", 20),
            ("Dana", "Interesting. How did you handle a tenant that bursts past its limit?", 80),
            ("Sam Rivera", long + " in production.", 95),
            ("Dana", "Great.", 150),
            ("Sam Rivera", long + " again for the second region.", 155),
            ("Dana", "What do you mean by the second region?", 210),
            ("Sam Rivera", long + " with replication between them and it was", 225),
            ("Dana", "Okay.", 290),
            ("Sam Rivera", long + ".", 295),
            ("Dana", "That's exactly what we need on our platform team. You'd own this from day one.", 400),
            ("Sam Rivera", "Thanks, that sounds great.", 410),
            ("Dana", "In the interest of time, let's move on to the coding part.", 600),
            ("Sam Rivera", long + ".", 610),
            ("Dana", "Thanks for your time, we'll get back to you.", 1000)]
    return [{"speaker": s, "text": t, "start": float(at)} for s, t, at in rows]


def meeting(**extra) -> dict:
    return {"source": "fathom", "id": "901", "title": "Northwind Labs technical interview",
            "date": "2026-10-10T09:00:00Z", "duration": 1020.0, "scheduled_duration": 3600.0,
            "participants": ["Dana", "Sam Rivera"], "vendor_summary": "", "turns": turns()} | extra


class CueLLM:
    """Answers the cue, coach, report, rubric and classifier schemas with fixed data."""

    def __init__(self, cues=None, fail_coach=False):
        self.ledger = Ledger()
        self.calls = []
        self.fail_coach = fail_coach
        self.cues = cues if cues is not None else [
            {"kind": "engagement.follow_up_probe", "strength": 2,
             "quote": "How did you handle a tenant that bursts past its limit?", "timestamp": "01:20",
             "after": "Described a per-tenant token bucket."},
            {"kind": "positive.explicit_praise", "strength": 3,
             "quote": "That's exactly what we need on our platform team.", "timestamp": "06:40",
             "after": "Gave the measured latency numbers."},
            {"kind": "positive.explicit_praise", "strength": 3,  # invented: not in the transcript
             "quote": "You are the best candidate we have ever seen", "timestamp": "07:00", "after": ""},
            {"kind": "negative.brush_off_close", "strength": 2,
             "quote": "Thanks for your time, we'll get back to you.", "timestamp": "16:40", "after": ""}]

    def json(self, *, model, system, user, schema, max_tokens, effort, constrained=True):
        self.calls.append({"system": system, "user": user, "schema": schema})
        self.ledger.add(model, len(user) // 4, 50)
        props = schema["properties"]
        if "is_interview" in props:
            return {"is_interview": True, "owner_is_candidate": True, "company": "Northwind Labs",
                    "round_type": "technical", "confidence": 0.9, "reason": "interview"}
        if "leaned_in" in props:
            return {"cues": self.cues, "leaned_in": "Dana leaned in on the rate limiter numbers.",
                    "disengaged": "She rushed the coding part.", "close": "brush_off"}
        if "where_you_stand" in props:
            if self.fail_coach:
                raise LLMError("declined")
            return {"where_you_stand": "You are close. Your expected 66 LPA is fine.",
                    "whats_working": [{"headline": "Numbers", "detail": "You measure.", "quote_id": "Q1"}],
                    "whats_costing_you": [{"headline": "Rambling", "plain_meaning": "Answer first.",
                                           "why_it_matters": "They tune out.", "quote_id": "Q2",
                                           "what_happened": "Long answers.", "do_instead": "Lead with it.",
                                           "model_rephrase": "Short answer: a token bucket."}] * 2,
                    "this_weeks_focus": [{"drill": "Timer", "how": "60 s", "done_when": "Always"}] * 3,
                    "negotiation": {"read": "Early numbers.", "scripts": [
                        {"moment": "Current pay", "say": "I'm at ₹31 L now; what is your band?"}]},
                    "interviewers": {"leaned_in_when": "Numbers.", "disengaged_when": "Long intros.",
                                     "quote_ids": ["Q9999"]},
                    "headline": "Close to the bar."}
        if "verdict" in props:
            return {"verdict": "Solid design round; the close was cool.",
                    "went_well": [{"point": "Concrete numbers", "quote_id": "Q1"}] * 2,
                    "to_fix": [{"point": "Long answers", "quote_id": "",
                                "say_instead": "Short answer: a token bucket per tenant. I target 77 LPA."}] * 2,
                    "impression_evidence": "Dana probed, then rushed.",
                    "impression_quote_ids": [q for q in ("Q1", "Q2", "Q3") if q],
                    "outcome_signals": "No dates or next round were mentioned.",
                    "drill": "Answer five questions in under 60 seconds each."}
        if "overall_score" in props:
            reply = rubric_reply(6)
            reply["company"] = "Northwind Labs"
            return reply
        if "topics" in props:
            return {"topics": []}
        raise AssertionError(f"unexpected schema {sorted(props)}")


# ---------- measured cues ----------

def test_measure_counts_followups_acks_rushing_and_early_end():
    out = impressions.measure(turns(), CANDIDATE, duration=1020.0, scheduled=3600.0)
    m = out["metrics"]
    assert m["follow_ups"] >= 1 and m["warm_acknowledgements"] == 1 and m["flat_acknowledgements"] == 1
    assert m["clarification_requests"] == 1 and m["interruptions_approx"] == 1
    assert 0.7 < m["candidate_talk_share"] < 0.95 and m["length_vs_slot"] == pytest.approx(0.28, abs=0.01)
    kinds = {c["kind"] for c in out["cues"]}
    assert {"rushing", "clarification_request", "brush_off_close", "cut_short"} <= kinds
    assert out["next_step"] == "brush_off"
    for cue in out["cues"]:
        assert len(cue["quote"].rstrip("…").split()) <= 20 and re.fullmatch(r"\d\d:\d\d", cue["timestamp"])


def test_model_cues_are_kept_only_when_the_quote_is_in_an_interviewer_turn():
    imp = impressions.analyze(meeting(), CueLLM(), "m", CANDIDATE)
    quotes = [c["quote"] for c in imp["cues"] if c["origin"] == "llm"]
    assert "You are the best candidate we have ever seen" not in quotes
    assert imp["metrics"]["model_cues_dropped"] == 1
    praise = next(c for c in imp["cues"] if c["kind"] == "explicit_praise")
    assert praise["timestamp"] == "06:40" and praise["after"]
    assert imp["coverage"] == "transcript" and imp["next_step"] == "brush_off"
    assert 0 <= imp["score"] <= 10 and set(imp["components"]) == {
        "engagement", "positive", "negative", "next_steps", "balance"}
    # the measured and model brush-off at the same moment are one cue
    assert sum(1 for c in imp["cues"] if c["kind"] == "brush_off_close") == 1


def test_score_is_reproducible_from_components_and_weights():
    parts = {"engagement": 8.0, "positive": 6.0, "negative": 2.0, "next_steps": 10.0, "balance": 10.0}
    expected = 0.30 * 8 + 0.25 * 6 + 0.20 * 10 + 0.15 * 8 + 0.10 * 10
    assert impressions.score(parts) == pytest.approx(round(expected, 1))
    assert impressions.score({"balance": 9.0}) is None  # too little evidence for a score
    assert impressions.balance_points(0.6) == 10 and impressions.balance_points(0.95) == 0


def test_derived_impression_and_carry_over():
    derived = impressions.derived({"speaking_metrics": {"talk_share": 0.85}})
    assert derived["coverage"] == "derived" and derived["score"] is None
    assert derived["components"]["balance"] < 10
    earlier = impressions.analyze(meeting(), CueLLM(), "m", CANDIDATE)
    assert impressions.carry_over(derived, earlier) is earlier
    assert impressions.derived(None)["coverage"] == "none"


def test_short_or_unattributed_transcripts_get_no_score():
    assert impressions.analyze(meeting(turns=turns()[:4]), None, "m", CANDIDATE)["coverage"] == "none"
    other = [dict(t, speaker="Lee") for t in turns()]
    assert impressions.analyze(meeting(turns=other), None, "m", CANDIDATE)["score"] is None


def _iv(i, outcome, imp_score, overall):
    return {"interview_id": f"fathom:{i}", "outcome": outcome, "overall_score": overall,
            "impression": {"coverage": "transcript", "score": imp_score,
                           "components": {"engagement": imp_score, "positive": imp_score, "negative": 10 - imp_score,
                                         "next_steps": imp_score, "balance": 8.0}}}


def test_summarize_correlates_with_outcome_only_with_known_rejections():
    ivs = [_iv(1, "advanced", 8.0, 7), _iv(2, "advanced", 7.0, 6), _iv(3, "rejected", 3.0, 5),
           _iv(4, "rejected", 4.0, 5), _iv(5, "unknown", 5.0, 6)]
    out = impressions.summarize(ivs)
    assert out["by_outcome"]["advanced"] == {"n": 2, "mean": 7.5}
    assert out["point_biserial_advanced"] > 0.9 and out["r_with_rubric_overall"] > 0
    only_advanced = impressions.summarize([iv for iv in ivs if iv["outcome"] != "rejected"])
    assert only_advanced["point_biserial_advanced"] is None
    assert "No recorded rejections" in only_advanced["note"]


# ---------- the coach's read ----------

def scored_iv(day="2026-10-10", base=6, iid="fathom:901"):
    out = rubric.normalise(rubric_reply(base))
    out.update(interview_id=iid, source="fathom", id=iid.split(":")[1], title="t",
               date=f"{day}T09:00:00Z", company="Northwind Labs", transcript_used=True)
    out["scores"]["communication"]["conciseness_answer_first"].update(
        score=3, quote="So basically the context is that we had many tenants and")
    return out


def test_coach_quotes_come_from_the_bank_and_pay_figures_are_masked():
    iv = scored_iv()
    iv["impression"] = impressions.analyze(meeting(), CueLLM(), "m", CANDIDATE)
    perf = performance.build([iv], [])
    llm = CueLLM()
    read = coach.ensure(perf, llm, "m", CANDIDATE, COMP)
    bank = {q["quote"] for q in coach.quote_bank(perf["interviews"])}
    assert read["whats_working"][0]["example"]["quote"] in bank
    assert read["whats_working"][0]["example"]["date"] == "2026-10-10"
    assert read["interviewers"]["examples"] == []  # unknown quote id dropped, not invented
    text = json.dumps(read)
    assert not re.search(r"\b(31|66)\b", text)
    assert "[amount]" in text or "[redacted]" in text
    # system prompt asks for plain language and forbids figures
    assert "information control" in llm.calls[0]["system"] and "NEVER write salary" in llm.calls[0]["system"]


def test_coach_is_cached_until_the_data_changes():
    perf = performance.build([scored_iv()], [])
    llm = CueLLM()
    first = coach.ensure(perf, llm, "m", CANDIDATE)
    perf["coach"] = first
    again = coach.ensure(perf, llm, "m", CANDIDATE)
    assert again["input_hash"] == first["input_hash"] and len(llm.calls) == 1
    changed = performance.build([scored_iv(), scored_iv("2026-10-12", 7, "fathom:902")], [])
    third = coach.ensure(changed, llm, "m", CANDIDATE, previous=first)
    assert third["input_hash"] != first["input_hash"] and len(llm.calls) == 2
    stale = coach.ensure(changed, None, "m", CANDIDATE, previous=first)
    assert stale["stale"] is True


def test_with_coach_makes_trend_text_a_plain_headline():
    perf = performance.with_coach(performance.build([scored_iv()], []), CueLLM(), "m", CANDIDATE)
    assert perf["trend_text"].startswith("Close to the bar.") and "This week: Timer." in perf["trend_text"]
    plain_text = performance.build([scored_iv(), scored_iv("2026-10-12", 7, "fathom:902"),
                                    scored_iv("2026-10-14", 8, "fathom:903")], [])["trend_text"]
    assert "Across 3 interviews" in plain_text and "_" not in plain_text


def test_mask_pay_keeps_technical_numbers():
    assert coach.mask_pay("I want 45 LPA or 40-50 lakhs, ₹30L; we served 10k QPS in 3 regions") == \
        "I want [amount] or [amount], [amount]; we served 10k QPS in 3 regions"


# ---------- refresh on stored data ----------

def write_perf(cfg, ivs):
    perf = performance.build(ivs, [])
    perf["schema_version"] = 2
    for key in ("impressions", "coach"):
        perf.pop(key, None)
    cfg.performance_path.write_text(json.dumps(perf))
    cfg.evaluation_path.write_text(json.dumps({"interviews": [
        {"source": "fathom", "id": "902", "speaking_metrics": {"talk_share": 0.7}}],
        "brief": {"top_strengths": ["x"], "trend": "old"}}))
    cfg.profile_path.write_text(json.dumps({"compensation": COMP}))


def test_refresh_insights_upgrades_v2_to_v3_with_impressions_and_coach(tmp_path):
    cfg = settings(tmp_path)
    write_perf(cfg, [scored_iv(), scored_iv("2026-10-12", 7, "fathom:902")])
    llm = CueLLM()
    perf = importer.refresh_insights(cfg, {"fathom:901": meeting()}, llm, COMP)
    assert perf["schema_version"] == 3
    by_id = {iv["interview_id"]: iv["impression"]["coverage"] for iv in perf["interviews"]}
    assert by_id == {"fathom:901": "transcript", "fathom:902": "derived"}
    assert perf["coach"]["input_hash"] and perf["impressions"]["n_scored"] == 1
    saved = cfg.performance_path.read_text()
    assert not re.search(r"\b(31|66|77)\s*(LPA|L\b)", saved)
    assert stat.S_IMODE(cfg.performance_path.stat().st_mode) == 0o600
    brief = json.loads(cfg.evaluation_path.read_text())["brief"]
    assert brief["trend"] == perf["trend_text"]
    # second run without transcripts: impressions are kept, the coach is not regenerated
    calls = len(llm.calls)
    again = importer.refresh_insights(cfg, {}, llm, COMP)
    assert again["interviews"][0]["impression"]["coverage"] == "transcript" and len(llm.calls) == calls


def test_refresh_script_deletes_scratch_and_prints_no_quotes(tmp_path, monkeypatch, capsys):
    import scripts.refresh_insights as script
    cfg = settings(tmp_path)
    write_perf(cfg, [scored_iv()])
    for name, path in (("VALR_PERFORMANCE_PATH", cfg.performance_path), ("VALR_PERFORMANCE_CSV", cfg.performance_csv),
                       ("VALR_EVALUATION_PATH", cfg.evaluation_path), ("VALR_PROFILE_PATH", cfg.profile_path)):
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("VALR_CANDIDATE_NAME", CANDIDATE)
    monkeypatch.setenv("VALR_SCRATCH_DIR", str(tmp_path))
    monkeypatch.setenv("VALR_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    assert script.main(["--no-fetch"], llm=CueLLM()) == 0
    out = capsys.readouterr().out
    assert "scratch deleted: True" in out and "coach read: yes" in out
    assert "token bucket" not in out and "Northwind" not in out
    assert not list(tmp_path.glob("valr-insights-*"))


# ---------- dashboard ----------

def test_view_reads_v3_and_still_reads_v2():
    iv = scored_iv()
    iv["impression"] = impressions.analyze(meeting(), CueLLM(), "m", CANDIDATE)
    v3 = performance.with_coach(performance.build([iv], []), CueLLM(), "m", CANDIDATE)
    view = valr_view.performance(v3)
    assert view["coach"]["costing"][0]["headline"] == "Rambling"
    card = view["interviews"][0]["impression"]
    assert card["coverage"] == "transcript" and any(c["kind"] == "Praise" for c in card["cues"])
    v2 = dict(v3, schema_version=2)
    for key in ("coach", "impressions"):
        v2.pop(key)
    for one in v2["interviews"]:
        one.pop("impression")
    old = valr_view.performance(v2)
    assert old["coach"] is None and old["interviews"][0]["impression"] is None and old["available"]


def test_page_leads_with_the_narrative_and_folds_the_numbers():
    from datetime import datetime, timezone
    iv = scored_iv()
    iv["impression"] = impressions.analyze(meeting(), CueLLM(), "m", CANDIDATE)
    perf = performance.with_coach(performance.build([iv], []), CueLLM(), "m", CANDIDATE)
    view = {"performance": valr_view.performance(perf),
            "post_call": {"pending": 1, "queued": 0, "failed": 0,
                          "recent": [{"title": "Northwind Labs", "status": "pending_telegram",
                                      "updated_at": "2026-10-10T10:00:00+00:00"}]}}
    html = valr_page.performance(view, valr_page.Clock("UTC", datetime(2026, 10, 16, tzinfo=timezone.utc)))
    order = [html.index(x) for x in ("Where you stand", "What's working", "What's costing you",
                                     "This week's focus", "The numbers behind this", 'id="perf-chart"')]
    assert order == sorted(order)
    assert '<details class="numbers">' in html  # folded when there is a coach read
    assert "How the interviewer reacted" in html and 'id="past-fathom-901"' in html
    assert "1 post-call report pending" in html


# ---------- post-call report ----------

def test_report_renders_html_without_pay_figures_or_contacts(tmp_path):
    iv = scored_iv()
    iv["impression"] = impressions.analyze(meeting(), CueLLM(), "m", CANDIDATE)
    report = post_call.compose(iv, CueLLM(), "m", CANDIDATE, COMP)
    report["drill"] += " Mail dana@example.com or call +1 415 555 0100."
    text = post_call.render(post_call.compose(iv, None, "m", CANDIDATE, COMP) | {
        k: report[k] for k in ("verdict", "went_well", "to_fix", "impression_evidence", "outcome_signals")}
        | {"drill": post_call.clean(report["drill"])}, "valr.example.com", "https://fathom.example.com/share/abc")
    assert "<b>Post-call: Northwind Labs · system design round · 10 Oct</b>" in text and "Say instead:" in text
    assert "https://valr.example.com/valr#past-fathom-901" in text and 'href="https://fathom.example.com/share/abc"' in text
    assert not re.search(r"\b77\b", text) and "@" not in text and "555" not in text
    assert report["impression"] in ("engaged", "neutral", "cool")
    assert len(text) <= 4000


def test_report_falls_back_to_rubric_feedback_without_a_model():
    iv = scored_iv()
    iv["impression"] = impressions.analyze(meeting(), None, "m", CANDIDATE)
    report = post_call.compose(iv, None, "m", CANDIDATE)
    assert len(report["went_well"]) == 2 and len(report["to_fix"]) == 2 and report["drill"]
    assert report["to_fix"][0]["point"].startswith("Giving the answer first")


def test_process_scores_adds_to_performance_and_reports(tmp_path):
    cfg = settings(tmp_path)
    write_perf(cfg, [scored_iv("2026-10-01", 5, "fathom:800")])
    out = post_call.process(meeting(), cfg, CueLLM(), COMP)
    assert out["status"] == "ready" and out["performance_updated"]
    perf = json.loads(cfg.performance_path.read_text())
    assert {iv["interview_id"] for iv in perf["interviews"]} == {"fathom:800", "fathom:901"}
    assert perf["schema_version"] == 3 and perf["coach"]
    cfg.performance_path.chmod(0o400)
    try:
        again = post_call.process(meeting(), cfg, CueLLM(), COMP)
        assert again["status"] == "ready" and not again["performance_updated"]
        assert "read-only" in again["performance_note"]
    finally:
        cfg.performance_path.chmod(0o600)


def test_process_skips_non_interviews(tmp_path):
    llm = CueLLM()
    llm.json_orig = llm.json

    def not_interview(**kw):
        if "is_interview" in kw["schema"]["properties"]:
            return {"is_interview": False, "owner_is_candidate": False, "company": None,
                    "round_type": None, "confidence": 0.9, "reason": "team sync"}
        return llm.json_orig(**kw)
    llm.json = not_interview
    assert post_call.process(meeting(title="Interview debrief"), settings(tmp_path), llm)["status"] == "not_interview"


class FakeConn:
    """The post_call_reports and telegram_link queries, in memory."""

    def __init__(self, chat=None):
        self.rows: dict[tuple, dict] = {}
        self.chat = chat

    async def execute(self, sql, params=()):
        return FakeCursor(self, " ".join(sql.split()), params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCursor:
    def __init__(self, conn, sql, params):
        self.result = []
        rows = conn.rows
        if sql.startswith("INSERT INTO post_call_reports"):
            key = (params[0], params[1])
            if key not in rows:
                rows[key] = {"provider": params[0], "external_id": params[1], "title": params[2],
                             "recording_url": params[3], "sealed": params[4], "status": "queued",
                             "text": None, "updated_at": None, "interview_id": None}
                self.result = [(1,)]
        elif sql.startswith("UPDATE post_call_reports"):
            names = re.findall(r"(\w+) = %s", sql.split(" WHERE ")[0])
            key = (params[-2], params[-1])
            for name, value in zip(names, params):
                rows[key][name] = value
        elif "SELECT chat_id FROM telegram_link" in sql:
            self.result = [(conn.chat,)] if conn.chat else []
        elif "WHERE status = 'queued'" in sql:
            self.result = [(r["provider"], r["external_id"], r["sealed"], r["recording_url"])
                           for r in rows.values() if r["status"] == "queued"]
        elif "WHERE status = 'pending_telegram'" in sql:
            self.result = [(r["provider"], r["external_id"], r["text"]) for r in rows.values()
                           if r["status"] == "pending_telegram" and r["text"]]
        elif "GROUP BY status" in sql:
            counts = {}
            for r in rows.values():
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            self.result = list(counts.items())
        elif "ORDER BY updated_at DESC" in sql:
            self.result = [(r["title"], r["status"], r["updated_at"], r["interview_id"]) for r in rows.values()]

    async def fetchone(self):
        return self.result[0] if self.result else None

    async def fetchall(self):
        return self.result


class FakeBot(telegram_api.Bot):
    def __init__(self, fail=False):
        super().__init__("token")
        self.sent, self.fail = [], fail

    async def send(self, chat_id, text, keyboard=None):
        if self.fail:
            raise telegram_api.TelegramError("sendMessage: 400")
        self.sent.append((chat_id, text))
        return 42


def ready(meeting_):
    report = post_call.fallback(scored_iv()) | {"impression": "neutral", "impression_score": 5.0,
                                                "next_step": "vague", "company": "Northwind Labs",
                                                "round": "technical", "date": "2026-10-10",
                                                "overall": 6, "interview_id": "fathom:901"}
    return {"status": "ready", "interview_id": "fathom:901", "report": report, "cost_usd": 0.12,
            "performance_note": ""}


def test_on_new_interview_sends_when_linked(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    conn, bot = FakeConn(chat=1234), FakeBot()
    status = asyncio.run(post_call_app.on_new_interview(meeting(), connection=conn, process=ready, bot=bot,
                                                        recording_url="https://fathom.example.com/share/x"))
    assert status == "sent" and bot.sent[0][0] == 1234 and "Post-call" in bot.sent[0][1]
    row = conn.rows[("fathom", "901")]
    assert row["status"] == "sent" and row["message_id"] == 42 and row["sealed"] is None


def test_report_waits_for_telegram_and_is_sent_once_linked(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    conn, bot = FakeConn(chat=None), FakeBot()
    assert asyncio.run(post_call_app.on_new_interview(meeting(), connection=conn, process=ready, bot=bot)) \
        == "pending_telegram"
    view = asyncio.run(post_call_app.overview(conn))
    assert view["pending"] == 1 and not bot.sent
    conn.chat = 1234
    assert asyncio.run(post_call_app.send_pending(conn, bot)) == 1
    assert conn.rows[("fathom", "901")]["status"] == "sent"


def test_failures_are_recorded_and_telegram_errors_retry(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    conn = FakeConn(chat=1)

    def boom(m):
        raise LLMError("x")
    assert asyncio.run(post_call_app.on_new_interview(meeting(), connection=conn, process=boom)) == "failed"
    conn2 = FakeConn(chat=1)
    assert asyncio.run(post_call_app.on_new_interview(meeting(), connection=conn2, process=ready,
                                                      bot=FakeBot(fail=True))) == "pending_telegram"


def test_notetaker_hook_only_queues_and_the_worker_processes_the_queue(monkeypatch):
    from app.profile import notetaker_hook
    monkeypatch.delenv("VALR_TOKEN_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    conn = FakeConn(chat=7)

    async def connect():
        return conn
    async def no_meeting(provider, external_id):
        return None
    first = asyncio.run(notetaker_hook.evaluate_recording("fathom", "901", None, connect=connect, fetch=no_meeting))
    again = asyncio.run(notetaker_hook.evaluate_recording("fathom", "901", None, connect=connect, fetch=no_meeting))
    assert first.startswith("queued") and "worker" in first and again.startswith("already queued")
    assert conn.rows[("fathom", "901")]["status"] == "queued"
    bot = FakeBot()
    done = asyncio.run(post_call_app.run_queue(conn, fetch=lambda p, i: meeting(), process=ready, bot=bot))
    assert done == 1 and bot.sent and conn.rows[("fathom", "901")]["status"] == "sent"
    missing = FakeConn()
    asyncio.run(post_call_app.enqueue(missing, "fathom", "902"))
    asyncio.run(post_call_app.run_queue(missing, fetch=lambda p, i: None, process=ready))
    assert missing.rows[("fathom", "902")]["status"] == "failed"


def test_check_recent_queues_only_recordings_with_transcripts(monkeypatch):
    from app.profile import recordings
    monkeypatch.setenv("FATHOM_API_KEY", "test-key")
    items = [{"recording_id": 1, "title": "Bluefin Systems screen", "share_url": "https://fathom.example.com/share/1",
              "recording_start_time": "2026-10-16T08:00:00Z", "recording_end_time": "2026-10-16T08:30:00Z",
              "transcript": [{"speaker": {"display_name": "Lee"}, "text": "Hi", "timestamp": "00:00:01"}]},
             {"recording_id": 2, "title": "Still processing", "transcript": []}]
    monkeypatch.setattr(recordings, "fetch_fathom", lambda key, since, http, cache: items)
    conn = FakeConn()
    assert asyncio.run(post_call_app.check_recent(conn)) == 1
    assert set(conn.rows) == {("fathom", "1")}
    assert conn.rows[("fathom", "1")]["recording_url"] == "https://fathom.example.com/share/1"


def test_notetaker_hook_fetches_and_seals_the_meeting(monkeypatch):
    from cryptography.fernet import Fernet
    from app import google_oauth
    from app.profile import notetaker_hook
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", key)
    conn = FakeConn()

    async def connect():
        return conn

    async def fetch(provider, external_id):
        assert (provider, external_id) == ("fathom", "901")
        return meeting()
    status = asyncio.run(notetaker_hook.evaluate_recording("fathom", "901", None, connect=connect, fetch=fetch))
    assert status == "queued for the post-call report"
    sealed = conn.rows[("fathom", "901")]["sealed"]
    assert "token bucket" not in sealed and json.loads(google_oauth.decrypt(key, sealed))["id"] == "901"
    bot = FakeBot()
    conn.chat = 5
    asyncio.run(post_call_app.run_queue(conn, fetch=lambda p, i: None, process=ready, bot=bot))
    assert bot.sent and conn.rows[("fathom", "901")]["sealed"] is None  # dropped once processed
