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

from app import gmail_api, google_oauth, lead_classify, lead_worker, leads_store
from app.interview_types import Person, SlotEvent
from app.lead_classify import Decision
from app.lead_workflow import GmailSyncInput
from tests.leads_sample import NOW, fixture_emails

OWNER, EMAILS = fixture_emails()


def test_calendar_interviews_intro_chats_and_known_contacts_become_touchpoints():
    start = NOW + timedelta(days=2)

    def ev(key, title, attendees=(), company="", domain="", **kw):
        return SlotEvent(key, title, start.isoformat(), (start + timedelta(hours=1)).isoformat(),
                         attendees=[Person(OWNER, "Me", is_self=True), *attendees], company=company,
                         company_domain=domain, **kw)
    ravi = Person("ravi@juniper.example", "Ravi Menon")
    asha = Person("asha@juniper.example", "Asha (Talent Acquisition)")
    events = [
        ev("k1", "Juniper · System design round", [ravi, asha, Person("room@resource.calendar.google.com", "Room", resource=True)],
           "Juniper", "juniper.example"),
        ev("k2", "Dentist"),
        ev("k3", "Kite interview", company="Kite", status="cancelled"),
        ev("k4", "Orbit post offer connect", [Person("tom@orbit.example", "Tom")], "Orbit", "orbit.example"),
        ev("k5", "Intro chat", [Person("jo@nimbus.example", "Jo Ibarra")]),  # Intro with an outside attendee.
        ev("k6", "Intro chat"),  # Nobody from outside: not a lead.
        ev("k7", "Catch-up", [Person("lena@orbitlabs.example", "Lena Park")]),  # A known Gmail contact.
        ev("k8", "Recruiter screen", [Person("sam@tessel.example", "Sam")], "Tessel", "tessel.example"),
    ]
    touches = {t.id: t for t in lead_worker.calendar_to_touches(events, {OWNER}, {"lena@orbitlabs.example"})}
    assert sorted(touches) == ["cal:k1", "cal:k5", "cal:k7", "cal:k8"]
    assert {p.email: p.role for p in touches["cal:k1"].people} == {"ravi@juniper.example": "interviewer",
                                                                 "asha@juniper.example": "recruiter"}
    assert touches["cal:k1"].stage_hint == "interviewing"
    assert touches["cal:k5"].stage_hint == touches["cal:k7"].stage_hint == touches["cal:k8"].stage_hint == "screening"
    assert touches["cal:k5"].summary.startswith("Recruiter chat")


def test_interview_detail_anchors_match_the_scorecards():
    assert lead_worker.detail_url("fathom:1000") == "/valr/interviews/past/fathom-1000"
    assert lead_worker.detail_url("fireflies:ab_9") == "/valr/interviews/past/fireflies-ab-9"
    assert lead_worker.detail_url("") == "/valr/performance"


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
    assert score.title == "System design scored 6.5/10" and score.detail_url == "/valr/interviews/past/fathom-77"
    assert touch.detail_url == "/valr/interviews/past/fathom-77"


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


RATE_LIMITED = {"error": {"code": 403, "message": "Quota exceeded for quota metric 'Total Query Cost'",
                           "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}


class FakeGmail:
    def __init__(self):
        self.messages = {email.id: raw_message(email) for email, _ in EMAILS}
        self.history_id = 5000
        self.history: list[str] = []
        self.history_gone = False
        self.calls: list[str] = []
        self.gets_allowed: int | None = None  # Then every messages.get is rate limited.
        self.unread: set[str] = set()  # What "is:unread newer_than:30d" lists.
        self.label_events: list[dict] = []  # Extra history items (labelsAdded / labelsRemoved).

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        self.calls.append(path)
        if path.startswith("/messages/") and self.gets_allowed is not None:
            if self.gets_allowed <= 0:
                return httpx.Response(403, json=RATE_LIMITED)
            self.gets_allowed -= 1
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": OWNER, "historyId": str(self.history_id)})
        if path == "/messages":
            if request.url.params.get("q", "").startswith("is:unread"):
                return httpx.Response(200, json={"messages": [{"id": i} for i in sorted(self.unread)]})
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.messages]})
        if path.startswith("/messages/"):
            found = self.messages.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path == "/history":
            if self.history_gone:
                return httpx.Response(404)
            return httpx.Response(200, json={"historyId": str(self.history_id), "history": [
                {"id": "1", "messagesAdded": [{"message": {"id": i}} for i in self.history]}, *self.label_events]})
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
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(gmail_api, "_pacer", gmail_api.Pacer(1e9, sleep=no_sleep))

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
                                     "lead_overrides, research_spend, research_jobs, calendar_events, "
                                     "calendar_state, oauth_tokens, notifications, notify_settings")
    asyncio.run(reset())
    return gmail, activities, connect, key


async def rows(connect, sql, *args):
    async with await connect() as connection:
        cursor = await connection.execute(sql, args or None)
        return await cursor.fetchall()


def spy_dispatch(monkeypatch):
    """notifications.dispatch_now replaced by a recorder (items stay pending, nothing is sent)."""
    from app import notifications
    calls = []

    async def record(connection, bot=None, tries=5, wait=0.5):
        calls.append(datetime.now(timezone.utc))
    monkeypatch.setattr(notifications, "dispatch_now", record)
    return calls


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
    sent = next(e for e, i in EMAILS if i["id"] == "e17")  # His own reply: in e03's thread now.
    gmail.messages["r2"] = raw_message(dataclasses.replace(sent, id="r2", thread_id="t-e03"))
    gmail.messages["r3"] = raw_message(dataclasses.replace(sent, id="r3", thread_id="t-unknown"))
    gmail.history, gmail.history_id = ["r1", "r2", "r3"], 5100
    second = asyncio.run(activities.gmail_sync(GmailSyncInput("push", 5100)))
    assert (second.mode, second.fetched, second.kept, second.llm_calls) == ("history", 3, 2, 1)

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
    data = asyncio.run(rows(connect, "SELECT data FROM lead_touchpoints WHERE id = 'gmail:r2'"))[0][0]
    assert data["direction"] == "out" and data["thread"] == "t-e03"


@needs_postgres
def test_overrides_are_stored_and_replayed(world):
    _, _, connect, _ = world

    async def main():
        async with await connect() as connection:
            await leads_store.setup(connection)
            await leads_store.add_override(connection, "lead-a", "dismiss", touch_ids=["gmail:1"], threads=["t1"])
            await leads_store.add_override(connection, "lead-b", "stage", "offer")
            await leads_store.add_override(connection, "lead-c", "stage", "applied")
            await leads_store.add_override(connection, "lead-c", "stage", "")  # Back to automatic.
            await leads_store.add_override(connection, "lead-d", "merge", "lead-b")
            return await leads_store.overrides(connection)
    o = asyncio.run(main())
    assert o.dismissed_touches == {"gmail:1"} and o.dismissed_threads == {"t1"}
    assert o.stages == {"lead-b": "offer"} and o.merges == {"lead-d": "lead-b"}



@needs_postgres
def test_an_expired_gmail_token_alerts_once_in_the_bell(world, monkeypatch):
    from app import interview_worker
    _, activities, connect, _ = world

    async def refused(connection, http):
        return None, "reconnect needed"
    monkeypatch.setattr(interview_worker, "gmail_access", refused)

    async def token_row(connected_at):
        async with await connect() as connection:
            await google_oauth.setup(connection)
            await connection.execute("DELETE FROM oauth_tokens")
            await connection.execute(
                "INSERT INTO oauth_tokens (provider, account, refresh_token, scope, status, connected_at) "
                "VALUES ('google_gmail', 'me@example.com', 'x', 's', 'reconnect_needed', %s)", (connected_at,))
    asyncio.run(token_row(NOW - timedelta(days=7)))
    for _ in range(2):
        assert asyncio.run(activities.gmail_sync(GmailSyncInput("poll"))).error == "Gmail reconnect needed"
    alerts = asyncio.run(rows(connect, "SELECT kind, title, link FROM notifications WHERE kind = 'account'"))
    assert alerts == [("account", "Google disconnected", "/valr/settings#google")]
    asyncio.run(token_row(NOW))  # Reconnected, then expired again: one more alert.
    asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert len(asyncio.run(rows(connect, "SELECT 1 FROM notifications WHERE kind = 'account'"))) == 2


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


def gets(gmail):
    return [c.rsplit("/", 1)[1] for c in gmail.calls if c.startswith("/messages/")]


@needs_postgres
def test_a_rate_limit_pauses_the_backfill_and_the_next_run_resumes_it(world, monkeypatch):
    gmail, activities, connect, _ = world
    monkeypatch.setattr(lead_worker, "BATCH", 5)
    gmail.gets_allowed = 7  # One full batch of 5, then 2 of the second batch.

    first = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert (first.mode, first.more, first.fetched, first.error) == ("paused-ratelimit", True, 7, "rate limited, resuming")
    state = asyncio.run(rows(connect, "SELECT history_id, backfill_done_at, backfill_count, last_error FROM gmail_state"))
    assert state == [(5000, None, 7, "rate limited, resuming")]  # The baseline is saved first.
    assert len(asyncio.run(rows(connect, "SELECT 1 FROM lead_emails"))) == 7  # The partial batch too.

    # A push while the backfill is not done only records the push: no Gmail call.
    calls = len(gmail.calls)
    push = asyncio.run(activities.gmail_sync(GmailSyncInput("push", 5200)))
    assert push.mode == "skipped" and len(gmail.calls) == calls
    assert asyncio.run(rows(connect, "SELECT last_push_at IS NOT NULL FROM gmail_state")) == [(True,)]

    done_before = set(gets(gmail)[:7])
    gmail.gets_allowed = None
    calls = len(gmail.calls)
    second = asyncio.run(activities.gmail_sync(GmailSyncInput("resume")))
    assert (second.mode, second.more, second.fetched, second.left) == ("backfill", False, len(EMAILS) - 7, 0)
    resumed = [c.rsplit("/", 1)[1] for c in gmail.calls[calls:] if c.startswith("/messages/")]
    assert not done_before & set(resumed)  # Nothing fetched twice.
    state = asyncio.run(rows(connect, "SELECT backfill_done_at IS NOT NULL, backfill_count, last_error FROM gmail_state"))
    assert state == [(True, len(EMAILS), None)]
    assert asyncio.run(activities.gmail_sync(GmailSyncInput("poll"))).mode == "history"


@needs_postgres
def test_each_run_fetches_at_most_the_per_run_cap(world, monkeypatch):
    gmail, activities, connect, _ = world
    monkeypatch.setenv("VALR_LEADS_FETCH_PER_RUN", "8")
    passes = []
    while True:
        summary = asyncio.run(activities.gmail_sync(GmailSyncInput("resume")))
        passes.append((summary.mode, summary.fetched, summary.more))
        if not summary.more:
            break
    expected = [8] * (len(EMAILS) // 8) + ([len(EMAILS) % 8] if len(EMAILS) % 8 else [])
    assert [p[1] for p in passes] == expected and all(p[0] == "backfill" for p in passes)
    assert len(gets(gmail)) == len(EMAILS) == len(set(gets(gmail)))
    assert asyncio.run(rows(connect, "SELECT backfill_done_at IS NOT NULL FROM gmail_state")) == [(True,)]


@needs_postgres
def test_the_bell_follows_gmail_unread_mail_silently_and_new_mail_still_notifies(world, monkeypatch):
    gmail, activities, connect, _ = world
    pushed = spy_dispatch(monkeypatch)
    now = datetime.now(timezone.utc)
    inbound = next(e for e, item in EMAILS if item["step"] == "model" and item["expect"] == "keep")
    for message_id, age in (("nw1", timedelta(days=5)), ("nw2", timedelta(days=6))):
        gmail.messages[message_id] = raw_message(dataclasses.replace(inbound, id=message_id, thread_id=f"t-{message_id}",
                                                                     at=now - age))
    gmail.unread = {"nw1"}  # nw2 was read already.

    first = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert first.mode == "backfill"
    bell = asyncio.run(rows(connect, "SELECT source_id, read_at, push_state, created_at FROM notifications"))
    assert [(r[0], r[1], r[2]) for r in bell] == [("gmail:nw1", None, "skipped")]  # Silent.
    assert abs(bell[0][3] - (now - timedelta(days=5))) < timedelta(seconds=1)  # Dated when it arrived.

    # Next pass: he read nw1 in Gmail (labelRemoved UNREAD); a new email arrives, unread.
    gmail.messages["nw3"] = raw_message(dataclasses.replace(inbound, id="nw3", thread_id="t-nw3",
                                                            at=now - timedelta(minutes=3)))
    gmail.history, gmail.history_id, gmail.unread = ["nw3"], 5100, {"nw3"}
    gmail.label_events = [{"id": "2", "labelsRemoved": [{"message": {"id": "nw1"}, "labelIds": ["UNREAD"]}]}]
    second = asyncio.run(activities.gmail_sync(GmailSyncInput("push", 5100)))
    assert second.mode == "history"
    bell = {r[0]: r[1:] for r in asyncio.run(rows(connect, "SELECT source_id, read_at IS NOT NULL, push_state "
                                                            "FROM notifications"))}
    assert bell == {"gmail:nw1": (True, "skipped"), "gmail:nw3": (False, "pending")}  # Read; the new one pushes.
    assert len(pushed) == 1 and second.notified == 1  # Pushed right after its batch, not at the loop's tick.


# Latency.

@needs_postgres
def test_mail_arriving_during_a_backfill_is_looked_at_first_and_notifies(world, monkeypatch):
    gmail, activities, connect, _ = world
    pushed = spy_dispatch(monkeypatch)
    monkeypatch.setenv("VALR_LEADS_FETCH_PER_RUN", "5")  # A backfill that needs several runs.
    first = asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert first.mode == "backfill" and first.more and first.notified == 0  # Old mail is not news.
    started = asyncio.run(rows(connect, "SELECT backfill_started_at FROM gmail_state"))[0][0]
    inbound = next(e for e, item in EMAILS if item["step"] == "model" and item["expect"] == "keep")
    fresh = dataclasses.replace(inbound, id="live1", thread_id="t-live1", at=started + timedelta(seconds=30))
    gmail.messages["live1"] = raw_message(fresh)
    gmail.history, gmail.history_id = ["live1"], 5100
    gmail.calls.clear()
    second = asyncio.run(activities.gmail_sync(GmailSyncInput("resume")))
    assert second.mode == "backfill" and gets(gmail)[0] == "live1"  # Before the month of older mail.
    bell = asyncio.run(rows(connect, "SELECT source_id, push_state FROM notifications"))
    assert ("gmail:live1", "pending") in bell and len(pushed) >= 1
    assert all(r[0] == "gmail:live1" or r[1] == "skipped" for r in bell)  # Older mail: silent at most.
    assert asyncio.run(rows(connect, "SELECT history_id FROM gmail_state")) == [(5000,)]  # Baseline kept.


@needs_postgres
def test_a_classifier_failure_on_new_mail_runs_the_sync_again_soon(world, monkeypatch):
    gmail, activities, connect, _ = world
    spy_dispatch(monkeypatch)
    asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))  # The backfill.
    inbound = next(e for e, item in EMAILS if item["step"] == "model" and item["expect"] == "keep")
    gmail.messages["f1"] = raw_message(dataclasses.replace(inbound, id="f1", thread_id="t-f1",
                                                           at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    gmail.history, gmail.history_id = ["f1"], 5100

    def down(email, owners):
        raise RuntimeError("model unavailable")
    activities._classify = down
    failed = asyncio.run(activities.gmail_sync(GmailSyncInput("push", 5100)))
    assert failed.deferred == 1 and failed.retry and failed.more  # The workflow runs again in ~30 s.
    activities._classify = fake_classify
    gmail.history = []
    again = asyncio.run(activities.gmail_sync(GmailSyncInput("resume")))
    assert again.kept == 1 and not again.more and again.notified == 1


@needs_postgres
def test_the_fast_check_starts_a_sync_only_for_mail_no_push_picked_up(world, monkeypatch):
    gmail, activities, connect, _ = world
    monkeypatch.setenv("VALR_LEADS", "1")
    started = []

    async def start(reason):
        started.append(reason)
    from app import interview_worker
    tokens = []
    real_access = interview_worker.gmail_access

    async def access(connection, client):
        tokens.append(1)
        return await real_access(connection, client)
    monkeypatch.setattr(interview_worker, "gmail_access", access)
    check = lead_worker.GmailCheck(connect, start, activities.http)
    assert asyncio.run(check.tick()) == "backfill"  # Nothing before the backfill is done.
    spy_dispatch(monkeypatch)
    asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    assert asyncio.run(check.tick()) == "idle"
    inbound = next(e for e, item in EMAILS if item["step"] == "model" and item["expect"] == "keep")
    gmail.messages["p1"] = raw_message(dataclasses.replace(inbound, id="p1", thread_id="t-p1"))
    gmail.history = ["p1"]
    assert asyncio.run(check.tick()) == "waiting"  # A push-started sync gets one interval first.
    assert asyncio.run(check.tick()) == "started" and started == ["check"]
    asyncio.run(activities.gmail_sync(GmailSyncInput("check")))
    assert asyncio.run(check.tick()) == "idle"  # Decided now; the stored historyId moved on.
    gmail.history_gone = True
    assert asyncio.run(check.tick()) == "started" and started == ["check", "check"]  # The sync recovers.
    assert len(tokens) == 3  # Two syncs fetch their own; the check fetched once for its 5 ticks.


def test_the_fast_check_is_off_without_leads(monkeypatch):
    monkeypatch.setenv("VALR_LEADS", "0")
    check = lead_worker.GmailCheck(lambda: None, lambda reason: None)
    assert asyncio.run(check.tick()) == "off"
    monkeypatch.setenv("VALR_GMAIL_CHECK_S", "0")
    asyncio.run(lead_worker.check_loop(lambda: None, lambda reason: None))  # Returns at once.


def test_the_backfill_leaves_model_calls_for_new_mail(monkeypatch):
    monkeypatch.setenv("VALR_LEADS_LLM_PER_DAY", "300")
    monkeypatch.delenv("VALR_LEADS_LLM_RESERVE", raising=False)
    assert lead_worker.llm_reserve() == 50
    budget = lead_worker.Budget(51, True, lead_worker.llm_reserve())
    assert budget.take() and not budget.take()  # The last 50 are for live mail.
    monkeypatch.setenv("VALR_LEADS_LLM_PER_DAY", "8")
    assert lead_worker.llm_reserve() == 2  # At most a quarter of a small budget.


@needs_postgres
def test_the_push_path_puts_the_email_in_the_bell_before_the_classifier_answers(world, monkeypatch):
    gmail, activities, connect, _ = world
    pushed = spy_dispatch(monkeypatch)
    asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))  # The backfill.
    inbound = next(e for e, item in EMAILS if item["step"] == "model" and item["expect"] == "keep")
    arrived = datetime.now(timezone.utc) - timedelta(seconds=3)
    for message_id in ("job1", "junk1"):
        gmail.messages[message_id] = raw_message(dataclasses.replace(inbound, id=message_id, thread_id=f"t-{message_id}",
                                                                     at=arrived))
    gmail.history, gmail.history_id = ["job1", "junk1"], 5100
    seen_while_classifying = {}

    def slow_model(email, owners):
        # The model is still thinking: the bell must already show the email.
        with psycopg.connect(os.environ["VALR_TEST_POSTGRES"]) as sync:
            seen_while_classifying[email.id] = sync.execute(
                "SELECT kind FROM notifications WHERE source_id = %s", (f"gmail:{email.id}",)).fetchone()
        decided = fake_classify(email, owners)
        return dataclasses.replace(decided, keep=email.id == "job1")
    activities._classify = slow_model
    result = asyncio.run(activities.gmail_sync(GmailSyncInput("push", 5100)))
    assert seen_while_classifying == {"job1": ("incoming",), "junk1": ("incoming",)}
    bell = {r[0]: r[1:] for r in asyncio.run(rows(connect, "SELECT source_id, kind, push_state, "
                                                            "extract(epoch FROM created_at - arrived_at) FROM notifications"))}
    assert set(bell) == {"gmail:job1"}  # The dropped one was retracted.
    kind, push_state, latency = bell["gmail:job1"]
    assert kind == "interview" and push_state == "pending" and len(pushed) == 1 and result.notified == 1
    assert 0 <= float(latency) < 10  # Counted from when it first showed, not from the classification.


@needs_postgres
def test_without_pubsub_the_check_syncs_new_mail_at_once(world, monkeypatch):
    gmail, activities, connect, _ = world
    monkeypatch.setenv("VALR_LEADS", "1")
    monkeypatch.delenv("VALR_GMAIL_TOPIC")
    monkeypatch.delenv("VALR_GMAIL_CHECK_S", raising=False)
    assert lead_worker.check_every() == 15
    started = []

    async def start(reason):
        started.append(reason)
    spy_dispatch(monkeypatch)
    asyncio.run(activities.gmail_sync(GmailSyncInput("poll")))
    check = lead_worker.GmailCheck(connect, start, activities.http)
    gmail.messages["q1"] = raw_message(dataclasses.replace(EMAILS[0][0], id="q1", thread_id="t-q1"))
    gmail.history = ["q1"]
    assert asyncio.run(check.tick()) == "started" and started == ["check"]  # No push to wait for.
