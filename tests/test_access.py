"""Cloudflare Access identity: RS256 tokens verified against cached team keys, with a test key."""
import asyncio
import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.hashes import SHA256

from app import access, memory

TEAM = "https://team.cloudflareaccess.com"
AUD = "aud-tag-for-voice"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk(key, kid="kid-1"):
    numbers = key.public_key().public_numbers()
    return {"kty": "RSA", "alg": "RS256", "kid": kid,
            "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": b64(numbers.e.to_bytes(3, "big"))}


def token(key=KEY, kid="kid-1", alg="RS256", **claims):
    now = int(time.time())
    body = {"aud": [AUD], "iss": TEAM, "email": "Asha@Example.com", "iat": now, "nbf": now,
            "exp": now + 3600, "type": "app", **claims}
    head = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = b64(json.dumps(body).encode())
    signature = key.sign(f"{head}.{payload}".encode(), PKCS1v15(), SHA256())
    return f"{head}.{payload}.{b64(signature)}"


def keys():
    return access.parse_certs({"keys": [jwk(KEY)], "public_cert": {"kid": "kid-1"}})


@pytest.fixture
def verifier(monkeypatch):
    verifier = access.AccessVerifier(TEAM, AUD, keys=keys(), fetched_at=time.monotonic())
    monkeypatch.setattr(access, "verifier", verifier)
    return verifier


def test_a_valid_token_yields_its_claims():
    claims = access.verified_claims(token(), keys(), AUD, TEAM)
    assert claims is not None and claims["email"] == "Asha@Example.com"
    assert access.verified_claims(token(aud=AUD), keys(), AUD, TEAM) is not None  # aud as a string.


@pytest.mark.parametrize("bad", [
    token(key=OTHER_KEY),  # Signed by someone else.
    token(kid="unknown"),
    token(aud=["another-app"]),
    token(iss="https://evil.cloudflareaccess.com"),
    token(exp=int(time.time()) - 120),
    token(nbf=int(time.time()) + 600),
    token(alg="HS256"),
    token(alg="none"),
    "not-a-token", "a.b", "", "a.b.c.d",
])
def test_invalid_tokens_are_rejected(bad):
    assert access.verified_claims(bad, keys(), AUD, TEAM) is None


def test_a_tampered_payload_is_rejected():
    head, _, signature = token().split(".")
    forged = b64(json.dumps({"aud": [AUD], "iss": TEAM, "email": "boss@example.com",
                             "exp": int(time.time()) + 600}).encode())
    assert access.verified_claims(f"{head}.{forged}.{signature}", keys(), AUD, TEAM) is None


def test_only_a_verified_token_identifies_a_web_caller(verifier):
    async def key(headers):
        return await memory.web_caller_key(headers)
    assert asyncio.run(key({access.HEADER: token()})) == "email:asha@example.com"
    assert asyncio.run(key({access.HEADER: token(key=OTHER_KEY)})) is None
    assert asyncio.run(key({})) is None  # The quick tunnel: no token, anonymous.
    # The old header-only path is gone: an email header alone means nothing.
    assert asyncio.run(key({"cf-access-authenticated-user-email": "asha@example.com"})) is None


def test_without_an_audience_every_web_caller_is_anonymous(monkeypatch):
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    assert access.from_env() is None
    monkeypatch.setattr(access, "verifier", None)
    assert asyncio.run(memory.web_caller_key({access.HEADER: token()})) is None
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    built = access.from_env()
    assert built is not None and built.certs_url == (
        "https://small-brook-8402.cloudflareaccess.com/cdn-cgi/access/certs")


class CertsServer:
    """Stands in for <team>/cdn-cgi/access/certs; counts fetches."""

    def __init__(self, monkeypatch, *served):
        self.served = list(served)
        self.fetches = 0
        real = httpx.AsyncClient

        def client(**kwargs):
            return real(transport=httpx.MockTransport(self.handle), **kwargs)
        monkeypatch.setattr(access.httpx, "AsyncClient", client)

    def handle(self, request):
        assert str(request.url) == f"{TEAM}/cdn-cgi/access/certs"
        self.fetches += 1
        body = self.served[min(self.fetches, len(self.served)) - 1]
        return httpx.Response(200, json=body) if body is not None else httpx.Response(503)


def test_certs_are_fetched_once_and_cached(monkeypatch):
    server = CertsServer(monkeypatch, {"keys": [jwk(KEY)]})
    verifier = access.AccessVerifier(TEAM, AUD)

    async def scenario():
        return [await verifier.email(token()) for _ in range(3)]
    assert asyncio.run(scenario()) == ["Asha@Example.com"] * 3
    assert server.fetches == 1


def test_a_rotated_key_is_fetched_but_unknown_kids_cannot_force_refetches(monkeypatch):
    server = CertsServer(monkeypatch, {"keys": [jwk(KEY)]},
                         {"keys": [jwk(KEY), jwk(OTHER_KEY, "kid-2")]})
    verifier = access.AccessVerifier(TEAM, AUD)

    async def scenario():
        assert await verifier.email(token()) == "Asha@Example.com"
        verifier.attempted_at -= access.REFETCH_AFTER_S + 1
        assert await verifier.email(token(key=OTHER_KEY, kid="kid-2")) == "Asha@Example.com"
        for _ in range(3):  # Junk key ids within the minute: no more fetches.
            assert await verifier.email(token(kid="junk")) is None
    asyncio.run(scenario())
    assert server.fetches == 2


def test_stale_certs_are_used_while_refreshing_and_kept_if_the_refresh_fails(monkeypatch):
    server = CertsServer(monkeypatch, None)
    verifier = access.AccessVerifier(TEAM, AUD, keys=keys(),
                                     fetched_at=time.monotonic() - access.CERTS_TTL_S - 1)

    async def scenario():
        assert await verifier.email(token()) == "Asha@Example.com"  # Did not wait.
        await verifier.refreshing
        assert await verifier.email(token()) == "Asha@Example.com"
        await verifier.close()
    asyncio.run(scenario())
    assert server.fetches >= 1 and verifier.keys
