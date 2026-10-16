"""Postgres for the lead tracker (same database as the rest of VALR; created if missing).

- `gmail_state`: one row: the account, the last processed historyId, the watch's expiry,
  the backfill's progress, the last push and sync, the last error (a class name only).
- `lead_emails`: one row per Gmail message ever looked at: its decision, how it was made,
  the category and confidence. No subject, no text, no address: only the sender's domain.
  This is the de-duplication record and the decision log.
- `lead_touchpoints`: kept messages, calendar interviews, recordings and scores: derived
  metadata (title, a summary of at most ~24 words, people, cue, stage hint...). The Gmail
  snippet (at most 200 characters) is Fernet-encrypted with VALR_TOKEN_KEY, and never shown.
- `leads`: the result of the last rebuild (app/leads.build), read by the page.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from app.leads import Contact, Lead, Overrides, Touch

SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_state (
    id                  integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    account             text,
    history_id          bigint,
    watch_expires       timestamptz,
    watch_renewed_at    timestamptz,
    last_push_at        timestamptz,
    last_sync_at        timestamptz,
    backfill_started_at timestamptz,
    backfill_done_at    timestamptz,
    backfill_count      integer NOT NULL DEFAULT 0,
    last_error          text
);
CREATE TABLE IF NOT EXISTS lead_emails (
    message_id    text PRIMARY KEY,
    thread_id     text NOT NULL,
    received_at   timestamptz NOT NULL,
    sender_domain text NOT NULL DEFAULT '',
    decision      text NOT NULL,          -- keep | drop | deferred
    method        text NOT NULL,          -- heuristic | llm | deferred
    reason        text NOT NULL DEFAULT '',  -- heuristic code, or the model's short reason
    category      text NOT NULL DEFAULT '',
    confidence    real NOT NULL DEFAULT 0,
    usd           double precision NOT NULL DEFAULT 0,
    processed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_emails_by_thread ON lead_emails (thread_id);
CREATE INDEX IF NOT EXISTS lead_emails_by_time ON lead_emails (processed_at);
CREATE TABLE IF NOT EXISTS lead_touchpoints (
    id          text PRIMARY KEY,
    kind        text NOT NULL,
    at          timestamptz NOT NULL,
    lead_id     text NOT NULL DEFAULT '',
    data        jsonb NOT NULL,           -- app.leads.Touch without id, kind, at
    snippet_enc text,                     -- Fernet (VALR_TOKEN_KEY); emails only
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_touchpoints_by_time ON lead_touchpoints (at);
CREATE TABLE IF NOT EXISTS leads (
    id            text PRIMARY KEY,
    company       text NOT NULL,
    key           text NOT NULL,
    domain        text NOT NULL DEFAULT '',
    role          text NOT NULL DEFAULT '',
    stage         text NOT NULL,
    status        text NOT NULL,          -- active | expired
    temperature   integer NOT NULL,
    parts         jsonb NOT NULL,
    contacts      jsonb NOT NULL,
    touches       integer NOT NULL,
    last_touch_at timestamptz,
    next_step     text NOT NULL DEFAULT '',
    next_step_at  timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    archived_at   timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lead_overrides (
    id          bigserial PRIMARY KEY,
    lead_id     text NOT NULL,
    kind        text NOT NULL,             -- dismiss | stage | merge
    value       text NOT NULL DEFAULT '',  -- the stage ('' clears), or the lead merged into
    touch_ids   jsonb NOT NULL DEFAULT '[]',  -- dismiss: its touchpoints then
    threads     jsonb NOT NULL DEFAULT '[]',  -- dismiss: its Gmail threads then
    created_by  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE gmail_state ADD COLUMN IF NOT EXISTS reconnect_alerted_for timestamptz;
"""
STATE_COLUMNS = ("account", "history_id", "watch_expires", "watch_renewed_at", "last_push_at",
                 "last_sync_at", "backfill_started_at", "backfill_done_at", "backfill_count",
                 "last_error", "reconnect_alerted_for")


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)
    await connection.execute("INSERT INTO gmail_state (id) VALUES (1) ON CONFLICT DO NOTHING")


# The owner's corrections.

async def add_override(connection: Any, lead_id: str, kind: str, value: str = "",
                       touch_ids: list[str] | None = None, threads: list[str] | None = None,
                       by: str = "") -> None:
    if kind not in ("dismiss", "stage", "merge"):
        raise ValueError(kind)
    await connection.execute(
        "INSERT INTO lead_overrides (lead_id, kind, value, touch_ids, threads, created_by) "
        "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s)",
        (lead_id, kind, value, json.dumps(touch_ids or []), json.dumps(threads or []), by))


async def overrides(connection: Any) -> Overrides:
    """Replayed in order: the latest stage per lead wins ('' clears), merges accumulate."""
    cursor = await connection.execute(
        "SELECT lead_id, kind, value, touch_ids, threads FROM lead_overrides ORDER BY id")
    out = Overrides()
    for lead_id, kind, value, touch_ids, threads in await cursor.fetchall():
        if kind == "dismiss":
            out.dismissed_touches.update(touch_ids or [])
            out.dismissed_threads.update(threads or [])
        elif kind == "stage":
            if value:
                out.stages[lead_id] = value
            else:
                out.stages.pop(lead_id, None)
        elif kind == "merge" and value and value != lead_id:
            out.merges[lead_id] = value
    return out


async def load_state(connection: Any) -> dict[str, Any]:
    cursor = await connection.execute(f"SELECT {', '.join(STATE_COLUMNS)} FROM gmail_state WHERE id = 1")
    row = await cursor.fetchone()
    return dict(zip(STATE_COLUMNS, row)) if row else {}


async def save_state(connection: Any, **values: Any) -> None:
    unknown = set(values) - set(STATE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown gmail_state columns: {sorted(unknown)}")
    if values:
        columns = ", ".join(f"{name} = %s" for name in values)
        await connection.execute(f"UPDATE gmail_state SET {columns} WHERE id = 1", tuple(values.values()))


async def seen(connection: Any, message_ids: list[str]) -> set[str]:
    """Ids already decided (deferred ones are not: they are retried)."""
    if not message_ids:
        return set()
    cursor = await connection.execute(
        "SELECT message_id FROM lead_emails WHERE message_id = ANY(%s) AND decision <> 'deferred'",
        (message_ids,))
    return {row[0] for row in await cursor.fetchall()}


async def deferred(connection: Any, limit: int) -> list[str]:
    cursor = await connection.execute(
        "SELECT message_id FROM lead_emails WHERE decision = 'deferred' ORDER BY received_at DESC LIMIT %s",
        (limit,))
    return [row[0] for row in await cursor.fetchall()]


async def kept_threads(connection: Any, thread_ids: list[str]) -> set[str]:
    if not thread_ids:
        return set()
    cursor = await connection.execute(
        "SELECT DISTINCT thread_id FROM lead_emails WHERE thread_id = ANY(%s) AND decision = 'keep'",
        (thread_ids,))
    return {row[0] for row in await cursor.fetchall()}


async def record_email(connection: Any, message_id: str, thread_id: str, received_at: datetime,
                       sender_domain: str, decision: str, method: str, reason: str,
                       category: str, confidence: float, usd: float = 0.0) -> None:
    await connection.execute(
        "INSERT INTO lead_emails (message_id, thread_id, received_at, sender_domain, decision, method, "
        "reason, category, confidence, usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (message_id) DO UPDATE SET decision = excluded.decision, method = excluded.method, "
        "reason = excluded.reason, category = excluded.category, confidence = excluded.confidence, "
        "usd = lead_emails.usd + excluded.usd, processed_at = now()",
        (message_id, thread_id, received_at, sender_domain[:120], decision, method, reason[:160],
         category, confidence, usd))


async def llm_calls_today(connection: Any) -> int:
    cursor = await connection.execute(
        "SELECT count(*) FROM lead_emails WHERE method = 'llm' AND "
        "processed_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'")
    return int((await cursor.fetchone())[0])


async def decision_counts(connection: Any, days: int = 30) -> dict[str, int]:
    cursor = await connection.execute(
        "SELECT decision || ':' || method, count(*) FROM lead_emails "
        "WHERE processed_at >= now() - make_interval(days => %s) GROUP BY 1", (days,))
    return {row[0]: int(row[1]) for row in await cursor.fetchall()}


# Touchpoints.

def touch_data(touch: Touch) -> dict[str, Any]:
    data = asdict(touch)
    for name in ("id", "kind", "at"):
        data.pop(name)
    return data


def touch_from_row(touch_id: str, kind: str, at: datetime, lead_id: str, data: dict[str, Any]) -> Touch:
    data = dict(data)
    data["people"] = [Contact(**p) for p in data.get("people") or []]
    data["lead_id"] = lead_id
    known = set(Touch.__dataclass_fields__)
    return Touch(id=touch_id, kind=kind, at=at, **{k: v for k, v in data.items() if k in known and k not in ("id", "kind", "at")})


async def upsert_touch(connection: Any, touch: Touch, snippet_enc: str | None = None) -> None:
    await connection.execute(
        "INSERT INTO lead_touchpoints (id, kind, at, lead_id, data, snippet_enc, updated_at) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, now()) ON CONFLICT (id) DO UPDATE SET "
        "kind = excluded.kind, at = excluded.at, data = excluded.data, "
        "snippet_enc = coalesce(excluded.snippet_enc, lead_touchpoints.snippet_enc), updated_at = now()",
        (touch.id, touch.kind, touch.at, touch.lead_id, json.dumps(touch_data(touch)), snippet_enc))


async def touches(connection: Any) -> list[Touch]:
    cursor = await connection.execute("SELECT id, kind, at, lead_id, data FROM lead_touchpoints ORDER BY at")
    return [touch_from_row(*row) for row in await cursor.fetchall()]


# Leads.

async def save_leads(connection: Any, leads: list[Lead], all_touches: list[Touch], now: datetime) -> None:
    """Replace the leads table with this rebuild and record each touchpoint's lead."""
    async with connection.transaction():
        ids = [lead.id for lead in leads]
        await connection.execute("DELETE FROM leads WHERE NOT (id = ANY(%s))", (ids,))
        for lead in leads:
            contacts = [asdict(c) for c in lead.contacts.values()]
            await connection.execute(
                "INSERT INTO leads (id, company, key, domain, role, stage, status, temperature, parts, contacts, "
                "touches, last_touch_at, next_step, next_step_at, archived_at, updated_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET company = excluded.company, key = excluded.key, "
                "domain = excluded.domain, role = excluded.role, stage = excluded.stage, status = excluded.status, "
                "temperature = excluded.temperature, parts = excluded.parts, contacts = excluded.contacts, "
                "touches = excluded.touches, last_touch_at = excluded.last_touch_at, next_step = excluded.next_step, "
                "next_step_at = excluded.next_step_at, "
                "archived_at = CASE WHEN excluded.status = 'expired' THEN coalesce(leads.archived_at, %s) END, "
                "updated_at = now()",
                (lead.id, lead.company[:120], lead.key[:160], lead.domain, lead.role[:120], lead.stage, lead.status,
                 lead.temperature, json.dumps(lead.parts), json.dumps(contacts), len(lead.touches),
                 lead.last_touch_at, lead.next_step[:200], lead.next_step_at,
                 now if lead.status == "expired" else None, now))
        for touch in all_touches:
            await connection.execute("UPDATE lead_touchpoints SET lead_id = %s WHERE id = %s AND lead_id <> %s",
                                     (touch.lead_id, touch.id, touch.lead_id))
