"""Notetaker routes: the dashboard's connect forms and recordings, and the provider webhooks.

Dashboard (behind Cloudflare Access; actions need the owner, same origin and the CSRF
token, see app/valr_dashboard.action_owner):

- `POST /valr/api/notetakers/{provider}/connect` {api_key}: checked with one call to the
  provider, stored encrypted; the answer never contains the key.
- `POST /valr/api/notetakers/{provider}/disconnect`: forgets the stored key.
- `POST /valr/api/notetakers/{provider}/sync`: "Check for new recordings" (last 7 days).
- `POST /valr/api/notetakers/granola/import` {export}: a Granola note export (JSON).
- `GET /valr/api/recording?ref=<slot ref>` or `?id=<provider>:<id>`: a past interview's
  recording (embed or link) and its key moments; read when the detail view opens.
- `GET /valr/api/past-interviews?days=60` (1-180): past interviews, newest first, each with
  its matched recording (no provider call: what webhooks and syncs stored), plus
  recordings that matched no calendar event. For the Past Interviews page.

Webhooks (Cloudflare Access bypass needed; each request is signed):

- `POST /hooks/fathom`: Standard Webhooks signature (`webhook-id`, `webhook-timestamp`,
  `webhook-signature`), secret FATHOM_WEBHOOK_SECRET (`whsec_` + base64), 5-minute tolerance.
- `POST /hooks/granola`: Standard Webhooks again, GRANOLA_WEBHOOK_SECRET; the body names
  the note (`note_id`), which is fetched with the stored key.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import notetakers, research, valr_dashboard
from app.notetakers import base, fathom, granola, pipeline, store
from app.notetakers.base import KeyRefused, ProviderError
from app.notetakers.signatures import verify_standard_webhook

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
NO_CACHE = valr_dashboard.NO_CACHE
MAX_HOOK_BYTES = 2_000_000  # A Fathom payload with a long transcript.
MAX_IMPORT_BYTES = 3_000_000


def provider_or_404(provider: str) -> str:
    if provider not in notetakers.PROVIDERS:
        raise HTTPException(404, "Unknown notetaker.")
    return provider


def pool():
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    return research.pool


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: str = Field(min_length=8, max_length=512)


@router.post("/valr/api/notetakers/{provider}/connect")
async def connect(request: Request, provider: str) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    provider = provider_or_404(provider)
    try:
        body = ConnectRequest.model_validate_json(await request.body())
    except ValidationError as error:
        raise HTTPException(400, "Paste the API key (at least 8 characters).") from None
    key = body.api_key.strip()
    label = notetakers.INFO[provider]["label"]
    try:
        async with pool().connection() as connection, notetakers.http_client() as http:
            account = await notetakers.connect(connection, provider, key, http)
    except KeyRefused:
        raise HTTPException(400, f"{label} refused that key. Copy it again and retry.") from None
    except (ProviderError, httpx.HTTPError) as error:
        logger.warning("VALR notetaker %s connect failed: %s", provider, type(error).__name__)
        raise HTTPException(502, f"{label} didn't answer; the key was not saved. Try again.") from None
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from None
    return JSONResponse({"ok": True, "provider": provider, "state": "connected", "account": account,
                         "message": f"{label} connected" + (f" ({account})." if account else ".")},
                        headers=NO_CACHE)


@router.post("/valr/api/notetakers/{provider}/disconnect")
async def disconnect(request: Request, provider: str) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    provider = provider_or_404(provider)
    async with pool().connection() as connection:
        await notetakers.disconnect(connection, provider)
    label = notetakers.INFO[provider]["label"]
    server = " The server's own key still applies." if notetakers.env_key(provider) else ""
    return JSONResponse({"ok": True, "message": f"{label} disconnected.{server}"}, headers=NO_CACHE)


@router.post("/valr/api/notetakers/{provider}/sync")
async def sync(request: Request, provider: str) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    provider = provider_or_404(provider)
    label = notetakers.INFO[provider]["label"]
    try:
        async with pool().connection() as connection, notetakers.http_client() as http:
            result = await notetakers.sync_recent(connection, provider, http)
    except KeyRefused:
        raise HTTPException(400, f"{label} isn't connected or refused the key.") from None
    except (ProviderError, httpx.HTTPError) as error:
        logger.warning("VALR notetaker %s sync failed: %s", provider, type(error).__name__)
        raise HTTPException(502, f"{label} didn't answer. Try again in a minute.") from None
    return JSONResponse({"ok": True, **result,
                         "message": f"{label}: {result['found']} recordings in the last "
                                    f"{notetakers.SYNC_DAYS} days, {result['new']} new."}, headers=NO_CACHE)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(default="", max_length=200)
    export: str = Field(min_length=2, max_length=MAX_IMPORT_BYTES)


@router.post("/valr/api/notetakers/granola/import")
async def granola_import(request: Request) -> JSONResponse:
    """A Granola export (JSON from the API or the MCP export, or one Markdown note)."""
    await valr_dashboard.action_owner(request)
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES + 1000:
        raise HTTPException(413, "That export is too large (3 MB at most).")
    try:
        body = ImportRequest.model_validate_json(raw)
        recordings = granola.parse_export(body.export, body.filename)
    except (ValidationError, ValueError) as error:
        raise HTTPException(400, "That file isn't a Granola export VALR can read (JSON or Markdown).") from None
    if not recordings:
        raise HTTPException(400, "No notes found in that file.")
    new = 0
    async with pool().connection() as connection:
        for recording in recordings[:50]:
            result = await pipeline.intake(connection, recording, "import",
                                           match=lambda r: notetakers.match_event(connection, r))
            new += result["new"]
    return JSONResponse({"ok": True, "found": len(recordings), "new": new,
                         "message": f"Granola: {len(recordings)} notes read, {new} new."}, headers=NO_CACHE)


@router.get("/valr/api/recording")
async def recording(request: Request, ref: str = "", id: str = "") -> JSONResponse:
    """The recording of a past interview: by its slot ref (calendar match) or `provider:id`."""
    await valr_dashboard.viewer(request)
    from app.profile.config import settings
    owner_names = {settings().candidate_name}
    async with pool().connection() as connection, notetakers.http_client() as http:
        found = None
        if id:
            provider, _, external_id = id.partition(":")
            if provider not in notetakers.PROVIDERS or not external_id or len(external_id) > 120:
                raise HTTPException(404, "Unknown recording.")
            await store.setup(connection)
            row = await store.recording(connection, provider, external_id)
            found = base.Recording(provider, external_id, *(
                (row["title"], notetakers._iso(row["started_at"]), notetakers._iso(row["ended_at"]),
                 row["share_url"], row["embed_url"]) if row else ()))
            if not row and provider == "fathom":
                found = await _fathom_by_id(connection, external_id, http) or found
        elif ref.startswith("slot-") and len(ref) <= 40:
            event = await _past_event(connection, ref)
            if event is None:
                raise HTTPException(404, "That interview isn't on the calendar any more.")
            found = await notetakers.find_for_event(connection, event, http)
        else:
            raise HTTPException(400, "Pass ref or id.")
        if found is None:
            return JSONResponse({"ok": True, "found": False}, headers=NO_CACHE)
        view = await notetakers.recording_view(connection, found, http, owner_names)
    return JSONResponse({"ok": True, "found": True, **view}, headers=NO_CACHE)


PAST_CALENDAR = timedelta(days=3)  # calendar_events keeps 3 days of the past (app/calendar_sync.py).


def recording_summary(row: dict[str, Any]) -> dict[str, Any]:
    """A stored recording for listings: its links (embed whenever it has a share token)."""
    provider = row["provider"]
    return {"id": f"{provider}:{row['external_id']}", "provider": provider,
            "label": notetakers.INFO.get(provider, {}).get("label", provider), "title": row["title"],
            "start": notetakers._iso(row["started_at"]), "end": notetakers._iso(row["ended_at"]),
            "share_url": base.safe_link(row["share_url"]),
            "embed_url": base.safe_embed(row["embed_url"]), "evaluation": row["eval_status"]}


def past_view(events: list[Any], rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Calendar events that are over, and stored recordings, joined by event key."""
    from app.interview_types import slot_workflow_id
    from app.valr_view import scrub
    by_event = {r["event_key"]: r for r in rows if r.get("event_key")}
    out, seen = [], set()
    for event in events:
        if event.all_day or event.declined or event.status == "cancelled":
            continue
        if datetime.fromisoformat(event.end_utc) > now:
            continue
        row = by_event.get(event.key)
        seen.add(event.key)
        out.append({"ref": slot_workflow_id(event.key), "title": scrub(event.title),
                    "company": scrub(event.company or ""), "start": notetakers._iso(base._dt(event.start_utc)),
                    "end": notetakers._iso(base._dt(event.end_utc)),
                    "interviewers": [scrub(p.name) for p in event.attendees if p.name and not p.is_self][:6],
                    "recording": recording_summary(row) if row else None})
    for row in rows:
        key = row.get("event_key")
        if key in seen:
            continue
        if key:
            seen.add(key)
        out.append({"ref": slot_workflow_id(key) if key else None,
                    "title": scrub(row.get("event_title") or row["title"]),
                    "company": scrub(row.get("event_company") or ""),
                    "start": notetakers._iso(row.get("event_start") or row["started_at"]), "end": None,
                    "interviewers": [], "matched": bool(key), "recording": recording_summary(row)})
    return sorted(out, key=lambda item: item["start"] or "", reverse=True)


@router.get("/valr/api/past-interviews")
async def past_interviews(request: Request, days: int = 60) -> JSONResponse:
    await valr_dashboard.viewer(request)
    from app import calendar_sync
    days = max(1, min(days, 180))
    now = datetime.now(timezone.utc)
    async with pool().connection() as connection:
        await store.setup(connection)
        rows = await store.between(connection, now - timedelta(days=days), now)
        await calendar_sync.setup(connection)
        events = await calendar_sync.upcoming(connection, now - min(PAST_CALENDAR, timedelta(days=days)), now)
    items = past_view(events, rows, now)
    return JSONResponse({"ok": True, "days": days, "generated_at": now.isoformat(), "interviews": items},
                        headers=NO_CACHE)


async def _past_event(connection: Any, ref: str):
    from app import calendar_sync
    from app.interview_types import slot_workflow_id
    now = datetime.now(timezone.utc)
    await calendar_sync.setup(connection)
    events = await calendar_sync.upcoming(connection, now - PAST_CALENDAR, now + timedelta(days=1))
    return next((e for e in events if slot_workflow_id(e.key) == ref), None)


async def _fathom_by_id(connection: Any, external_id: str, http: httpx.AsyncClient):
    """A scorecard's Fathom recording that VALR has not stored yet: its links from the list."""
    key = await notetakers.api_key(connection, "fathom")
    if not key:
        return None
    try:
        now = datetime.now(timezone.utc)
        for item in await fathom.list_recordings(key, now - timedelta(days=120), now, http):
            if item.external_id == external_id:
                await store.upsert_recording(connection, item.public(), "sync")
                return item
    except (KeyRefused, ProviderError, httpx.HTTPError) as error:
        logger.warning("VALR Fathom lookup failed: %s", type(error).__name__)
    return None


# Webhooks.

async def _read_hook(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MAX_HOOK_BYTES:
        raise HTTPException(413, "Too large.")
    return body


@router.post("/hooks/fathom")
async def fathom_hook(request: Request) -> dict[str, bool]:
    secret = os.getenv("FATHOM_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Unavailable.")
    body = await _read_hook(request)
    if not verify_standard_webhook(secret, {k.lower(): v for k, v in request.headers.items()}, body):
        logger.warning("Fathom webhook refused: bad or stale signature")
        raise HTTPException(401, "Bad signature.")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Expected a JSON object.") from None
    recording = fathom.payload_recording(payload)
    if not recording.external_id:
        return {"ok": True}  # Nothing to take in (e.g. a test delivery).
    async with pool().connection() as connection:
        await pipeline.intake(connection, recording, "webhook",
                              match=lambda r: notetakers.match_event(connection, r))
    logger.info("Fathom webhook: a recording was taken in")
    return {"ok": True}


@router.post("/hooks/granola")
async def granola_hook(request: Request) -> dict[str, bool]:
    secret = os.getenv("GRANOLA_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Unavailable.")
    body = await _read_hook(request)
    if not verify_standard_webhook(secret, {k.lower(): v for k, v in request.headers.items()}, body):
        logger.warning("Granola webhook refused: bad or stale signature")
        raise HTTPException(401, "Bad signature.")
    try:
        payload = json.loads(body)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        note_id = str(data.get("note_id") or "")
    except (ValueError, AttributeError):
        raise HTTPException(400, "Expected a JSON object.") from None
    if not note_id or len(note_id) > 120:
        return {"ok": True}
    async with pool().connection() as connection, notetakers.http_client() as http:
        key = await notetakers.api_key(connection, "granola")
        if not key:
            logger.warning("Granola webhook: Granola is not connected")
            return {"ok": True}
        try:
            recording = await granola.get_recording(key, note_id, http)
        except (KeyRefused, ProviderError, httpx.HTTPError) as error:
            logger.warning("Granola webhook: fetch failed: %s", type(error).__name__)
            raise HTTPException(502, "Fetch failed; retry later.") from None
        await pipeline.intake(connection, recording, "webhook",
                              match=lambda r: notetakers.match_event(connection, r))
    logger.info("Granola webhook: a note was taken in")
    return {"ok": True}
