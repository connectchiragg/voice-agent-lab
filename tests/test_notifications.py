"""Notifications: which emails notify, the dedupe, quiet hours and the digest push, push text
without PII, calendar changes, the removed inferred kinds, and the owner-only API. All names, addresses and numbers are fictional."""
import asyncio
import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import notifications as n
from app.lead_classify import Decision, Email
from app.leads import Lead

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("VALR_NOTIFY_QUIET", "VALR_NOTIFY_IMMEDIATE", "VALR_NOTIFY_DIGEST", "VALR_NOTIFY_QUIET_HOURS",
                 "VALR_NOTIFY_DIGEST_AT", "VALR_NOTIFY_URGENT_HOURS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("VALR_HOST", "valr.example.test")


class MemoryStore:
    """The same methods as notifications.Store, in memory."""

    def __init__(self):
        self.rows, self.states, self.settings, self.next_id = [], {}, {}, 1

    async def setup(self):
        # Store.hide_removed: stored items of the removed kinds are read and never sent.
        for r in self.rows:
            if r["kind"] in n.REMOVED:
                r["read_at"] = r["read_at"] or NOW
                r["push_state"] = "skipped" if r.get("push_state") == "pending" else r.get("push_state")

    async def get_source(self, source_id):
        return next((r for r in self.rows if r["source_id"] == source_id), None)

    async def delete(self, note_id):
        self.rows = [r for r in self.rows if r["id"] != note_id]

    async def latencies(self, since):
        return [((r["created_at"] - r["arrived_at"]).total_seconds(),
                 (r["push_sent_at"] - r["arrived_at"]).total_seconds() if r.get("push_sent_at") else None)
                for r in self.rows if r.get("arrived_at") and r["created_at"] >= since]

    async def recent_for_thread(self, thread_id, since):
        found = [r for r in self.rows if r.get("thread_id") == thread_id and r["created_at"] >= since
                 and r["kind"] != n.INCOMING]
        return max(found, key=lambda r: r["created_at"]) if found else None

    async def recent_for_lead(self, lead_id, since):
        return [r for r in self.rows if r.get("lead_id") == lead_id and r["created_at"] >= since]

    async def insert(self, note):
        if any(r["source_id"] == note.get("source_id") for r in self.rows):
            return None
        row = {k: None for k in n.FIELDS} | {"body": "", "company": "", "link": "", "gmail_url": "", "action": ""} | note
        row["id"] = self.next_id
        self.next_id += 1
        self.rows.append(row)
        return row["id"]

    async def update(self, note_id, **values):
        next(r for r in self.rows if r["id"] == note_id).update(values)

    async def unlinked(self):
        return [r for r in self.rows if r.get("lead_id") is None and r.get("thread_id")]

    async def push_pending(self):
        return [dict(r) for r in self.rows if r.get("push_state") == "pending" and r["kind"] not in n.REMOVED]

    async def listing(self, unread_only, since, limit=100):
        rows = [r for r in self.rows if r["created_at"] >= since and r["kind"] not in n.REMOVED
                and not (unread_only and r["read_at"])]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    async def unread_count(self, since):
        return sum(1 for r in self.rows if not r["read_at"] and r["created_at"] >= since and r["kind"] not in n.REMOVED)

    async def email_items(self, since):
        return {r["source_id"].removeprefix("gmail:"): {"id": r["id"], "read_at": r["read_at"]} for r in self.rows
                if str(r["source_id"]).startswith("gmail:") and r["created_at"] >= since}

    async def mark_read(self, ids, now):
        hit = [r for r in self.rows if not r["read_at"] and (ids is None or r["id"] in ids)]
        for r in hit:
            r["read_at"] = now
        return len(hit)

    async def mark_unread(self, ids):
        hit = [r for r in self.rows if r["read_at"] and r["id"] in ids]
        for r in hit:
            r["read_at"] = None
        return len(hit)

    async def switches(self):
        return n.Switches.from_row(self.settings or None)

    async def save_switches(self, **values):
        self.settings.update(values)

    def by_source(self, source_id):
        return next((r for r in self.rows if r["source_id"] == source_id), None)


def email(message_id="m1", thread="t1", subject="Next steps", at=NOW - timedelta(minutes=5)):
    return Email(id=message_id, thread_id=thread, at=at, from_name="Riya Sen", from_email="riya@acme.example",
                 subject=subject)


def decision(category="interviewer_followup", keep=True, action_requested=None, action="", stage_hint="",
             summary="Recruiter follow-up."):
    return Decision(keep, "ok", 0.9, "llm", category=category, action_requested=action_requested, action=action,
                    stage_hint=stage_hint, summary=summary)


def touch(direction="in", company="Acme"):
    return SimpleNamespace(direction=direction, company=company, url="https://mail.google.com/mail/#all/t1")


# Which emails notify.

@pytest.mark.parametrize("category, asked, hint, new_thread, subject, kind", [
    ("offer", None, "offer", False, "Offer letter", "offer"),
    ("scheduling", None, "interviewing", False, "Interview slots", "interview"),
    ("interviewer_followup", None, "", False, "Need to reschedule our call", "interview"),
    ("assessment", True, "", False, "Take-home", "take_home"),
    ("recruiter_outreach", True, "screening", False, "Quick chat?", "reply_needed"),
    ("rejection", None, "closed-lost", False, "Your application", "rejection"),
    ("recruiter_outreach", False, "sourced", True, "Staff Backend role", "reply"),  # No "new lead" kind: the email.
    ("interviewer_followup", False, "", False, "Thanks for your time", "reply"),
    ("application_update", False, "applied", False, "Thank you for applying", None),
    ("application_update", False, "applied", False, "Your application moved to the hiring team", "update"),  # FYI: a dot too.
    ("newsletter", None, "", True, "Weekly jobs", None),
    ("job_board_alert", None, "", True, "10 new jobs", None),
    ("own_reply", None, "", False, "Re: hi", None),
])
def test_email_kinds(category, asked, hint, new_thread, subject, kind):
    assert n.email_kind(category, asked, hint, new_thread, subject) == kind


def test_dropped_outbound_and_old_mail_never_notify():
    store = MemoryStore()
    assert run(n.from_email(store, email(), decision(keep=False), touch(), False, NOW)) is None
    assert run(n.from_email(store, email(), decision(), touch(direction="out"), False, NOW)) is None
    old = email(at=NOW - timedelta(days=3))
    assert run(n.from_email(store, old, decision(category="offer"), touch(), False, NOW)) is None
    assert run(n.from_email(store, email(), decision(category="newsletter"), touch(), True, NOW)) is None
    assert store.rows == []


def test_a_recruiter_asking_for_availability_is_one_immediate_item_with_lead_and_gmail_links():
    store = MemoryStore()
    got = run(n.from_email(store, email(), decision(action_requested=True, action="Please share your availability"),
                           touch(), False, NOW))
    row = store.rows[0]
    assert got == row["id"] and row["kind"] == "reply_needed" and row["importance"] == "immediate"
    assert row["title"] == "Acme asked you to share your availability"
    assert row["gmail_url"].startswith("https://mail.google.com/") and row["link"] == "/valr/leads"
    lead = Lead("lead-acme", "Acme", "acme", touches=[SimpleNamespace(thread="t1")])
    run(n.after_rebuild(store, [lead], NOW))
    assert store.rows[0]["lead_id"] == "lead-acme" and store.rows[0]["link"] == "/valr/leads?lead=lead-acme"


def test_dedupe_per_thread_within_ten_minutes_and_upgrade():
    store = MemoryStore()
    run(n.from_email(store, email("m1"), decision(), touch(), False, NOW))
    assert run(n.from_email(store, email("m2"), decision(), touch(), False, NOW + timedelta(minutes=3))) is None
    assert len(store.rows) == 1 and store.rows[0]["kind"] == "reply"
    # A more important email in the same thread upgrades the item instead of adding one.
    run(n.from_email(store, email("m3"), decision(category="offer"), touch(), False, NOW + timedelta(minutes=5)))
    assert len(store.rows) == 1 and store.rows[0]["kind"] == "offer" and store.rows[0]["importance"] == "immediate"
    # Read, then another email lands in the same thread: the item is unread again (its dot comes back).
    store.rows[0]["read_at"] = NOW + timedelta(minutes=6)
    run(n.from_email(store, email("m3b"), decision(), touch(), False, NOW + timedelta(minutes=7)))
    assert len(store.rows) == 1 and store.rows[0]["read_at"] is None
    # After 10 minutes the thread notifies again; a re-run of the same message never does.
    run(n.from_email(store, email("m4"), decision(), touch(), False, NOW + timedelta(minutes=16)))
    assert len(store.rows) == 2
    assert run(n.from_email(store, email("m4"), decision(), touch(), False, NOW + timedelta(minutes=40))) is None


# Push text: no pay figures, phone numbers or email addresses.

def test_push_text_carries_no_pii():
    note = {"kind": "reply_needed", "company": "Acme", "link": "/valr/leads?lead=x", "importance": "immediate",
            "action": "Call riya@acme.example or +91 98765 43210 about the 45 LPA CTC, ₹30,00,000 base"}
    message = n.push_message(note)
    assert message["title"] == "Action needed · Acme" and message["url"] == "/valr/leads?lead=x"
    for leak in ("@", "98765", "43210", "45", "LPA", "30,00,000", "₹"):
        assert leak not in message["body"], leak
    digest = n.digest_message([{"kind": "rejection", "company": "Tessel"}, {"kind": "reply", "company": "Quillworks"},
                               {"kind": "calendar", "company": "Glintworks",
                                "title": "Interview with Glintworks cancelled: Mon 19 Oct, 6:00 PM"}])
    assert digest["title"] == "VALR · 3 updates" and digest["tag"] == "valr-digest" and not digest["urgent"]
    assert digest["body"].startswith("Tessel said no · Quillworks replied · Interview with Glintworks cancelled")


def test_own_kinds_push_their_own_words():
    heads_up = {"kind": "briefing", "importance": "immediate", "title": "Nextiva interview in 30 min",
                "body": "Tap for your briefing call.", "link": "/valr/interviews/slot-x?call=ready", "source_id": "heads_up:b"}
    message = n.push_message(heads_up)
    assert (message["title"], message["body"], message["url"], message["urgent"]) == (
        "Nextiva interview in 30 min", "Tap for your briefing call.", "/valr/interviews/slot-x?call=ready", True)


# Quiet hours and the digest.

def ist(hour, minute=0):
    return datetime(2026, 10, 17, hour, minute, tzinfo=n.zone()).astimezone(timezone.utc)


def test_quiet_hours_window():
    assert n.in_quiet_hours(ist(23, 30)) and n.in_quiet_hours(ist(2)) and n.in_quiet_hours(ist(7, 59))
    assert not n.in_quiet_hours(ist(8)) and not n.in_quiet_hours(ist(22, 59))


def test_quiet_hours_and_digest_time_come_from_settings():
    switches = n.Switches.from_row({"quiet_start": "22:00", "quiet_end": "07:00", "digest_at": "18:00"})
    assert n.in_quiet_hours(ist(22, 30), switches) and not n.in_quiet_hours(ist(7, 30), switches)
    assert switches.view() | {} == {"immediate": True, "digest": True, "quiet": True, "digest_at": "18:00",
                                    "quiet_start": "22:00", "quiet_end": "07:00"}
    assert not n.digest_due(ist(17, 59), switches) and n.digest_due(ist(18, 1), switches)


def test_immediate_items_wait_for_8am_unless_an_interview_is_within_12_hours():
    store, sender = MemoryStore(), PushSender()
    night = ist(2)
    run(n.add(store, {"source_id": "gmail:a", "kind": "offer", "title": "x", "company": "Arcworks"}, night))
    run(n.add(store, {"source_id": "gmail:b", "kind": "interview", "title": "y", "company": "Nimbus",
                      "interview_at": night + timedelta(hours=7)}, night))
    run(n.add(store, {"source_id": "gmail:c", "kind": "interview", "title": "z", "company": "Kite",
                      "interview_at": night + timedelta(days=2)}, night))
    result = run(n.dispatch(store, night, sender))
    assert result["pushed"] == 1 and result["held"] == 2 and "Nimbus" in sender.sent[0]["title"]
    result = run(n.dispatch(store, ist(8, 1), sender))
    assert result["pushed"] == 2 and {r["push_state"] for r in store.rows} == {"sent"}
    # Quiet hours switched off: pushed at once.
    store2, sender2 = MemoryStore(), PushSender()
    run(store2.save_switches(quiet=False))
    run(n.add(store2, {"source_id": "gmail:d", "kind": "offer", "title": "x", "company": "Arcworks"}, night))
    assert run(n.dispatch(store2, night, sender2))["pushed"] == 1


def test_digest_is_one_push_a_day_at_nine_with_something_in_it():
    store, sender = MemoryStore(), PushSender()
    run(n.add(store, {"source_id": "gmail:r", "kind": "rejection", "title": "x", "company": "Tessel"}, ist(1)))
    run(n.add(store, {"source_id": "gmail:l", "kind": "reply", "title": "y", "company": "Quillworks"}, ist(1)))
    assert run(n.dispatch(store, ist(8, 30), sender)) == {"pushed": 0, "held": 0, "digest": 0} and not sender.sent
    assert run(n.dispatch(store, ist(9, 1), sender))["digest"] == 2
    assert len(sender.sent) == 1 and sender.sent[0]["title"] == "VALR · 2 updates"
    assert {r["push_state"] for r in store.rows} == {"digested"}
    run(n.add(store, {"source_id": "gmail:x", "kind": "rejection", "title": "z", "company": "Onyx"}, ist(10)))
    run(n.dispatch(store, ist(11), sender))
    assert len(sender.sent) == 1  # Tomorrow's digest, not a second one today.


def test_with_no_phone_subscribed_items_stay_in_the_bell_only():
    store, sender = MemoryStore(), PushSender(devices=0)
    run(n.add(store, {"source_id": "gmail:a", "kind": "offer", "title": "x"}, NOW))
    assert run(n.dispatch(store, NOW, sender))["pushed"] == 0 and sender.sent == []
    assert store.rows[0]["push_state"] == "skipped" and not store.rows[0]["read_at"]
    assert run(store.unread_count(NOW - n.KEEP_FOR)) == 1


# No inferred items: only new events notify.

def lead(stage, waiting="nobody", since=None, lead_id="l1", company="Glintworks"):
    return Lead(lead_id, company, company.lower(), stage=stage, waiting_on=waiting, waiting_since=since)


def test_lead_rebuilds_never_create_stage_waiting_quiet_or_new_lead_items():
    store = MemoryStore()
    for leads in ([lead("screening")], [lead("interviewing", "you", NOW - timedelta(hours=1))], [lead("offer")],
                  [lead("interviewing", "them", NOW - timedelta(days=20))]):
        assert run(n.after_rebuild(store, leads, NOW)) == 0
    assert store.rows == []
    assert not hasattr(n, "lead_events") and not set(n.REMOVED) & set(n.KINDS)
    run(n.from_email(store, email(), decision("recruiter_outreach"), touch(), True, NOW))
    assert [(r["kind"], r["title"]) for r in store.rows] == [("reply", "Acme emailed you")]


def stored_old_items(store, at):
    """Rows of the removed kinds, as an older version stored them."""
    for i, kind in enumerate(n.REMOVED):
        run(store.insert({"source_id": f"{kind}:l{i}", "kind": kind, "importance": "digest", "title": "old",
                          "company": "Glintworks", "created_at": at, "push_state": "pending"}))


def test_stored_inferred_items_are_hidden_from_count_list_push_and_digest():
    store = MemoryStore()
    stored_old_items(store, ist(1))
    run(n.add(store, {"source_id": "gmail:r", "kind": "rejection", "title": "x", "company": "Tessel"}, ist(1)))
    run(store.setup())
    assert all(r["read_at"] for r in store.rows if r["kind"] in n.REMOVED)
    assert run(store.unread_count(ist(0))) == 1
    assert [r["kind"] for r in run(store.listing(False, ist(0)))] == ["rejection"]
    sender = PushSender()
    result = run(n.dispatch(store, ist(9, 1), sender))
    assert result["digest"] == 1 and sender.sent[0]["title"] == "VALR · 1 update"
    assert len(sender.sent) == 1 and sender.sent[0]["body"] == "Tessel said no"


class PushSender:
    def __init__(self, devices=1):
        self.sent, self.devices = [], devices

    async def subscribers(self):
        return self.devices

    async def send(self, message, now):
        self.sent.append(message)
        return SimpleNamespace(delivered=True)


# Calendar changes.

def cal_event(key="ev1", title="Acme: technical interview", start="2026-10-19T12:30:00+00:00", company="Acme",
              status="confirmed"):
    from app.interview_types import Person, SlotEvent
    return SlotEvent(key=key, title=title, start_utc=start, end_utc=start.replace("12:30", "13:30"), status=status,
                     organizer_email="pat@acme.example", organizer_name="Pat Lee",
                     attendees=[Person("pat@acme.example", "Pat Lee", "accepted", organizer=True),
                                Person("owner@example.test", "", "accepted", is_self=True)],
                     company=company, company_domain="acme.example")


def test_calendar_added_moved_and_cancelled_each_notify_once():
    store = MemoryStore()
    event = cal_event()
    assert run(n.from_calendar(store, event, "added", NOW)) is not None
    assert run(n.from_calendar(store, event, "added", NOW)) is None  # Once.
    added = store.by_source("calendar:ev1:added")
    assert added["title"] == "Interview with Acme added: Mon 19 Oct, 6:00 PM"  # 12:30 UTC = 18:00 IST.
    assert added["kind"] == "calendar" and added["importance"] == "digest"  # More than 48 hours away.
    from app.interview_types import slot_workflow_id
    assert added["link"] == f"/valr/interviews/{slot_workflow_id('ev1')}"
    moved = cal_event(start="2026-10-18T11:30:00+00:00")
    assert run(n.from_calendar(store, moved, "moved", NOW, event.start_utc)) is not None
    assert run(n.from_calendar(store, moved, "moved", NOW, event.start_utc)) is None
    row = store.rows[-1]
    assert row["title"] == "Interview with Acme moved to Sun 18 Oct, 5:00 PM" and row["body"] == "Was Mon 19 Oct, 6:00 PM."
    assert row["importance"] == "immediate"  # Within 48 hours.
    assert run(n.from_calendar(store, moved, "cancelled", NOW, moved.start_utc)) is not None
    assert run(n.from_calendar(store, moved, "cancelled", NOW, moved.start_utc)) is None
    assert store.rows[-1]["title"] == "Interview with Acme cancelled: Sun 18 Oct, 5:00 PM"
    assert len(store.rows) == 3
    assert n.push_message(store.rows[-1])["body"].startswith("Interview with Acme cancelled: Sun 18 Oct, 5:00 PM")
    assert n.push_message(store.rows[-1])["title"] == "Calendar · Acme"


def test_calendar_changes_from_the_sync_first_sync_silent_and_non_interviews_ignored():
    from app import interview_worker as w
    from app.interview_types import SlotDiff
    calls = []

    async def fake(store, event, change, now, before_utc=""):
        calls.append((event.key, change, before_utc))

    class Connection:
        async def execute(self, *args):
            return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

    import unittest.mock as mock
    owners = {"owner@example.test"}
    interview, dinner = cal_event(), cal_event("ev2", "Dinner with friends", company="")
    dinner.organizer_email, dinner.attendees = "", []
    with mock.patch.object(n, "from_calendar", fake), mock.patch.object(n.Store, "setup", lambda self: asyncio.sleep(0)):
        # First sync: nothing stored before -> seeds silently.
        run(w.notify_calendar(Connection(), [(interview, SlotDiff(["new"]))], {}, True, NOW, owners))
        assert calls == []
        before = {"seen": cal_event("seen")}
        run(w.notify_calendar(Connection(), [(interview, SlotDiff(["new"])), (dinner, SlotDiff(["new"]))],
                              before, False, NOW, owners))
        assert calls == [("ev1", "added", "")]  # The dinner is not an interview.
        calls.clear()
        old = cal_event()
        moved = cal_event(start="2026-10-20T12:30:00+00:00")
        gone = cal_event("ev3", status="cancelled")
        retitled = cal_event("ev4")
        before = {"ev1": old, "ev3": cal_event("ev3"), "ev4": cal_event("ev4", title="Acme: coding round")}
        run(w.notify_calendar(Connection(), [(moved, SlotDiff(["time"])), (gone, SlotDiff(["cancelled"])),
                                             (retitled, SlotDiff(["round"]))], before, False, NOW, owners))
        assert calls == [("ev1", "moved", old.start_utc), ("ev3", "cancelled", gone.start_utc)]
        calls.clear()
        # A full listing: an event only entering the 14-day window is not "added".
        far = cal_event("ev5", start=(NOW + timedelta(days=13, hours=12)).isoformat())
        run(w.notify_calendar(Connection(), [(far, SlotDiff(["new"]))], before, True, NOW, owners))
        assert calls == []


# The API: owner only.

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app import research, valr_dashboard, valr_view
    from app.main import app
    from tests.test_notetakers import GOOD, OWNER, Pool, Verifier
    from tests.valr_sample import sample_view

    async def collect(pool, now=None):
        return sample_view()

    memory = MemoryStore()
    run(n.add(memory, {"source_id": "gmail:a", "kind": "offer", "title": "Arcworks sent an offer", "company": "Arcworks",
                       "gmail_url": "https://mail.google.com/mail/#all/t9"}, datetime.now(timezone.utc)))
    run(n.add(memory, {"source_id": "gmail:b", "kind": "reply", "title": "Kite replied", "company": "Kite"},
              datetime.now(timezone.utc)))

    class Store(MemoryStore):
        def __init__(self, connection):
            self.__dict__ = memory.__dict__

        async def listing(self, unread_only, since, limit=100):
            rows = [r for r in self.rows if r["created_at"] >= since and not (unread_only and r["read_at"])]
            return sorted(rows, key=lambda r: r["created_at"], reverse=True)

        async def unread_count(self, since):
            return sum(1 for r in self.rows if not r["read_at"])

        async def mark_read(self, ids, now):
            hit = [r for r in self.rows if not r["read_at"] and (ids is None or r["id"] in ids)]
            for r in hit:
                r["read_at"] = now
            return len(hit)

    monkeypatch.setattr(n, "Store", Store)
    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")
    test_client = TestClient(app)
    test_client.good = GOOD
    return test_client


def csrf_headers(client):
    page = client.get("/valr", headers=client.good).text
    token = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    return {**client.good, "Content-Type": "application/json", "X-CSRF-Token": token}


def test_the_api_is_owner_only_and_marks_read(client):
    assert client.get("/valr/api/notifications").status_code == 403
    assert client.get("/valr/api/notifications", headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    assert client.post("/valr/api/notifications/read", content=b'{"all": true}').status_code == 403
    data = client.get("/valr/api/notifications?unread=1", headers=client.good).json()
    assert data["unread"] == 2 and sorted(i["kind"] for i in data["items"]) == ["offer", "reply"]
    offer = next(i for i in data["items"] if i["kind"] == "offer")
    assert offer["gmail_url"].startswith("https://") and offer["day"] == "Today"
    no_csrf = client.post("/valr/api/notifications/read", content=json.dumps({"ids": [offer["id"]]}),
                          headers={**client.good, "Content-Type": "application/json"})
    assert no_csrf.status_code == 403
    done = client.post("/valr/api/notifications/read", content=json.dumps({"ids": [offer["id"]]}),
                       headers=csrf_headers(client)).json()
    assert done["marked"] == 1 and done["unread"] == 1
    assert client.post("/valr/api/notifications/read", content=b"{}", headers=csrf_headers(client)).status_code == 400
    assert client.post("/valr/api/notifications/read", content=b'{"all": true}',
                       headers=csrf_headers(client)).json()["unread"] == 0
    switched = client.post("/valr/api/notifications/settings", content=b'{"digest": false}', headers=csrf_headers(client))
    assert switched.json()["switches"]["digest"] is False


def test_the_bell_replaces_the_attention_pill(client):
    page = client.get("/valr", headers=client.good).text
    assert "data-notif" in page and "valr-notify.js" in page and 'class="att-badge' not in page


def test_a_processed_call_recording_is_one_immediate_item_linking_to_its_past_interview():
    store = MemoryStore()
    got = run(n.from_recording(store, "phone", "ab12", "Northwind Labs", "Sam", NOW))
    row = store.rows[0]
    assert got == row["id"] and row["kind"] == "recording" and row["importance"] == "immediate"
    assert row["title"] == "Call with Northwind Labs is ready"
    assert row["link"] == "/valr/interviews/past/phone-ab12"
    assert run(n.from_recording(store, "phone", "ab12", "Northwind Labs", "Sam", NOW)) is None  # Once per recording.
    run(n.from_recording(store, "phone", "cd34", "", "Sam", NOW))
    assert store.rows[-1]["title"] == "Call with Sam is ready"


# Unread mail: every kept inbound job email still unread in Gmail is in the bell.

def kept_touch(message_id, at, thread=None, category="interviewer_followup", direction="in", company="Acme",
               action_requested=None, action="", lead_id=""):
    from app.leads import Touch
    return Touch(id=f"gmail:{message_id}", kind="email", at=at, company=company, title="Next steps",
                 summary="Recruiter follow-up.", direction=direction, thread=thread or f"t-{message_id}",
                 url=f"https://mail.google.com/mail/#all/t-{message_id}", category=category,
                 action_requested=action_requested, action=action, lead_id=lead_id)


def test_unread_kept_emails_appear_and_read_ones_do_not():
    store = MemoryStore()
    kept = [kept_touch("u1", NOW - timedelta(days=3), lead_id="lead-acme"),
            kept_touch("u2", NOW - timedelta(days=12), category="application_update", company="Tessel"),
            kept_touch("r1", NOW - timedelta(days=2)),                       # Read in Gmail.
            kept_touch("o1", NOW - timedelta(days=1), direction="out"),     # His own mail.
            kept_touch("n1", NOW - timedelta(days=1), category="newsletter")]
    out = run(n.sync_unread(store, kept, {"u1", "u2", "o1", "n1", "x9"}, True, {}, NOW, {"gmail:u2"}))
    assert out == {"added": 2, "read": 0, "unread": 0, "caught_up": 0}
    rows = {r["source_id"]: r for r in store.rows}
    assert set(rows) == {"gmail:u1", "gmail:u2"}
    assert rows["gmail:u1"]["created_at"] == NOW - timedelta(days=3)  # Dated when it arrived: grouped by day.
    assert rows["gmail:u1"]["lead_id"] == "lead-acme" and rows["gmail:u1"]["link"] == "/valr/leads?lead=lead-acme"
    assert rows["gmail:u2"]["kind"] == "update" and rows["gmail:u2"]["title"] == "Tessel sent an update"
    assert run(store.unread_count(NOW - n.KEEP_FOR)) == 2  # The bell's count.
    days = [n.view(r, NOW)["day"] for r in run(store.listing(True, NOW - n.KEEP_FOR))]
    assert days == ["Earlier", "Earlier"]
    assert run(n.sync_unread(store, kept, {"u1", "u2"}, True, {}, NOW, set()))["added"] == 0  # Once each.


def test_the_unread_backfill_is_silent_no_push_no_digest():
    store, sender = MemoryStore(), PushSender()
    kept = [kept_touch("u1", NOW - timedelta(hours=2), category="offer"), kept_touch("u2", NOW - timedelta(days=4))]
    run(n.sync_unread(store, kept, {"u1", "u2"}, True, {}, NOW, set()))
    assert len(store.rows) == 2 and all(not r["read_at"] for r in store.rows)
    for at in (NOW, ist(9, 5)):  # A normal pass and the digest hour.
        result = run(n.dispatch(store, at, sender))
        assert result["digest"] == result["pushed"] == 0
    assert sender.sent == []


def test_reading_in_gmail_reads_the_item_and_marking_unread_brings_it_back():
    store = MemoryStore()
    kept = [kept_touch("u1", NOW - timedelta(days=1)), kept_touch("u2", NOW - timedelta(days=2)),
            kept_touch("u3", NOW - timedelta(days=3))]
    run(n.sync_unread(store, kept, {"u1", "u2", "u3"}, True, {}, NOW, set()))
    row = lambda i: store.by_source(f"gmail:{i}")  # noqa: E731
    # The unread list no longer has u1: read in Gmail.
    assert run(n.sync_unread(store, kept, {"u2", "u3"}, True, {}, NOW, set()))["read"] == 1
    assert row("u1")["read_at"] and not row("u2")["read_at"]
    # A cut-short list reads nothing it does not see; the history's labelRemoved still reads.
    assert run(n.sync_unread(store, kept, {"u3"}, False, {"u2": False}, NOW, set()))["read"] == 1
    assert row("u2")["read_at"] and not row("u3")["read_at"]
    # No list at all (rate limited): the history alone.
    assert run(n.sync_unread(store, kept, None, False, {"u3": False}, NOW, set()))["read"] == 1
    # Marked unread again in Gmail (labelAdded UNREAD): the dot comes back.
    assert run(n.sync_unread(store, kept, {"u1"}, True, {"u1": True}, NOW, set()))["unread"] == 1
    assert not row("u1")["read_at"]
    # Read in VALR while still unread in Gmail: stays read.
    run(store.mark_read([row("u1")["id"]], NOW))
    run(n.sync_unread(store, kept, {"u1"}, True, {}, NOW, set()))
    assert row("u1")["read_at"]


def test_new_mail_still_pushes_and_is_not_doubled_by_the_unread_pass():
    store, sender = MemoryStore(), PushSender()
    mail = email("m1", "t-m1", at=NOW - timedelta(minutes=5))
    run(n.from_email(store, mail, decision("scheduling"), touch(), False, NOW))
    kept = [kept_touch("m1", mail.at, thread="t-m1", category="scheduling")]
    assert run(n.sync_unread(store, kept, {"m1"}, True, {}, NOW, set()))["added"] == 0
    result = run(n.dispatch(store, NOW, sender))
    assert result["pushed"] == 1 and len(store.rows) == 1


def test_new_mail_already_read_in_gmail_is_not_pushed():
    store, sender = MemoryStore(), PushSender()
    mail = email("m1", "t-m1", at=NOW - timedelta(minutes=5))
    run(n.from_email(store, mail, decision("scheduling"), touch(), False, NOW))
    run(n.sync_unread(store, [kept_touch("m1", mail.at, thread="t-m1")], set(), True, {}, NOW, set()))
    assert run(n.dispatch(store, NOW, sender))["pushed"] == 0 and sender.sent == []


def test_max_age_push_48_hours_bell_30_days():
    store, sender = MemoryStore(), PushSender()
    three_days = email("m3", "t-m3", at=NOW - timedelta(days=3))
    assert run(n.from_email(store, three_days, decision("scheduling"), touch(), False, NOW)) is None  # No push.
    kept = [kept_touch("m3", three_days.at, category="scheduling"), kept_touch("m29", NOW - timedelta(days=29)),
            kept_touch("m31", NOW - timedelta(days=31))]
    run(n.sync_unread(store, kept, {"m3", "m29", "m31"}, True, {}, NOW, set()))
    assert {r["source_id"] for r in store.rows} == {"gmail:m3", "gmail:m29"}  # In the bell, silently.
    assert run(n.dispatch(store, NOW, sender))["pushed"] == 0


# Latency: a missed new email is caught up loudly; new items are pushed right away.

def test_a_missed_new_email_is_caught_up_and_pushed_old_or_covered_ones_stay_silent():
    store, sender = MemoryStore(), PushSender()
    live = NOW - timedelta(days=1)  # The backfill began then: mail after it is news.
    run(store.insert({"source_id": "gmail:c0", "kind": "reply", "importance": "digest", "title": "x",
                      "thread_id": "t-covered", "created_at": NOW - timedelta(minutes=30),
                      "push_state": "sent"}))
    kept = [kept_touch("m1", NOW - timedelta(minutes=4), category="scheduling"),       # Missed: pushed.
            kept_touch("old", live - timedelta(hours=1), category="scheduling"),       # Before the backfill.
            kept_touch("aged", NOW - timedelta(hours=50), category="scheduling"),      # Over 48 h: not news.
            kept_touch("c1", NOW - timedelta(minutes=25), thread="t-covered"),         # Its thread is in the bell.
            dataclasses.replace(kept_touch("ack", NOW - timedelta(minutes=3), category="application_update"),
                                title="Thank you for applying")]  # An automatic receipt: never pushed.
    out = run(n.sync_unread(store, kept, {"m1", "old", "aged", "c1", "ack"}, True, {}, NOW, set(), live))
    assert out["caught_up"] == 1 and out["added"] == 4
    loud = {r["source_id"] for r in store.rows if r["push_state"] == "pending"}
    assert loud == {"gmail:m1"}
    assert store.by_source("gmail:m1")["kind"] == "interview"
    assert run(n.dispatch(store, NOW, sender=sender))["pushed"] == 1 and len(sender.sent) == 1
    # Without a live mark (the first backfill not begun), everything stays silent.
    quiet = MemoryStore()
    assert run(n.sync_unread(quiet, kept[:1], {"m1"}, True, {}, NOW, set()))["caught_up"] == 0


def test_dispatch_now_retries_while_the_loop_holds_the_lock(monkeypatch):
    results = [{"locked": 1}, {"locked": 1}, {"pushed": 1}]
    calls = []

    async def fake(connection, now=None):
        calls.append(connection)
        return results.pop(0)
    monkeypatch.setattr(n, "run_dispatch", fake)
    assert run(n.dispatch_now("conn", wait=0)) == {"pushed": 1} and len(calls) == 3
    results[:] = [{"locked": 1}] * 5
    assert run(n.dispatch_now("conn", tries=2, wait=0)) is None  # The loop sends it on its tick.

    async def broken(connection, now=None):
        raise RuntimeError("db gone")
    monkeypatch.setattr(n, "run_dispatch", broken)
    assert run(n.dispatch_now("conn", wait=0)) is None  # Never raises.


def test_a_calendar_change_is_pushed_at_once():
    from app import interview_worker as w
    from app.interview_types import SlotDiff
    import unittest.mock as mock
    pushed = []

    async def created(store, event, change, now, before_utc=""):
        return 7

    async def dispatch_now(connection, tries=5, wait=0.5):
        pushed.append(connection)

    owners = {"owner@example.test"}
    with mock.patch.object(n, "from_calendar", created), mock.patch.object(n, "dispatch_now", dispatch_now), \
            mock.patch.object(n.Store, "setup", lambda self: asyncio.sleep(0)):
        run(w.notify_calendar("conn", [(cal_event(), SlotDiff(["new"]))], {"seen": cal_event("seen")}, False,
                              NOW, owners))
        assert pushed == ["conn"]
        run(w.notify_calendar("conn", [(cal_event(), SlotDiff(["new"]))], {}, True, NOW, owners))
        assert pushed == ["conn"]  # The seeding sync creates nothing, so nothing to push.


# The SLA: a new email is in the bell before the model classifies it.

def test_an_incoming_email_shows_at_once_then_becomes_the_real_item_or_goes():
    store, sender = MemoryStore(), PushSender()
    mail = email("m1", "t-m1", subject="Interview availability", at=NOW - timedelta(seconds=4))
    assert run(n.from_incoming(store, mail, NOW)) is not None
    row = store.by_source("gmail:m1")
    assert row["kind"] == n.INCOMING and row["title"] == "Riya Sen: Interview availability"
    assert row["push_state"] == "hold" and row["arrived_at"] == mail.at
    assert run(n.dispatch(store, NOW, sender=sender))["pushed"] == 0  # Not pushed yet.
    assert run(n.from_incoming(store, mail, NOW)) is None  # Once per message.
    # The model kept it: the same bell entry becomes the real one, dated when it first showed.
    later = NOW + timedelta(seconds=3)
    run(n.from_email(store, mail, decision("scheduling"), touch(), False, later))
    row = store.by_source("gmail:m1")
    assert row["kind"] == "interview" and row["created_at"] == NOW and row["push_state"] == "pending"
    assert len(store.rows) == 1
    assert run(n.dispatch(store, later, sender=sender))["pushed"] == 1
    # Another one the model dropped: retracted.
    junk = email("m2", "t-m2", subject="Webinar", at=NOW)
    run(n.from_incoming(store, junk, NOW))
    assert run(n.retract_incoming(store, "m2")) and store.by_source("gmail:m2") is None
    assert not run(n.retract_incoming(store, "m1"))  # A settled item is never retracted.
    # Kept but not news (an automatic receipt): retracted too.
    receipt = email("m3", "t-m3", subject="Thank you for applying", at=NOW)
    run(n.from_incoming(store, receipt, NOW))
    assert run(n.from_email(store, receipt, decision("application_update"), touch(), False, later)) is None
    assert store.by_source("gmail:m3") is None
    # Old mail never shows as incoming.
    assert run(n.from_incoming(store, email("m4", at=NOW - timedelta(days=3)), NOW)) is None


def test_two_incoming_emails_in_one_thread_settle_into_one_item():
    store = MemoryStore()
    first, second = email("a1", "t-x", at=NOW - timedelta(seconds=5)), email("a2", "t-x", at=NOW - timedelta(seconds=4))
    run(n.from_incoming(store, first, NOW))
    run(n.from_incoming(store, second, NOW))
    run(n.from_email(store, first, decision("scheduling"), touch(), False, NOW))
    run(n.from_email(store, second, decision("scheduling"), touch(), False, NOW))
    assert [r["source_id"] for r in store.rows] == ["gmail:a1"]  # The 10-minute thread dedupe still holds.


def test_the_latency_report_against_the_sla():
    store = MemoryStore()
    for i, seconds in enumerate([2, 3, 4, 4, 5, 5, 6, 7, 8, 12]):
        run(store.insert({"source_id": f"gmail:l{i}", "kind": "reply", "importance": "digest", "title": "x",
                          "created_at": NOW, "arrived_at": NOW - timedelta(seconds=seconds),
                          "push_sent_at": NOW + timedelta(seconds=1), "push_state": "sent"}))
    run(store.insert({"source_id": "gmail:silent", "kind": "reply", "importance": "digest", "title": "x",
                      "created_at": NOW - timedelta(days=3), "push_state": "skipped"}))
    report = run(n.latency_report(store, NOW))
    assert report["count"] == 10 and report["p50_s"] == 5.0 and report["p95_s"] == 12.0 and report["max_s"] == 12.0
    assert report["push_p50_s"] == 6.0 and report["met"] is False and report["sla_p95_s"] == 10.0
    assert run(n.latency_report(MemoryStore(), NOW))["met"] is None  # Nothing measured yet.
    assert n.percentile([1.0], 0.95) == 1.0 and n.percentile([], 0.5) is None


def test_the_latency_endpoint_is_owner_only(client):
    assert client.get("/valr/api/notifications/latency").status_code == 403
    data = client.get("/valr/api/notifications/latency", headers=client.good).json()
    assert data["ok"] and data["sla_p95_s"] == 10.0 and "p95_s" in data
