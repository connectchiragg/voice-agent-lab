"""Notifications: which emails notify, the dedupe, quiet hours and the digest, Telegram text
without PII, lead events, and the owner-only API. All names, addresses and numbers are fictional."""
import asyncio
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
        return None

    async def recent_for_thread(self, thread_id, since):
        found = [r for r in self.rows if r.get("thread_id") == thread_id and r["created_at"] >= since]
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

    async def pending(self):
        return [dict(r) for r in self.rows if r["tg_state"] == "pending"]

    async def push_pending(self):
        return [dict(r) for r in self.rows if r.get("push_state") == "pending"]

    async def unread_count(self, since):
        return sum(1 for r in self.rows if not r["read_at"] and r["created_at"] >= since)

    async def lead_states(self):
        return dict(self.states)

    async def save_lead_states(self, states):
        self.states = dict(states)

    async def switches(self):
        return n.Switches.from_row(self.settings or None)

    async def save_switches(self, **values):
        self.settings.update(values)

    def by_source(self, source_id):
        return next((r for r in self.rows if r["source_id"] == source_id), None)


class Bot:
    def __init__(self):
        self.sent = []

    async def send(self, chat, text, keyboard=None):
        self.sent.append(text)
        return 1


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
    ("recruiter_outreach", False, "sourced", True, "Staff Backend role", "new_lead"),
    ("interviewer_followup", False, "", False, "Thanks for your time", "reply"),
    ("application_update", False, "applied", False, "Thank you for applying", None),
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
    # After 10 minutes the thread notifies again; a re-run of the same message never does.
    run(n.from_email(store, email("m4"), decision(), touch(), False, NOW + timedelta(minutes=16)))
    assert len(store.rows) == 2
    assert run(n.from_email(store, email("m4"), decision(), touch(), False, NOW + timedelta(minutes=40))) is None


# Telegram text: no pay figures, phone numbers or email addresses.

def test_telegram_text_carries_no_pii():
    note = {"kind": "reply_needed", "company": "Acme", "link": "/valr/leads?lead=x",
            "action": "Call riya@acme.example or +91 98765 43210 about the 45 LPA CTC, ₹30,00,000 base"}
    text = n.telegram_text(note)
    assert text.startswith("📩 Acme — recruiter asked you to ")
    assert text.endswith("Open: https://valr.example.test/valr/leads?lead=x")
    for leak in ("@", "98765", "43210", "45", "LPA", "30,00,000", "₹"):
        assert leak not in text, leak
    assert n.telegram_text({"kind": "reply_needed", "company": "Acme", "link": "/valr", "action": "share your availability"}) \
        == "📩 Acme — recruiter asked you to share your availability. Open: https://valr.example.test/valr"
    digest = n.digest_text([{"kind": "rejection", "company": "Tessel"}, {"kind": "new_lead", "company": "Quillworks"},
                            {"kind": "stage", "company": "Glintworks", "action": "interviewing"}])
    assert digest.startswith("🗞 3 updates: Tessel said no · Quillworks is new · Glintworks moved to Interviewing.")


# Quiet hours and the digest.

def ist(hour, minute=0):
    return datetime(2026, 10, 17, hour, minute, tzinfo=n.zone()).astimezone(timezone.utc)


def test_quiet_hours_window():
    assert n.in_quiet_hours(ist(23, 30)) and n.in_quiet_hours(ist(2)) and n.in_quiet_hours(ist(7, 59))
    assert not n.in_quiet_hours(ist(8)) and not n.in_quiet_hours(ist(22, 59))


def test_immediate_items_wait_for_8am_unless_an_interview_is_within_12_hours():
    store, bot = MemoryStore(), Bot()
    night = ist(2)
    run(n.add(store, {"source_id": "gmail:a", "kind": "offer", "title": "x", "company": "Arcworks"}, night))
    run(n.add(store, {"source_id": "gmail:b", "kind": "interview", "title": "y", "company": "Nimbus",
                      "interview_at": night + timedelta(hours=7)}, night))
    run(n.add(store, {"source_id": "gmail:c", "kind": "interview", "title": "z", "company": "Kite",
                      "interview_at": night + timedelta(days=2)}, night))
    result = run(n.dispatch(store, night, bot, chat=42))
    assert result["sent"] == 1 and result["held"] == 2 and "Nimbus" in bot.sent[0]
    result = run(n.dispatch(store, ist(8, 1), bot, chat=42))
    assert result["sent"] == 2 and {r["tg_state"] for r in store.rows} == {"sent"}
    # Quiet hours switched off: sent at once.
    store2, bot2 = MemoryStore(), Bot()
    run(store2.save_switches(quiet=False))
    run(n.add(store2, {"source_id": "gmail:d", "kind": "offer", "title": "x", "company": "Arcworks"}, night))
    assert run(n.dispatch(store2, night, bot2, chat=42))["sent"] == 1


def test_digest_once_a_day_at_nine_with_something_in_it():
    store, bot = MemoryStore(), Bot()
    run(n.add(store, {"source_id": "gmail:r", "kind": "rejection", "title": "x", "company": "Tessel"}, ist(1)))
    run(n.add(store, {"source_id": "gmail:l", "kind": "new_lead", "title": "y", "company": "Quillworks"}, ist(1)))
    assert run(n.dispatch(store, ist(8, 30), bot, chat=1)) == {"sent": 0, "held": 0, "digest": 0, "pushed": 0,
                                                               "push_held": 0} and not bot.sent
    assert run(n.dispatch(store, ist(9, 1), bot, chat=1))["digest"] == 2
    assert len(bot.sent) == 1 and bot.sent[0].startswith("🗞 2 updates")
    run(n.add(store, {"source_id": "gmail:x", "kind": "rejection", "title": "z", "company": "Onyx"}, ist(10)))
    run(n.dispatch(store, ist(11), bot, chat=1))
    assert len(bot.sent) == 1  # Tomorrow's digest, not a second one today.


def test_without_a_linked_chat_nothing_is_marked():
    store = MemoryStore()
    run(n.add(store, {"source_id": "gmail:a", "kind": "offer", "title": "x"}, NOW))
    run(n.dispatch(store, NOW, None, None))
    assert store.rows[0]["tg_state"] == "pending"


# Lead events.

def lead(stage, waiting="nobody", since=None, lead_id="l1", company="Glintworks"):
    return Lead(lead_id, company, company.lower(), stage=stage, waiting_on=waiting, waiting_since=since)


def test_first_rebuild_only_remembers_then_stage_and_waiting_notify():
    store = MemoryStore()
    assert run(n.after_rebuild(store, [lead("screening")], NOW)) == 0 and store.rows == []
    added = run(n.after_rebuild(store, [lead("interviewing", "you", NOW - timedelta(hours=1))], NOW))
    assert added == 2
    kinds = {r["kind"] for r in store.rows}
    assert kinds == {"stage", "waiting"} and store.by_source("stage:l1:interviewing")["title"] == \
        "Glintworks moved to Interviewing"
    assert run(n.after_rebuild(store, [lead("interviewing", "you", NOW - timedelta(hours=1))], NOW)) == 0
    assert run(n.after_rebuild(store, [lead("screening")], NOW)) == 0  # Backwards: nothing.
    assert run(n.after_rebuild(store, [lead("offer")], NOW)) == 1 and store.by_source("stage:l1:offer")["kind"] == "offer"


def test_gone_quiet_after_14_days():
    events = n.lead_events([lead("interviewing", "them", NOW - timedelta(days=15))], {"l1": ("interviewing", "them")}, NOW)
    assert [e.kind for e in events] == ["quiet"]


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
