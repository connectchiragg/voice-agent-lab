"""Interview research context in Postgres: per slot, per company, per briefing block.

Shared by the research worker (writes) and the app (reads, for briefing calls and the
calendar tool). psycopg and the standard library only.

- `slot_context`: one row per calendar event key: the merged findings of each part
  (private, round, interviewers per email, and which company research it uses), the
  spoken brief and notes, the input fingerprint, status. A partial job (one new
  interviewer) overlays its part and keeps the rest.
- `company_research`: R3 findings per company key, reused for 7 days by every slot.
- `briefing_blocks`: block id -> slot keys, start and label, so the answered call
  loads exactly that block's briefs.
- `interview_context_push`: optional per-interview email context POSTed by a
  Mac-side job (/valr/context), encrypted with VALR_TOKEN_KEY; R1 reads it.
- `valr_flags`: owner switches set from the dashboard. `dials_paused`: while true (and
  until its `until`, when set: "Pause for 1 day"), the block workflows skip their heads-ups
  and automatic briefing dials (on-demand callbacks the owner asks for on a call still
  ring). VALR_BRIEFING_DIAL=0 stays the hard off.
- `heads_up_log`: per block, how its T-30 heads-up went (sent | no_device | failed).
"""
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Any

COMPANY_TTL = timedelta(days=7)
SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_context (
    key          text PRIMARY KEY,
    company_key  text,
    title        text,
    start_utc    timestamptz,
    parts        jsonb NOT NULL DEFAULT '{}'::jsonb,
    spoken       text,
    notes        jsonb,
    sources      jsonb,
    status       text NOT NULL DEFAULT 'pending',
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS company_research (
    company_key  text PRIMARY KEY,
    company      text,
    findings     jsonb NOT NULL,
    fetched_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS briefing_blocks (
    block_id     text PRIMARY KEY,
    start_utc    timestamptz NOT NULL,
    slot_keys    jsonb NOT NULL,
    label        text,
    state        text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS interview_context_push (
    id           bigserial PRIMARY KEY,
    event_key    text,
    company      text,
    sealed       text NOT NULL,      -- Fernet ciphertext of the pushed JSON
    received_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS interview_context_push_by_company ON interview_context_push (lower(company));
CREATE TABLE IF NOT EXISTS valr_flags (
    name         text PRIMARY KEY,
    enabled      boolean NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE valr_flags ADD COLUMN IF NOT EXISTS until timestamptz;
CREATE TABLE IF NOT EXISTS heads_up_log (
    block_id     text PRIMARY KEY,
    outcome      text NOT NULL,       -- sent | no_device | failed | duplicate
    sent_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prep_checks (
    slot_ref     text NOT NULL,      -- the interview (slot workflow id)
    item         text NOT NULL,      -- sha256 of the prep line, so a reworded line starts unticked
    done         boolean NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (slot_ref, item)
);
"""


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


async def load_slot(connection: Any, key: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        "SELECT key, company_key, title, start_utc, parts, spoken, notes, sources, status, updated_at "
        "FROM slot_context WHERE key = %s", (key,))
    row = await cursor.fetchone()
    if row is None:
        return None
    names = ("key", "company_key", "title", "start_utc", "parts", "spoken", "notes", "sources",
             "status", "updated_at")
    slot = dict(zip(names, row))
    slot["parts"] = slot["parts"] or {}
    slot["notes"] = slot["notes"] or []
    slot["sources"] = slot["sources"] or []
    return slot


async def load_slots(connection: Any, keys: list[str]) -> list[dict[str, Any]]:
    slots = [await load_slot(connection, key) for key in keys]
    return [slot for slot in slots if slot is not None]


async def save_parts(connection: Any, key: str, company_key: str, title: str, start_utc: str,
                     parts: dict[str, Any]) -> None:
    await connection.execute(
        "INSERT INTO slot_context (key, company_key, title, start_utc, parts, status) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, 'researching') ON CONFLICT (key) DO UPDATE SET "
        "company_key = excluded.company_key, title = excluded.title, start_utc = excluded.start_utc, "
        "parts = excluded.parts, updated_at = now()",
        (key, company_key, title, start_utc, json.dumps(parts)))


async def save_brief(connection: Any, key: str, spoken: str, notes: list[dict[str, Any]],
                     sources: list[dict[str, Any]], status: str) -> None:
    await connection.execute(
        "UPDATE slot_context SET spoken = %s, notes = %s::jsonb, sources = %s::jsonb, status = %s, "
        "updated_at = now() WHERE key = %s",
        (spoken, json.dumps(notes), json.dumps(sources), status, key))


async def company(connection: Any, company_key: str,
                  now: datetime | None = None) -> dict[str, Any] | None:
    """Fresh (under 7 days) company research, or None."""
    cursor = await connection.execute(
        "SELECT findings, fetched_at FROM company_research WHERE company_key = %s", (company_key,))
    row = await cursor.fetchone()
    if row is None or (now or datetime.now(timezone.utc)) - row[1] > COMPANY_TTL:
        return None
    return row[0]


async def save_company(connection: Any, company_key: str, name: str,
                       findings: dict[str, Any]) -> None:
    await connection.execute(
        "INSERT INTO company_research (company_key, company, findings, fetched_at) "
        "VALUES (%s, %s, %s::jsonb, now()) ON CONFLICT (company_key) DO UPDATE SET "
        "company = excluded.company, findings = excluded.findings, fetched_at = now()",
        (company_key, name, json.dumps(findings)))


async def record_block(connection: Any, block_id: str, start_utc: str, slot_keys: list[str],
                       label: str, state: str) -> None:
    await connection.execute(
        "INSERT INTO briefing_blocks (block_id, start_utc, slot_keys, label, state) "
        "VALUES (%s, %s, %s::jsonb, %s, %s) ON CONFLICT (block_id) DO UPDATE SET "
        "slot_keys = excluded.slot_keys, label = excluded.label, state = excluded.state, "
        "updated_at = now()", (block_id, start_utc, json.dumps(slot_keys), label, state))


async def block(connection: Any, block_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        "SELECT block_id, start_utc, slot_keys, label, state FROM briefing_blocks WHERE block_id = %s",
        (block_id,))
    row = await cursor.fetchone()
    return dict(zip(("block_id", "start_utc", "slot_keys", "label", "state"), row)) if row else None


async def blocks_between(connection: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    cursor = await connection.execute(
        "SELECT block_id, start_utc, slot_keys, label, state FROM briefing_blocks "
        "WHERE start_utc >= %s AND start_utc < %s ORDER BY start_utc", (start, end))
    return [dict(zip(("block_id", "start_utc", "slot_keys", "label", "state"), row))
            for row in await cursor.fetchall()]


async def ready(connection: Any, keys: list[str]) -> bool:
    if not keys:
        return True
    cursor = await connection.execute(
        "SELECT count(*) FROM slot_context WHERE key = ANY(%s) AND status IN "
        "('done', 'partial', 'apologized')", (keys,))
    row = await cursor.fetchone()
    return bool(row and row[0] >= len(keys))


async def push_context(connection: Any, sealed: str, event_key: str, company_name: str) -> None:
    await connection.execute(
        "INSERT INTO interview_context_push (event_key, company, sealed) VALUES (%s, %s, %s)",
        (event_key or None, company_name or None, sealed))


async def pushed_context(connection: Any, event_key: str, company_name: str,
                         limit: int = 5) -> list[str]:
    """Sealed pushes for this event or company, newest first (decrypted by the caller)."""
    cursor = await connection.execute(
        "SELECT sealed FROM interview_context_push WHERE event_key = %s OR lower(company) = lower(%s) "
        "ORDER BY received_at DESC LIMIT %s", (event_key, company_name or "\x00", limit))
    return [row[0] for row in await cursor.fetchall()]


DIALS_PAUSED = "dials_paused"
AUTO_DIAL = "auto_dial"  # Set from Settings: true = automatic T-30 call, false = a push heads-up.


async def flag(connection: Any, name: str) -> dict[str, Any]:
    """{enabled, updated_at, until}; a flag never set is off, and one past its `until` too."""
    cursor = await connection.execute(
        "SELECT enabled, updated_at, until FROM valr_flags WHERE name = %s", (name,))
    row = await cursor.fetchone()
    if not row:
        return {"enabled": False, "updated_at": None, "until": None}
    until = row[2] if len(row) > 2 else None
    expired = until is not None and until <= datetime.now(timezone.utc)
    return {"enabled": bool(row[0]) and not expired, "updated_at": row[1], "until": None if expired else until}


async def set_flag(connection: Any, name: str, enabled: bool, until: datetime | None = None) -> None:
    await connection.execute(
        "INSERT INTO valr_flags (name, enabled, updated_at, until) VALUES (%s, %s, now(), %s) "
        "ON CONFLICT (name) DO UPDATE SET enabled = excluded.enabled, updated_at = now(), until = excluded.until",
        (name, enabled, until))


async def flag_choice(connection: Any, name: str) -> bool | None:
    """The stored value, or None when the owner never chose (the server default applies)."""
    cursor = await connection.execute("SELECT enabled FROM valr_flags WHERE name = %s", (name,))
    row = await cursor.fetchone()
    return bool(row[0]) if row else None


def default_auto_dial() -> bool:
    """VALR_AUTO_DIAL=1: the automatic T-30 call instead of the heads-up, until Settings says."""
    return os.getenv("VALR_AUTO_DIAL", "0").strip() == "1"


async def auto_dial(connection: Any) -> bool:
    """How VALR reaches the owner at T-30: the Settings choice, else VALR_AUTO_DIAL (default off)."""
    chosen = await flag_choice(connection, AUTO_DIAL)
    return default_auto_dial() if chosen is None else chosen


async def record_heads_up(connection: Any, block_id: str, outcome: str) -> None:
    await connection.execute(
        "INSERT INTO heads_up_log (block_id, outcome) VALUES (%s, %s) ON CONFLICT (block_id) "
        "DO UPDATE SET outcome = excluded.outcome, sent_at = now()", (block_id, outcome[:40]))


async def heads_ups(connection: Any, block_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not block_ids:
        return {}
    cursor = await connection.execute(
        "SELECT block_id, outcome, sent_at FROM heads_up_log WHERE block_id = ANY(%s)", (block_ids,))
    return {row[0]: {"block_id": row[0], "outcome": "sent" if row[1] in ("sent", "duplicate") else
                     "failed" if row[1] != "no_device" else "no_device", "sent_at": row[2]}
            for row in await cursor.fetchall()}


def prep_item(text: str) -> str:
    import hashlib
    return hashlib.sha256(" ".join(text.split()).lower().encode()).hexdigest()[:32]


async def set_prep_check(connection: Any, slot_ref: str, item: str, done: bool) -> None:
    await connection.execute(
        "INSERT INTO prep_checks (slot_ref, item, done, updated_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (slot_ref, item) DO UPDATE SET done = excluded.done, updated_at = now()",
        (slot_ref, item, done))


async def prep_checks(connection: Any, slot_refs: list[str]) -> dict[str, set[str]]:
    """slot ref -> the item hashes ticked done."""
    if not slot_refs:
        return {}
    cursor = await connection.execute(
        "SELECT slot_ref, item FROM prep_checks WHERE done AND slot_ref = ANY(%s)", (slot_refs,))
    out: dict[str, set[str]] = {}
    for ref, item in await cursor.fetchall():
        out.setdefault(ref, set()).add(item)
    return out


async def slot_jobs(connection: Any, keys: list[str], limit: int = 200) -> list[dict[str, Any]]:
    """Research runs for these slots, newest first: status, cost, duration, cache detail."""
    if not keys:
        return []
    cursor = await connection.execute(
        "SELECT event_key, status, created_at, duration_s, spend_usd, detail FROM research_jobs "
        "WHERE event_key = ANY(%s) ORDER BY created_at DESC LIMIT %s", (keys, limit))
    names = ("event_key", "status", "created_at", "duration_s", "spend_usd", "detail")
    return [dict(zip(names, row)) for row in await cursor.fetchall()]
