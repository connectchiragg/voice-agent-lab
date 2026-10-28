"""The company-context builder (app/company_context.py) and the iPhone-call gaps it closed.
Made-up companies, people and figures only."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import company_context as cc

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def email(tid, days, direction="in", summary="", title="Re: role", ask="", thread="t1", hint=""):
    return {"id": tid, "kind": "email", "at": ago(days),
            "data": {"direction": direction, "summary": summary, "title": title, "thread": thread,
                     "action_requested": bool(ask), "action": ask, "stage_hint": hint,
                     "people": [{"name": "Asha Rao", "email": "asha@acme-robotics.example", "role": "recruiter"}]}}


LEAD = {"id": "lead-1", "company": "Acme Robotics", "domain": "acme-robotics.example", "stage": "interviewing",
        "status": "active", "next_step": "Technical round 2", "next_step_at": ago(-2),
        "contacts": [{"name": "Asha Rao", "email": "asha@acme-robotics.example", "role": "recruiter"}]}
TOUCHES = [
    email("gmail:1", 20, summary="Asha reached out about the backend role.", hint="screening"),
    email("gmail:2", 18, direction="out", summary="You shared your availability."),
    email("gmail:3", 10, summary="Budget for the role is 40-45 LPA fixed. Call me on +91 98765 43210 or asha@acme-robotics.example.",
          hint="interviewing"),
    email("gmail:4", 3, summary="Please send your notice period and two references.", ask="send notice period and references",
          thread="t2"),
    email("gmail:5", 15, summary="Can you share your portfolio?", ask="share portfolio", thread="t3"),
    email("gmail:6", 14, direction="out", summary="Sent the portfolio.", thread="t3"),
    {"id": "cal:k1", "kind": "calendar", "at": ago(9), "data": {"title": "Acme Robotics technical round 1",
                                                                 "people": [{"name": "Dev Kumar", "role": "interviewer"}]}},
    {"id": "fathom:f1", "kind": "recording", "at": ago(9), "data": {"title": "Recording: round 1", "summary": "x"}},
]
RECORDINGS = [
    {"provider": "fathom", "external_id": "f1", "title": "Acme Robotics technical round 1", "started_at": ago(9),
     "company": "Acme Robotics", "company_domain": "acme-robotics.example", "interviewers": '["Dev Kumar"]'},
    {"provider": "phone", "external_id": "p1", "title": "Phone call with Asha", "started_at": ago(12),
     "company": "Acme Robotics", "company_domain": "", "interviewers": '["Asha"]', "eval_status": "handed_off"},
]
TRANSCRIPTS = {"phone:p1": [
    {"speaker": "Asha", "text": "Thanks for taking the call. What is your current notice period, and when could you join?"},
    {"speaker": "Sam Rivera", "text": "Sixty days. My expectation is around 45 LPA fixed."},
    {"speaker": "Asha", "text": "The band for this role is 38-46 LPA plus ESOPs."}]}
REVIEWS = [{"interview_id": "fathom:f1", "date": ago(9).date().isoformat(), "company": "Acme Robotics",
            "round_type": "technical", "overall_score": 6.5, "outcome": "advanced",
            "question_feedback": [{"question": "Design a job queue with retries", "improve": "Size the load first."}]}]


def context():
    return cc.build("Acme Robotics", LEAD, TOUCHES, RECORDINGS, TRANSCRIPTS, REVIEWS,
                    calendar=[{"event_key": "old", "title": "Acme Robotics intro chat", "start_utc": ago(25),
                               "interviewers": '["Asha"]'}],
                    notes=[{"at": ago(5), "summary": "Sam said the Acme Robotics team works on warehouse robots."}],
                    candidate="Sam Rivera")


def test_render_has_every_kind_of_prior_interaction_newest_first():
    text = cc.render(context(), NOW)
    assert text.startswith("Previous interactions with Acme Robotics")
    assert "Lead stage: interviewing; next step: Technical round 2" in text
    assert "Stage history: screening" in text and "→ interviewing" in text
    assert "People so far: Asha Rao (recruiter)" in text
    # Open asks: the unanswered one only (the portfolio got a reply in its thread).
    assert "send notice period and references" in text and "share portfolio" not in text.split("Open asks")[1].split("\n")[0]
    # Pay: from an email and from the iPhone call, labelled as said.
    pay = next(line for line in text.split("\n") if line.startswith("Pay discussed"))
    assert "not a confirmed budget" in pay and "40-45 LPA" in pay and "38-46 LPA" in pay and "you: " in pay
    # The phone call: the interviewer's questions; the Fathom round: its coach review, once.
    assert "Recorded phone call: Phone call with Asha" in text and "notice period" in text
    assert "Coach review: technical, coach score 6.5/10, outcome advanced" in text
    assert text.count("Design a job queue") == 1 and "Recording: round 1" not in text
    assert "Past round (no recording): Acme Robotics intro chat" in text
    assert "Your note from a VALR call" in text
    timeline = [line for line in text.split("\n") if line.startswith("- ")]
    dates = [datetime.strptime(line[2:].split(":")[0] + " +0000", "%d %b %Y %z") for line in timeline]
    assert dates == sorted(dates, reverse=True)
    assert "[email hidden]" in text and "98765" not in text and "asha@" not in text


def test_render_stays_within_the_token_budget_and_keeps_the_most_relevant():
    many = TOUCHES + [email(f"gmail:x{i}", 30 + i, summary="Weekly newsletter style update " * 8, thread=f"n{i}")
                      for i in range(200)]
    ctx = cc.build("Acme Robotics", LEAD, many, RECORDINGS, TRANSCRIPTS, REVIEWS, candidate="Sam Rivera")
    text = cc.render(ctx, NOW)
    assert cc.estimate_tokens(text) <= cc.TOKEN_BUDGET
    assert "older or less relevant items left out" in text
    assert "Coach review" in text and "Recorded phone call" in text  # Interviews beat old emails.
    small = cc.render(ctx, NOW, budget_tokens=600)
    assert cc.estimate_tokens(small) <= 600 and "Lead stage" in small


def test_no_history_renders_nothing():
    assert cc.render(cc.build("Nobody Inc", None, [], [], {}, []), NOW) == ""
    assert cc.view(cc.build("Nobody Inc", None, [], [], {}, []), NOW) == {}


def test_view_splits_summary_and_timeline():
    shown = cc.view(context(), NOW)
    assert shown["company"] == "Acme Robotics" and shown["tokens"] <= cc.TOKEN_BUDGET
    assert any(line.startswith("Lead stage") for line in shown["summary"])
    assert shown["timeline"] and all(not line.startswith("- ") for line in shown["timeline"])


def test_matching_by_domain_name_and_people():
    assert cc.lead_matches(LEAD, "", "acme-robotics.example", set())
    assert cc.lead_matches(LEAD, "Acme Robotics Pvt Ltd", "", set())
    assert cc.lead_matches({**LEAD, "company": "X", "domain": ""}, "", "", {"asha@acme-robotics.example"})
    assert not cc.lead_matches({**LEAD, "company": "Acmex Foods", "domain": "acmex.example"}, "Acme Robotics",
                               "acme-robotics.example", set())
    phone = {"company": "Acme Robotics", "company_domain": ""}
    assert cc.recording_matches(phone, "Acme Robotics", "acme-robotics.example", set())
    namesake = {"company": "Acme Robotics", "company_domain": "acme-robotics-uk.example"}
    assert not cc.recording_matches(namesake, "Acme Robotics", "acme-robotics.example", set())
    from app.notetakers.base import email_hash
    by_person = {"company": "", "company_domain": "", "invitee_hashes": json.dumps([email_hash("dev@x.example")])}
    assert cc.recording_matches(by_person, "Acme Robotics", "", {email_hash("dev@x.example")})


class FakeConnection:
    """Answers the builder's SELECTs from made-up tables."""

    def __init__(self, tables):
        self.tables, self.calls = tables, []

    async def execute(self, sql, params=()):
        self.calls.append((sql, params))
        for name in ("lead_touchpoints", "notetaker_recordings", "calendar_history", "call_summaries",
                     "recording_transcripts", "phone_uploads", "leads"):
            if f"FROM {name}" in sql or (name == "notetaker_recordings" and "notetaker_recordings r" in sql):
                rows = self.tables.get(name, [])
                return SimpleNamespace(description=[SimpleNamespace(name=k) for k in (rows[0] if rows else {})],
                                       fetchall=lambda rows=rows: _async([tuple(r.values()) for r in rows]),
                                       fetchone=lambda: _async(None))
        return SimpleNamespace(description=[], fetchall=lambda: _async([]), fetchone=lambda: _async(None))


async def _async(value):
    return value


def test_gather_reads_every_store_and_includes_the_iphone_call(monkeypatch):
    lead_row = {**LEAD, "contacts": json.dumps(LEAD["contacts"]), "last_touch_at": ago(3)}
    other = {**lead_row, "id": "lead-2", "company": "Globex", "domain": "globex.example", "contacts": "[]"}
    recs = [{**r, "event_key": None, "event_title": "", "event_company": "", "invitee_hashes": "[]",
             "eval_status": r.get("eval_status", "handed_off"), "report_status": "sent"} for r in RECORDINGS]
    recs.append({**recs[0], "external_id": "now", "event_key": "current-event"})  # The interview itself.
    recs.append({**recs[1], "external_id": "p9", "company": "Globex"})
    tables = {"leads": [lead_row, other], "lead_touchpoints": [{**t, "data": json.dumps(t["data"])} for t in TOUCHES],
              "notetaker_recordings": recs,
              "calendar_history": [{"event_key": "old", "title": "Acme Robotics intro chat", "company": "Acme Robotics",
                                    "start_utc": ago(25), "interviewers": "[]"}],
              "call_summaries": [{"at": ago(5), "summary": "Acme Robotics builds warehouse robots."},
                                 {"at": ago(4), "summary": "Globex called back."}]}
    loaded = []

    async def transcript(connection, provider, external_id):
        loaded.append(f"{provider}:{external_id}")
        return TRANSCRIPTS.get(f"{provider}:{external_id}", [])
    monkeypatch.setattr(cc, "load_transcript", transcript)
    conn = FakeConnection(tables)
    ctx = asyncio.run(cc.gather(conn, "Acme Robotics", "acme-robotics.example", ["asha@acme-robotics.example"],
                                before=NOW, exclude_event="current-event", candidate="Sam Rivera", reviews=REVIEWS))
    assert ctx.lead["id"] == "lead-1"
    assert sorted(loaded) == ["fathom:f1", "phone:p1"]  # Not the current event, not Globex's call.
    text = cc.render(ctx, NOW)
    assert "Recorded phone call" in text and "Globex" not in text and "warehouse robots" in text
    touch_sql = next(params for sql, params in conn.calls if "FROM lead_touchpoints" in sql)
    assert touch_sql[0] == ["lead-1"]


def test_for_event_leaves_the_owner_out_and_excludes_the_event(monkeypatch):
    seen = {}

    async def gather(connection, company, domain, emails, before=None, exclude_event="", candidate=""):
        seen.update(company=company, domain=domain, emails=list(emails), before=before, exclude=exclude_event)
        return cc.build(company, None, [], [], {}, [])
    monkeypatch.setattr(cc, "gather", gather)
    event = SimpleNamespace(key="ev1", company="Acme Robotics", company_domain="acme-robotics.example",
                            start_utc="2026-10-30T10:00:00+00:00", organizer_email="me@mine.example",
                            attendees=[SimpleNamespace(email="me@mine.example"), SimpleNamespace(email="dev@acme-robotics.example")])
    assert asyncio.run(cc.for_event(None, event, {"me@mine.example"})) == ""
    assert seen["emails"] == ["dev@acme-robotics.example"] and seen["exclude"] == "ev1"
    assert seen["before"] == datetime(2026, 10, 30, 10, tzinfo=timezone.utc)


# The brief, the briefing call and the page use it.

def test_synthesis_and_claim_check_get_the_history():
    import pytest
    interview_agents = pytest.importorskip("app.interview_agents", reason="crewai is only in the worker image")
    block = interview_agents.history_block("Lead stage: interviewing.", "valr://company-history/acme")
    assert "valr://company-history/acme" in block and "not a confirmed budget" in block
    assert interview_agents.history_block("", "x") == ""
    assert interview_agents.PRIVATE_URL.match("valr://company-history/acme")


def test_briefing_call_and_get_briefing_include_what_was_discussed(monkeypatch):
    from app import interviews
    slot = {"title": "Acme Robotics round 2", "spoken": "Brief.", "notes": [],
            "parts": {"_history_short": "Previous interactions with Acme Robotics:\nOpen asks (no reply yet): references."}}
    note = interviews.briefing_note({"label": "today", "slots": [slot]})
    assert "Already discussed with them" in note and "Open asks (no reply yet): references" in note
    event = SimpleNamespace(title="Acme Robotics round 2", company="Acme Robotics", start_utc="2026-10-30T10:00:00+00:00",
                            attendees=[], key="k")
    monkeypatch.setattr(interviews, "spoken_label", lambda event, zone: "Acme Robotics at ten")
    payload = interviews.brief_payload(event, slot, timezone.utc)
    assert "references" in payload["already_discussed"]


def test_interview_page_shows_the_history_section():
    from app.valr_ui import interviews
    html = interviews.history_section({"company": "Acme Robotics", "summary": ["Lead stage: interviewing."],
                                       "timeline": ["21 Oct 2026: Email from Asha <b>"], "more": ""}, "Acme Robotics")
    assert "What you&#x27;ve already discussed with Acme Robotics" in html or \
        "What you've already discussed with Acme Robotics" in html
    assert "&lt;b&gt;" in html and "Lead stage: interviewing." in html
    assert interviews.history_section({}, "Acme Robotics") == ""


def test_the_history_is_for_the_owner_only(monkeypatch):
    from app import valr_dashboard
    from app.valr_ui import routes
    monkeypatch.setattr(valr_dashboard, "owner_emails", lambda: {"owner@mine.example"})
    assert routes.is_owner("Owner@mine.example") and not routes.is_owner("guest@else.example")


# The iPhone-call gaps (issue 6): leads, the coach's company, re-reading, the brief.

def test_stored_recordings_become_touchpoints_for_every_provider():
    from app import lead_worker, leads
    rows = [{"provider": "phone", "external_id": "p1", "title": "Phone call with Asha", "started_at": ago(2),
             "share_url": "", "company": "Acme Robotics", "company_domain": "", "company_how": "a lead with the same person",
             "event_company": None, "interviewers": '["Asha"]'},
            {"provider": "fireflies", "external_id": "ff1", "title": "Acme Robotics round 1", "started_at": ago(4),
             "share_url": "https://app.fireflies.ai/view/ff1", "company": "Acme Robotics",
             "company_domain": "acme-robotics.example", "company_how": "", "event_company": None, "interviewers": "[]"},
            {"provider": "fathom", "external_id": "f1", "title": "Known from the API", "started_at": ago(4),
             "share_url": "", "company": "Acme Robotics", "company_domain": "", "company_how": "", "event_company": None,
             "interviewers": "[]"}]
    touches = lead_worker.stored_to_touches(rows, {"fathom:f1"})
    assert [t.id for t in touches] == ["phone:p1", "fireflies:ff1"]
    phone = touches[0]
    assert phone.kind == "recording" and phone.company == "Acme Robotics" and phone.people[0].name == "Asha"
    assert phone.title == "Recording: Phone call with Asha" and "Phone call recording" in phone.summary
    assert phone.detail_url.endswith("phone-p1")
    # An email started the lead; the recordings attach to it by company (they never start one).
    mail = leads.Touch(id="gmail:1", kind="email", at=ago(10), company="Acme Robotics",
                       domain="acme-robotics.example", stage_hint="screening", direction="in")
    built = leads.build([mail] + touches, NOW)
    assert len(built) == 1 and {t.id for t in built[0].touches} == {"gmail:1", "phone:p1", "fireflies:ff1"}


def test_a_phone_calls_company_reaches_the_coach_review(tmp_path):
    from app.profile import post_call
    from tests.test_profile_evaluate import COMP
    from tests.test_profile_insights import CueLLM, meeting, settings
    cfg = settings(tmp_path)
    out = post_call.process(meeting(source="phone", id="p1", company_hint="Acme Robotics"), cfg, CueLLM(), COMP)
    assert out["status"] == "ready"
    perf = json.loads(cfg.performance_path.read_text())
    assert next(iv for iv in perf["interviews"] if iv["interview_id"] == "phone:p1")["company"] == "Acme Robotics"


def test_a_phone_call_can_be_read_again_from_valrs_own_copies(monkeypatch):
    from app import phone_recordings
    from app.notetakers import history, store, transcripts
    kept_turns = [{"speaker": "Asha", "text": "Hello?", "start": 0.0}, {"speaker": "Sam Rivera", "text": "Hi.", "start": 1.0}]

    async def sealed(connection, upload_id):
        return None  # Its sealed copy is gone: the transcript copy is used.

    async def load(connection, provider, external_id, key=None):
        return {"title": "Phone call with Asha", "turns": kept_turns}

    async def setup(connection):
        return None

    async def recording(connection, provider, external_id):
        return {"title": "Phone call with Asha", "started_at": ago(2)}
    monkeypatch.setattr(phone_recordings, "sealed_meeting", sealed)
    monkeypatch.setattr(transcripts, "load", load)
    monkeypatch.setattr(store, "setup", setup)
    monkeypatch.setattr(store, "recording", recording)
    meeting = asyncio.run(history.fetch_meeting(object(), "phone", "p1"))
    assert meeting["source"] == "phone" and meeting["id"] == "p1" and meeting["turns"] == kept_turns
    assert meeting["participants"] == ["Asha", "Sam Rivera"] and meeting["date"].startswith(ago(2).date().isoformat())


def test_try_again_is_offered_for_a_phone_call():
    from app.valr_ui import interviews
    html = interviews.reaction_block({"coverage": "derived", "talk_share": None}, {"id": "phone:p1"})
    assert "your phone recording" in html and 'data-reanalyse="phone:p1"' in html


def test_the_transcript_company_repair_keeps_a_calendar_or_lead_match():
    from app import lead_worker

    class Cursor:
        async def fetchall(self):
            return [("phone", "p1", "Phone call with Asha", None, "Acme Robotics", "", None, '["Asha"]',
                     "a lead with the same person")]

    class Conn:
        async def execute(self, sql, params=()):
            return Cursor()

    class Resolver:
        kept = 0

        async def resolve(self, *args, **kwargs):
            raise AssertionError("a calendar or lead match is not re-derived from the title")

    async def setup(connection):
        return None
    from app.notetakers import store
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "setup", setup)
    try:
        resolver = Resolver()
        assert asyncio.run(lead_worker.repair_recordings(Conn(), resolver, dry_run=True)) == []
        assert resolver.kept == 1
    finally:
        mp.undo()
