"""The calendar: ICS parsing, snapshots and diffs, the HR guard, blocks, the Google sync."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import calendar_ics, calendar_sync, interview_worker
from app.interview_types import (Person, SlotEvent, attendee_role, block_id_for, diff,
                                 group_blocks, people)
from app.interview_workflow import SyncInput

OWNER = "owner@gmail.com"
ICS = """BEGIN:VCALENDAR\r
X-WR-CALNAME:owner@gmail.com\r
X-WR-TIMEZONE:Asia/Kolkata\r
BEGIN:VEVENT\r
UID:aaa@google.com\r
DTSTART;TZID=Asia/Kolkata:20261020T160000\r
DTEND;TZID=Asia/Kolkata:20261020T170000\r
SUMMARY:Interview with Acme\\, round 2\r
ORGANIZER;CN=Asha Rao:mailto:asha@acme.com\r
ATTENDEE;CN=Dev One;PARTSTAT=ACCEPTED:mailto:dev@acme.com\r
ATTENDEE;PARTSTAT=ACCEPTED:mailto:owner@gmail.com\r
DESCRIPTION:Bring a laptop.\\nSystem design.\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:bbb@google.com\r
DTSTART:20261020T110000Z\r
DTEND:20261020T113000Z\r
SUMMARY:Chat <> Beta | Staff Engineer\r
ATTENDEE;PARTSTAT=DECLINED:mailto:owner@gmail.com\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:ccc@google.com\r
DTSTART;VALUE=DATE:20261020\r
DTEND;VALUE=DATE:20261021\r
SUMMARY:Holiday\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:ddd@google.com\r
DTSTART:20261021T090000Z\r
DTEND:20261021T093000Z\r
RRULE:FREQ=DAILY;COUNT=3\r
EXDATE:20261022T090000Z\r
SUMMARY:Standup\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_ics_parsing_zones_all_day_declined_and_recurrence():
    events = calendar_ics.parse(ICS)
    acme = next(e for e in events if e.uid == "aaa@google.com")
    assert acme.start == datetime(2026, 10, 20, 10, 30, tzinfo=timezone.utc)  # 4 PM IST.
    assert acme.title == "Interview with Acme, round 2" and "System design" in acme.description
    assert acme.organizer == "asha@acme.com" and not acme.declined
    assert next(e for e in events if e.uid == "bbb@google.com").declined  # By the owner (calname).
    assert next(e for e in events if e.uid == "ccc@google.com").all_day
    standups = [e for e in events if e.title == "Standup"]
    assert [e.start.day for e in standups] == [21, 23]  # COUNT 3 minus the EXDATE.
    assert calendar_ics.company_name(acme) == "Acme"


def test_ics_keys_match_google_ids():
    slots = {s.title: s for s in calendar_sync.from_ics_text(ICS, {OWNER})}
    assert slots["Interview with Acme, round 2"].key == "aaa"
    assert {s.key for s in calendar_sync.from_ics_text(ICS, {OWNER}) if s.title == "Standup"} == \
        {"ddd_20261021T090000Z", "ddd_20261023T090000Z"}


def slot(key="e1", start="2026-10-20T10:00:00+00:00", minutes=60, title="Interview with Acme",
         attendees=None, **extra) -> SlotEvent:
    begin = datetime.fromisoformat(start)
    return SlotEvent(key, title, start, (begin + timedelta(minutes=minutes)).isoformat(),
                     attendees=attendees if attendees is not None else [Person("dev@acme.com", "Dev One")],
                     company="Acme", company_domain="acme.com", **extra)


def test_blocks_join_gaps_under_30_minutes_and_skip_all_day_and_declined():
    events = [slot("a", "2026-10-20T10:00:00+00:00", 60),
              slot("b", "2026-10-20T11:29:00+00:00", 30),   # 29 min gap: same block.
              slot("c", "2026-10-20T12:29:00+00:00", 30),   # 30 min gap: new block.
              slot("d", "2026-10-20T10:30:00+00:00", 30, declined=True),
              slot("e", "2026-10-20T00:00:00+00:00", 1440, all_day=True),
              slot("f", "2026-10-20T13:00:00+00:00", 30, status="cancelled")]
    blocks = group_blocks(events)
    assert [b.slot_keys for b in blocks] == [["a", "b"], ["c"]]
    assert blocks[0].block_id == block_id_for("2026-10-20T10:00:00+00:00") == "briefing-20261020T1000Z"


def test_diff_kinds():
    base = slot()
    assert diff(None, base).kinds == ["new"]
    assert diff(base, slot()).kinds == ["none"]
    assert diff(base, slot(start="2026-10-20T11:00:00+00:00")).kinds == ["time"]
    added = diff(base, slot(attendees=[Person("dev@acme.com", "Dev One"), Person("cto@acme.com", "Cee")]))
    assert added.kinds == ["interviewers"] and [p.email for p in added.added] == ["cto@acme.com"]
    removed = diff(base, slot(attendees=[]))
    assert removed.kinds == ["interviewers"] and removed.removed == ["dev@acme.com"]
    assert diff(base, slot(title="Acme: system design round")).kinds == ["round"]
    other = slot()
    other.company, other.company_domain = "Beta", "beta.io"
    assert diff(base, other).kinds == ["company"]
    assert diff(base, slot(status="cancelled")).kinds == ["cancelled"]
    assert diff(base, slot(declined=True)).kinds == ["cancelled"]
    # A recruiter joining changes nothing material.
    recruiter = slot(attendees=[Person("dev@acme.com", "Dev One"),
                                Person("priya@acme.com", "Priya (Talent Partner)")])
    assert diff(base, recruiter).kinds == ["none"]


@pytest.mark.parametrize("person, role", [
    (Person("owner@gmail.com", "Sam"), "owner"),
    (Person("x@acme.com", "Me", is_self=True), "owner"),
    (Person("room1@resource.calendar.google.com", "Room"), "resource"),
    (Person("team@group.calendar.google.com", ""), "resource"),
    (Person("no-reply@acme.com", ""), "tool"),
    (Person("interviews@acme.com", "Acme Interviews"), "tool"),
    (Person("bob@greenhouse.io", "Bob"), "tool"),
    (Person("sched@goodtime.io", "GoodTime"), "tool"),
    (Person("talent@acme.com", ""), "tool"),
    (Person("hr@acme.com", ""), "tool"),
    (Person("priya@acme.com", "Priya Shah - Talent Partner"), "recruiter"),
    (Person("sam@acme.com", "Sam (HR)"), "recruiter"),
    (Person("kim@acme.com", "Kim, People Ops"), "recruiter"),
    (Person("lee@acme.com", "Lee - Recruiting Coordinator"), "recruiter"),
    (Person("ana@acme.com", "Ana, TA"), "recruiter"),
    (Person("dev@acme.com", "Dev One"), "candidate"),
    (Person("ta@acme.com", "Ta Nguyen"), "tool"),  # The address, not the name.
    (Person("tnguyen@acme.com", "Ta Nguyen"), "candidate"),
    (Person("cto@acme.com", "Hiring Manager", organizer=True), "candidate"),
    (Person("someone@gmail.com", "A Founder"), "candidate"),
    (Person("eng@google.com", "Googler"), "candidate"),  # Google itself can be the employer.
])
def test_hr_guard(person, role):
    assert attendee_role(person, {OWNER}) == role


def test_organizer_hiring_manager_is_a_candidate_but_a_recruiter_organizer_is_not():
    manager = slot(attendees=[])
    manager.organizer_email, manager.organizer_name = "cto@acme.com", "Ravi Kumar"
    assert [p.email for p in people(manager, {OWNER})] == ["cto@acme.com"]
    recruiter = slot(attendees=[])
    recruiter.organizer_email, recruiter.organizer_name = "priya@acme.com", "Priya (Talent Partner)"
    assert people(recruiter, {OWNER}) == []


def test_google_event_conversion():
    item = {"id": "g1", "status": "confirmed", "summary": "Interview with Acme",
            "start": {"dateTime": "2026-10-20T16:00:00+05:30"}, "end": {"dateTime": "2026-10-20T17:00:00+05:30"},
            "organizer": {"email": "asha@acme.com", "displayName": "Asha"},
            "attendees": [{"email": "owner@gmail.com", "self": True, "responseStatus": "declined"},
                          {"email": "dev@acme.com", "displayName": "Dev"}]}
    event = calendar_sync.from_google(item)
    assert event.start_utc == "2026-10-20T10:30:00+00:00" and event.declined
    assert event.company == "Acme" and event.company_domain == "acme.com"
    events, cancelled = calendar_sync.from_google_items([item, {"id": "gone", "status": "cancelled"}])
    assert [e.key for e in events] == ["g1"] and cancelled == ["gone"]


def test_incremental_listing_pages_and_410():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params.get("syncToken") == "old":
            return httpx.Response(410, json={"error": {"code": 410}})
        if "pageToken" not in request.url.params:
            return httpx.Response(200, json={"items": [{"id": "1"}], "nextPageToken": "p2"})
        return httpx.Response(200, json={"items": [{"id": "2"}], "nextSyncToken": "new"})

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(calendar_sync.SyncTokenExpired):
                await calendar_sync.list_events(http, "t", "old")
            return await calendar_sync.list_events(http, "t", None)
    items, token = asyncio.run(main())
    assert [i["id"] for i in items] == ["1", "2"] and token == "new"
    assert calls[-1]["singleEvents"] == "true" and "syncToken" not in calls[-1]


def test_channel_renewal_window():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert calendar_sync.needs_renewal({}, now)
    assert calendar_sync.needs_renewal({"channel_id": "c", "channel_expires": now + timedelta(hours=5)}, now)
    assert not calendar_sync.needs_renewal({"channel_id": "c", "channel_expires": now + timedelta(days=3)}, now)


def test_watch_and_renewal_stop_the_old_channel(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.content))
        if request.url.path.endswith("/watch"):
            return httpx.Response(200, json={"resourceId": "r2", "expiration": "1792700000000"})
        return httpx.Response(204)

    saved = {}

    async def save_state(connection, **values):
        saved.update(values)
    monkeypatch.setattr(calendar_sync, "save_state", save_state)
    monkeypatch.delenv("VALR_GCAL_PUSH", raising=False)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            worker = interview_worker.InterviewActivities(client=None, http=http)
            return await worker.renew_channel(None, "token", {"channel_id": "c1", "resource_id": "r1",
                                                              "channel_expires": datetime.now(timezone.utc)},
                                              datetime.now(timezone.utc))
    result = asyncio.run(main())
    assert result.startswith("renewed") and saved["resource_id"] == "r2"
    assert saved["channel_token"] and saved["channel_id"].startswith("valr-")
    assert [path for path, _ in requests] == ["/calendar/v3/calendars/primary/events/watch",
                                              "/calendar/v3/channels/stop"]
    assert b"https://valr.haciensus.com/hooks/gcal" in requests[0][1]


def test_sync_falls_back_to_a_full_resync_on_410(monkeypatch):
    seen = []

    async def gmail_access(connection, http):
        return "token", "connected"

    async def list_events(http, token, sync_token):
        seen.append(sync_token)
        if sync_token:
            raise calendar_sync.SyncTokenExpired()
        return [], "fresh"

    full_flags = []

    async def changes(connection, incoming, cancelled, full, now, owners):
        full_flags.append(full)
        return []

    stored = {}

    async def load_state(connection):
        return {"sync_token": "stale", "channel_id": "c", "channel_expires": now + timedelta(days=3)}

    async def save_state(connection, **values):
        stored.update(values)

    async def reconcile(self, connection, now, summary):
        return None

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(interview_worker, "gmail_access", gmail_access)
    monkeypatch.setattr(calendar_sync, "list_events", list_events)
    monkeypatch.setattr(calendar_sync, "changes", changes)
    monkeypatch.setattr(calendar_sync, "load_state", load_state)
    monkeypatch.setattr(calendar_sync, "save_state", save_state)
    monkeypatch.setattr(interview_worker.InterviewActivities, "reconcile", reconcile)
    worker = interview_worker.InterviewActivities(client=None, http=None)
    summary = asyncio.run(worker.sync(None, SyncInput("push"), now))
    assert seen == ["stale", None] and full_flags == [True]
    assert stored["sync_token"] == "fresh" and summary.source == "google"


class FakeConnection:
    """Just enough of psycopg for calendar_sync.changes."""

    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    async def execute(self, query, params=()):
        rows = self.rows
        if query.startswith("SELECT key, snapshot FROM calendar_events"):
            result = [(key, value) for key, value in rows.items()]
        else:
            result = []
            if query.startswith("INSERT INTO calendar_events"):
                import json
                rows[params[0]] = json.loads(params[1])

        class Cursor:
            async def fetchall(self):
                return result
        return Cursor()


def test_changes_stores_snapshots_and_cancels_what_a_full_listing_lost():
    from dataclasses import asdict
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    old_a, old_b = slot("a"), slot("b", "2026-10-21T10:00:00+00:00")
    connection = FakeConnection({"a": asdict(old_a), "b": asdict(old_b)})
    moved = slot("a", "2026-10-20T12:00:00+00:00")
    out = asyncio.run(calendar_sync.changes(connection, [moved, slot("n", "2026-10-22T10:00:00+00:00")],
                                            [], True, now, {OWNER}))
    kinds = {event.key: change.kinds for event, change in out}
    assert kinds == {"a": ["time"], "n": ["new"], "b": ["cancelled"]}
    assert connection.rows["b"]["status"] == "cancelled"
    again = asyncio.run(calendar_sync.changes(connection, [moved], [], False, now, {OWNER}))
    assert again == []  # Nothing material changed: a cache hit, no signals.
