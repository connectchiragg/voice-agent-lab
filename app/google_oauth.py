"""Read-only Gmail access for VALR's private-context researcher (R1), by Google OAuth.

One-time consent, behind Cloudflare Access on the VALR host:

1. The owner opens `/oauth/google/start` (valr.haciensus.com). The app checks the
   Access JWT (and, with VALR_OWNER_EMAILS set, that it is the owner) and redirects to
   Google's consent screen for `gmail.readonly` only, offline access, with a signed,
   expiring `state`.
2. Google redirects to `/oauth/google/callback` (GOOGLE_OAUTH_REDIRECT_URI). The code
   is exchanged for tokens; only the refresh token is kept, encrypted (Fernet, key
   VALR_TOKEN_KEY) in Postgres table `oauth_tokens`. The page says which account
   was connected; no token is ever shown or logged.
3. The research worker decrypts it, gets a short-lived access token and searches mail
   (app/private_sources.py). Google invalidates refresh tokens of apps in "Testing"
   mode after 7 days: the next refresh fails with `invalid_grant`, the row is marked
   `reconnect_needed`, and the dashboard and briefings say "reconnect needed".

Settings: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI
(default https://valr.haciensus.com/oauth/google/callback), VALR_TOKEN_KEY (a Fernet key),
VALR_GMAIL_ACCOUNT (the expected account, e.g. owner@example.com; another
account is refused). Imports only the standard library, httpx and cryptography.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
PROVIDER = "google_gmail"
STATE_TTL_S = 900
SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider       text PRIMARY KEY,
    account        text NOT NULL,
    refresh_token  text NOT NULL,          -- Fernet ciphertext (VALR_TOKEN_KEY)
    scope          text NOT NULL,
    status         text NOT NULL,          -- connected | reconnect_needed
    connected_at   timestamptz NOT NULL DEFAULT now(),
    last_ok_at     timestamptz,
    last_error     text
);
"""


class NotConfigured(Exception):
    pass


class ReconnectNeeded(Exception):
    """The refresh token was revoked or expired (Testing mode: after 7 days)."""


@dataclass(frozen=True)
class OAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    token_key: str
    account: str = ""

    @classmethod
    def from_env(cls) -> "OAuthSettings | None":
        client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        key = os.getenv("VALR_TOKEN_KEY", "").strip()
        if not (client_id and secret and key):
            return None
        return cls(client_id, secret,
                   os.getenv("GOOGLE_OAUTH_REDIRECT_URI",
                             "https://valr.haciensus.com/oauth/google/callback").strip(),
                   key, os.getenv("VALR_GMAIL_ACCOUNT", "").strip().lower())


def fernet(key: str) -> Fernet:
    return Fernet(key.encode())


def encrypt(key: str, text: str) -> str:
    return fernet(key).encrypt(text.encode()).decode()


def decrypt(key: str, token: str) -> str:
    try:
        return fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as error:
        raise ReconnectNeeded("the stored token cannot be decrypted with VALR_TOKEN_KEY") from error


# The signed state: nonce and expiry, HMAC'd with a key derived from VALR_TOKEN_KEY.

def _state_key(token_key: str) -> bytes:
    return hashlib.sha256(b"valr-oauth-state:" + token_key.encode()).digest()


def make_state(token_key: str, now: float | None = None) -> str:
    body = json.dumps({"n": secrets.token_urlsafe(12),
                       "exp": int((now or time.time()) + STATE_TTL_S)}).encode()
    mac = hmac.new(_state_key(token_key), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + \
        base64.urlsafe_b64encode(mac).decode().rstrip("=")


def check_state(token_key: str, state: str, now: float | None = None) -> bool:
    try:
        body_b64, mac_b64 = state.split(".")
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        mac = base64.urlsafe_b64decode(mac_b64 + "=" * (-len(mac_b64) % 4))
        expected = hmac.new(_state_key(token_key), body, hashlib.sha256).digest()
        return hmac.compare_digest(mac, expected) and json.loads(body)["exp"] >= (now or time.time())
    except (ValueError, KeyError, TypeError):
        return False


def consent_url(settings: OAuthSettings, state: str) -> str:
    params = {"client_id": settings.client_id, "redirect_uri": settings.redirect_uri,
              "response_type": "code", "scope": SCOPE, "access_type": "offline",
              "prompt": "consent", "include_granted_scopes": "false", "state": state}
    if settings.account:
        params["login_hint"] = settings.account
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(settings: OAuthSettings, code: str,
                        http: httpx.AsyncClient) -> tuple[str, str, str]:
    """-> (refresh_token, granted scope, account email). Raises ValueError on refusal."""
    response = await http.post(TOKEN_URL, data={
        "code": code, "client_id": settings.client_id, "client_secret": settings.client_secret,
        "redirect_uri": settings.redirect_uri, "grant_type": "authorization_code"})
    if response.status_code != 200:
        raise ValueError(f"Google refused the code (HTTP {response.status_code})")
    body = response.json()
    refresh, scope, access = body.get("refresh_token"), body.get("scope", ""), body.get("access_token")
    if not refresh or not access:
        raise ValueError("Google returned no refresh token; remove the app's access in the "
                         "Google account and connect again")
    if SCOPE not in scope.split():
        raise ValueError("gmail.readonly was not granted")
    profile = await http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
    account = str(profile.json().get("emailAddress", "")).lower() if profile.status_code == 200 else ""
    return refresh, scope, account


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


async def store(connection: Any, settings: OAuthSettings, account: str, refresh: str,
                scope: str) -> None:
    await connection.execute(
        "INSERT INTO oauth_tokens (provider, account, refresh_token, scope, status, connected_at) "
        "VALUES (%s, %s, %s, %s, 'connected', now()) ON CONFLICT (provider) DO UPDATE SET "
        "account = excluded.account, refresh_token = excluded.refresh_token, scope = excluded.scope, "
        "status = 'connected', connected_at = now(), last_error = NULL",
        (PROVIDER, account, encrypt(settings.token_key, refresh), scope))


async def status(connection: Any) -> dict[str, Any]:
    """{state: not_connected | connected | reconnect_needed, account, connected_at, ...}."""
    cursor = await connection.execute(
        "SELECT account, status, connected_at, last_ok_at, last_error FROM oauth_tokens "
        "WHERE provider = %s", (PROVIDER,))
    row = await cursor.fetchone()
    if row is None:
        return {"state": "not_connected"}
    state = row[1]
    # Testing-mode tokens die after 7 days: say so a little before.
    age_days = (time.time() - row[2].timestamp()) / 86400
    return {"state": state, "account": row[0], "connected_at": row[2], "last_ok_at": row[3],
            "last_error": row[4], "age_days": round(age_days, 1),
            "expires_soon": state == "connected" and age_days >= 6}


async def access_token(connection: Any, settings: OAuthSettings, http: httpx.AsyncClient) -> str:
    """A fresh access token from the stored refresh token. Raises ReconnectNeeded or NotConfigured."""
    cursor = await connection.execute(
        "SELECT refresh_token, status FROM oauth_tokens WHERE provider = %s", (PROVIDER,))
    row = await cursor.fetchone()
    if row is None:
        raise NotConfigured("Gmail is not connected")
    if row[1] != "connected":
        raise ReconnectNeeded("Gmail needs to be reconnected")
    refresh = decrypt(settings.token_key, row[0])
    response = await http.post(TOKEN_URL, data={
        "client_id": settings.client_id, "client_secret": settings.client_secret,
        "refresh_token": refresh, "grant_type": "refresh_token"})
    if response.status_code in (400, 401):
        error = ""
        try:
            error = str(response.json().get("error", ""))
        except ValueError:
            pass
        if error in ("invalid_grant", "unauthorized_client", "invalid_client"):
            await connection.execute(
                "UPDATE oauth_tokens SET status = 'reconnect_needed', last_error = %s "
                "WHERE provider = %s", (error, PROVIDER))
            raise ReconnectNeeded(f"Gmail refresh refused ({error})")
    response.raise_for_status()
    await connection.execute("UPDATE oauth_tokens SET last_ok_at = now() WHERE provider = %s",
                             (PROVIDER,))
    return str(response.json()["access_token"])
