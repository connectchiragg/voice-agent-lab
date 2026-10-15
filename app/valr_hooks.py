"""VALR webhooks: Google Calendar push notifications, and the optional context push.

- `POST /hooks/gcal` (valr.haciensus.com, bypassed by Cloudflare Access: Google cannot
  log in). A notification carries no event data, only "something changed": the request
  must name our current channel (X-Goog-Channel-ID), its secret token
  (X-Goog-Channel-Token, compared in constant time) and its resource
  (X-Goog-Resource-ID), all stored by the worker when it opened the channel. Then a
  calendar sync is started (signal-with-start: a burst of notifications makes one more
  pass). The first `sync` message of a new channel is only acknowledged.
- `POST /valr/context` (behind Access): a Mac-side job (not built yet) may push
  per-interview email context: JSON {event_key?, company?, subject?, summary?, excerpt?,
  notes?}, at most 8 KB. Allowed with the owner's Access login, or with
  `Authorization: Bearer <VALR_CONTEXT_TOKEN>` (for a job using an Access service token).
  Stored encrypted (VALR_TOKEN_KEY); R1 reads it for that event or company.
"""
import hmac
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Request

from app import calendar_sync, google_oauth, interview_store, interviews, research, valr_dashboard

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
MAX_PUSH_BYTES = 8192


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


@router.post("/hooks/gcal")
async def gcal(request: Request) -> dict[str, bool]:
    if research.pool is None:
        raise HTTPException(503, "Unavailable.")
    channel = request.headers.get("x-goog-channel-id")
    async with research.pool.connection() as connection:
        await calendar_sync.setup(connection)
        state = await calendar_sync.load_state(connection)
    if not (same(channel, state.get("channel_id"))
            and same(request.headers.get("x-goog-channel-token"), state.get("channel_token"))
            and same(request.headers.get("x-goog-resource-id"), state.get("resource_id"))):
        logger.warning("Calendar notification refused: unknown channel or token")
        raise HTTPException(403, "Unknown channel.")
    kind = request.headers.get("x-goog-resource-state", "")
    if kind != "sync":
        await interviews.sync_now("push")
    return {"ok": True}


@router.post("/valr/context")
async def push_context(request: Request) -> dict[str, bool]:
    token = os.getenv("VALR_CONTEXT_TOKEN", "").strip()
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not (token and same(bearer, token)):
        await valr_dashboard.owner(request)
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    if research.pool is None or not key:
        raise HTTPException(503, "Context push is not configured.")
    body = await request.body()
    if len(body) > MAX_PUSH_BYTES:
        raise HTTPException(413, "At most 8 KB.")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError as error:
        raise HTTPException(400, "Expected a JSON object.") from error
    async with research.pool.connection() as connection:
        await interview_store.setup(connection)
        await interview_store.push_context(connection, google_oauth.encrypt(key, json.dumps(data)),
                                           str(data.get("event_key", ""))[:200],
                                           str(data.get("company", ""))[:120])
    logger.info("VALR context pushed")
    return {"ok": True}
