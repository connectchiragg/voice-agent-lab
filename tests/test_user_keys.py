"""A workspace's own API keys (app/user_keys.py, app/key_providers.py): sealing bound to the
workspace, the cache, each provider's check and request shape, and the owner never touching
any of it. No network: httpx.MockTransport. Every key here is made up."""
import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet

from app import key_providers, tenant, user_keys

SECRET = Fernet.generate_key().decode()
GUEST = "u_" + "1" * 20
OTHER = "u_" + "2" * 20
KEY = "sk-test-made-up-0000-1234"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("VALR_TOKEN_KEY", SECRET)
    user_keys.forget()
    yield
    user_keys.forget()


# Sealing.

def test_a_sealed_key_opens_only_for_its_workspace_and_purpose():
    blob = user_keys.seal(SECRET, GUEST, "llm", KEY)
    assert KEY.encode() not in blob
    assert user_keys.unseal(SECRET, GUEST, "llm", blob) == KEY
    for workspace, purpose in ((OTHER, "llm"), (GUEST, "speech"), (tenant.OWNER, "llm")):
        with pytest.raises(ValueError):
            user_keys.unseal(SECRET, workspace, purpose, blob)
    with pytest.raises(ValueError):  # VALR_TOKEN_KEY changed.
        user_keys.unseal(Fernet.generate_key().decode(), GUEST, "llm", blob)


def test_the_key_never_shows_in_a_repr_and_only_its_last_four_are_kept():
    found = user_keys.UserKey("openai", "gpt-6-luna", KEY)
    assert KEY not in repr(found) and "1234" in repr(found)
    assert user_keys.last4(KEY) == "1234" and user_keys.last4("short") == ""


def test_no_token_key_means_no_storing(monkeypatch):
    monkeypatch.delenv("VALR_TOKEN_KEY")
    with pytest.raises(user_keys.NotConfigured):
        user_keys.token_key()


# The cache.

def test_the_cache_expires_and_stays_bounded(monkeypatch):
    found = {"llm": user_keys.UserKey("groq", "openai/gpt-oss-120b", KEY)}
    user_keys.remember(GUEST, found)
    assert user_keys.cached(GUEST) == found
    assert user_keys.cached(GUEST, now=10**9) is None
    monkeypatch.setattr(user_keys, "CACHE_MAX", 2)
    for n in range(3):
        user_keys.remember(f"u_{n}", found)
    assert user_keys.cached("u_0") is None and user_keys.cached("u_2") == found


def test_a_miss_on_the_event_loop_fails_closed_instead_of_blocking():
    async def main():
        return user_keys.keys_sync(GUEST)
    with pytest.raises(user_keys.KeyMissing, match="Settings → API keys"):
        run(main())


def test_a_miss_in_a_thread_loads_in_the_workspace(monkeypatch):
    seen = []

    async def load(connection=None, pool=True):
        seen.append((tenant.get(), pool))
        return {}
    monkeypatch.setattr(user_keys, "load_current", load)
    assert user_keys.keys_sync(GUEST) == {}
    assert seen == [(GUEST, False)]  # A private connection: the web app's pool is another loop's.


# The store, against a recording stand-in (the real SQL: tests/test_tenant_postgres.py).

class Connection:
    def __init__(self, rows=()):
        self.statements, self.rows = [], list(rows)

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        rows = self.rows

        class Cursor:
            rowcount = 1

            async def fetchall(self):
                return rows
        return Cursor()


def test_save_upserts_on_the_workspace_key_and_stores_only_ciphertext():
    connection = Connection()
    with tenant.as_user(GUEST):
        run(user_keys.save(connection, "llm", "openai", "", KEY, "ok"))
    sql, params = connection.statements[-1]
    assert "ON CONFLICT (owner_id, purpose)" in sql
    assert KEY not in repr(params) and "1234" in params
    assert user_keys.unseal(SECRET, GUEST, "llm", params[3]) == KEY


def test_load_skips_rejected_foreign_and_unreadable_rows():
    good = user_keys.seal(SECRET, GUEST, "llm", KEY)
    moved = user_keys.seal(SECRET, OTHER, "speech", "dg-made-up-key-5678")
    rows = [("llm", "openai", "", good, "ok"), ("speech", "deepgram", "", moved, "ok")]
    with tenant.as_user(GUEST):
        keys = run(user_keys.load(Connection(rows)))
        assert keys == {"llm": user_keys.UserKey("openai", "gpt-6-luna", KEY)}
        rejected = run(user_keys.load(Connection([("llm", "openai", "", good, "rejected")])))
        assert rejected == {}


def test_the_public_view_has_no_key():
    from datetime import datetime, timezone
    rows = [("llm", "anthropic", "", "1234", "ok", datetime(2026, 10, 31, tzinfo=timezone.utc))]
    with tenant.as_user(GUEST):
        view = run(user_keys.public(Connection(rows)))
    assert view["speech"] is None
    assert view["llm"] == {"provider": "anthropic", "label": "Anthropic", "model": "claude-haiku-5-5",
                           "custom_model": False, "last4": "1234", "status": "ok",
                           "checked_at": "2026-10-31T00:00:00+00:00"}


# Checking a key: one free GET per provider.

def checker(status, seen):
    def handle(request):
        seen.append(request)
        if status == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(status, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("name,url,header,value", [
    ("groq", "https://api.groq.com/openai/v1/models", "authorization", f"Bearer {KEY}"),
    ("openai", "https://api.openai.com/v1/models", "authorization", f"Bearer {KEY}"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/models", "authorization", f"Bearer {KEY}"),
    ("openrouter", "https://openrouter.ai/api/v1/key", "authorization", f"Bearer {KEY}"),
    ("anthropic", "https://api.anthropic.com/v1/models", "x-api-key", KEY),
    ("deepgram", "https://api.deepgram.com/v1/projects", "authorization", f"Token {KEY}"),
])
def test_each_provider_is_checked_with_one_authenticated_get(name, url, header, value):
    seen = []
    assert run(key_providers.check(key_providers.PROVIDERS[name], KEY, checker(200, seen))) == "ok"
    assert len(seen) == 1 and seen[0].method == "GET" and str(seen[0].url) == url
    assert seen[0].headers[header] == value
    if name == "anthropic":
        assert seen[0].headers["anthropic-version"] == key_providers.ANTHROPIC_VERSION


def test_gemini_says_a_bad_key_with_a_400():
    def handle(request):
        return httpx.Response(400, json={"error": {"code": 400, "message": "Please pass a valid API key"}})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    assert run(key_providers.check(key_providers.PROVIDERS["gemini"], KEY, http)) == "rejected"
    assert run(key_providers.check(key_providers.PROVIDERS["openai"], KEY, http)) == "unchecked"


@pytest.mark.parametrize("name,status,expected", [
    ("openai", 401, "rejected"), ("openai", 403, "rejected"), ("deepgram", 401, "rejected"),
    ("deepgram", 403, "ok"), ("openai", 500, "unchecked"), ("openai", 429, "unchecked"),
    ("openai", "timeout", "unchecked"),
])
def test_check_results(name, status, expected):
    assert run(key_providers.check(key_providers.PROVIDERS[name], KEY, checker(status, []))) == expected


# Request shapes.

def test_chat_bodies_drop_groq_fields_and_rename_limits():
    body = {"model": "m", "messages": [], "max_completion_tokens": 300, "reasoning_effort": "none",
            "reasoning_format": "hidden"}
    assert key_providers.chat_body("openai", body) == {"model": "m", "messages": [], "max_completion_tokens": 300}
    for name in ("gemini", "openrouter"):
        assert key_providers.chat_body(name, body) == {"model": "m", "messages": [], "max_tokens": 300}
    assert key_providers.chat_body("groq", body)["reasoning_effort"] == "none"  # A Qwen model: as sent.
    oss = key_providers.chat_body("groq", {**body, "model": "openai/gpt-oss-120b"})
    assert oss["reasoning_effort"] == "low" and "reasoning_format" not in oss
    assert key_providers.chat_body("openai", body, voice=True)["reasoning_effort"] == "low"
    assert key_providers.chat_body("openrouter", body, voice=True)["reasoning"] == {"effort": "low"}
    assert body["reasoning_format"] == "hidden"  # The caller's body is not changed.


def test_every_purpose_has_providers_and_defaults():
    assert [p.name for p in key_providers.for_purpose("llm")] == ["groq", "openai", "anthropic", "gemini", "openrouter"]
    assert [p.name for p in key_providers.for_purpose("speech")] == ["deepgram"]
    assert all(p.default_model for p in key_providers.for_purpose("llm"))
