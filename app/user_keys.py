"""A workspace's own API keys ("bring your own key"), so other users cost the owner nothing.

The owner's workspace uses the server's keys (GROQ_API_KEY, DEEPGRAM_API_KEY, ...) exactly as
before and never reads this table. Every other workspace pays for its own LLM and speech: with
no key of its own a feature is unavailable (`KeyMissing`, "Add your API key in Settings → API
keys"), never served on a server key.

- `user_keys` (per user, app/tenant.py): one row per purpose, `llm` or `speech`: provider,
  model ('' = the provider's default, app/key_providers.py), the key as Fernet ciphertext
  (VALR_TOKEN_KEY, the Google tokens' key and helpers, app/google_oauth.py), its last four
  characters, and the last check's status (ok | rejected | unchecked) and time.
- Fernet has no associated data, so the sealed plaintext names its workspace and purpose
  ({"w", "p", "k"}) and `unseal` refuses a ciphertext moved to another row or workspace.
- The key is decrypted only to call its provider. It is never logged, returned by the API,
  rendered on a page or exported.
- `cached(user)`: the decrypted keys of a workspace, for up to CACHE_S seconds, at most
  CACHE_MAX workspaces, filled by `load_current` (async callers: a voice call, once per call)
  or `keys_sync` (worker threads; it opens its own short connection). Save and remove clear
  this process's entry; another process sees the change within CACHE_S.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app import google_oauth, key_providers, schema, tenant

logger = logging.getLogger("valr")
ADD_KEY = "Add your API key in Settings → API keys."
ADD_SPEECH_KEY = "Voice needs a Deepgram key. Add it in Settings → API keys."
CACHE_S = 60.0
CACHE_MAX = 256
SCHEMA = """
CREATE TABLE IF NOT EXISTS user_keys (
    purpose     text PRIMARY KEY,         -- llm | speech
    provider    text NOT NULL,
    model       text NOT NULL DEFAULT '', -- '' = the provider's default
    ciphertext  bytea NOT NULL,           -- Fernet (VALR_TOKEN_KEY) of {"w": workspace, "p": purpose, "k": key}
    last4       text NOT NULL,
    status      text NOT NULL,            -- ok | rejected | unchecked
    checked_at  timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""


class KeyMissing(Exception):
    """This workspace has no usable key for the purpose; the message is for the user."""

    def __init__(self, purpose: str = "llm") -> None:
        super().__init__(ADD_SPEECH_KEY if purpose == "speech" else ADD_KEY)
        self.purpose = purpose


class NotConfigured(Exception):
    """VALR_TOKEN_KEY is not set: keys cannot be stored."""


@dataclass(frozen=True)
class UserKey:
    provider: str
    model: str  # Resolved: the provider's default when none was chosen.
    key: str

    def __repr__(self) -> str:  # Never the key, in a log line or a traceback.
        return f"UserKey({self.provider!r}, {self.model!r}, '…{self.key[-4:]}')"


def token_key() -> str:
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    if not key:
        raise NotConfigured("VALR_TOKEN_KEY is not set")
    return key


def seal(secret: str, workspace: str, purpose: str, key: str) -> bytes:
    return google_oauth.encrypt(secret, json.dumps({"w": workspace, "p": purpose, "k": key})).encode()


def unseal(secret: str, workspace: str, purpose: str, blob: bytes) -> str:
    """The key; ValueError if the ciphertext is not this workspace's key for this purpose
    (or VALR_TOKEN_KEY changed)."""
    try:
        sealed = json.loads(google_oauth.decrypt(secret, bytes(blob).decode()))
    except (google_oauth.ReconnectNeeded, ValueError, UnicodeDecodeError) as error:
        raise ValueError("the key cannot be decrypted") from error
    if not isinstance(sealed, dict) or sealed.get("w") != workspace or sealed.get("p") != purpose:
        raise ValueError("the key belongs to another workspace or purpose")
    return str(sealed["k"])


def last4(key: str) -> str:
    return key[-4:] if len(key) >= 8 else ""


async def setup(connection: Any) -> None:
    """The table, once per process (app/schema.py); owner_id and row-level security come
    from app/tenant.py."""
    await schema.once(connection, "user_keys", lambda: connection.execute(SCHEMA))


def _workspace() -> str:
    user = tenant.get()
    if not user:
        raise RuntimeError("no workspace in scope")
    return user


async def save(connection: Any, purpose: str, provider: str, model: str, key: str, status: str) -> None:
    user = _workspace()
    await setup(connection)
    await connection.execute(
        "INSERT INTO user_keys (purpose, provider, model, ciphertext, last4, status, checked_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s = 'unchecked' THEN NULL ELSE now() END, now()) "
        f"ON CONFLICT {tenant.conflict('purpose')} DO UPDATE SET provider = excluded.provider, "
        "model = excluded.model, ciphertext = excluded.ciphertext, last4 = excluded.last4, "
        "status = excluded.status, checked_at = excluded.checked_at, updated_at = now()",
        (purpose, provider, model, seal(token_key(), user, purpose, key), last4(key), status, status))
    forget(user)


async def set_status(connection: Any, purpose: str, status: str) -> None:
    await setup(connection)
    await connection.execute("UPDATE user_keys SET status = %s, checked_at = now() WHERE purpose = %s",
                             (status, purpose))
    forget(_workspace())


async def remove(connection: Any, purpose: str) -> bool:
    await setup(connection)
    cursor = await connection.execute("DELETE FROM user_keys WHERE purpose = %s", (purpose,))
    forget(_workspace())
    return bool(getattr(cursor, "rowcount", 0))


async def public(connection: Any) -> dict[str, dict[str, Any] | None]:
    """What the page may see, per purpose: provider, model, last4, status, checked_at."""
    await setup(connection)
    cursor = await connection.execute(
        "SELECT purpose, provider, model, last4, status, checked_at FROM user_keys")
    out: dict[str, dict[str, Any] | None] = {purpose: None for purpose in key_providers.PURPOSES}
    for purpose, provider, model, four, status, checked in await cursor.fetchall():
        known = key_providers.PROVIDERS.get(provider)
        out[purpose] = {"provider": provider, "label": known.label if known else provider,
                        "model": model or (known.default_model if known else ""), "custom_model": bool(model),
                        "last4": four, "status": status,
                        "checked_at": checked.isoformat() if checked else None}
    return out


async def load(connection: Any) -> dict[str, UserKey]:
    """The current workspace's usable keys (a rejected or undecryptable one is left out)."""
    user = _workspace()
    await setup(connection)
    cursor = await connection.execute("SELECT purpose, provider, model, ciphertext, status FROM user_keys")
    keys: dict[str, UserKey] = {}
    secret = token_key()
    for purpose, provider, model, blob, status in await cursor.fetchall():
        known = key_providers.PROVIDERS.get(provider)
        if status == "rejected" or known is None or known.purpose != purpose:
            continue
        try:
            keys[purpose] = UserKey(provider, model or known.default_model, unseal(secret, user, purpose, blob))
        except ValueError:
            logger.warning("VALR user key (%s) cannot be decrypted; ignored", purpose)
    return keys


# The per-process cache.

_cache: OrderedDict[str, tuple[float, dict[str, UserKey]]] = OrderedDict()


def cached(user: str, now: float | None = None) -> dict[str, UserKey] | None:
    hit = _cache.get(user)
    if hit is None or (now if now is not None else time.monotonic()) - hit[0] > CACHE_S:
        return None
    return hit[1]


def remember(user: str, keys: dict[str, UserKey]) -> None:
    _cache[user] = (time.monotonic(), keys)
    _cache.move_to_end(user)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def forget(user: str | None = None) -> None:
    if user is None:
        _cache.clear()
    else:
        _cache.pop(user, None)


async def _connected_load(pool: bool = True) -> dict[str, UserKey]:
    from app import research
    if pool and research.pool is not None:  # The web app's pool (its own event loop only).
        async with research.pool.connection() as connection:
            return await load(connection)
    from app import research_jobs
    async with await tenant.connect(research_jobs.conninfo(), autocommit=True) as connection:
        return await load(connection)


async def load_current(connection: Any = None, pool: bool = True) -> dict[str, UserKey]:
    """The current (non-owner) workspace's keys, from the cache or the database."""
    user = _workspace()
    if (hit := cached(user)) is not None:
        return hit
    keys = await (load(connection) if connection is not None else _connected_load(pool))
    remember(user, keys)
    return keys


def keys_sync(user: str) -> dict[str, UserKey]:
    """For synchronous callers (worker threads): the cache, else a load on a private event
    loop. On the event loop's own thread a miss raises KeyMissing rather than block it."""
    if (hit := cached(user)) is not None:
        return hit
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with tenant.as_user(user):
            return asyncio.run(load_current(pool=False))
    logger.warning("VALR user keys requested on the event loop before they were loaded")
    raise KeyMissing("llm")
