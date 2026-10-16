"""Notetaker credentials and recordings in Postgres. psycopg and the standard library only.

- `notetaker_credentials`: one row per provider. The API key is Fernet ciphertext
  (VALR_TOKEN_KEY, the same key and helpers as the Google refresh token,
  app/google_oauth.py). It is decrypted only to call the provider; it is never
  returned by the API, rendered on the page or logged.
- `notetaker_recordings`: one row per recording VALR has heard of (webhook, a "check
  now" sync, or an uploaded export): title, times, the share and embed links, the
  calendar event it matched (its key, title, company and start are copied here, because
  calendar_events keeps only 3 days of the past), and the evaluation hand-off status. `sealed` holds the
  normalised meeting (transcript included) as Fernet ciphertext only until the
  evaluation took it; `clear_sealed` drops it. Fathom's calendar invitees add `company`,
  `company_domain` and `interviewers` (the external invitees' names), kept here because
  the calendar keeps only 3 days of the past; `invitee_hashes` holds a short SHA-256 of
  each external invitee's email (never the address), to match a Gmail lead's contacts.
- `recording_media`: the provider's signed download of a recording (Fathom's official
  download API, app/notetakers/media.py): its status and the signed URL, sealed with
  VALR_TOKEN_KEY like the keys, until it expires.
- `notetaker_hidden`: recordings the owner removed from Past interviews: "Hide" (a Fathom or
  Fireflies recording, which lives at the provider) or "Delete" (a phone upload, whose data
  is gone). Keeps them (and any scorecard of theirs) out of the list and off a re-sync.
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
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS event_title text;
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS event_company text;
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS event_start timestamptz;
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS company text;
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS company_domain text;
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS interviewers text;    -- JSON list of names
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS invitee_hashes text;  -- JSON list
ALTER TABLE notetaker_recordings ADD COLUMN IF NOT EXISTS company_how text;     -- app/company_resolver.py
CREATE TABLE IF NOT EXISTS recording_media (
    provider      text NOT NULL,
    external_id   text NOT NULL,
    download_id   text,
    status        text NOT NULL,            -- processing | completed | failed | expired | unavailable
    url           text,                     -- Fernet ciphertext (VALR_TOKEN_KEY) of the signed URL
    content_type  text,
    size          bigint,
    expires_at    timestamptz,
    error         text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, external_id)
);
CREATE TABLE IF NOT EXISTS notetaker_hidden (
    provider      text NOT NULL,
    external_id   text NOT NULL,
    how           text NOT NULL,            -- hidden (Fathom/Fireflies: kept at the provider) | deleted (phone)
    at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, external_id)
);
CREATE INDEX IF NOT EXISTS notetaker_recordings_by_start ON notetaker_recordings (started_at);
CREATE INDEX IF NOT EXISTS notetaker_recordings_by_event ON notetaker_recordings (event_key);
"""

FIELDS = ("provider", "external_id", "title", "started_at", "ended_at", "share_url", "embed_url",
          "event_key", "origin", "received_at", "eval_status", "eval_detail", "event_title",
          "event_company", "event_start", "company", "company_domain", "interviewers", "invitee_hashes", "company_how")
DETAILS = ("company", "company_domain", "interviewers", "invitee_hashes")


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
        "embed_url, origin, sealed, company, company_domain, interviewers, invitee_hashes) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET title = excluded.title, "
        "started_at = COALESCE(excluded.started_at, notetaker_recordings.started_at), "
        "ended_at = COALESCE(excluded.ended_at, notetaker_recordings.ended_at), "
        "share_url = COALESCE(NULLIF(excluded.share_url, ''), notetaker_recordings.share_url), "
        "embed_url = COALESCE(NULLIF(excluded.embed_url, ''), notetaker_recordings.embed_url), "
        + "".join(f"{c} = COALESCE(NULLIF(excluded.{c}, ''), notetaker_recordings.{c}), " for c in DETAILS) +
        "sealed = CASE WHEN notetaker_recordings.eval_status = 'handed_off' THEN NULL "
        "ELSE COALESCE(excluded.sealed, notetaker_recordings.sealed) END "
        "RETURNING (xmax = 0)",
        (recording["provider"], recording["external_id"], recording.get("title", "")[:300],
         _dt(recording.get("start")), _dt(recording.get("end")), recording.get("share_url", ""),
         recording.get("embed_url", ""), origin, sealed, *details_values(recording)))
    row = await cursor.fetchone()
    return bool(row and row[0])


def details_values(recording: dict[str, Any]) -> tuple[str, str, str, str]:
    """(company, company_domain, interviewers JSON, invitee_hashes JSON); "" when unknown."""
    names, hashes = recording.get("interviewers") or [], recording.get("invitee_hashes") or []
    return (str(recording.get("company") or "")[:120], str(recording.get("company_domain") or "")[:120],
            json.dumps(names[:8]) if names else "", json.dumps(hashes[:12]) if hashes else "")


async def set_details(connection: Any, recording: dict[str, Any]) -> bool:
    """Fill the invitee details of a recording VALR already stores (backfill); True when updated."""
    values = details_values(recording)
    if not any(values):
        return False
    cursor = await connection.execute(
        "UPDATE notetaker_recordings SET "
        + ", ".join(f"{c} = COALESCE(NULLIF(%s, ''), {c})" for c in DETAILS)
        + " WHERE provider = %s AND external_id = %s",
        (*values, recording["provider"], recording["external_id"]))
    return bool(getattr(cursor, "rowcount", 0))


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


async def set_event(connection: Any, provider: str, external_id: str, event_key: str, title: str = "",
                    company: str = "", start: Any = None) -> None:
    await connection.execute(
        "UPDATE notetaker_recordings SET event_key = %s, event_title = %s, event_company = %s, "
        "event_start = %s WHERE provider = %s AND external_id = %s",
        (event_key, title[:300], company[:120], _dt(start), provider, external_id))


async def between(connection: Any, start: datetime, end: datetime, limit: int = 200) -> list[dict[str, Any]]:
    """Recordings that started (or whose event started) in [start, end), newest first."""
    cursor = await connection.execute(
        f"SELECT {', '.join(FIELDS)} FROM notetaker_recordings "
        "WHERE COALESCE(event_start, started_at) >= %s AND COALESCE(event_start, started_at) < %s "
        "ORDER BY COALESCE(event_start, started_at) DESC LIMIT %s", (start, end, limit))
    return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]


async def recording(connection: Any, provider: str, external_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        f"SELECT {', '.join(FIELDS)} FROM notetaker_recordings WHERE provider = %s AND external_id = %s",
        (provider, external_id))
    row = await cursor.fetchone()
    return dict(zip(FIELDS, row)) if row else None


async def many(connection: Any, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Stored rows for these (provider, external_id) pairs, by pair."""
    if not keys:
        return {}
    cursor = await connection.execute(
        f"SELECT {', '.join(FIELDS)} FROM notetaker_recordings "
        "WHERE (provider || ':' || external_id) = ANY(%s)", ([f"{p}:{i}" for p, i in keys],))
    rows = [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]
    return {(r["provider"], r["external_id"]): r for r in rows}


def names(row: dict[str, Any] | None, field: str = "interviewers") -> list[str]:
    """A JSON list column -> list of strings ([] when empty or unreadable)."""
    try:
        value = json.loads((row or {}).get(field) or "[]")
    except ValueError:
        return []
    return [str(x) for x in value if isinstance(x, str)] if isinstance(value, list) else []


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


# Recording media (signed downloads).

MEDIA_FIELDS = ("download_id", "status", "url", "content_type", "size", "expires_at", "error", "updated_at")


async def get_media(connection: Any, provider: str, external_id: str) -> dict[str, Any] | None:
    """The stored download (url still sealed), or None."""
    cursor = await connection.execute(
        f"SELECT {', '.join(MEDIA_FIELDS)} FROM recording_media WHERE provider = %s AND external_id = %s",
        (provider, external_id))
    row = await cursor.fetchone()
    return dict(zip(MEDIA_FIELDS, row)) if row else None


async def save_media(connection: Any, provider: str, external_id: str, media: dict[str, Any]) -> None:
    """Insert or replace one download; `media["url"]` must already be sealed (or None)."""
    values = [media.get(k) for k in MEDIA_FIELDS[:-1]]
    await connection.execute(
        f"INSERT INTO recording_media (provider, external_id, {', '.join(MEDIA_FIELDS[:-1])}, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now()) ON CONFLICT (provider, external_id) DO UPDATE SET "
        + ", ".join(f"{k} = excluded.{k}" for k in MEDIA_FIELDS[:-1]) + ", updated_at = now()",
        (provider, external_id, *values))


# The owner's Hide / Delete (Past interviews).

async def hide(connection: Any, provider: str, external_id: str, how: str = "hidden") -> None:
    await connection.execute(
        "INSERT INTO notetaker_hidden (provider, external_id, how) VALUES (%s, %s, %s) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET how = EXCLUDED.how, at = now()",
        (provider, external_id, how))


async def hidden(connection: Any) -> set[tuple[str, str]]:
    cursor = await connection.execute("SELECT provider, external_id FROM notetaker_hidden")
    return {(p, x) for p, x in await cursor.fetchall()}


async def delete_recording(connection: Any, provider: str, external_id: str) -> None:
    """The recording's row (its sealed transcript too) and its cached media."""
    for table in ("notetaker_recordings", "recording_media"):
        await connection.execute(f"DELETE FROM {table} WHERE provider = %s AND external_id = %s",
                                 (provider, external_id))
