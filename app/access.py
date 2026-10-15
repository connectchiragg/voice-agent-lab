"""Cloudflare Access identity: the verified email of a browser caller, or None.

Access signs every request it lets through with a JWT in `Cf-Access-Jwt-Assertion`
(RS256, keys at <team domain>/cdn-cgi/access/certs). Only a token that verifies
against those keys, for this app's audience tag (CF_ACCESS_AUD) and this team's
issuer, and that has not expired, identifies the caller. Anything else (no
token, as through the quick tunnel; a bad one; CF_ACCESS_AUD unset) is anonymous.

The check is written out here, for RS256 only, rather than with a JWT library:
the algorithm is fixed, so a token cannot choose a weaker one ("none", HS256).

Keys are cached for CERTS_TTL_S and refreshed in the background when stale, so
a call never waits on Cloudflare once they are loaded. A token signed with an
unknown key id (Access rotated its keys) triggers one refetch, at most every
REFETCH_AFTER_S.
"""
import asyncio
import base64
import binascii
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey, RSAPublicNumbers
from cryptography.hazmat.primitives.hashes import SHA256

logger = logging.getLogger("uvicorn.error")
HEADER = "cf-access-jwt-assertion"
DEFAULT_TEAM_DOMAIN = "https://small-brook-8402.cloudflareaccess.com"
CERTS_TTL_S = 3600.0
REFETCH_AFTER_S = 60.0
FETCH_TIMEOUT_S = 2.0
LEEWAY_S = 60  # Clock skew allowed on exp and nbf.
MAX_TOKEN_CHARS = 8192


def b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwk_key(jwk: Mapping[str, Any]) -> RSAPublicKey | None:
    if jwk.get("kty") != "RSA" or not isinstance(jwk.get("kid"), str):
        return None
    try:
        exponent = int.from_bytes(b64url(jwk["e"]), "big")
        modulus = int.from_bytes(b64url(jwk["n"]), "big")
        return RSAPublicNumbers(exponent, modulus).public_key()
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None


def parse_certs(body: Mapping[str, Any]) -> dict[str, RSAPublicKey]:
    """Access's certs document -> {key id: RSA public key}."""
    keys = {}
    for jwk in body.get("keys") or []:
        if isinstance(jwk, Mapping) and (key := jwk_key(jwk)) is not None:
            keys[jwk["kid"]] = key
    return keys


def verified_claims(token: str, keys: Mapping[str, RSAPublicKey], audience: str, issuer: str,
                    now: float | None = None) -> dict[str, Any] | None:
    """The token's claims if it is a valid RS256 Access token for `audience`; else None."""
    if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        return None
    head, body, signature = token.split(".")
    try:
        header = json.loads(b64url(head))
        claims = json.loads(b64url(body))
        if not isinstance(header, dict) or not isinstance(claims, dict):
            return None
        if header.get("alg") != "RS256" or header.get("kid") not in keys:
            return None
        keys[header["kid"]].verify(b64url(signature), f"{head}.{body}".encode(), PKCS1v15(),
                                   SHA256())
    except (ValueError, binascii.Error, InvalidSignature, TypeError):
        return None
    now = time.time() if now is None else now
    audiences = claims.get("aud")
    audiences = [audiences] if isinstance(audiences, str) else audiences
    if not isinstance(audiences, list) or audience not in audiences:
        return None
    if claims.get("iss") != issuer:
        return None
    expires, not_before = claims.get("exp"), claims.get("nbf", 0)
    if not isinstance(expires, (int, float)) or expires + LEEWAY_S < now:
        return None
    if not isinstance(not_before, (int, float)) or not_before - LEEWAY_S > now:
        return None
    return claims


def unverified_kid(token: str) -> str | None:
    try:
        kid = json.loads(b64url(token.split(".", 1)[0])).get("kid")
    except (ValueError, binascii.Error, AttributeError):
        return None
    return kid if isinstance(kid, str) else None


@dataclass
class AccessVerifier:
    team_domain: str  # https://<team>.cloudflareaccess.com, also the tokens' issuer.
    audience: str  # The Access application's AUD tag.
    keys: dict[str, RSAPublicKey] = field(default_factory=dict)
    fetched_at: float = -float("inf")  # monotonic; when the keys were last fetched.
    attempted_at: float = -float("inf")  # monotonic; the last fetch, failed or not.
    refreshing: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def certs_url(self) -> str:
        return f"{self.team_domain}/cdn-cgi/access/certs"

    async def fetch(self) -> None:
        """Replace the keys; on failure keep the old ones."""
        async with self.lock:
            self.attempted_at = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S) as client:
                    response = await client.get(self.certs_url)
                    response.raise_for_status()
                    keys = parse_certs(response.json())
            except (httpx.HTTPError, ValueError) as error:
                logger.warning("Cloudflare Access certs fetch failed: %s", type(error).__name__)
                return
            if keys:
                self.keys, self.fetched_at = keys, time.monotonic()
                logger.info("Cloudflare Access certs loaded: %d keys", len(keys))

    def refresh_in_background(self) -> None:
        if self.refreshing is None or self.refreshing.done():
            self.refreshing = asyncio.create_task(self.fetch())

    async def email(self, token: str | None) -> str | None:
        if not token:
            return None
        now = time.monotonic()
        kid = unverified_kid(token)
        if kid not in self.keys:  # Not loaded yet, or rotated: this call waits (bounded).
            if self.refreshing is not None and not self.refreshing.done():
                await asyncio.shield(self.refreshing)
            elif now - self.attempted_at > REFETCH_AFTER_S:
                await self.fetch()
        elif now - self.fetched_at > CERTS_TTL_S:
            self.refresh_in_background()
        claims = verified_claims(token, self.keys, self.audience, self.team_domain)
        if claims is None:
            logger.info("Cloudflare Access token rejected; the caller is anonymous")
            return None
        email = claims.get("email")
        return email if isinstance(email, str) else None

    async def close(self) -> None:
        if self.refreshing is not None:
            self.refreshing.cancel()
            await asyncio.gather(self.refreshing, return_exceptions=True)


verifier: AccessVerifier | None = None  # None: browser callers are anonymous.


def from_env() -> AccessVerifier | None:
    audience = os.getenv("CF_ACCESS_AUD", "").strip()
    if not audience:
        return None
    team = os.getenv("CF_ACCESS_TEAM_DOMAIN", DEFAULT_TEAM_DOMAIN).strip().rstrip("/")
    return AccessVerifier(team, audience)


async def verified_email(headers: Mapping[str, str]) -> str | None:
    """The browser caller's Access email, only from a verified token."""
    if verifier is None:
        return None
    try:
        return await verifier.email(headers.get(HEADER))
    except Exception as error:  # Never fail a call over identity: it is just anonymous.
        logger.warning("Cloudflare Access check failed: %s", type(error).__name__)
        return None


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """App lifespan: load the keys in the background, so the first call need not wait."""
    global verifier
    verifier = from_env()
    if verifier is None:
        logger.info("CF_ACCESS_AUD is not set; browser callers are anonymous.")
        yield
        return
    verifier.refresh_in_background()
    try:
        yield
    finally:
        await verifier.close()
        verifier = None
