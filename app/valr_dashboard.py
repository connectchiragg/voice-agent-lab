"""The VALR cockpit at /valr (valr.haciensus.com redirects / there).

Pages and data (any verified Access login): `GET /valr` and the other pages
(app/valr_ui/: Home, Interviews, Leads, Performance, Settings, System; the components in
app/valr_page.py), and `GET /valr/api/state`, the view as JSON (app/valr_view.py).

Actions (`POST /valr/api/...`, the owner only, see `action_owner`): `sync` starts a
calendar sync; `dials` {paused} sets the pause switch the briefing workflows check
before each heads-up or automatic dial; `telegram/code` issues a one-time `/start`
code (15 minutes) and its t.me deep link that links the owner's Telegram chat;
`telegram/test` sends the linked chat a test message; `slots/<ref>/refresh` re-runs one interview's research
with the caches bypassed. Each needs a same-origin JSON request carrying the page's
CSRF token (HMAC of the viewer's email and the issue time, 12 hours).

Who may see it is app/auth's decision (VALR_AUTH_MODE): the owner's VALR session (Sign in
with Google, VALR_OWNER_EMAILS), or a valid `Cf-Access-Jwt-Assertion` for the dashboard's Access
application (VALR_ACCESS_AUD, else CF_ACCESS_AUD), verified with app/access.py. app/auth's gate
checks it before any route runs; `viewer` checks it again.
"""
import hashlib
import hmac
import html
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import (access, auth, calendar_sync, callbacks, google_oauth, interview_store, interviews,
                 research, telegram_api, valr_view)
from app.interview_types import group_blocks, slot_workflow_id

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
verifier: access.AccessVerifier | None = None


def dashboard_host() -> str:
    return os.getenv("VALR_HOST", "valr.haciensus.com").strip().lower()


def is_dashboard_host(host: str | None) -> bool:
    return bool(host) and host.split(":")[0].strip().lower() == dashboard_host()  # type: ignore[union-attr]


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    global verifier
    audience = (os.getenv("VALR_ACCESS_AUD") or os.getenv("CF_ACCESS_AUD") or "").strip()
    if not audience:
        logger.info("VALR dashboard is closed: VALR_ACCESS_AUD is not set.")
        yield
        return
    team = os.getenv("CF_ACCESS_TEAM_DOMAIN", access.DEFAULT_TEAM_DOMAIN).strip().rstrip("/")
    verifier = access.AccessVerifier(team, audience)
    verifier.refresh_in_background()
    try:
        yield
    finally:
        await verifier.close()
        verifier = None


# Local dev preview (scripts/preview_ui.py) only: the Access check is skipped when the
# preview process enabled it (VALR_DEV_PREVIEW=1 and bound to 127.0.0.1) and the request
# itself comes from and is addressed to 127.0.0.1. The app's own startup never enables it.
PREVIEW_EMAIL = "preview@localhost"
LOOPBACK = "127.0.0.1"
dev_preview = False


def enable_dev_preview(bind_host: str) -> None:
    """Called by scripts/preview_ui.py before it serves; refuses anything but loopback."""
    global dev_preview
    if os.getenv("VALR_DEV_PREVIEW") != "1":
        raise RuntimeError("The dev preview needs VALR_DEV_PREVIEW=1.")
    if bind_host != LOOPBACK:
        raise RuntimeError("The dev preview only binds to 127.0.0.1.")
    dev_preview = True


def is_preview_request(request: Request) -> bool:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    return (dev_preview and os.getenv("VALR_DEV_PREVIEW") == "1"
            and request.client is not None and request.client.host == LOOPBACK
            and host == LOOPBACK)


async def viewer(request: Request) -> str:
    """The signed-in owner's email (app/auth: a VALR session or an Access login), or 403."""
    who = await auth.identify(request)
    if who is None:
        raise HTTPException(403, "Sign in to VALR first.")
    return who.email


def owner_emails() -> set[str]:
    return {email.strip().lower() for email in os.getenv("VALR_ADMIN_EMAILS", "").split(",")
            if email.strip()} | auth.owner_emails()


async def owner(request: Request) -> str:
    """The signed-in email, and (with VALR_ADMIN_EMAILS or VALR_OWNER_EMAILS set) one of
    them; else 403."""
    email = await viewer(request)
    allowed = owner_emails()
    if allowed and email.lower() not in allowed:
        raise HTTPException(403, "Only the owner can do this.")
    return email


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


NO_CACHE = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


@router.get("/oauth/google/start")
async def google_start(request: Request) -> RedirectResponse:
    await owner(request)
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None:
        raise HTTPException(503, "Google OAuth is not configured (client id, secret, VALR_TOKEN_KEY).")
    return RedirectResponse(google_oauth.consent_url(settings, google_oauth.make_state(
        settings.token_key)), status_code=303, headers=NO_CACHE)


@router.get("/oauth/google/callback", response_class=HTMLResponse)
async def google_callback(request: Request, code: str = "", state: str = "",
                          error: str = "") -> Response:
    if auth.is_login_state(state):  # Signing in to VALR (app/auth), not the Gmail grant.
        return await auth.finish_login(code, state, error)
    await owner(request)
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None or research.pool is None:
        raise HTTPException(503, "Google OAuth or Postgres is not configured.")
    if error or not code:
        return HTMLResponse(f"<p>Gmail was not connected: {e(error or 'no code')}.</p>",
                            status_code=400, headers=NO_CACHE)
    if not google_oauth.check_state(settings.token_key, state):
        raise HTTPException(400, "This consent link expired; start again from /oauth/google/start.")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            refresh, scope, account = await google_oauth.exchange_code(settings, code, http)
    except (ValueError, httpx.HTTPError) as failure:
        logger.warning("Gmail consent failed: %s", type(failure).__name__)
        return HTMLResponse(f"<p>Gmail was not connected: {e(failure)}.</p>", status_code=400,
                            headers=NO_CACHE)
    if settings.account and account != settings.account:
        return HTMLResponse(f"<p>That was {e(account) or 'another account'}; please connect "
                            f"{e(settings.account)}.</p>", status_code=400, headers=NO_CACHE)
    async with research.pool.connection() as connection:
        await google_oauth.setup(connection)
        await google_oauth.store(connection, settings, account, refresh, scope)
    logger.info("Gmail connected for VALR (read-only)")
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'>"
                        f"<p style='font:16px system-ui;margin:24px'>Gmail connected read-only for "
                        f"{e(account)}. <a href='/valr'>Back to the dashboard</a></p>",
                        headers=NO_CACHE)


@router.get("/valr", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Home: the leads map and "right now" (the other pages: app/valr_ui/routes.py)."""
    from app.valr_ui import routes
    return await routes.render(request, "home")


@router.get("/valr/api/state")
async def state(request: Request) -> JSONResponse:
    """The same view as the page, as JSON (the page polls it for live status)."""
    await viewer(request)
    return JSONResponse(await state_view(), headers=NO_CACHE)


async def state_view() -> dict[str, Any]:
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    return await valr_view.collect(research.pool)


# Actions: POST, owner only, CSRF-checked.

CSRF_TTL_S = 12 * 3600
_process_secret = secrets.token_bytes(32)


def csrf_secret() -> bytes:
    """Derived from VALR_TOKEN_KEY when set (tokens survive a restart), else per process."""
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    return hmac.new(key.encode(), b"valr-dashboard-csrf", hashlib.sha256).digest() if key \
        else _process_secret


def csrf_token(email: str, now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    mac = hmac.new(csrf_secret(), f"{email.lower()}|{issued}".encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{mac}"


def csrf_ok(email: str, token: str | None, now: float | None = None) -> bool:
    issued, _, mac = (token or "").partition(".")
    if not issued.isdigit() or not mac:
        return False
    age = (time.time() if now is None else now) - int(issued)
    expected = hmac.new(csrf_secret(), f"{email.lower()}|{issued}".encode(), hashlib.sha256).hexdigest()
    return 0 <= age <= CSRF_TTL_S and hmac.compare_digest(mac, expected)


async def action_owner(request: Request) -> str:
    """Owner's Access login, a same-origin JSON request and a valid CSRF token; else 403."""
    email = await owner(request)
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-site request refused.")
    if origin and origin.split("://", 1)[-1] != host:
        raise HTTPException(403, "Cross-origin request refused.")
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "Expected JSON.")
    if not csrf_ok(email, request.headers.get("x-csrf-token")):
        raise HTTPException(403, "The page is stale: reload it and try again.")
    return email


class DialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paused: bool


@router.post("/valr/api/sync")
async def sync_calendar(request: Request) -> JSONResponse:
    await action_owner(request)
    started = await interviews.sync_now("dashboard")
    if not started:
        raise HTTPException(503, "Temporal is unavailable: the sync was not started.")
    logger.info("VALR calendar sync requested from the dashboard")
    return JSONResponse({"ok": True, "message": "Calendar sync started."}, headers=NO_CACHE)


@router.post("/valr/api/dials")
async def set_dials(request: Request) -> JSONResponse:
    await action_owner(request)  # Before the body is even parsed.
    try:
        body = DialsRequest.model_validate_json(await request.body())
    except ValidationError as error:
        raise HTTPException(400, "Expected {\"paused\": true|false}.") from error
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    async with research.pool.connection() as connection:
        await interview_store.setup(connection)
        await interview_store.set_flag(connection, interview_store.DIALS_PAUSED, body.paused)
    logger.info("VALR automatic briefing calls %s from the dashboard",
                "paused" if body.paused else "resumed")
    return JSONResponse({"ok": True, "paused": body.paused,
                         "message": "Automatic calls paused." if body.paused
                         else "Automatic calls resumed."}, headers=NO_CACHE)


class ReachRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["telegram", "call"]


@router.post("/valr/api/reach")
async def set_reach(request: Request) -> JSONResponse:
    """Settings: a Telegram heads-up (default) or the automatic T-30 call. Stored in the
    database; overrides VALR_AUTO_DIAL. Either way one attempt, no retries."""
    await action_owner(request)
    try:
        body = ReachRequest.model_validate_json(await request.body())
    except ValidationError as error:
        raise HTTPException(400, "Expected {\"mode\": \"telegram\"|\"call\"}.") from error
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    async with research.pool.connection() as connection:
        await interview_store.setup(connection)
        await interview_store.set_flag(connection, interview_store.AUTO_DIAL, body.mode == "call")
    logger.info("VALR reach mode set to %s from the dashboard", body.mode)
    return JSONResponse({"ok": True, "mode": body.mode,
                         "message": ("VALR will call you 30 minutes before each interview, once."
                                     if body.mode == "call" else
                                     "VALR will send a Telegram heads-up; no automatic calls.")},
                        headers=NO_CACHE)


class PrepCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    item: str = Field(min_length=1, max_length=400)
    done: bool


@router.post("/valr/api/prep")
async def set_prep(request: Request) -> JSONResponse:
    """Tick (or untick) one prep-checklist line; kept on the server so Home can show it done."""
    await action_owner(request)
    try:
        body = PrepCheckRequest.model_validate_json(await request.body())
    except ValidationError as error:
        raise HTTPException(400, "Expected {ref, item, done}.") from error
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    async with research.pool.connection() as connection:
        await interview_store.setup(connection)
        await interview_store.set_prep_check(connection, body.ref, interview_store.prep_item(body.item), body.done)
    return JSONResponse({"ok": True, "done": body.done, "message": "Ticked." if body.done else "Unticked."},
                        headers=NO_CACHE)


@router.post("/valr/api/telegram/code")
async def telegram_code(request: Request) -> JSONResponse:
    """A one-time code: the owner sends `/start <code>` to the bot to link his chat."""
    await action_owner(request)
    if not telegram_api.configured():
        raise HTTPException(503, "Telegram is not set up yet (deploy/install-telegram.sh).")
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    async with research.pool.connection() as connection:
        await telegram_api.setup(connection)
        code = await telegram_api.new_code(connection)
    logger.info("VALR Telegram link code issued from the dashboard")
    minutes = int(telegram_api.CODE_TTL.total_seconds() // 60)
    username = await telegram_api.bot_username()
    link = telegram_api.deep_link(username, code) if username else None
    return JSONResponse({"ok": True, "command": f"/start {code}", "expires_minutes": minutes,
                         "link": link, "bot": f"@{username}" if username else None,
                         "message": (f"Tap \"Open Telegram\" and press Start within {minutes} minutes."
                                     if link else
                                     f"Send /start {code} to the VALR bot within {minutes} minutes.")},
                        headers=NO_CACHE)


@router.post("/valr/api/telegram/test")
async def telegram_test(request: Request) -> JSONResponse:
    """Send the linked owner chat a short test message, so he can see the link works."""
    await action_owner(request)
    client = telegram_api.bot()
    if client is None or not telegram_api.configured():
        raise HTTPException(503, "Telegram is not set up yet.")
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    async with research.pool.connection() as connection:
        await telegram_api.setup(connection)
        chat = await telegram_api.owner_chat(connection)
    if chat is None:
        raise HTTPException(409, "Telegram isn't connected yet: tap \"Open Telegram & connect\" first.")
    try:
        await client.send(chat, "VALR test message: heads-ups will arrive here 30 minutes before "
                                "each interview.")
    except telegram_api.TelegramError as error:
        logger.warning("VALR Telegram test message failed: %s", type(error).__name__)
        raise HTTPException(502, "Telegram didn't accept the message. Try again in a minute.") from None
    logger.info("VALR Telegram test message sent from the dashboard")
    return JSONResponse({"ok": True, "message": "Test message sent. Check Telegram."}, headers=NO_CACHE)


@router.post("/valr/api/call-now")
async def call_me_now(request: Request) -> JSONResponse:
    """Call the owner now with the brief for his next interview (research it first if needed);
    with nothing coming up, a general chat. Rings once, no retries."""
    await action_owner(request)
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    client = await callbacks.temporal()
    if client is None:
        raise HTTPException(503, "Temporal is unavailable.")
    from app import call_now
    from app.interview_worker import owner_key, owner_phone
    if not owner_phone():
        raise HTTPException(409, "Add your phone in Settings first, or use Talk here.")
    now = datetime.now(timezone.utc)
    async with research.pool.connection() as connection:
        await calendar_sync.setup(connection)
        events = await calendar_sync.upcoming(connection, now, now + valr_view.LOOK_AHEAD)
    event = call_now.pick(events, "next", now)
    if event is None:  # Nothing coming up: still ring him, for a general chat.
        await call_now.call_talk_now(client, owner_key(), owner_phone(), rpc_timeout=callbacks.RPC_TIMEOUT)
        logger.info("VALR call-now requested from the dashboard (no interview: talk mode)")
        return JSONResponse({"ok": True, "mode": "talk", "message": "Calling you now. Nothing's coming up, "
                             "so it's a general chat."}, headers=NO_CACHE)
    ready, _ = await interviews.split_ready([event])
    await call_now.call_now(client, event, owner_key(), owner_phone(), bool(ready),
                            rpc_timeout=callbacks.RPC_TIMEOUT)
    logger.info("VALR call-now requested from the dashboard (brief %s)", "ready" if ready else "researching")
    return JSONResponse({"ok": True, "mode": "brief", "message": "Calling you now." if ready else
                         "Researching first; VALR calls you within five minutes."}, headers=NO_CACHE)


@router.post("/valr/api/slots/{ref}/refresh")
async def refresh_slot(request: Request, ref: str) -> JSONResponse:
    """Research one upcoming interview again now, caches bypassed. `ref`: its slot workflow id."""
    await action_owner(request)
    if not ref.startswith("slot-") or len(ref) > 40:
        raise HTTPException(404, "Unknown interview.")
    if research.pool is None:
        raise HTTPException(503, "Postgres is unavailable.")
    client = await callbacks.temporal()
    if client is None:
        raise HTTPException(503, "Temporal is unavailable.")
    now = datetime.now(timezone.utc)
    async with research.pool.connection() as connection:
        await calendar_sync.setup(connection)
        events = await calendar_sync.upcoming(connection, now, now + valr_view.LOOK_AHEAD)
    event = next((e for e in events if slot_workflow_id(e.key) == ref), None)
    if event is None:
        raise HTTPException(404, "That interview is not upcoming.")
    blocks = {key: block.block_id for block in group_blocks(events) for key in block.slot_keys}
    from app.interview_worker import owner_key
    await interviews.start_slot_now(client, event, owner_key(), blocks.get(event.key, ""), True)
    logger.info("VALR slot research refresh requested from the dashboard")
    return JSONResponse({"ok": True, "message": "Research restarted: R1, R2 and R3 run now."},
                        headers=NO_CACHE)
