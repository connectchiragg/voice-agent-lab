"""The notification bell's API (owner only, app/auth/gate.py: everything under /valr).

- `GET /valr/api/notifications[?unread=1]`: {ok, unread, items: [{id, kind, label, importance,
  title, body, company, link, gmail_url, created_at, day, read}], switches}. The last 30 days,
  newest first. The bell polls it every 60 s.
- `POST /valr/api/notifications/read` {ids: [..]}, {lead: "<lead id>"} or {all: true}: marks them read (CSRF, same
  origin, JSON: app/valr_dashboard.action_owner). The page marks an item read when it is
  clicked, not when the list opens.
- `GET /valr/api/notifications/latency`: {ok, count, p50_s, p95_s, max_s, push_count, push_p50_s,
  push_p95_s, sla_p50_s, sla_p95_s, met, window_h}: how fast new emails reached the bell
  (Gmail's arrival time -> the item) and the phone (-> the push) over the last 24 hours, against
  the owner's SLA (p95 10 s, p50 5 s; notifications.latency_report).
- `POST /valr/api/notifications/settings` {immediate?, digest?, quiet?, delivery?}: the Telegram
  switches (immediate alerts, the 09:00 digest, quiet hours 23:00-08:00) and where alerts go:
  "both" (Telegram + phone push), "push" (phone push only) or "telegram" (app/webpush.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import notifications, research, valr_dashboard

router = APIRouter()
NO_CACHE = valr_dashboard.NO_CACHE


def pool():
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    return research.pool


LIST_MAX = 500


@router.get("/valr/api/notifications")
async def listing(request: Request, unread: int = 0) -> JSONResponse:
    await valr_dashboard.owner(request)
    now = datetime.now(timezone.utc)
    since = now - notifications.KEEP_FOR
    async with pool().connection() as connection:
        store = notifications.Store(connection)
        await store.setup()
        rows = await store.listing(bool(unread), since, LIST_MAX)  # Every unread item of 30 days fits.
        count = await store.unread_count(since)
        switches = await store.switches()
    return JSONResponse({"ok": True, "unread": count, "items": [notifications.view(r, now) for r in rows],
                         "switches": switches.view()}, headers=NO_CACHE)


@router.get("/valr/api/notifications/latency")
async def latency(request: Request) -> JSONResponse:
    await valr_dashboard.owner(request)
    now = datetime.now(timezone.utc)
    async with pool().connection() as connection:
        store = notifications.Store(connection)
        await store.setup()
        report = await notifications.latency_report(store, now)
    return JSONResponse({"ok": True, **report}, headers=NO_CACHE)


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[int] = Field(default_factory=list, max_length=500)
    lead: str = Field(default="", max_length=64, pattern=r"^[\w-]*$")
    all: bool = False


@router.post("/valr/api/notifications/read")
async def mark_read(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    try:
        body = ReadRequest.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(400, 'Expected {"ids": [..]} or {"all": true}.') from None
    if not body.all and not body.ids and not body.lead:
        raise HTTPException(400, 'Expected {"ids": [..]}, {"lead": ".."} or {"all": true}.')
    now = datetime.now(timezone.utc)
    async with pool().connection() as connection:
        store = notifications.Store(connection)
        await store.setup()
        if body.lead and not body.all:
            changed = await store.mark_lead_read(body.lead, now)
        else:
            changed = await store.mark_read(None if body.all else body.ids, now)
        count = await store.unread_count(now - notifications.KEEP_FOR)
    return JSONResponse({"ok": True, "marked": changed, "unread": count}, headers=NO_CACHE)


class SwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    immediate: bool | None = None
    digest: bool | None = None
    quiet: bool | None = None
    delivery: Literal["both", "push", "telegram"] | None = None


@router.post("/valr/api/notifications/settings")
async def switches(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    try:
        body = SwitchRequest.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(400, 'Expected {immediate?, digest?, quiet?} as true or false, or '
                                 '{delivery: "both" | "push" | "telegram"}.') from None
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    if not values:
        raise HTTPException(400, "Nothing to change.")
    async with pool().connection() as connection:
        store = notifications.Store(connection)
        await store.setup()
        await store.save_switches(**values)
        now = await store.switches()
    return JSONResponse({"ok": True, "switches": now.view()}, headers=NO_CACHE)
