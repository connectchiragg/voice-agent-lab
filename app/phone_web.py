"""Phone call recordings on the web (app/phone_recordings.py does the work).

- `POST /hooks/recording-upload` (PUBLIC in app/auth/gate.py, and a Cloudflare Access bypass:
  the iOS Shortcut cannot sign in with Google). `Authorization: Bearer <upload token>`,
  compared in constant time with the stored SHA-256 (VALR_UPLOAD_TOKEN is the first seed).
  multipart/form-data: `file` (m4a, mp3, wav or aac, at most VALR_PHONE_MAX_MB, 100) and the
  optional `title`, `name` (the other party), `recorded_at` (ISO 8601) and `filename` (else the
  file part's own name: its date and time, and a contact name, are hints; digits are never
  kept). A file VALR already has answers 200 {ok, duplicate: true, id} and is not stored or
  processed again, so the batch Shortcut can re-send safely. Success also carries
  `"result": "ok"` (text), which a Shortcut's If can compare reliably. The audio is
  checked by its first bytes, streamed in 1 MB pieces, each encrypted (VALR_TOKEN_KEY), into
  Postgres; the answer is {ok, id} at once and the research worker transcribes it.
  Per IP (CF-Connecting-IP): at most VALR_UPLOAD_PER_HOUR (60) requests an hour, good or bad.
  401 for a missing or wrong token, 413 too large, 415 not audio, 429 too many.
- `GET /valr/api/recording/audio?id=phone:<id>` (owner): the decrypted audio, with Range
  support, for the past-interview player. Gone (410) once the audio was deleted.
- `GET /valr/api/phone/status` (owner): the Settings card's state.
- `POST /valr/api/phone/token` {action: "show" | "rotate"} (owner, CSRF): the token, shown
  once. Rotating stops the old one at once.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from app import phone_recordings as phone
from app import research, valr_dashboard

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
NO_CACHE = valr_dashboard.NO_CACHE
FIELD_LIMIT = 400
WINDOW_S = 3600.0


class RateLimit:
    """A sliding window per key, in memory (one app process)."""

    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = {}

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        recent = self.hits.setdefault(key, deque())
        while recent and now - recent[0] > WINDOW_S:
            recent.popleft()
        if len(recent) >= limit:
            return False
        recent.append(now)
        if len(self.hits) > 5000:  # Forget idle addresses.
            for idle in [k for k, v in self.hits.items() if not v][:1000]:
                del self.hits[idle]
        return True


limiter = RateLimit()


def per_hour() -> int:
    import os
    return max(1, int(os.getenv("VALR_UPLOAD_PER_HOUR", "60")))  # The batch Shortcut sends several at once.


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "") or "?").strip()


def pool():
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    return research.pool


async def run(fn, *args):
    """One short statement on a pooled connection (the pool is small: never held for a whole upload)."""
    async with pool().connection() as connection:
        return await fn(connection, *args)


@router.post("/hooks/recording-upload")
async def recording_upload(request: Request) -> JSONResponse:
    if not limiter.allow(client_ip(request), per_hour()):
        raise HTTPException(429, "Too many uploads. Try again in an hour.")
    bearer = request.headers.get("authorization", "")
    bearer = bearer[7:].strip() if bearer[:7].lower() == "bearer " else ""
    if not phone.token_key():
        raise HTTPException(503, "Recording uploads are not configured.")
    await run(phone.setup)
    if not phone.token_matches(bearer, await run(phone.current_hash)):
        logger.warning("Recording upload refused: missing or wrong token")
        raise HTTPException(401, "Missing or wrong upload token.")
    limit = phone.max_bytes()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + 64 * 1024:
        raise HTTPException(413, f"At most {limit // (1024 * 1024)} MB.")
    try:
        boundary = phone.boundary_of(request.headers.get("content-type", ""))
    except phone.MultipartError as error:
        raise HTTPException(415, "Send the recording as a form with a field named file.") from error
    upload_id = phone.new_id()
    try:
        result = await receive(request, boundary, upload_id, limit)
    except HTTPException:
        await run(phone.drop_upload, upload_id)
        raise
    except Exception as error:  # noqa: BLE001 - a dropped connection: nothing half-stored stays
        await run(phone.drop_upload, upload_id)
        logger.warning("Recording upload failed: %s", type(error).__name__)
        raise HTTPException(400, "The upload did not finish. Try again.") from None
    if result.get("duplicate"):
        logger.info("Recording upload was a duplicate (%s, %.1f MB)", result["kind"], result["size"] / 1048576)
        return JSONResponse({"ok": True, "result": "ok", "duplicate": True, "id": f"{phone.PROVIDER}:{result['duplicate']}",
                             "message": "Already received. Nothing new to do."}, headers=NO_CACHE)
    logger.info("Recording upload received (%s, %.1f MB)", result["kind"], result["size"] / 1048576)
    return JSONResponse({"ok": True, "result": "ok", "duplicate": False, "id": f"{phone.PROVIDER}:{upload_id}",
                         "message": "Got it. VALR will transcribe it and add it to Past interviews."},
                        headers=NO_CACHE)


async def receive(request: Request, boundary: bytes, upload_id: str, limit: int) -> dict[str, Any]:
    """Streams the form: text fields kept (short), the file sniffed, sealed and stored per 1 MB."""
    key = phone.token_key()
    parser = phone.MultipartParser(boundary)
    chunker = phone.Chunker()
    fields: dict[str, str] = {}
    part: dict[str, str] | None = None
    head = bytearray()
    kind = filename = ""
    size = idx = 0
    seen_file = started = False
    digest = hashlib.sha256()

    async def store(piece: bytes) -> None:
        nonlocal idx
        digest.update(piece)
        await run(phone.put_chunk, upload_id, idx, phone.seal_chunk(key, piece))
        idx += 1

    async for data in request.stream():
        for event, value in parser.feed(data):
            if event == "part":
                part = value
                if part["name"] == "file" and not seen_file:
                    seen_file, head, filename = True, bytearray(), part["filename"]
                elif part["name"] == "file":
                    raise HTTPException(400, "Send one file at a time.")
            elif event == "data" and part is not None:
                if part["name"] != "file":
                    if len(fields.get(part["name"], "")) < FIELD_LIMIT:
                        fields[part["name"]] = (fields.get(part["name"], "") + value.decode("utf-8", "replace"))[:FIELD_LIMIT]
                    continue
                size += len(value)
                if size > limit:
                    raise HTTPException(413, f"At most {limit // (1024 * 1024)} MB.")
                if not kind:
                    head += value
                    if len(head) < 16:
                        continue
                    kind = phone.kind_of(bytes(head[:16]), part["filename"], part["content_type"]) or ""
                    if not kind:
                        raise HTTPException(415, "That isn't an m4a, mp3, wav or aac recording.")
                    if not started:
                        await run(phone.begin_upload, upload_id)
                        started = True
                    value = bytes(head)
                for piece in chunker.feed(value):
                    await store(piece)
            elif event == "end" and part is not None and part["name"] == "file" and not kind:
                kind = phone.kind_of(bytes(head[:16]), part["filename"], part["content_type"]) or ""
                if not kind:
                    raise HTTPException(415, "That isn't an m4a, mp3, wav or aac recording.")
                await run(phone.begin_upload, upload_id)
                started = True
                for piece in chunker.feed(bytes(head)):
                    await store(piece)
    if not parser.done or not seen_file:
        raise HTTPException(400, "No file field in the form.")
    if not size:
        raise HTTPException(400, "The file is empty.")
    rest = chunker.rest()
    if rest:
        await store(rest)
    sha = digest.hexdigest()
    earlier = await run(phone.find_duplicate, sha, upload_id)
    if earlier:
        await run(phone.drop_upload, upload_id)
        return {"size": size, "kind": kind, "duplicate": earlier}
    await run(phone.release_failed, sha)
    hint_name, hint_when = phone.from_filename(fields.get("filename") or filename)
    when = phone.parse_when(fields.get("recorded_at", "")) if fields.get("recorded_at") else None
    name = phone.clean_text(phone.strip_phones(fields.get("name", "")), 80) or hint_name
    await run(phone.finish_upload, upload_id, size, kind,
              phone.clean_text(phone.strip_phones(fields.get("title", "")), 200), name, when or hint_when, sha)
    return {"size": size, "kind": kind}


def parse_phone_id(value: str) -> str:
    provider, _, upload_id = (value or "").partition(":")
    if provider != phone.PROVIDER or not (0 < len(upload_id) <= 40) or not upload_id.isalnum():
        raise HTTPException(404, "Unknown recording.")
    return upload_id


@router.get("/valr/api/recording/audio")
async def recording_audio(request: Request, id: str = "") -> Response:
    await valr_dashboard.owner(request)
    upload_id = parse_phone_id(id)
    key = phone.token_key()
    async with pool().connection() as connection:
        await phone.setup(connection)
        row = await phone.upload(connection, upload_id)
    if row is None or row["status"] == "receiving" or not key:
        raise HTTPException(404, "Unknown recording.")
    if row.get("audio_deleted_at"):
        raise HTTPException(410, "The audio was deleted after a few days, as set up.")
    size = int(row["size"])
    try:
        span = phone.byte_range(request.headers.get("range", ""), size)
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end = span or (0, size - 1)

    async def get(idx: int) -> bytes | None:
        async with pool().connection() as connection:
            return await phone.chunk(connection, upload_id, idx)

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "private, no-store",
               "X-Content-Type-Options": "nosniff"}
    if span:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(phone.read_range(get, key, start, end), status_code=206 if span else 200,
                             media_type=phone.KINDS.get(row["kind"], "application/octet-stream"), headers=headers)


@router.get("/valr/api/phone/status")
async def phone_status(request: Request) -> JSONResponse:
    await valr_dashboard.owner(request)
    async with pool().connection() as connection:
        await phone.setup(connection)
        state = await phone.status(connection)
    return JSONResponse({"ok": True, **state}, headers=NO_CACHE)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["show", "rotate"]


@router.post("/valr/api/phone/token")
async def phone_token(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    try:
        body = TokenRequest.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(400, 'Expected {"action": "show" | "rotate"}.') from None
    async with pool().connection() as connection:
        await phone.setup(connection)
        try:
            token = await (phone.reveal(connection) if body.action == "show" else phone.rotate(connection))
        except phone.AlreadyShown:
            raise HTTPException(409, "The token was already shown once. Rotate it to get a new one "
                                     "(then paste the new one into the Shortcut).") from None
    logger.info("VALR upload token %s", "shown" if body.action == "show" else "rotated")
    return JSONResponse({"ok": True, "token": token, "message": "Copy it now: it won't be shown again."},
                        headers=NO_CACHE)


@router.get("/valr/settings/phone-help", response_class=HTMLResponse)
async def phone_help_page(request: Request) -> HTMLResponse:
    """Settings › Phone call recordings › "iPhone shortcut steps": docs/phone-recordings.md."""
    from app import phone_help, valr_page
    from app.valr_ui import attention, shell
    email = await valr_dashboard.owner(request)
    try:
        view = await valr_dashboard.state_view()
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR page unavailable: %s", type(error).__name__)
        return HTMLResponse(valr_page.error_page("The database didn't answer."), status_code=503, headers=NO_CACHE)
    from datetime import datetime
    from app.valr_ui import routes
    body = shell.page_head("iPhone shortcut steps", eyebrow="Settings", crumb="/valr/settings#phone-recordings|Settings",
                           sub="Send Truecaller recordings to VALR: one at a time, or all at once from a Home Screen button."
                           ) + f'<div class="prose card">{phone_help.page_body()}</div>'
    clock = valr_page.Clock(view.get("zone") or "UTC", datetime.fromisoformat(view["generated_at"]))
    notes = await routes.load_notes()
    html = shell.layout(view, email, valr_dashboard.csrf_token(email), "settings", "iPhone shortcut steps", body,
                        theme=request.query_params.get("theme"), bell=shell.bell(notes, attention.items(view), clock),
                        lead_dot=bool(notes["leads"]))
    return HTMLResponse(html, headers=NO_CACHE)


@router.get("/valr/downloads/mac")
async def mac_download(request: Request) -> FileResponse:
    """Settings › Phone call recordings › VALR for Mac › Download: the DMG (owner only), from
    downloads/ (app/mac_release.py)."""
    from app import mac_release
    await valr_dashboard.owner(request)
    release = mac_release.info()
    if release is None:
        raise HTTPException(404, "The Mac app isn't in this build.")
    return FileResponse(release["path"], media_type="application/x-apple-diskimage", filename=release["file"],
                        content_disposition_type="attachment",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
