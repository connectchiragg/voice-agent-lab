"""The installable app and phone push: the manifest, the service worker and the push API.

Public (app/auth/gate.py): iOS fetches the manifest and the icons without the session cookie
in some cases, and they hold nothing private.
- `GET /manifest.webmanifest`: name, colours, icons (static/icons/), start_url /valr.
- `GET /sw.js`: static/sw.js from the root, `Service-Worker-Allowed: /`, never cached long,
  so a deploy reaches the phone on its next launch.

Owner only (the session or Access login; POSTs also need the CSRF token, same origin, JSON):
- `GET /valr/api/push`: {configured, public_key, devices, delivery}.
- `POST /valr/api/push/subscribe` {endpoint, keys: {p256dh, auth}}: stores this device.
- `POST /valr/api/push/unsubscribe` {endpoint}: forgets it.
- `POST /valr/api/push/test`: "VALR test notification ✅" to every subscribed device.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import notifications, research, valr_dashboard, webpush

router = APIRouter()
NO_CACHE = valr_dashboard.NO_CACHE
STATIC = Path(__file__).resolve().parent.parent / "static"
THEME = "#05070c"       # --char in static/theme.css: the app's background, and the status bar.
BACKGROUND = "#05070c"

MANIFEST = {
    "name": "VALR",
    "short_name": "VALR",
    "description": "Your job search: interviews, leads and alerts.",
    "id": "/valr",
    "start_url": "/valr",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "theme_color": THEME,
    "background_color": BACKGROUND,
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        {"src": "/static/icons/apple-touch-icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any"},
    ],
}


@router.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    return JSONResponse(MANIFEST, media_type="application/manifest+json",
                        headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


def pool():
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    return research.pool


async def state(connection) -> dict:
    store = webpush.Store(connection)
    await store.setup()
    switches_store = notifications.Store(connection)
    await switches_store.setup()
    switches = await switches_store.switches()
    key = webpush.public_key()
    return {"ok": True, "configured": bool(key), "public_key": key, "devices": await store.count(),
            "delivery": switches.delivery}


@router.get("/valr/api/push")
async def status(request: Request) -> JSONResponse:
    await valr_dashboard.owner(request)
    async with pool().connection() as connection:
        return JSONResponse(await state(connection), headers=NO_CACHE)


class Keys(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class Subscription(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)  # The browser adds expirationTime.
    endpoint: str = Field(max_length=1024)
    keys: Keys


class Unsubscribe(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    endpoint: str = Field(max_length=1024)


@router.post("/valr/api/push/subscribe")
async def subscribe(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    try:
        body = Subscription.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(400, "Expected a push subscription {endpoint, keys: {p256dh, auth}}.") from None
    if not webpush.valid_endpoint(body.endpoint):
        raise HTTPException(400, "That push service isn't one VALR sends to.")
    if not webpush.public_key():
        raise HTTPException(503, "Phone push isn't set up on the server yet (VALR_VAPID_* keys).")
    async with pool().connection() as connection:
        store = webpush.Store(connection)
        await store.setup()
        await store.save(body.endpoint, body.keys.p256dh, body.keys.auth, request.headers.get("user-agent", ""))
        return JSONResponse(await state(connection), headers=NO_CACHE)


@router.post("/valr/api/push/unsubscribe")
async def unsubscribe(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    try:
        body = Unsubscribe.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(400, 'Expected {"endpoint": "..."}.') from None
    async with pool().connection() as connection:
        store = webpush.Store(connection)
        await store.setup()
        await store.remove(body.endpoint)
        return JSONResponse(await state(connection), headers=NO_CACHE)


TEST = webpush.payload("VALR test notification ✅", "Phone notifications work. Tap to open VALR.",
                       "/valr/settings#phone-push", "valr-test")


@router.post("/valr/api/push/test")
async def test(request: Request) -> JSONResponse:
    await valr_dashboard.action_owner(request)
    async with pool().connection() as connection:
        sender = await webpush.sender(connection)
        if sender is None:
            raise HTTPException(503, "Phone push isn't set up on the server yet (VALR_VAPID_* keys).")
        if not await sender.subscribers():
            raise HTTPException(409, "No device is subscribed yet: tap Enable notifications first.")
        result = await sender.send(TEST)
        out = await state(connection)
    message = (f"Sent to {result.delivered} device{'s' if result.delivered != 1 else ''}." if result.delivered
               else "Not delivered: the push service refused it. Try Turn off, then Enable again.")
    return JSONResponse(out | {"delivered": result.delivered, "gone": result.gone, "failed": result.failed,
                               "message": message}, headers=NO_CACHE)
