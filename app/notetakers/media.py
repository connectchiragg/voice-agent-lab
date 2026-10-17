"""Playing a past interview inside VALR: the provider's signed download, cached.

Fathom (its official download API, app/notetakers/fathom.py) and Fireflies (the transcript's
signed `video_url`, else `audio_url`, app/notetakers/fireflies.py: Pro plans and up get audio,
Business and up video; the links live 24 hours) offer one. The page asks
`GET /valr/api/recording/media?id=fathom:<id>` (owner only) and gets
{status, url?, expires_at?, content_type?, message?}:

- `completed`: `url` is Fathom's signed MP4 (video, or audio for an audio-only recording);
  the browser streams it straight from Fathom's storage with range requests, so nothing
  passes through VALR. A cached URL is reused while it has at least 30 minutes left.
- `processing`: Fathom is still making the video; the page asks again (every 5 s, backing off).
- `busy`: Fathom rate-limited us (429); try again in a minute.
- `unavailable` / `failed`: a sentence for the owner; the page keeps "Open in Fathom".

The download is cached in `recording_media` (app/notetakers/store.py), the URL sealed with
VALR_TOKEN_KEY. Neither the signed URL nor the key is ever logged.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from app import google_oauth
from app.notetakers import fathom, fireflies, store

logger = logging.getLogger("uvicorn.error")

REUSE_MARGIN = timedelta(minutes=30)      # Reuse a signed URL only with this much life left.
STALE_PROCESSING = timedelta(minutes=20)  # A download "processing" this long is requested again.
UNAVAILABLE_FOR = timedelta(hours=6)      # Don't ask Fathom again about a 403/404/422 sooner.
REFRESH_AFTER = timedelta(seconds=60)     # ?refresh=1 (the player's URL failed) at most this often.
# Where Fathom's signed downloads live (measured 2026-10-16, scripts/fathom_media_smoke.py):
# video on streamkit, audio-only recordings on fathom-recording-data.
MEDIA_HOSTS = frozenset({"streamkit.storage.googleapis.com", "fathom-recording-data.storage.googleapis.com"})
MESSAGES = {
    401: "Fathom refused the API key. Reconnect Fathom in Settings.",
    403: "Fathom says this recording isn't downloadable for your account.",
    404: "Fathom can't find this recording any more.",
    422: "Fathom has no video or audio for this recording.",
    429: "Fathom is busy, try again in a minute.",
}
UNAVAILABLE = (403, 404, 422)
FAILED = {"generation_failed": "Fathom couldn't prepare the video. Try again later, or open it in Fathom.",
          "generation_timeout": "Fathom took too long to prepare the video. Try again in a few minutes."}
PREPARING = "Preparing the video… (first play can take a minute or two)"
# Fireflies' signed media: its own storage or its CDN (not measured: no Fireflies key was
# available locally on 2026-10-17), https only.
FIREFLIES_HOST_SUFFIXES = (".fireflies.ai", ".amazonaws.com", ".cloudfront.net", ".googleapis.com")
FIREFLIES_LIFETIME = timedelta(hours=24)
FIREFLIES_NONE = ("Fireflies gives no audio or video for this recording through its API: audio needs a "
                  "Fireflies Pro plan or higher, video a Business plan with video recording on.")


def fireflies_allowed(url: str) -> bool:
    parts = urlsplit(url or "")
    host = parts.hostname or ""
    return parts.scheme == "https" and any(host.endswith(s) or host == s[1:] for s in FIREFLIES_HOST_SUFFIXES)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _parse(value: Any) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00"))) if value else None
    except ValueError:
        return None


def allowed(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme == "https" and (parts.hostname or "") in MEDIA_HOSTS


def answer(status: str, message: str = "", **extra: Any) -> dict[str, Any]:
    out = {"status": status, **{k: v for k, v in extra.items() if v is not None}}
    if message:
        out["message"] = message
    return out


def reusable(row: dict[str, Any] | None, now: datetime) -> bool:
    expires = _aware((row or {}).get("expires_at"))
    return bool(row and row["status"] == "completed" and row.get("url") and expires
                and expires - now >= REUSE_MARGIN)


async def resolve(connection: Any, provider: str, external_id: str, http: httpx.AsyncClient, key: str | None,
                  token_key: str, now: datetime | None = None, refresh: bool = False) -> dict[str, Any]:
    """The playable media of one recording, from the cache or Fathom (see the module docstring)."""
    if provider == "fireflies":
        return await resolve_fireflies(connection, external_id, http, key, token_key, now, refresh)
    if provider != "fathom":
        return answer("unavailable", "This recording opens in its notetaker.")
    if not key:
        return answer("unavailable", "Connect Fathom in Settings to play recordings here.")
    now = now or datetime.now(timezone.utc)
    await store.setup(connection)
    row = await store.get_media(connection, provider, external_id)
    age = now - (_aware(row["updated_at"]) or now) if row else None
    if row and reusable(row, now) and not (refresh and age >= REFRESH_AFTER):
        url = _unseal(token_key, row["url"])
        if url:
            return _completed(url, row["expires_at"], row["content_type"])
    if row and row["status"] == "unavailable" and age < UNAVAILABLE_FOR and not refresh:
        return answer("unavailable", row.get("error") or MESSAGES[422])
    if row and row["status"] == "processing" and row.get("download_id") and age < STALE_PROCESSING:
        response = await fathom.download_status(key, external_id, row["download_id"], http)
        if response.status_code == 200:
            return await _record(connection, external_id, response.json(), token_key, now)
        if response.status_code == 429:
            return answer("busy", MESSAGES[429])
        # 404 (the download is gone) or anything else: ask for a new one below.
    return await request(connection, external_id, http, key, token_key, now)


async def resolve_fireflies(connection: Any, external_id: str, http: httpx.AsyncClient, key: str | None,
                            token_key: str, now: datetime | None = None, refresh: bool = False) -> dict[str, Any]:
    """Fireflies' signed video (else audio), cached sealed until 30 minutes before it expires."""
    if not key:
        return answer("unavailable", "Connect Fireflies in Settings to play recordings here.")
    now = now or datetime.now(timezone.utc)
    await store.setup(connection)
    row = await store.get_media(connection, "fireflies", external_id)
    age = now - (_aware(row["updated_at"]) or now) if row else None
    if row and reusable(row, now) and not (refresh and age >= REFRESH_AFTER):
        url = _unseal(token_key, row["url"])
        if url:
            return _completed(url, row["expires_at"], row["content_type"])
    if row and row["status"] == "unavailable" and age < UNAVAILABLE_FOR and not refresh:
        return answer("unavailable", row.get("error") or FIREFLIES_NONE)
    try:
        urls = await fireflies.media_urls(key, external_id, http)
    except base_errors() as error:
        logger.info("Fireflies media unavailable: %s", type(error).__name__)
        return answer("failed", "Fireflies didn't answer. Try again in a minute, or open it in Fireflies.")
    url, content_type = urls["video_url"], "video/mp4"
    if not url:
        url, content_type = urls["audio_url"], "audio/mpeg"
    if not url:
        await store.save_media(connection, "fireflies", external_id, {"status": "unavailable", "error": FIREFLIES_NONE})
        return answer("unavailable", FIREFLIES_NONE)
    if not fireflies_allowed(url):
        logger.warning("Fireflies media on an unexpected host: %s", urlsplit(url).hostname)
        return answer("failed", "Fireflies sent the recording from an unexpected place, so VALR won't play it.")
    expires = now + FIREFLIES_LIFETIME
    if token_key:
        await store.save_media(connection, "fireflies", external_id, {
            "status": "completed", "url": google_oauth.encrypt(token_key, url), "content_type": content_type,
            "expires_at": expires})
    return _completed(url, expires, content_type)


def base_errors() -> tuple[type[BaseException], ...]:
    from app.notetakers.base import KeyRefused, ProviderError
    return (KeyRefused, ProviderError, httpx.HTTPError)


async def request(connection: Any, external_id: str, http: httpx.AsyncClient, key: str, token_key: str,
                  now: datetime) -> dict[str, Any]:
    response = await fathom.request_download(key, external_id, http)
    code = response.status_code
    if code in (200, 201, 202):
        return await _record(connection, external_id, response.json(), token_key, now)
    logger.info("Fathom download refused: HTTP %s", code)
    if code in UNAVAILABLE:
        await store.save_media(connection, "fathom", external_id, {"status": "unavailable", "error": MESSAGES[code]})
        return answer("unavailable", MESSAGES[code])
    if code == 429:
        return answer("busy", MESSAGES[429])
    if code == 401:
        return answer("unavailable", MESSAGES[401])
    return answer("failed", "Fathom didn't answer. Try again in a minute.")


async def _record(connection: Any, external_id: str, data: dict[str, Any], token_key: str,
                  now: datetime) -> dict[str, Any]:
    status = data.get("status")
    download_id = str(data.get("download_id") or "") or None
    if status == "processing":
        await store.save_media(connection, "fathom", external_id, {"download_id": download_id, "status": "processing"})
        return answer("processing", PREPARING)
    if status == "completed":
        media = data.get("video") or data.get("audio") or {}
        url = str(media.get("url") or "")
        expires = _parse(media.get("expires_at")) or now + timedelta(hours=23)
        if not allowed(url):
            logger.warning("Fathom download on an unexpected host: %s", urlsplit(url).hostname)
            return answer("failed", "Fathom sent the video from an unexpected place, so VALR won't play it.")
        content_type = str(media.get("content_type") or "video/mp4")[:60]
        if token_key:  # Cached only when it can be sealed.
            await store.save_media(connection, "fathom", external_id, {
                "download_id": download_id, "status": "completed",
                "url": google_oauth.encrypt(token_key, url), "content_type": content_type,
                "size": media.get("file_size_bytes") if isinstance(media.get("file_size_bytes"), int) else None,
                "expires_at": expires})
        return _completed(url, expires, content_type)
    reason = str(data.get("failure_reason") or "")
    await store.save_media(connection, "fathom", external_id, {"download_id": download_id,
                                                               "status": status or "failed", "error": reason[:60]})
    if status == "expired":
        return answer("expired", "That video link expired; ask again.")
    return answer("failed", FAILED.get(reason, "Fathom couldn't prepare the video. Try again later."))


def _completed(url: str, expires: Any, content_type: str | None) -> dict[str, Any]:
    expires = _aware(expires) if isinstance(expires, datetime) else _parse(expires)
    return answer("completed", url=url, expires_at=expires.isoformat() if expires else None,
                  content_type=content_type or "video/mp4")


def _unseal(token_key: str, sealed: str | None) -> str:
    if not (token_key and sealed):
        return ""
    try:
        return google_oauth.decrypt(token_key, sealed)
    except google_oauth.ReconnectNeeded:
        return ""


# Pre-warm: a new Fathom interview starts its download in the background, so the video is
# ready when the owner opens it. One request, no polling; a 429 or any failure is skipped.

_tasks: set[asyncio.Task] = set()


def prewarm_enabled() -> bool:
    return os.getenv("VALR_MEDIA_PREWARM", "1") == "1"


def prewarm(provider: str, external_id: str) -> None:
    if provider != "fathom" or not prewarm_enabled():
        return
    try:
        task = asyncio.get_running_loop().create_task(_prewarm(external_id))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _prewarm(external_id: str) -> None:
    from app import notetakers, research
    if research.pool is None:
        return
    try:
        async with research.pool.connection() as connection, notetakers.http_client() as http:
            key = await notetakers.api_key(connection, "fathom")
            if key:
                result = await resolve(connection, "fathom", external_id, http, key, notetakers.token_key())
                logger.info("Fathom download pre-warm: %s", result["status"])
    except Exception as error:  # noqa: BLE001 - best effort; the page asks again when opened.
        logger.info("Fathom download pre-warm skipped: %s", type(error).__name__)
