"""Verifies the OIDC token Cloud Pub/Sub puts on each authenticated push.

A push subscription with authentication sends `Authorization: Bearer <JWT>`, a Google-signed
ID token (RS256) whose `aud` is the subscription's audience, `iss` is accounts.google.com,
and `email` is the push service account (with `email_verified`). Only such a token is
accepted (app/access.py's RS256-only check, with Google's keys from GOOGLE_CERTS, cached for
an hour and refetched once when a token names an unknown key id).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from app import access

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CERTS_TTL_S = 3600.0
REFETCH_AFTER_S = 60.0


def check(token: str, keys: dict[str, RSAPublicKey], audience: str, email: str,
          now: float | None = None) -> dict[str, Any] | None:
    """The claims of a valid push token for `audience` from service account `email`; else None."""
    for issuer in ISSUERS:
        claims = access.verified_claims(token, keys, audience, issuer, now)
        if claims is not None:
            if claims.get("email", "").lower() == email.lower() and claims.get("email_verified") is True:
                return claims
            return None
    return None


@dataclass
class PushVerifier:
    audience: str
    email: str
    keys: dict[str, RSAPublicKey] = field(default_factory=dict)
    fetched_at: float = -float("inf")
    attempted_at: float = -float("inf")

    async def fetch(self) -> None:
        self.attempted_at = time.monotonic()
        async with httpx.AsyncClient(timeout=access.FETCH_TIMEOUT_S) as client:
            response = await client.get(GOOGLE_CERTS)
            response.raise_for_status()
            keys = access.parse_certs(response.json())
        if keys:
            self.keys, self.fetched_at = keys, time.monotonic()

    async def verify(self, authorization: str | None) -> dict[str, Any] | None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not token or not self.audience or not self.email:
            return None
        now = time.monotonic()
        kid = access.unverified_kid(token)
        stale = now - self.fetched_at > CERTS_TTL_S
        if (kid not in self.keys or stale) and now - self.attempted_at > REFETCH_AFTER_S:
            try:
                await self.fetch()
            except (httpx.HTTPError, ValueError):
                pass  # Keep the old keys.
        return check(token, self.keys, self.audience, self.email)
