"""The gate in front of every route: who the caller is, and whether the route needs the owner.

Every HTTP request and WebSocket passes through `Gate` (an ASGI middleware, app/main.py).
A route is either PUBLIC (it checks its own signature or secret, or is part of signing in)
or needs the owner. Anything not listed as public needs the owner, and
tests/test_auth.py fails when a route is in neither list, so a new route is a decision.

Who the caller is depends on VALR_AUTH_MODE (app/auth/core.py):
    app     the `__Host-valr` session cookie (VALR's own Google sign-in)
    access  a verified Cloudflare Access JWT (Cf-Access-Jwt-Assertion), as before
    both    either (the default, for the cut-over)
A session is accepted only for an address still in VALR_OWNER_EMAILS. An Access login must be
in VALR_OWNER_EMAILS or VALR_ADMIN_EMAILS when either is set. The local dev preview
(scripts/preview_ui.py, loopback only) is the preview owner.

Refusals: a page redirects to /login?next=<path>; an API call, a POST or the WebRTC offer gets
401 JSON; a WebSocket is refused before it opens. In access mode the answer stays 403, as before.
Every POST (and WebSocket) to an owner route must also be same-origin.
"""
import json
import logging
from typing import Literal
from urllib.parse import quote

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import core, store

logger = logging.getLogger("uvicorn.error")

# Public: each verifies its own signature or secret, or is part of signing in.
PUBLIC_PATHS = frozenset({
    "/hooks/gcal", "/hooks/gmail", "/hooks/fathom", "/hooks/granola", "/hooks/telegram",
    "/hooks/recording-upload",  # The iPhone Shortcut: its own bearer token (app/phone_web.py).
    "/telnyx/webhook", "/ws/telnyx",
    "/healthz",
    "/login", "/login/google", "/oauth/google/callback", "/logout", "/private",
    # The Home Screen app (app/webpush_web.py): iOS may fetch these without the cookie; nothing private.
    "/manifest.webmanifest", "/sw.js", "/favicon.ico",
})
PUBLIC_PREFIXES = ("/static",)  # Static assets (no data: the pages' data needs the owner).
# The owner: every page, API, action and voice surface.
OWNER_PATHS = frozenset({"/", "/call", "/ws/voice", "/webrtc/offer", "/webrtc/ice-servers",
                         "/oauth/google/start"})
OWNER_PREFIXES = ("/valr",)  # /valr, /valr/api/*, /valr/interviews/..., /valr/context.
# Owner routes whose handler also accepts its own bearer token (a Mac-side job).
BEARER_PATHS = frozenset({"/valr/context"})
API_PREFIXES = ("/valr/api/", "/webrtc/", "/valr/context")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SCOPE_KEY = "valr.identity"


def under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def classify(path: str) -> Literal["public", "owner"] | None:
    """public, owner, or None for a path no one has decided about (treated as owner)."""
    if path in PUBLIC_PATHS or under(path, PUBLIC_PREFIXES):
        return "public"
    if path in OWNER_PATHS or under(path, OWNER_PREFIXES):
        return "owner"
    return None


async def session_identity(connection: HTTPConnection) -> core.Identity | None:
    token = core.cookie_token(connection.cookies)
    if token is None:
        return None
    backend = store.backend()
    if backend is None:
        return None
    try:
        email = await backend.session_email(token)
    except Exception as error:  # noqa: BLE001 - the database is down: anonymous, not a 500.
        logger.warning("VALR session lookup failed: %s", type(error).__name__)
        return None
    if email is None or email.lower() not in core.owner_emails():
        return None
    return core.Identity(email.lower(), "session")


async def access_identity(connection: HTTPConnection) -> core.Identity | None:
    """A verified Cloudflare Access login: the dashboard's Access app, or the call page's."""
    from app import access, valr_dashboard
    token = connection.headers.get(access.HEADER)
    if not token:
        return None
    email = None
    for verifier in (valr_dashboard.verifier, access.verifier):
        if verifier is None:
            continue
        try:
            email = await verifier.email(token)
        except Exception as error:  # noqa: BLE001
            logger.warning("Cloudflare Access check failed: %s", type(error).__name__)
        if email:
            break
    if not email:
        return None
    allowed = core.owner_emails() | core.emails("VALR_ADMIN_EMAILS")
    if allowed and email.lower() not in allowed:
        return None
    return core.Identity(email, "access")


async def identify(connection: HTTPConnection) -> core.Identity | None:
    """The owner making this request, or None. Cached on the request."""
    if SCOPE_KEY in connection.scope:
        return connection.scope[SCOPE_KEY]
    from app import valr_dashboard
    who: core.Identity | None = None
    if valr_dashboard.is_preview_request(connection):  # type: ignore[arg-type]
        who = core.Identity(valr_dashboard.PREVIEW_EMAIL, "preview")
    else:
        mode = core.mode()
        if mode in ("app", "both"):
            who = await session_identity(connection)
        if who is None and mode in ("access", "both"):
            who = await access_identity(connection)
    connection.scope[SCOPE_KEY] = who
    return who


def wants_json(path: str, method: str) -> bool:
    return method not in SAFE_METHODS or under(path, API_PREFIXES)


async def respond(send: Send, status: int, body: bytes, content_type: str,
                  extra: list[tuple[bytes, bytes]] | None = None) -> None:
    headers = [(b"content-type", content_type.encode()), (b"cache-control", b"no-store"),
               (b"content-length", str(len(body)).encode())] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def refuse(scope: Scope, send: Send, status: int, message: str) -> None:
    """Not the owner: a redirect to /login for a page, JSON for the rest, a closed socket."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})  # Before accept: HTTP 403.
        return
    path, method = scope["path"], scope["method"]
    if status == 401 and not wants_json(path, method):
        query = scope.get("query_string", b"").decode("latin-1")
        target = path + (f"?{query}" if query else "")
        location = "/login?next=" + quote(core.safe_next(target), safe="/")
        await respond(send, 303, b"", "text/plain", [(b"location", location.encode())])
        return
    body = {"detail": message} | ({"login": "/login"} if status == 401 else {})
    await respond(send, status, json.dumps(body).encode(), "application/json")


class Gate:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or classify(scope["path"]) == "public":
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        if scope["path"] in BEARER_PATHS and \
                connection.headers.get("authorization", "").startswith("Bearer "):
            await self.app(scope, receive, send)  # The handler checks the token, or the owner.
            return
        if (scope["type"] == "websocket" or scope["method"] not in SAFE_METHODS) and \
                not core.same_origin(connection.headers, connection.headers.get("host")):
            await refuse(scope, send, 403, "Cross-origin request refused.")
            return
        who = await identify(connection)
        if who is None:
            if core.mode() == "access":
                await refuse(scope, send, 403, "A valid Cloudflare Access login is required.")
            else:
                await refuse(scope, send, 401, "Sign in to VALR first.")
            return
        token = core.current_email.set(who.email)
        try:
            await self.app(scope, receive, send)
        finally:
            core.current_email.reset(token)
