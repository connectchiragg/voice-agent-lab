"""Lead worker: sources -> touchpoints (pure), and a full Gmail pass against a fake Gmail
and a scratch Postgres (VALR_TEST_POSTGRES), with a fake classifier."""
import asyncio
import dataclasses
import base64
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet

from app import google_oauth, lead_classify, lead_worker, leads_store
from app.interview_types import Person, SlotEvent
from app.lead_classify import Decision
from app.lead_workflow import GmailSyncInput
from tests.leads_sample import NOW, fixture_emails

OWNER, EMAILS = fixture_emails()


def test_calendar_interviews_become_touchpoints():
    start = NOW + timedelta(days=2)
    events = [
        SlotEvent("k1", "Juniper · System design round", start.isoformat(), (start + timedelta(hours=1)).isoformat(),
                  attendees=[Person(OWNER, "Me", is_self=True), Person("ravi@juniper.example", "Ravi Menon"),
                             Person("asha@juniper.example", "Asha (Talent Acquisition)"),
                             Person("room@resource.calendar.google.com", "Room", resource=True)],
                  company="Juniper", company_domain="juniper.example"),
        SlotEvent("k2", "Dentist", start.isoformat(), start.isoformat()),
        SlotEvent("k3", "Kite interview", start.isoformat(), start.isoformat(), status="cancelled", company="Kite"),
        SlotEvent("k4", "Orbit post offer connect", start.isoformat(), start.isoformat(),
                  company="Orbit", company_domain="orbit.example"),
    ]
    touches = lead_worker.calendar_to_touches(events, {OWNER})
    assert [t.id for t in touches] == ["cal:k1", "cal:k4"]
    people = {p.email: p.role for p in touches[0].people}
    assert people == {"ravi@juniper.example": "interviewer", "asha@juniper.example": "recruiter"}
    assert touches[0].stage_hint == "interviewing" and touches[1].stage_hint == ""


def test_fathom_and_performance_touchpoints():
    items = [{"recording_id": 77, "meeting_title": "Interview with Quartz (R1)", "recording_start_time": "2026-10-15T10:00:00Z",
              "share_url": "https://fathom.video/share/x", "calendar_invitees": [
                  {"name": "Zed Alami", "email": "zed@quartz.example", "is_external": True},
                  {"name": "Me", "email": OWNER, "is_external": False}],
              "default_summary": {"markdown_formatted": "## Summary\n**Streaming** pipelines."}}]
    touch = lead_worker.fathom_to_touches(items, {OWNER})[0]
    assert touch.id == "fathom:77" and touch.company == "Quartz" and touch.domain == "quartz.example"
    assert [p.email for p in touch.people] == ["zed@quartz.example"] and touch.summary == "Summary Streaming pipelines."
    perf = {"interviews": [{"interview_id": "fathom:77", "date": "2026-10-15", "company": "Quartz", "round_type": "system_design",
                            "overall_score": 6.5, "outcome": "rejected"}, {"company": "", "date": "x"}]}
    [score] = lead_worker.performance_touches(perf)
    assert score.score == 6.5 and score.stage_hint == "closed-lost" and score.cue == "negative"
    assert score.title == "System design scored 6.5/10"


# A fake Gmail.

def raw_message(email):
    headers = [{"name": "From", "value": f"{email.from_name} <{email.from_email}>" if email.from_name else email.from_email},
               {"name": "To", "value": ", ".join(f"{n} <{a}>" if n else a for n, a in email.to)},
               {"name": "Subject", "value": email.subject}]
    headers += [{"name": k.title(), "value": v} for k, v in email.headers.items()]
    data = base64.urlsafe_b64encode(email.text.encode()).decode()
    return {"id": email.id, "threadId": email.thread_id, "labelIds": email.labels, "snippet": email.snippet,
            "internalDate": str(int(email.at.timestamp() * 1000)),
            "payload": {"mimeType": "multipart/alternative", "headers": headers,
                        "parts": [{"mimeType": "text/plain", "body": {"data": data}}]}}


class FakeGmail:
    def __init__(self):
        self.messages = {email.id: raw_message(email) for email, _ in EMAILS}
        self.history_id = 5000
        self.history: list[str] = []
        self.history_gone = False
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        self.calls.append(path)
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": OWNER, "historyId": str(self.history_id)})
        if path == "/messages":
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.messages]})
        if path.startswith("/messages/"):
            found = self.messages.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path == "/history":
            if self.history_gone:
                return httpx.Response(404)
            return httpx.Response(200, json={"historyId": str(self.history_id), "history": [
                {"id": "1", "messagesAdded": [{"message": {"id": i}} for i in self.history]}]})
        if path == "/watch":
            return httpx.Response(200, json={"historyId": str(self.history_id), "expiration": str(int((NOW + timedelta(days=7)).timestamp() * 1000))})
        return httpx.Response(404)


def fake_classify(email, owners):
    item = next(item for e, item in EMAILS if e.id == email.id) if email.id in {e.id for e, _ in EMAILS} else {"expect": "keep"}
    return Decision(item["expect"] == "keep", "fake", 0.9, "llm", "scheduling", company="Orbit Labs",
                    sender_role="recruiter", stage_hint=item.get("stage", ""), sentiment=0.4,
                    cue=item.get("cue", "none"), summary="A derived summary.",
                    usage={"input_tokens": 850, "output_tokens": 170, "cache_read": 0, "cache_write": 0})


needs_postgres = pytest.mark.skipif(not os.getenv("VALR_TEST_POSTGRES"),
                                    reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")


@pytest.fixture
def world(monkeypatch, tmp_path):
    gmail = FakeGmail()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", key)
    monkeypatch.setenv("VALR_GMAIL_TOPIC", "projects/p/topics/t")
    monkeypatch.setenv("VALR_PERFORMANCE_PATH", str(tmp_path / "performance.json"))
    (tmp_path / "performance.json").write_text(json.dumps({"interviews": [
        {"interview_id": "fathom:9", "date": (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat(),
         "company": "Orbit Labs", "overall_score": 8.0, "outcome": "advanced"}]}))
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)

    async def connect():
        return await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"], autocommit=True)

    async def access(connection, http):
        return "token", "connected"

    from app import interview_worker
    monkeypatch.setattr(lead_worker, "connect", connect)
    monkeypatch.setattr(lead_worker, "owner_emails", lambda: {OWNER})
    monkeypatch.setattr(interview_worker, "gmail_access", access)
    http = httpx.AsyncClient(transport=httpx.MockTransport(gmail.handler))
    activities = lead_worker.LeadActivities(http, fake_classify)

    async def reset():
        async with await connect() as connection:
            await connection.execute("DROP TABLE IF EXISTS gmail_state, lead_emails, lead_touchpoints, leads, "
                                     "research_spend, research_jobs, calendar_events, calendar_state")
    asyncio.run(reset())
    return gmail, activities, connect, key


async def rows(connect, sql, *args):
    async with await connect() as connection:
        cursor = await connection.execute(sql, args or None)
        return await cursor.fetchall()


@needs_postgres
def test_backfill_then_history_then_recovery(world, monkeypatch):
    gmail, activities, connect, key = world

    first = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    model_steps = sum(1 for _, item in EMAILS if item["step"] == "model")
    kept = sum(1 for _, item in EMAILS if item["expect"] == "keep")
    assert first.mode == "backfill" and first.fetched == len(EMAILS)
    assert first.kept == kept and first.llm_calls == model_steps and first.deferred == 0
    assert first.usd == pytest.approx(model_steps * (850 * 0.10 + 170 * 0.50) / 1e6)
    state = asyncio.run(rows(connect, "SELECT history_id, backfill_count FROM gmail_state"))
    assert state == [(5000, len(EMAILS))]
    # The decision log holds no subject, address or text; touchpoints only derived fields.
    log = asyncio.run(rows(connect, "SELECT * FROM lead_emails"))
    stored = json.dumps([list(map(str, r)) for r in log]) + json.dumps(
        [r[0] for r in asyncio.run(rows(connect, "SELECT data FROM lead_touchpoints"))])
    for email, _ in EMAILS:
        assert email.text not in stored and email.from_email not in json.dumps([list(map(str, r)) for r in log])
    snippet = asyncio.run(rows(connect, "SELECT snippet_enc FROM lead_touchpoints WHERE id = 'gmail:e01'"))[0][0]
    assert google_oauth.decrypt(key, snippet) == EMAILS[0][0].snippet[:120] and EMAILS[0][0].snippet not in snippet

    # A push for something already processed is skipped; a new reply in a kept thread is kept.
    assert asyncio.run(activities.gmail_sync(GmailSyncInput("push", 4000))).mode == "skipped"
    newsletter = next(e for e, i in EMAILS if i["id"] == "e16")  # Dropped by itself...
    reply = dataclasses.replace(newsletter, id="r1", thread_id="t-e03")  # ...but a reply in a kept thread.
    gmail.messages["r1"] = raw_message(reply)
    gmail.history, gmail.history_id = ["r1"], 5100
    second = asyncio.run(activities.gmail_sync(GmailSyncInput("push", 5100)))
    assert (second.mode, second.fetched, second.kept, second.llm_calls) == ("history", 1, 1, 1)

    gmail.history_gone, gmail.history_id = True, 6000
    third = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert third.mode == "recovery" and third.fetched == 0  # Everything seen already.
    assert asyncio.run(rows(connect, "SELECT history_id FROM gmail_state")) == [(6000,)]

    summary = asyncio.run(activities.leads_refresh("test"))
    assert summary.sources["evaluation"] == 1 and summary.leads >= 1
    names = [r[0] for r in asyncio.run(rows(connect, "SELECT company FROM leads"))]
    assert "Orbit Labs" in names
    spend = asyncio.run(rows(connect, "SELECT count(*), sum(usd) FROM research_spend WHERE job_id LIKE 'leads-%%'"))
    assert spend[0][0] == model_steps + 1


@needs_postgres
def test_over_budget_is_deferred_and_retried(world, monkeypatch):
    gmail, activities, connect, _ = world
    monkeypatch.setenv("VALR_LEADS_LLM_PER_DAY", "2")
    first = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    model_steps = sum(1 for _, item in EMAILS if item["step"] == "model")
    assert first.llm_calls == 2 and first.deferred == model_steps - 2
    monkeypatch.setenv("VALR_LEADS_LLM_PER_DAY", "100")
    gmail.history = []
    second = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert second.llm_calls == model_steps - 2 and second.deferred == 0


@needs_postgres
def test_watch_stores_the_expiry(world):
    _, activities, connect, _ = world
    result = asyncio.run(activities.gmail_watch("test"))
    assert result.error == "" and result.history_id == 5000
    assert asyncio.run(rows(connect, "SELECT watch_expires FROM gmail_state"))[0][0] == NOW + timedelta(days=7)
