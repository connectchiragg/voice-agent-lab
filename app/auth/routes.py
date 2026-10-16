"""Sign-in routes: /login, /login/google, the login half of /oauth/google/callback, /logout, /private.

The login reuses the Gmail/Calendar OAuth client and its redirect URI
(GOOGLE_OAUTH_REDIRECT_URI, app/google_oauth.py), so nothing new is registered with Google.
The callback tells the two apart by the state: a login state starts with "login." and
is a one-time row in Postgres; a Gmail/Calendar state is the signed one of google_oauth.
A login never stores, changes or re-requests the Gmail/Calendar grant.
"""
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import google_oauth
from app.auth import core, gate, pages, store

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
NO_CACHE = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def to_login(error: str) -> RedirectResponse:
    return RedirectResponse(f"/login?error={error}", status_code=303, headers=NO_CACHE)


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str = "", error: str = "", signed_out: str = "",
                theme: str = "") -> Response:
    target = core.safe_next(next)
    who = await gate.identify(request) if not error and core.mode() != "access" else None
    if who is not None and who.via != "preview":
        return RedirectResponse(target, status_code=303, headers=NO_CACHE)  # Already in.
    return HTMLResponse(pages.login_page(target, error[:20], signed_out == "1", theme or None),
                        headers=NO_CACHE)


@router.get("/login/google")
async def start(next: str = "") -> RedirectResponse:
    """A one-time state and nonce, then Google's account chooser (openid email profile)."""
    if core.mode() == "access":
        return to_login("off")
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None:
        return to_login("config")
    backend = store.backend()
    if backend is None:
        return to_login("db")
    state, nonce = core.new_state(), core.new_nonce()
    try:
        await backend.save_state(state, nonce, core.safe_next(next))
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR sign-in could not start: %s", type(error).__name__)
        return to_login("db")
    return RedirectResponse(core.login_url(settings.client_id, settings.redirect_uri, state, nonce),
                            status_code=303, headers=NO_CACHE)


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10)


async def finish_login(code: str, state: str, error: str) -> Response:
    """Google came back with a login state: check it once, exchange the code, check the ID
    token, and give the owner (only) a session."""
    if core.mode() == "access":
        return to_login("off")
    settings = google_oauth.OAuthSettings.from_env()
    backend = store.backend()
    if settings is None or backend is None:
        return to_login("config" if settings is None else "db")
    try:
        taken = await backend.take_state(state)
    except Exception as failure:  # noqa: BLE001
        logger.warning("VALR sign-in state lookup failed: %s", type(failure).__name__)
        return to_login("db")
    if taken is None:
        return to_login("expired")
    nonce, next_path = taken
    if error or not code:
        return to_login("cancelled" if error == "access_denied" else "failed")
    try:
        async with http_client() as client:
            email = await core.exchange(settings.client_id, settings.client_secret,
                                        settings.redirect_uri, code, nonce, client)
    except (core.LoginError, httpx.HTTPError) as failure:
        logger.warning("VALR sign-in failed: %s", failure if isinstance(failure, core.LoginError)
                       else type(failure).__name__)
        return to_login("failed")
    if email not in core.owner_emails():
        logger.warning("VALR sign-in refused for a non-owner account at %s", core.domain_of(email))
        return RedirectResponse("/private", status_code=303, headers=NO_CACHE)
    try:
        token = await backend.create_session(email)
    except Exception as failure:  # noqa: BLE001
        logger.warning("VALR session could not be created: %s", type(failure).__name__)
        return to_login("db")
    logger.info("VALR owner signed in")
    response = RedirectResponse(core.safe_next(next_path), status_code=303, headers=NO_CACHE)
    response.headers.append("Set-Cookie", core.session_cookie(token))
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    """End this session (the row is deleted) and clear the cookie."""
    if not core.same_origin(request.headers, request.headers.get("host")):
        return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403,
                            headers=NO_CACHE)
    token = core.cookie_token(request.cookies)
    backend = store.backend()
    if token and backend is not None:
        try:
            await backend.end_session(token)
        except Exception as failure:  # noqa: BLE001 - the cookie is still cleared.
            logger.warning("VALR sign-out could not delete the session: %s", type(failure).__name__)
    response = RedirectResponse("/login?signed_out=1", status_code=303, headers=NO_CACHE)
    response.headers.append("Set-Cookie", core.CLEARED_COOKIE)
    return response


@router.get("/private", response_class=HTMLResponse)
async def private(theme: str = "") -> HTMLResponse:
    return HTMLResponse(pages.private_page(theme or None), status_code=403, headers=NO_CACHE)
