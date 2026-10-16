"""The pure parts of VALR's sign-in: settings, tokens, cookies, redirects and Google's ID token.

Nothing here touches the database; the only I/O is the code exchange at Google's token
endpoint (`exchange`), and it takes its HTTP client from the caller.
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("uvicorn.error")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
SCOPE = "openid email profile"  # Sign-in only: never Gmail or Calendar.
COOKIE = "__Host-valr"
SESSION_TTL_S = 30 * 86400
STATE_TTL_S = 600
STATE_PREFIX = "login."  # Gmail/Calendar consent states (app/google_oauth.py) never start so.
DEFAULT_NEXT = "/valr"
LEEWAY_S = 60

Mode = Literal["access", "app", "both"]
MODES: tuple[Mode, ...] = ("access", "app", "both")

# The signed-in owner's email for the request being handled (set by the gate), so code that
# only sees headers (memory.web_caller_key) can still tell who is calling.
current_email: ContextVar[str | None] = ContextVar("valr_auth_email", default=None)


def signed_in_email() -> str | None:
    return current_email.get()


def mode() -> Mode:
    """VALR_AUTH_MODE: access (Cloudflare Access only), app (VALR's own sign-in) or both."""
    raw = os.getenv("VALR_AUTH_MODE", "both").strip().lower() or "both"
    if raw not in MODES:
        logger.warning("VALR_AUTH_MODE=%r is not access, app or both; using both", raw[:20])
        return "both"
    return raw  # type: ignore[return-value]


def emails(name: str) -> frozenset[str]:
    return frozenset(email.strip().lower() for email in os.getenv(name, "").split(",") if email.strip())


def owner_emails() -> frozenset[str]:
    """VALR_OWNER_EMAILS: the only Google accounts that get a session."""
    return emails("VALR_OWNER_EMAILS")


def domain_of(email: str) -> str:
    """For logs: the domain only, never the address."""
    return email.rsplit("@", 1)[-1][:80] if "@" in email else "?"


@dataclass(frozen=True)
class Identity:
    email: str
    via: Literal["session", "access", "preview"]


# Session tokens: 32 random bytes in the cookie; the database keeps only their SHA-256.

TOKEN_RE = re.compile(r"[0-9a-f]{64}")


def new_token() -> str:
    return secrets.token_hex(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cookie_token(cookies: Mapping[str, str]) -> str | None:
    value = cookies.get(COOKIE, "")
    return value if TOKEN_RE.fullmatch(value) else None


def session_cookie(token: str) -> str:
    return f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_S}"


CLEARED_COOKIE = f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"


# The one-time login state and the OpenID nonce.

def new_state() -> str:
    return STATE_PREFIX + secrets.token_urlsafe(24)


def is_login_state(state: str | None) -> bool:
    return bool(state) and state.startswith(STATE_PREFIX)  # type: ignore[union-attr]


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


NEXT_RE = re.compile(r"/(?!/)[\w\-./?=&%#~+]*")


def safe_next(value: str | None) -> str:
    """Only a same-site path survives as the post-login destination (never //host, \\, a
    scheme, or the login pages themselves, which would loop)."""
    if not value or len(value) > 512 or not NEXT_RE.fullmatch(value):
        return DEFAULT_NEXT
    if value.split("?", 1)[0].rstrip("/") in ("/login", "/login/google", "/logout", "/private"):
        return DEFAULT_NEXT
    return value


def same_origin(headers: Mapping[str, str], host: str | None) -> bool:
    """A browser request from this site (or not from a browser at all). Browsers send Origin on
    every cross-origin POST and WebSocket, and Sec-Fetch-Site on every request."""
    site = headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    return bool(host) and origin.split("://", 1)[-1].lower() == host.lower()  # type: ignore[union-attr]


def login_url(client_id: str, redirect_uri: str, state: str, nonce: str) -> str:
    """Google's sign-in screen: openid email profile only, always the account chooser."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "prompt": "select_account", "state": state, "nonce": nonce})


# The ID token, received straight from Google's token endpoint over TLS. OpenID Connect
# Core 3.1.3.7 lets a client trust such a token without checking its signature; the
# claims are still checked.

class LoginError(Exception):
    pass


def decode_payload(jwt: str) -> dict[str, Any]:
    parts = jwt.split(".")
    if len(parts) != 3 or len(jwt) > 16384:
        raise LoginError("Malformed ID token")
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, binascii.Error) as error:
        raise LoginError("Malformed ID token") from error
    if not isinstance(claims, dict):
        raise LoginError("Malformed ID token")
    return claims


def check_claims(claims: Mapping[str, Any], client_id: str, nonce: str,
                 now: float | None = None) -> str:
    """The verified, lower-cased email; LoginError on anything unexpected."""
    now = time.time() if now is None else now
    if claims.get("iss") not in ISSUERS:
        raise LoginError("Unexpected token issuer")
    audience = claims.get("aud")
    if not (audience == client_id or (isinstance(audience, list) and client_id in audience
                                      and claims.get("azp") == client_id)):
        raise LoginError("Token is for another app")
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires + LEEWAY_S < now:
        raise LoginError("Token expired")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce) or not nonce:
        raise LoginError("Token nonce does not match")
    email = claims.get("email")
    if claims.get("email_verified") not in (True, "true") or not isinstance(email, str) \
            or "@" not in email:
        raise LoginError("Google account email is not verified")
    return email.strip().lower()


async def exchange(client_id: str, client_secret: str, redirect_uri: str, code: str,
                   nonce: str, http: httpx.AsyncClient) -> str:
    """Code -> the verified email from the ID token. Nothing else is kept: no access or
    refresh token (this grant has no Gmail or Calendar scope anyway)."""
    response = await http.post(TOKEN_URL, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict) \
            or not isinstance(body.get("id_token"), str):
        raise LoginError(f"Google refused the sign-in (HTTP {response.status_code})")
    return check_claims(decode_payload(body["id_token"]), client_id, nonce)
