"""The owner's calendar, kept in Postgres as per-event snapshots, and what changed.

Sources, in order of preference:

1. Google Calendar API (calendar.readonly, the same OAuth consent as Gmail; see
   app/google_oauth.py). A push channel (`events.watch`) on the primary calendar posts
   to /hooks/gcal; each notification starts a sync that lists only what changed since
   the stored `syncToken`. A 410 (token expired) means a full resync. The channel is
   renewed before it expires by the 6-hourly safety sweep.
2. The secret ICS URL (VALR_CALENDAR_ICS_URL) when OAuth is not connected or lapsed:
   a full fetch, diffed against the same snapshots.

Each event's previous snapshot (time, attendees, title, description, organizer) is
kept in `calendar_events`; `changes()` returns (snapshot, diff) for every event whose
material fields changed, which the worker turns into signals to the slot workflows.
Imports the standard library, httpx and the pure modules only.
"""
import json
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import calendar_ics
from app.interview_types import Person, SlotDiff, SlotEvent, diff

API = "https://www.googleapis.com/calendar/v3"
CALENDAR_ID = "primary"
WINDOW_BEFORE = timedelta(hours=2)
WINDOW_AFTER = timedelta(days=14)
CHANNEL_TTL = timedelta(days=7)  # Google caps event channels at about a week.
RENEW_BEFORE = timedelta(hours=24)
MAX_PAGES = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    key         text PRIMARY KEY,
    snapshot    jsonb NOT NULL,
    start_utc   timestamptz NOT NULL,
    status      text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calendar_events_by_start ON calendar_events (start_utc);
CREATE TABLE IF NOT EXISTS calendar_state (
    id                integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    sync_token        text,
    channel_id        text,
    resource_id       text,
    channel_token     text,
    channel_expires   timestamptz,
    source            text,
    last_sync_at      timestamptz,
    last_full_sync_at timestamptz,
    last_error        text
);
"""


class SyncTokenExpired(Exception):
    """Google answered 410 Gone: the sync token is no longer valid; do a full sync."""


# Conversions.

def parse_google_time(value: dict[str, Any]) -> tuple[datetime | None, bool]:
    if not value:
        return None, False
    if value.get("dateTime"):
        moment = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        return moment.astimezone(timezone.utc), False
    if value.get("date"):
        return datetime.fromisoformat(value["date"]).replace(tzinfo=timezone.utc), True
    return None, False


def from_google(item: dict[str, Any]) -> SlotEvent | None:
    """A Calendar API event -> SlotEvent; None when it has no usable time (or is a
    cancelled stub without one: see `cancelled_key`)."""
    start, all_day = parse_google_time(item.get("start") or {})
    end, _ = parse_google_time(item.get("end") or {})
    if start is None:
        return None
    end = end if end and end > start else start + timedelta(hours=1)
    organizer = item.get("organizer") or {}
    attendees = [Person(str(a.get("email", "")).lower(), str(a.get("displayName", "")),
                        str(a.get("responseStatus", "")), bool(a.get("self")),
                        bool(a.get("resource")), bool(a.get("organizer")))
                 for a in item.get("attendees") or [] if a.get("email")]
    event = SlotEvent(
        key=str(item["id"]), title=str(item.get("summary", "")), start_utc=start.isoformat(),
        end_utc=end.isoformat(), all_day=all_day, status=str(item.get("status", "confirmed")),
        organizer_email=str(organizer.get("email", "")).lower(),
        organizer_name=str(organizer.get("displayName", "")), attendees=attendees,
        description=str(item.get("description", ""))[:calendar_ics.MAX_DESCRIPTION_CHARS],
        location=str(item.get("location", ""))[:300],
        declined=any(a.is_self and a.response == "declined" for a in attendees))
    return with_company(event)


def ics_key(event: calendar_ics.CalEvent, recurring: bool) -> str:
    """The ICS UID in the Calendar API's id form: "<id>@google.com" -> "<id>", and an
    expanded occurrence -> "<id>_<YYYYMMDDTHHMMSSZ>" (Google's instance ids)."""
    base = event.uid.removesuffix("@google.com")
    return f"{base}_{event.start:%Y%m%dT%H%M%SZ}" if recurring else base


def from_ics(event: calendar_ics.CalEvent, recurring: bool = False) -> SlotEvent:
    attendees = [Person(a.email, a.name, {"DECLINED": "declined", "ACCEPTED": "accepted",
                                          "TENTATIVE": "tentative"}.get(a.partstat, "needsAction"))
                 for a in event.attendees]
    slot = SlotEvent(ics_key(event, recurring), event.title, event.start.isoformat(),
                     event.end.isoformat(), event.all_day,
                     "cancelled" if event.status == "CANCELLED" else
                     (event.status.lower() or "confirmed"),
                     event.organizer, event.organizer_name, attendees, event.description,
                     event.location, event.declined)
    return with_company(slot)


def with_company(event: SlotEvent) -> SlotEvent:
    """Fill the company guess (title, else a non-personal attendee domain)."""
    cal = calendar_ics.CalEvent(event.key, event.title, datetime.fromisoformat(event.start_utc),
                                datetime.fromisoformat(event.end_utc), event.all_day,
                                organizer=event.organizer_email,
                                attendees=[calendar_ics.Attendee(p.email, p.name)
                                           for p in event.attendees])
    event.company = calendar_ics.company_name(cal)
    event.company_domain = calendar_ics.company_domain(cal)
    return event


def snapshot_from_json(data: dict[str, Any]) -> SlotEvent:
    data = dict(data)
    data["attendees"] = [Person(**person) for person in data.get("attendees", [])]
    return SlotEvent(**data)


def in_window(event: SlotEvent, now: datetime) -> bool:
    start = datetime.fromisoformat(event.start_utc)
    return now - WINDOW_BEFORE <= start <= now + WINDOW_AFTER


# Google Calendar API.

def headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def list_events(http: httpx.AsyncClient, access_token: str,
                      sync_token: str | None) -> tuple[list[dict[str, Any]], str]:
    """All events (full) or only the changes since `sync_token`; -> (items, next token)."""
    params: dict[str, Any] = {"singleEvents": "true", "maxResults": 250, "showDeleted": "true"}
    if sync_token:
        params["syncToken"] = sync_token
    items: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        response = await http.get(f"{API}/calendars/{CALENDAR_ID}/events", params=params,
                                  headers=headers(access_token))
        if response.status_code == 410:
            raise SyncTokenExpired()
        response.raise_for_status()
        body = response.json()
        items += body.get("items") or []
        if body.get("nextPageToken"):
            params["pageToken"] = body["nextPageToken"]
            continue
        return items, str(body.get("nextSyncToken", ""))
    raise RuntimeError("calendar listing did not finish within the page limit")


async def watch(http: httpx.AsyncClient, access_token: str, address: str,
                now: datetime) -> dict[str, Any]:
    """Open a push channel; -> {channel_id, resource_id, channel_token, expires}."""
    channel_id, token = f"valr-{secrets.token_hex(8)}", secrets.token_urlsafe(24)
    expires = now + CHANNEL_TTL
    response = await http.post(f"{API}/calendars/{CALENDAR_ID}/events/watch",
                               headers=headers(access_token),
                               json={"id": channel_id, "type": "web_hook", "address": address,
                                     "token": token,
                                     "params": {"ttl": str(int(CHANNEL_TTL.total_seconds()))}})
    response.raise_for_status()
    body = response.json()
    if body.get("expiration"):
        expires = datetime.fromtimestamp(int(body["expiration"]) / 1000, timezone.utc)
    return {"channel_id": channel_id, "resource_id": str(body.get("resourceId", "")),
            "channel_token": token, "expires": expires}


async def stop(http: httpx.AsyncClient, access_token: str, channel_id: str,
               resource_id: str) -> None:
    """Close a channel; a channel that is already gone is fine."""
    response = await http.post(f"{API}/channels/stop", headers=headers(access_token),
                               json={"id": channel_id, "resourceId": resource_id})
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


def needs_renewal(state: dict[str, Any], now: datetime) -> bool:
    expires = state.get("channel_expires")
    return not state.get("channel_id") or expires is None or expires - now < RENEW_BEFORE


# Postgres.

async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)
    await connection.execute("INSERT INTO calendar_state (id) VALUES (1) ON CONFLICT DO NOTHING")


async def load_state(connection: Any) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT sync_token, channel_id, resource_id, channel_token, channel_expires, source, "
        "last_sync_at, last_full_sync_at, last_error FROM calendar_state WHERE id = 1")
    row = await cursor.fetchone()
    names = ("sync_token", "channel_id", "resource_id", "channel_token", "channel_expires",
             "source", "last_sync_at", "last_full_sync_at", "last_error")
    return dict(zip(names, row)) if row else {}


async def save_state(connection: Any, **values: Any) -> None:
    if not values:
        return
    columns = ", ".join(f"{name} = %s" for name in values)
    await connection.execute(f"UPDATE calendar_state SET {columns} WHERE id = 1",
                             tuple(values.values()))


async def snapshots(connection: Any, keys: list[str] | None = None) -> dict[str, SlotEvent]:
    if keys is None:
        cursor = await connection.execute("SELECT key, snapshot FROM calendar_events")
    else:
        cursor = await connection.execute(
            "SELECT key, snapshot FROM calendar_events WHERE key = ANY(%s)", (keys,))
    return {row[0]: snapshot_from_json(row[1]) for row in await cursor.fetchall()}


async def store(connection: Any, event: SlotEvent) -> None:
    await connection.execute(
        "INSERT INTO calendar_events (key, snapshot, start_utc, status, updated_at) "
        "VALUES (%s, %s::jsonb, %s, %s, now()) ON CONFLICT (key) DO UPDATE SET "
        "snapshot = excluded.snapshot, start_utc = excluded.start_utc, status = excluded.status, "
        "updated_at = now()",
        (event.key, json.dumps(asdict(event)), event.start_utc, event.status))


async def changes(connection: Any, incoming: list[SlotEvent], cancelled_keys: list[str],
                  full: bool, now: datetime, owners: set[str]) -> list[tuple[SlotEvent, SlotDiff]]:
    """Store the new snapshots and return the material changes, in start order.

    A full listing also cancels every stored, upcoming event that is no longer in it.
    """
    previous = await snapshots(connection)
    out: list[tuple[SlotEvent, SlotDiff]] = []
    seen: set[str] = set()
    for event in incoming:
        seen.add(event.key)
        old = previous.get(event.key)
        if old is None and not in_window(event, now):
            continue
        change = diff(old, event, owners)
        await store(connection, event)
        if change.kinds != ["none"]:
            out.append((event, change))
    gone = list(cancelled_keys)
    if full:
        gone += [key for key, old in previous.items() if key not in seen
                 and old.status != "cancelled" and datetime.fromisoformat(old.start_utc) > now]
    for key in gone:
        old = previous.get(key)
        if old is None or old.status == "cancelled":
            continue
        old.status = "cancelled"
        await store(connection, old)
        out.append((old, SlotDiff(["cancelled"])))
    await connection.execute("DELETE FROM calendar_events WHERE start_utc < %s",
                             (now - timedelta(days=3),))
    return sorted(out, key=lambda pair: pair[0].start_utc)


def from_google_items(items: list[dict[str, Any]]) -> tuple[list[SlotEvent], list[str]]:
    """-> (events, keys of cancelled stubs that carry no time)."""
    events, cancelled = [], []
    for item in items:
        event = from_google(item)
        if event is not None:
            events.append(event)
        elif item.get("status") == "cancelled" and item.get("id"):
            cancelled.append(str(item["id"]))
    return events, cancelled


def from_ics_text(text: str, owners: set[str]) -> list[SlotEvent]:
    events = calendar_ics.parse(text, owners)
    recurring_uids = {e.uid for e in events if sum(1 for x in events if x.uid == e.uid) > 1}
    return [from_ics(event, event.uid in recurring_uids) for event in events]


async def upcoming(connection: Any, start: datetime, end: datetime) -> list[SlotEvent]:
    """Stored events starting in [start, end), for the calendar tool and the blocks."""
    cursor = await connection.execute(
        "SELECT snapshot FROM calendar_events WHERE start_utc >= %s AND start_utc < %s "
        "ORDER BY start_utc", (start, end))
    return [snapshot_from_json(row[0]) for row in await cursor.fetchall()]
