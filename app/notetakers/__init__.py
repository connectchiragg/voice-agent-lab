"""Notetakers VALR can link: Fathom, Fireflies and Granola, behind one small interface.

Per provider (app/notetakers/<name>.py, contract in app/notetakers/base.py): verify a
key, list recordings in a date range, get one recording's transcript and summary
(normalised to the profile importer's meeting shape), and its share link. Fathom
recordings play inside VALR from Fathom's official download API (app/notetakers/media.py).

The owner connects a provider on the dashboard ("Get set up"): the API key is posted
to the server, checked with one call, stored Fernet-encrypted (app/notetakers/store.py)
and never sent back. An environment key (FATHOM_API_KEY, FIREFLIES_API_KEY,
GRANOLA_API_KEY) still works and shows as "from server settings".

New recordings reach the evaluation through app/notetakers/pipeline.py (webhooks:
app/notetakers/routes.py: `/hooks/fathom`, `/hooks/granola`; Fireflies is a one-time
history import).
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.notetakers import base, fathom, fireflies, granola, phone, store
from app.notetakers.base import KeyRefused, ProviderError, Recording

logger = logging.getLogger("uvicorn.error")

PROVIDERS = {"fathom": fathom, "fireflies": fireflies, "granola": granola, "phone": phone}
ENV_KEYS = {"fathom": "FATHOM_API_KEY", "fireflies": "FIREFLIES_API_KEY", "granola": "GRANOLA_API_KEY"}
INFO: dict[str, dict[str, Any]] = {
    "fathom": {
        "label": "Fathom", "key_label": "Fathom API key",
        "key_help": "In Fathom: Settings → API Access → Generate API key.",
        "key_url": "https://developers.fathom.ai/api-overview", "webhook_env": "FATHOM_WEBHOOK_SECRET",
        "gets": "Recordings, transcripts and summaries; new ones arrive by webhook.",
        "video": "player", "import": False},
    "fireflies": {
        "label": "Fireflies", "key_label": "Fireflies API key",
        "key_help": "In Fireflies: Integrations → Fireflies API → copy the API key.",
        "key_url": "https://docs.fireflies.ai/fundamentals/authorization",
        "gets": "Past meetings (history import); audio or video plays here when your Fireflies plan allows it.",
        "video": "player", "import": False},
    "granola": {
        "label": "Granola", "key_label": "Granola API key (needs a Granola Business or Enterprise plan)",
        "optional": True,
        "key_help": "In Granola (desktop): Settings → Connectors → API keys. Needs a Business or "
                    "Enterprise plan; on other plans, upload the CSV export from Settings instead.",
        "key_url": "https://docs.granola.ai/introduction", "webhook_env": "GRANOLA_WEBHOOK_SECRET",
        "gets": "Notes, summaries and transcripts. Granola records no audio or video.",
        "video": "none", "import": True},
    "phone": {  # Uploaded from the iPhone (app/phone_recordings.py): nothing to connect, so hidden.
        "label": "Phone call", "key_label": "", "key_help": "", "hidden": True,
        "gets": "Call recordings shared from Truecaller with the iOS Shortcut.",
        "video": "player", "import": False},
}
SYNC_DAYS = 14  # The routine sync's window: two weeks, so nothing slips between runs.


def token_key() -> str:
    return os.getenv("VALR_TOKEN_KEY", "").strip()


def env_key(provider: str) -> str:
    return os.getenv(ENV_KEYS.get(provider, ""), "").strip() if provider in ENV_KEYS else ""


def connectable(provider: str) -> bool:
    return provider in PROVIDERS and not INFO.get(provider, {}).get("hidden")


async def api_key(connection: Any, provider: str) -> str | None:
    """The stored key (decrypted), else the server's environment key, else None."""
    stored = await store.load_key(connection, token_key(), provider) if token_key() else None
    return stored or env_key(provider) or None


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=base.TIMEOUT_S)


# Connect / disconnect.

async def connect(connection: Any, provider: str, key: str, http: httpx.AsyncClient) -> str:
    """Check the key with one call, then store it encrypted. -> account label ("" if unnamed).

    Raises KeyRefused (401/403), ProviderError (anything else) or RuntimeError (no VALR_TOKEN_KEY)."""
    if not token_key():
        raise RuntimeError("VALR_TOKEN_KEY is not set, so keys cannot be stored encrypted.")
    account = await PROVIDERS[provider].verify(key, http)
    await store.setup(connection)
    await store.save_key(connection, token_key(), provider, key, account)
    logger.info("VALR notetaker %s connected", provider)
    return account


async def disconnect(connection: Any, provider: str) -> bool:
    await store.setup(connection)
    removed = await store.delete_key(connection, provider)
    logger.info("VALR notetaker %s disconnected", provider)
    return removed


async def status_view(connection: Any) -> dict[str, dict[str, Any]]:
    """Per provider: state (connected | refused | server | not_connected), account, times, how
    many recordings arrived in the last 7 days, the webhook (set up? last delivery), the
    API-key help link; never the key."""
    await store.setup(connection)
    rows = await store.statuses(connection)
    activity = await store.activity(connection)
    out = {}
    for name, info in INFO.items():
        if info.get("hidden"):
            continue
        row = rows.get(name)
        state = row["state"] if row else ("server" if env_key(name) else "not_connected")
        seen = activity.get(name) or {}
        webhook = None
        if info.get("webhook_env"):
            webhook = {"configured": bool(os.getenv(info["webhook_env"], "").strip()),
                       "last_at": _iso(seen.get("last_webhook_at"))}
        out[name] = {"label": info["label"], "state": state, "account": (row or {}).get("account") or "",
                     "connected_at": _iso((row or {}).get("connected_at")),
                     "last_ok_at": _iso((row or {}).get("last_ok_at")),
                     "new_7d": int(seen.get("new_7d") or 0), "webhook": webhook,
                     "can_store": bool(token_key()), "optional": bool(info.get("optional")),
                     **{k: info[k] for k in ("key_label", "key_help", "gets", "video", "import")},
                     "key_url": info.get("key_url", "")}
    return out


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def from_row(row: dict[str, Any]) -> Recording:
    """A stored row -> Recording (links, company and people; no emails, no transcript)."""
    return Recording(row["provider"], row["external_id"], row.get("title") or "", _iso(row.get("started_at")),
                     _iso(row.get("ended_at")), row.get("share_url") or "", "",
                     company=row.get("company") or "", company_domain=row.get("company_domain") or "",
                     interviewers=store.names(row), invitee_hashes=store.names(row, "invitee_hashes"))


def connected(statuses: dict[str, dict[str, Any]]) -> list[str]:
    return [name for name, s in statuses.items() if s["state"] in ("connected", "server")]


# Recordings.

async def sync_recent(connection: Any, provider: str, http: httpx.AsyncClient, days: int = SYNC_DAYS,
                      now: datetime | None = None, hook=None) -> dict[str, int]:
    """List the last `days` of recordings and take in the new ones. -> {found, new}."""
    from app.notetakers import pipeline
    key = await api_key(connection, provider)
    if not key:
        raise KeyRefused(f"{provider} is not connected")
    now = now or datetime.now(timezone.utc)
    try:
        found = await PROVIDERS[provider].list_recordings(key, now - timedelta(days=days), now, http)
    except KeyRefused:
        await store.mark(connection, provider, False, "key refused")
        raise
    await store.mark(connection, provider, True)
    new = 0
    for recording in found:
        if recording.external_id:
            result = await pipeline.intake(connection, recording, "sync", hook=hook,
                                           match=lambda r: match_event(connection, r))
            new += result["new"]
    return {"found": len(found), "new": new}


def event_ref(event: Any) -> dict[str, Any]:
    """What a recording keeps of its calendar event (no attendees)."""
    from app.valr_view import scrub
    return {"key": event.key, "title": scrub(event.title), "company": scrub(event.company or ""),
            "start": event.start_utc}


async def match_event(connection: Any, recording: Recording) -> dict[str, Any] | None:
    """The calendar event this recording belongs to (time window + attendee emails), if any."""
    from app import calendar_sync
    from app.interview_worker import owner_emails
    start = base._dt(recording.start)
    if start is None:
        return None
    await calendar_sync.setup(connection)
    events = await calendar_sync.upcoming(connection, start - timedelta(hours=3), start + timedelta(hours=1))
    owners = owner_emails()
    best, best_score = None, 0.0
    for event in events:
        if event.all_day:
            continue
        emails = {p.email.lower() for p in event.attendees} - owners
        score = base.match_score(recording, event.start_utc, event.end_utc, emails, event.title)
        if score > best_score:
            best, best_score = event, score
    return event_ref(best) if best else None


async def find_for_event(connection: Any, event: Any, http: httpx.AsyncClient,
                         statuses: dict[str, dict[str, Any]] | None = None) -> Recording | None:
    """A past event's recording: a stored match first, else ask each connected provider."""
    await store.setup(connection)
    row = await store.by_event(connection, event.key)
    if row:
        return from_row(row)
    from app.interview_worker import owner_emails
    statuses = statuses or await status_view(connection)
    start, end = base._dt(event.start_utc), base._dt(event.end_utc)
    emails = {p.email.lower() for p in event.attendees} - owner_emails()
    candidates: list[Recording] = []
    for name in connected(statuses):
        if INFO[name]["video"] == "none" and candidates:
            continue  # Prefer a provider with a recording.
        key = await api_key(connection, name)
        try:
            candidates += await PROVIDERS[name].list_recordings(
                key, start - timedelta(hours=2), end + timedelta(hours=2), http)
        except (KeyRefused, ProviderError, httpx.HTTPError) as error:
            logger.warning("VALR notetaker %s lookup failed: %s", name, type(error).__name__)
    match = base.best_match(candidates, event.start_utc, event.end_utc, emails, event.title)
    if match:
        ref = event_ref(event)
        await store.upsert_recording(connection, match.public(), "sync")
        await store.set_event(connection, match.provider, match.external_id, ref["key"], ref["title"],
                              ref["company"], ref["start"])
    return match


async def recording_view(connection: Any, recording: Recording, http: httpx.AsyncClient,
                         owner_names: set[str] = frozenset()) -> dict[str, Any]:
    """What the dashboard shows for one recording: the player or the link, and key moments."""
    module = PROVIDERS[recording.provider]
    moments: list[dict[str, Any]] = []
    note = ""
    key = await api_key(connection, recording.provider) if connectable(recording.provider) else None
    if recording.provider == "phone" and recording.meeting is None:
        recording.meeting = await phone.stored_meeting(connection, recording.external_id)
        if recording.meeting is None:
            note = "The transcript isn't ready yet, so there are no key moments."
    if key and recording.meeting is None:
        try:
            recording = await module.get_recording(key, recording.external_id, http, listed=recording)
            from app.notetakers import transcripts
            await transcripts.save_quietly(connection, recording.provider, recording.external_id, recording.meeting)
        except (KeyRefused, ProviderError, httpx.HTTPError) as error:
            logger.warning("VALR notetaker %s transcript unavailable: %s", recording.provider, type(error).__name__)
            note = "The transcript could not be loaded just now, so there are no key moments."
    meeting = recording.meeting or {}
    raw = base.moments_from_summary(meeting.get("vendor_summary")) or \
        base.moments_from_turns(meeting.get("turns") or [], owner_names)
    for moment in raw:
        url = moment["url"] or (module.timestamp_url(recording.share_url, moment["seconds"])
                                if hasattr(module, "timestamp_url") else "")
        moments.append({"label": moment["label"], "seconds": moment["seconds"],
                        "clock": base.clock(moment["seconds"]), "url": base.safe_link(url)})
    return {"provider": recording.provider, "label": INFO[recording.provider]["label"],
            "id": f"{recording.provider}:{recording.external_id}",
            "title": recording.title, "start": recording.start,
            "share_url": base.safe_link(recording.share_url),
            "player": INFO[recording.provider]["video"] == "player",
            "company": recording.company, "interviewers": recording.interviewers,
            "moments": moments, "note": note}


BACKFILL_DAYS = 180


async def backfill_details(connection: Any, http: httpx.AsyncClient, days: int = BACKFILL_DAYS,
                           now: datetime | None = None) -> dict[str, int]:
    """Company and interviewers for the Fathom recordings VALR already stores (from Fathom's
    calendar invitees), so past interviews keep them after the calendar forgets the event.
    Updates existing rows only (nothing new is handed to the evaluation). -> {listed, updated}."""
    key = await api_key(connection, "fathom")
    if not key:
        return {"listed": 0, "updated": 0}
    now = now or datetime.now(timezone.utc)
    await store.setup(connection)
    found = await fathom.list_recordings(key, now - timedelta(days=days), now, http)
    updated = 0
    for recording in found:
        if recording.external_id and await store.set_details(connection, recording.public()):
            updated += 1
    logger.info("VALR Fathom details: %d listed, %d updated", len(found), updated)
    return {"listed": len(found), "updated": updated}


async def fetch_meeting(provider: str, external_id: str) -> dict | None:
    """For the evaluation hook: the normalised meeting (transcript included), or None.

    Uses what an earlier webhook or export left encrypted, else asks the provider."""
    from app import research
    if research.pool is None:
        return None
    async with research.pool.connection() as connection:
        await store.setup(connection)
        if token_key():
            sealed = await store.sealed_meeting(connection, token_key(), provider, external_id)
            if sealed:
                return sealed
        key = await api_key(connection, provider)
        if not key:
            return None
        row = await store.recording(connection, provider, external_id)
        listed = Recording(provider, external_id, (row or {}).get("title", ""),
                           _iso((row or {}).get("started_at")), _iso((row or {}).get("ended_at")))
        async with http_client() as http:
            return (await PROVIDERS[provider].get_recording(key, external_id, http, listed=listed)).meeting
