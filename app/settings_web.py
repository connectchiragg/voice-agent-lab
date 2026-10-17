"""Settings and Profile actions (owner only; every POST also needs the CSRF token, same origin
and JSON: app/valr_dashboard.action_owner). The pages are app/valr_ui/settings.py and
app/valr_ui/profile.py; the data is app/owner.py.

Profile
- `GET /valr/api/profile`: the profile (no full phone number).
- `POST /valr/api/profile` {display_name?, recording_name?, name_pronunciation?, timezone?}.
- `POST /valr/api/profile/photo` {image: base64 of the cropped square (JPEG, PNG or WebP),
  at most 2 MB}; the browser crops and re-encodes it, which drops EXIF.
- `POST /valr/api/profile/photo/remove` {}: back to the initials.
- `GET /valr/profile/photo`: the photo (owner only; private cache, ETag).
- `POST /logout/all` {}: deletes every session (each device), then this one's cookie.

Phone (Settings › Calls & reminders › Your phone)
- `GET /valr/api/profile/phone`: {masked, set, source: db | env | none, country}.
- `POST /valr/api/profile/phone` {phone}: validated (app/owner.normalize_phone) and saved; used
  from the next dial and the next inbound call. Not verified by a test call: VALR's Telnyx
  outbound profile allows only US and CA destinations today (see docs), so a call to +91
  would be refused.
- `POST /valr/api/profile/phone/remove` {}: no number; an automatic-call setting falls back to
  the heads-up.

Google (Settings › Connections › Google)
- `POST /valr/api/google/disconnect` {}: stop the push channels, revoke the grant at Google,
  delete the stored token (app/google_oauth.disconnect).
- `POST /valr/api/google/sync` {}: a calendar sync and a Gmail sync now.

Data & account
- `GET /valr/api/export`: a zip of JSON files: the profile, settings, interviews, briefings,
  leads, notifications, recordings' details and transcripts (decrypted), scorecards. No
  audio, no keys or tokens.
- `POST /valr/api/data/delete` {confirm: "DELETE"}: revoke Google, then wipe every VALR table
  (sessions included) and the generated scorecards; the résumé profile files stay. Signs out.

Nothing here logs a name, a number, an email or a photo.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import google_oauth, interview_store, owner, research, valr_dashboard

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
NO_CACHE = valr_dashboard.NO_CACHE
PHOTO_BODY_MAX = 2_900_000  # base64 of 2 MB, plus the JSON around it.


def pool():
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    return research.pool


def parse(model: type[BaseModel], raw: bytes, hint: str) -> Any:
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError:
        raise HTTPException(400, hint) from None


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# Profile.

class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str | None = Field(default=None, max_length=200)
    recording_name: str | None = Field(default=None, max_length=200)
    name_pronunciation: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)


def profile_out(email: str = "") -> dict[str, Any]:
    return {"ok": True, **owner.view(), **({"email": email} if email else {})}


@router.get("/valr/api/profile")
async def get_profile(request: Request) -> JSONResponse:
    email = await valr_dashboard.owner(request)
    await owner.refresh()
    return JSONResponse(profile_out(email), headers=NO_CACHE)


@router.post("/valr/api/profile")
async def save_profile(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    body = parse(ProfileRequest, await request.body(), "Expected {display_name?, recording_name?, "
                 "name_pronunciation?, timezone?}.")
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field, label in (("display_name", "Display name"), ("recording_name", "Name in recordings")):
        raw = getattr(body, field)
        if raw is not None:
            try:
                values[field] = owner.clean_name(raw, label)
            except owner.InvalidInput as error:
                errors[field] = str(error)
    if body.name_pronunciation is not None:
        try:
            values["name_pronunciation"] = owner.clean_pronunciation(body.name_pronunciation) or None
        except owner.InvalidInput as error:
            errors["name_pronunciation"] = str(error)
    if body.timezone is not None:
        if owner.valid_zone(body.timezone.strip()):
            values["timezone"] = body.timezone.strip()
        else:
            errors["timezone"] = "Pick a time zone from the list."
    if errors:
        return JSONResponse({"detail": next(iter(errors.values())), "errors": errors}, status_code=400,
                            headers=NO_CACHE)
    if not values:
        raise HTTPException(400, "Nothing to change.")
    async with pool().connection() as connection:
        await owner.save(connection, **values)
    logger.info("VALR profile saved (%s)", ", ".join(sorted(values)))
    return JSONResponse(profile_out() | {"message": "Profile saved."}, headers=NO_CACHE)


class PhotoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    image: str = Field(min_length=16, max_length=PHOTO_BODY_MAX)


@router.post("/valr/api/profile/photo")
async def upload_photo(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    raw = await request.body()
    if len(raw) > PHOTO_BODY_MAX + 200:
        raise HTTPException(413, "That photo is too large: 2 MB at most after cropping.")
    body = parse(PhotoRequest, raw, 'Expected {"image": "<base64>"}.')
    text = body.image.split(",", 1)[1] if body.image.startswith("data:") else body.image
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "That isn't an image VALR can read.") from None
    if len(data) > owner.PHOTO_MAX:
        raise HTTPException(413, "That photo is too large: 2 MB at most after cropping.")
    kind = owner.photo_type(data)
    if kind is None:
        raise HTTPException(415, "Use a JPEG, PNG or WebP photo.")
    async with pool().connection() as connection:
        await owner.save(connection, photo=data, photo_type=kind, photo_etag=owner.etag_of(data))
    logger.info("VALR profile photo updated")
    return JSONResponse(profile_out() | {"message": "Photo updated."}, headers=NO_CACHE)


@router.post("/valr/api/profile/photo/remove")
async def remove_photo(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    parse(Empty, await request.body(), "Expected {}.")
    async with pool().connection() as connection:
        await owner.save(connection, photo=None, photo_type=None, photo_etag=None)
    return JSONResponse(profile_out() | {"message": "Photo removed. Your initials show instead."}, headers=NO_CACHE)


@router.get("/valr/profile/photo")
async def photo(request: Request) -> Response:
    await valr_dashboard.viewer(request)
    async with pool().connection() as connection:
        await owner.setup(connection)
        found = await owner.photo(connection)
    if found is None:
        raise HTTPException(404, "No photo.")
    data, kind, etag = found
    headers = {"Cache-Control": "private, max-age=86400", "ETag": f'"{etag}"',
               "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}
    if request.headers.get("if-none-match", "").strip('"') == etag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=kind, headers=headers)


@router.post("/logout/all")
async def logout_all(request: Request) -> JSONResponse:
    """Every device signs out: all sessions are deleted (this one's cookie is cleared too)."""
    from app.auth import core, store
    await valr_dashboard.action_owner(request)
    backend = store.backend()
    if backend is None:
        raise HTTPException(503, "Postgres is unavailable.")
    ended = await backend.end_all()
    logger.info("VALR signed out of every device (%d sessions)", ended)
    response = JSONResponse({"ok": True, "ended": ended, "next": "/login?signed_out=1",
                             "message": "Signed out everywhere."}, headers=NO_CACHE)
    response.headers.append("Set-Cookie", core.CLEARED_COOKIE)
    return response


# Phone.

class PhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    phone: str = Field(min_length=1, max_length=40)


def phone_out() -> dict[str, Any]:
    return {"ok": True, **owner.view()["phone"]}


@router.get("/valr/api/profile/phone")
async def get_phone(request: Request) -> JSONResponse:
    await valr_dashboard.owner(request)
    await owner.refresh()
    return JSONResponse(phone_out(), headers=NO_CACHE)


@router.post("/valr/api/profile/phone")
async def set_phone(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    body = parse(PhoneRequest, await request.body(), 'Expected {"phone": "+91 …"}.')
    try:
        number = owner.normalize_phone(body.phone)
    except owner.PhoneError as error:
        raise HTTPException(400, str(error)) from None
    async with pool().connection() as connection:
        await owner.set_phone(connection, number)
    logger.info("VALR owner phone updated")
    return JSONResponse(phone_out() | {"message": "Phone updated. VALR calls this number from now on."},
                        headers=NO_CACHE)


@router.post("/valr/api/profile/phone/remove")
async def remove_phone(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    parse(Empty, await request.body(), "Expected {}.")
    switched = False
    async with pool().connection() as connection:
        await owner.remove_phone(connection)
        await interview_store.setup(connection)
        if await interview_store.auto_dial(connection):
            await interview_store.set_flag(connection, interview_store.AUTO_DIAL, False)
            switched = True
    logger.info("VALR owner phone removed")
    message = "Phone removed. VALR won't call you" + ("; reminders are heads-ups now." if switched else ".")
    return JSONResponse(phone_out() | {"message": message, "reach": "heads_up" if switched else None},
                        headers=NO_CACHE)


# Google.

@router.post("/valr/api/google/disconnect")
async def google_disconnect(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    parse(Empty, await request.body(), "Expected {}.")
    async with pool().connection() as connection, httpx.AsyncClient(timeout=10) as http:
        await google_oauth.setup(connection)
        result = await google_oauth.disconnect(connection, google_oauth.OAuthSettings.from_env(), http)
    logger.info("VALR Google disconnected (revoked %s)", result["revoked"])
    message = "Google disconnected." + ("" if result["revoked"] else
                                        " Google didn't confirm; remove VALR at myaccount.google.com › Security if it's still listed.")
    return JSONResponse({"ok": True, **result, "message": message}, headers=NO_CACHE)


@router.post("/valr/api/google/sync")
async def google_sync(request: Request) -> JSONResponse:
    from app import interviews, leads_web
    await valr_dashboard.action_owner(request)
    calendar = await interviews.sync_now("dashboard")
    gmail = await leads_web.start_sync("manual")
    if not calendar and not gmail:
        raise HTTPException(503, "Temporal is unavailable: nothing was synced. Try again in a minute.")
    what = " and ".join(w for w, ok in (("calendar", calendar), ("Gmail", gmail)) if ok)
    return JSONResponse({"ok": True, "message": f"Syncing your {what} now."}, headers=NO_CACHE)


# Data & account.

# Columns never exported: keys, tokens, ciphertexts and audio.
SECRET_COLUMNS = {"secret", "refresh_token", "sealed", "p256dh", "auth", "endpoint", "token_hash",
                  "photo", "code_mac", "channel_token", "url", "meeting", "snippet_enc", "sha256"}
EXPORT_TABLES = ("owner_profile", "notify_settings", "valr_flags", "notifications", "leads", "lead_touchpoints",
                 "lead_overrides", "lead_emails", "slot_context", "company_research", "briefing_blocks", "prep_checks",
                 "heads_up_log", "calendar_events", "notetaker_recordings", "notetaker_hidden", "notetaker_credentials",
                 "post_call_reports", "phone_uploads", "callers", "call_summaries", "company_overrides",
                 "research_jobs", "research_spend", "push_subscriptions", "oauth_tokens")
# Everything VALR stores in Postgres (LangGraph's conversation checkpoints included).
DELETE_TABLES = EXPORT_TABLES + (
    "recording_transcripts", "recording_media", "phone_audio", "phone_upload_token", "interview_context_push",
    "calendar_state", "calendar_history", "gmail_state", "company_resolutions", "call_recordings",
    "checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations", "valr_login_states",
    "valr_sessions")


async def existing(connection: Any, tables: tuple[str, ...]) -> list[str]:
    cursor = await connection.execute(
        "SELECT name FROM unnest(%s::text[]) AS name WHERE to_regclass(name) IS NOT NULL", (list(tables),))
    return [row[0] for row in await cursor.fetchall()]


def clean(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in SECRET_COLUMNS}


async def export_rows(connection: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for table in await existing(connection, EXPORT_TABLES):
        cursor = await connection.execute(f"SELECT row_to_json(t) FROM {table} t")  # Names from the list above.
        out[table] = [clean(row[0]) for row in await cursor.fetchall()]
    if await existing(connection, ("recording_transcripts",)):
        from app.notetakers import transcripts
        cursor = await connection.execute("SELECT provider, external_id FROM recording_transcripts")
        kept = []
        for provider, external_id in await cursor.fetchall():
            data = await transcripts.load(connection, provider, external_id)
            if data:
                kept.append({"provider": provider, "external_id": external_id, **data})
        out["transcripts"] = kept
    return out


def scorecard_files() -> list[Any]:
    """The generated scorecards (Progress, Past interviews): deleted with the data."""
    from app.profile.config import settings
    cfg = settings()
    return [cfg.performance_path, cfg.performance_csv, cfg.scores_path, cfg.scores_path.with_suffix(".json"),
            cfg.evaluation_path]


@router.get("/valr/api/export")
async def export(request: Request) -> Response:
    await valr_dashboard.owner(request)
    async with pool().connection() as connection:
        rows = await export_rows(connection)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", "Your VALR data, one JSON file per kind. No audio, keys or tokens.\n")
        for name, items in rows.items():
            archive.writestr(f"{name}.json", json.dumps(items, indent=1, default=str, ensure_ascii=False))
        for path in scorecard_files():
            try:
                archive.writestr(f"scorecards/{path.name}", path.read_bytes())
            except OSError:
                continue
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logger.info("VALR data exported (%d kinds)", len(rows))
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={**NO_CACHE, "Content-Disposition": f'attachment; filename="valr-data-{stamp}.zip"'})


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: Literal["DELETE"]


@router.post("/valr/api/data/delete")
async def delete_data(request: Request) -> JSONResponse:
    from app.auth import core
    await valr_dashboard.action_owner(request)
    parse(DeleteRequest, await request.body(), 'Type DELETE to confirm: {"confirm": "DELETE"}.')
    async with pool().connection() as connection:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await google_oauth.setup(connection)
                await google_oauth.disconnect(connection, google_oauth.OAuthSettings.from_env(), http)
        except Exception as error:  # noqa: BLE001 - the wipe goes ahead
            logger.warning("VALR delete: Google not revoked (%s)", type(error).__name__)
        tables = await existing(connection, DELETE_TABLES)
        async with connection.transaction():
            await connection.execute(f"TRUNCATE {', '.join(tables)}")  # Names from the list above.
    kept = []
    for path in scorecard_files():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            kept.append(path.name)
    owner.reset()
    logger.info("VALR data deleted (%d tables)", len(tables))
    response = JSONResponse({"ok": True, "tables": len(tables), "kept_files": kept, "next": "/login?signed_out=1",
                             "message": "Your VALR data is deleted."}, headers=NO_CACHE)
    response.headers.append("Set-Cookie", core.CLEARED_COOKIE)
    return response
