"""Notetaker credentials and recordings in Postgres. psycopg and the standard library only.

- `notetaker_credentials`: one row per provider. The API key is Fernet ciphertext
  (VALR_TOKEN_KEY, the same key and helpers as the Google refresh token,
  app/google_oauth.py). It is decrypted only to call the provider; it is never
  returned by the API, rendered on the page or logged.
- `notetaker_recordings`: one row per recording VALR has heard of (webhook, a "check
  now" sync, or an uploaded export): title, times, the share and embed links, the
  calendar event it matched, and the evaluation hand-off status. `sealed` holds the
  normalised meeting (transcript included) as Fernet ciphertext only until the
  evaluation took it; `clear_sealed` drops it.
"""
import json
from datetime import datetime
from typing import Any

from app import google_oauth

SCHEMA = """
CREATE TABLE IF NOT EXISTS notetaker_credentials (
    provider      text PRIMARY KEY,
    secret        text NOT NULL,            -- Fernet ciphertext (VALR_TOKEN_KEY)
    account       text NOT NULL DEFAULT '',
    status        text NOT NULL,            -- connected | refused
    connected_at  timestamptz NOT NULL DEFAULT now(),
    last_ok_at    timestamptz,
    last_error    text
);
CREATE TABLE IF NOT EXISTS notetaker_recordings (
    provider      text NOT NULL,
    external_id   text NOT NULL,
    title         text NOT NULL DEFAULT '',
    started_at    timestamptz,
    ended_at      timestamptz,
    share_url     text NOT NULL DEFAULT '',
    embed_url     text NOT NULL DEFAULT '',
    event_key     text,
    origin        text NOT NULL,            -- webhook | sync | import
    received_at   timestamptz NOT NULL DEFAULT now(),
    eval_status   text NOT NULL DEFAULT 'pending',  -- pending | handed_off | no_hook | failed
    eval_detail   text,
    sealed        text,                     -- Fernet ciphertext of the normalised meeting
    PRIMARY KEY (provider, external_id)
);
CREATE INDEX IF NOT EXISTS notetaker_recordings_by_start ON notetaker_recordings (started_at);
CREATE INDEX IF NOT EXISTS notetaker_recordings_by_event ON notetaker_recordings (event_key);
"""

FIELDS = ("provider", "external_id", "title", "started_at", "ended_at", "share_url", "embed_url",
          "event_key", "origin", "received_at", "eval_status", "eval_detail")


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


# Credentials.

async def save_key(connection: Any, token_key: str, provider: str, api_key: str, account: str) -> None:
    await connection.execute(
        "INSERT INTO notetaker_credentials (provider, secret, account, status, connected_at, last_ok_at) "
        "VALUES (%s, %s, %s, 'connected', now(), now()) ON CONFLICT (provider) DO UPDATE SET "
        "secret = excluded.secret, account = excluded.account, status = 'connected', "
        "connected_at = now(), last_ok_at = now(), last_error = NULL",
        (provider, google_oauth.encrypt(token_key, api_key), account[:200]))


async def delete_key(connection: Any, provider: str) -> bool:
    cursor = await connection.execute("DELETE FROM notetaker_credentials WHERE provider = %s", (provider,))
    return bool(getattr(cursor, "rowcount", 0))


async def load_key(connection: Any, token_key: str, provider: str) -> str | None:
    """The decrypted key, or None (not connected, or VALR_TOKEN_KEY changed)."""
    cursor = await connection.execute(
        "SELECT secret FROM notetaker_credentials WHERE provider = %s", (provider,))
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return google_oauth.decrypt(token_key, row[0])
    except google_oauth.ReconnectNeeded:
        return None


async def mark(connection: Any, provider: str, ok: bool, error: str = "") -> None:
    if ok:
        await connection.execute("UPDATE notetaker_credentials SET last_ok_at = now(), status = 'connected', "
                                 "last_error = NULL WHERE provider = %s", (provider,))
    else:
        await connection.execute("UPDATE notetaker_credentials SET status = 'refused', last_error = %s "
                                 "WHERE provider = %s", (error[:200], provider))


async def statuses(connection: Any) -> dict[str, dict[str, Any]]:
    """provider -> {state, account, connected_at, last_ok_at, last_error}; never the key."""
    cursor = await connection.execute(
        "SELECT provider, status, account, connected_at, last_ok_at, last_error FROM notetaker_credentials")
    return {row[0]: {"state": row[1], "account": row[2], "connected_at": row[3], "last_ok_at": row[4],
                     "last_error": row[5]} for row in await cursor.fetchall()}


# Recordings.

async def upsert_recording(connection: Any, recording: dict[str, Any], origin: str,
                           sealed: str | None = None) -> bool:
    """Insert or refresh one recording's links; True when it is new."""
    cursor = await connection.execute(
        "INSERT INTO notetaker_recordings (provider, external_id, title, started_at, ended_at, share_url, "
        "embed_url, origin, sealed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET title = excluded.title, "
        "started_at = COALESCE(excluded.started_at, notetaker_recordings.started_at), "
        "ended_at = COALESCE(excluded.ended_at, notetaker_recordings.ended_at), "
        "share_url = COALESCE(NULLIF(excluded.share_url, ''), notetaker_recordings.share_url), "
        "embed_url = COALESCE(NULLIF(excluded.embed_url, ''), notetaker_recordings.embed_url), "
        "sealed = CASE WHEN notetaker_recordings.eval_status = 'handed_off' THEN NULL "
        "ELSE COALESCE(excluded.sealed, notetaker_recordings.sealed) END "
        "RETURNING (xmax = 0)",
        (recording["provider"], recording["external_id"], recording.get("title", "")[:300],
         _dt(recording.get("start")), _dt(recording.get("end")), recording.get("share_url", ""),
         recording.get("embed_url", ""), origin, sealed))
    row = await cursor.fetchone()
    return bool(row and row[0])


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def set_eval(connection: Any, provider: str, external_id: str, status: str, detail: str = "") -> None:
    await connection.execute(
        "UPDATE notetaker_recordings SET eval_status = %s, eval_detail = %s "
        "WHERE provider = %s AND external_id = %s", (status, detail[:300], provider, external_id))


async def set_event(connection: Any, provider: str, external_id: str, event_key: str) -> None:
    await connection.execute(
        "UPDATE notetaker_recordings SET event_key = %s WHERE provider = %s AND external_id = %s",
        (event_key, provider, external_id))


async def recording(connection: Any, provider: str, external_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        f"SELECT {', '.join(FIELDS)} FROM notetaker_recordings WHERE provider = %s AND external_id = %s",
        (provider, external_id))
    row = await cursor.fetchone()
    return dict(zip(FIELDS, row)) if row else None


async def by_event(connection: Any, event_key: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        f"SELECT {', '.join(FIELDS)} FROM notetaker_recordings WHERE event_key = %s "
        "ORDER BY received_at DESC LIMIT 1", (event_key,))
    row = await cursor.fetchone()
    return dict(zip(FIELDS, row)) if row else None


async def pending(connection: Any, limit: int = 20) -> list[dict[str, Any]]:
    cursor = await connection.execute(
        f"SELECT {', '.join(FIELDS)} FROM notetaker_recordings WHERE eval_status IN ('pending', 'no_hook') "
        "ORDER BY received_at LIMIT %s", (limit,))
    return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]


async def sealed_meeting(connection: Any, token_key: str, provider: str, external_id: str) -> dict | None:
    cursor = await connection.execute(
        "SELECT sealed FROM notetaker_recordings WHERE provider = %s AND external_id = %s",
        (provider, external_id))
    row = await cursor.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(google_oauth.decrypt(token_key, row[0]))
    except (google_oauth.ReconnectNeeded, ValueError):
        return None


async def clear_sealed(connection: Any, provider: str, external_id: str) -> None:
    await connection.execute("UPDATE notetaker_recordings SET sealed = NULL "
                             "WHERE provider = %s AND external_id = %s", (provider, external_id))
