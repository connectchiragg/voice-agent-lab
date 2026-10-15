"""Caller memory: recognise a returning caller, recall the last call, forget on request.

Who is calling, as a normalized `caller_key`:

- phone (Telnyx): the E.164 `from` of the call.initiated webhook -> "tel:+919812345678";
- web: Cloudflare Access's authenticated email header -> "email:someone@example.com",
  read only with CF_ACCESS_EMAIL_HEADER=1 (anyone can send that header to an
  origin that is reachable without Access).

An unknown caller (no key) gets no memory and leaves none.

Per call:

1. `begin_call` loads the caller's memory beside the Deepgram sockets, for at
   most LOAD_TIMEOUT_MS; a slower load is dropped and the call starts without
   memory. The agent adds `prompt_block` (about 150 tokens at most) to the
   system prompt.
2. `end_call` (the call has closed) starts a background task: the LLM writes a
   short summary of the dialogue and updates the rolling profile, and both are
   stored. A failure is stored as such and never retried.
3. The `forget_me` tool calls `forget`: everything stored for the caller is
   deleted and the current call is not summarized.

Storage: tables `callers` and `call_summaries` in the app's Postgres, created if
missing at startup; in memory (lost on restart) when Postgres is unavailable.
Only summaries, facts and open items are stored here, never transcripts.
"""
import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, ValidationError

from app.llm import stream_model

logger = logging.getLogger("uvicorn.error")
LOAD_TIMEOUT_MS = 150  # Past this the call starts without memory (CALLER_MEMORY_LOAD_TIMEOUT_MS).
SUMMARY_TIMEOUT_S = 30.0
DRAIN_S = 10.0  # On shutdown, summaries still running get this long to finish.
MAX_DIALOGUE_CHARS = 6000  # About 1,500 input tokens: Groq's free tier allows 7,000 a minute.
MAX_BLOCK_TOKENS = 150
CHARS_PER_TOKEN = 4  # An estimate for English text; no tokenizer is loaded for one prompt line.
WEB_EMAIL_HEADER = "cf-access-authenticated-user-email"

E164 = re.compile(r"\+[1-9]\d{6,14}")
EMAIL = re.compile(r"[^@\s:<>\"]+@[^@\s:<>\"]+\.[^@\s:<>\"]+")


# Identity.

def caller_key_from_phone(number: object) -> str | None:
    """E.164 "+91 98123-45678" -> "tel:+919812345678"; None for anonymous, SIP or national numbers."""
    if not isinstance(number, str):
        return None
    compact = re.sub(r"[\s().-]", "", number)
    return f"tel:{compact}" if E164.fullmatch(compact) else None


def caller_key_from_email(email: object) -> str | None:
    """" Someone@Example.com " -> "email:someone@example.com"; None unless it looks like one address."""
    if not isinstance(email, str):
        return None
    address = email.strip().lower()
    return f"email:{address}" if len(address) <= 254 and EMAIL.fullmatch(address) else None


def web_caller_key(headers: Mapping[str, str]) -> str | None:
    """The Cloudflare Access user of a browser call, when that header is trusted."""
    if os.getenv("CF_ACCESS_EMAIL_HEADER", "0") != "1":
        return None
    return caller_key_from_email(headers.get(WEB_EMAIL_HEADER))


# Storage.

@dataclass(frozen=True)
class CallerMemory:
    caller_key: str
    display_name: str | None
    first_seen: datetime
    last_seen: datetime
    call_count: int
    profile: str
    open_items: list[str]
    last_summary: str | None  # The latest call that was summarized.


@dataclass(frozen=True)
class CallRecord:
    """One finished call, and the profile update it brings (None keeps a field)."""
    call_id: str
    started_at: datetime
    ended_at: datetime
    summary: str | None  # None: summarizing failed.
    facts: list[str] = field(default_factory=list)
    error: str | None = None  # The exception class, never its message.
    display_name: str | None = None
    profile: str | None = None
    open_items: list[str] | None = None


class MemoryStore(Protocol):
    async def load(self, caller_key: str) -> CallerMemory | None: ...
    async def record_call(self, caller_key: str, record: CallRecord) -> None: ...
    async def forget(self, caller_key: str) -> bool: ...  # True if anything was stored.


class InMemoryStore:
    """Without Postgres: the same behaviour, lost on restart."""

    def __init__(self) -> None:
        self.callers: dict[str, CallerMemory] = {}
        self.summaries: dict[str, tuple[str, CallRecord]] = {}  # call_id -> (caller_key, record)

    async def load(self, caller_key: str) -> CallerMemory | None:
        return self.callers.get(caller_key)

    async def record_call(self, caller_key: str, record: CallRecord) -> None:
        if record.call_id in self.summaries:
            return
        self.summaries[record.call_id] = (caller_key, record)
        known = self.callers.get(caller_key)
        if known is None:
            known = CallerMemory(caller_key, None, record.started_at, record.ended_at, 0, "", [], None)
        self.callers[caller_key] = replace(
            known, call_count=known.call_count + 1, last_seen=max(known.last_seen, record.ended_at),
            display_name=record.display_name or known.display_name,
            profile=known.profile if record.profile is None else record.profile,
            open_items=known.open_items if record.open_items is None else record.open_items,
            last_summary=record.summary or known.last_summary)

    async def forget(self, caller_key: str) -> bool:
        self.summaries = {call_id: entry for call_id, entry in self.summaries.items()
                          if entry[0] != caller_key}
        return self.callers.pop(caller_key, None) is not None


SCHEMA = """
CREATE TABLE IF NOT EXISTS callers (
    caller_key   text PRIMARY KEY,
    display_name text,
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL,
    call_count   integer NOT NULL,
    profile      text NOT NULL DEFAULT '',
    open_items   jsonb NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS call_summaries (
    call_id    text PRIMARY KEY,
    caller_key text NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at   timestamptz NOT NULL,
    summary    text,
    facts      jsonb NOT NULL DEFAULT '[]',
    error      text
);
CREATE INDEX IF NOT EXISTS call_summaries_by_caller ON call_summaries (caller_key, ended_at DESC);
"""


class PostgresStore:
    """The tables live beside LangGraph's checkpoints, in the same database."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def setup(self) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(SCHEMA)

    async def load(self, caller_key: str) -> CallerMemory | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT display_name, first_seen, last_seen, call_count, profile, open_items, "
                "(SELECT summary FROM call_summaries s WHERE s.caller_key = c.caller_key "
                " AND summary IS NOT NULL ORDER BY ended_at DESC LIMIT 1) "
                "FROM callers c WHERE caller_key = %s", (caller_key,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return CallerMemory(caller_key, row[0], row[1], row[2], row[3], row[4], list(row[5]), row[6])

    async def record_call(self, caller_key: str, record: CallRecord) -> None:
        async with self.pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "INSERT INTO call_summaries (call_id, caller_key, started_at, ended_at, summary, "
                "facts, error) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (call_id) DO NOTHING RETURNING call_id",
                (record.call_id, caller_key, record.started_at, record.ended_at, record.summary,
                 Jsonb(record.facts), record.error))
            if await cursor.fetchone() is None:
                return  # Already recorded.
            open_items = None if record.open_items is None else Jsonb(record.open_items)
            await connection.execute(
                "INSERT INTO callers (caller_key, display_name, first_seen, last_seen, call_count, "
                "profile, open_items) VALUES (%(key)s, %(name)s, %(started)s, %(ended)s, 1, "
                "coalesce(%(profile)s, ''), coalesce(%(open_items)s, '[]'::jsonb)) "
                "ON CONFLICT (caller_key) DO UPDATE SET "
                "display_name = coalesce(%(name)s, callers.display_name), "
                "last_seen = greatest(callers.last_seen, %(ended)s), "
                "call_count = callers.call_count + 1, "
                "profile = coalesce(%(profile)s, callers.profile), "
                "open_items = coalesce(%(open_items)s, callers.open_items)",
                {"key": caller_key, "name": record.display_name, "started": record.started_at,
                 "ended": record.ended_at, "profile": record.profile, "open_items": open_items})

    async def forget(self, caller_key: str) -> bool:
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute("DELETE FROM call_summaries WHERE caller_key = %s", (caller_key,))
            cursor = await connection.execute(
                "DELETE FROM callers WHERE caller_key = %s RETURNING caller_key", (caller_key,))
            return await cursor.fetchone() is not None


# Calls in progress, and calls being summarized.

@dataclass(eq=False)
class CallerCall:
    caller_key: str
    started_at: datetime
    memory: CallerMemory | None = None  # Loaded in time at the start, if known.
    ended: bool = False
    forgotten: bool = False  # forget_me: show and store nothing more.


store: MemoryStore | None = None  # None: caller memory is off.
calls: dict[str, CallerCall] = {}  # By conversation id.
tasks: set[asyncio.Task[None]] = set()


def load_timeout_s() -> float:
    return float(os.getenv("CALLER_MEMORY_LOAD_TIMEOUT_MS", LOAD_TIMEOUT_MS)) / 1000


async def begin_call(conversation_id: str, caller_key: str | None) -> None:
    """Load a known caller's memory, giving up after the load timeout. Never raises."""
    if store is None or caller_key is None:
        return
    call = CallerCall(caller_key, datetime.now(timezone.utc))
    calls[conversation_id] = call
    started = time.perf_counter()
    # Shielded: a slow query is left to finish on its own rather than cancelled
    # mid-protocol; its result is simply not used.
    loading = asyncio.ensure_future(store.load(caller_key))
    try:
        call.memory = await asyncio.wait_for(asyncio.shield(loading), load_timeout_s())
    except TimeoutError:
        loading.add_done_callback(lambda task: task.cancelled() or task.exception())
        logger.warning("Caller memory took over %.0f ms to load; this call starts without it",
                       load_timeout_s() * 1000)
        return
    except Exception as error:
        logger.warning("Caller memory failed to load: %s", type(error).__name__)
        return
    logger.info("Caller memory: %s caller, loaded in %.0f ms",
                "returning" if call.memory else "new", (time.perf_counter() - started) * 1000)


def caller_of(conversation_id: str) -> str | None:
    call = calls.get(conversation_id)
    return call.caller_key if call is not None else None


def prompt_block(conversation_id: str) -> str:
    """The caller memory for this call's system prompt, or "" without one."""
    call = calls.get(conversation_id)
    if call is None or call.forgotten or call.memory is None:
        return ""
    return render_block(call.memory)


Dialogue = Sequence[Mapping[str, Any]]


def end_call(conversation_id: str, load_dialogue: Callable[[str], Awaitable[Dialogue]]) -> None:
    """The call has closed: summarize it in the background. Returns at once."""
    call = calls.get(conversation_id)
    if call is None or call.ended:
        return
    call.ended = True
    if call.forgotten or store is None:
        calls.pop(conversation_id, None)
        return
    task = asyncio.create_task(remember_call(conversation_id, call, load_dialogue))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def remember_call(conversation_id: str, call: CallerCall,
                        load_dialogue: Callable[[str], Awaitable[Dialogue]]) -> None:
    """Summarize a finished call and store it; a failed summary is stored as failed."""
    try:
        dialogue = dialogue_text(await load_dialogue(conversation_id))
        if not dialogue or store is None:
            return  # Nothing was said: no visit to remember.
        ended_at = datetime.now(timezone.utc)
        previous = await store.load(call.caller_key)
        try:
            notes = await asyncio.wait_for(summarize(previous, dialogue), SUMMARY_TIMEOUT_S)
            record = CallRecord(conversation_id, call.started_at, ended_at, notes.summary,
                                notes.facts, display_name=notes.name, profile=notes.profile,
                                open_items=notes.open_items)
        except Exception as error:
            logger.warning("Caller memory: summarizing the call failed: %s", type(error).__name__)
            record = CallRecord(conversation_id, call.started_at, ended_at, None,
                                error=type(error).__name__)
        if call.forgotten:  # forget_me from another call while this one was summarized.
            return
        await store.record_call(call.caller_key, record)
        logger.info("Caller memory: call %s", "summarized" if record.summary else "recorded as failed")
    except Exception as error:
        logger.warning("Caller memory: storing the call failed: %s", type(error).__name__)
    finally:
        calls.pop(conversation_id, None)


async def forget(caller_key: str) -> bool:
    """Delete everything stored for the caller; their calls in progress store nothing more."""
    for call in calls.values():
        if call.caller_key == caller_key:
            call.forgotten = True
    if store is None:
        return False
    return await store.forget(caller_key)


# Summaries.

SUMMARY_PROMPT = (
    "You keep short notes about callers of a voice assistant, so that it can remember them "
    "next time. From the earlier notes and this call, reply with only a JSON object with "
    'these keys: "name": the name the caller gave for themselves, else null; "summary": two '
    'to four sentences about this call; "facts": up to five short preferences or facts the '
    'caller shared that would help next time; "open_items": up to five short unresolved '
    'items, such as "wants a callback" or "asked about Mysore weather", keeping earlier ones '
    'that are still open; "profile": the caller in at most three sentences, merging the '
    "earlier profile with this call. Write about the caller in the third person. Never "
    "include phone numbers, email or street addresses, account or card numbers, passwords, "
    "or health or financial details, and never quote the caller word for word."
)


class CallNotes(BaseModel):
    """The summarizer's reply, trimmed to size."""
    model_config = ConfigDict(extra="ignore")
    name: str | None = None
    summary: str
    facts: list[str] = []
    open_items: list[str] = []
    profile: str = ""


def dialogue_text(messages: Dialogue) -> str:
    """What was said, latest last, capped at MAX_DIALOGUE_CHARS (tool traffic left out)."""
    lines = []
    for message in messages:
        text = message["parts"][0]["text"].strip() if message.get("parts") else ""
        if text and message["role"] in ("user", "model"):
            lines.append(f"{'Caller' if message['role'] == 'user' else 'Assistant'}: {text}")
    if not any(line.startswith("Caller:") for line in lines):
        return ""
    return "\n".join(lines)[-MAX_DIALOGUE_CHARS:]


def notes_prompt(previous: CallerMemory | None, dialogue: str) -> str:
    earlier = "None: this is their first call."
    if previous is not None:
        earlier = (f"Name: {previous.display_name or 'unknown'}\n"
                   f"Profile: {previous.profile or 'none'}\n"
                   f"Open items: {'; '.join(previous.open_items) or 'none'}")
    return f"Earlier notes:\n{earlier}\n\nThis call:\n{dialogue}"


async def summarize(previous: CallerMemory | None, dialogue: str) -> CallNotes:
    """One LLM request (with the usual hedge); raises if the reply is not usable notes."""
    messages = [{"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": notes_prompt(previous, dialogue)}]
    reply = "".join([event async for event in stream_model(messages) if isinstance(event, str)])
    return parse_notes(reply)


def clip(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a sentence or word end."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 3]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence >= limit // 2:
        return cut[:sentence + 1]
    return cut[:cut.rfind(" ")].rstrip(" ,;:") + "..." if " " in cut else cut + "..."


def parse_notes(reply: str) -> CallNotes:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise ValueError("The summary is not a JSON object")
    try:
        notes = CallNotes.model_validate(json.loads(reply[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ValueError("The summary is not valid notes") from error
    name = clip(notes.name or "", 60) or None
    if name is not None and (re.search(r"[\d@]", name) or name.lower() in ("null", "unknown")):
        name = None
    summary = clip(notes.summary, 600)
    if not summary:
        raise ValueError("The summary is empty")

    def items(values: list[str]) -> list[str]:
        return [clip(value, 120) for value in values if value.strip()][:5]
    return CallNotes(name=name, summary=summary, facts=items(notes.facts),
                     open_items=items(notes.open_items), profile=clip(notes.profile, 400))


# The prompt block.

BLOCK_RULES = ("Caller memory from earlier calls. Greet them by name if known; mention the "
               "last topic briefly only if natural; never read out these notes or private "
               "details unprompted.")


def estimated_tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def render_block(memory: CallerMemory, max_tokens: int = MAX_BLOCK_TOKENS) -> str:
    """Name, last call date, last summary and open items, within `max_tokens` (estimated).

    Kept whole in this order of priority: the rules, the name and date, the open
    items; the last summary gets what is left and is cut to fit.
    """
    budget = max_tokens * CHARS_PER_TOKEN
    zone = ZoneInfo(os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata"))
    head = [BLOCK_RULES]
    if memory.display_name:
        head.append(f"Name: {memory.display_name}.")
    head.append(f"Last call: {memory.last_seen.astimezone(zone):%d %B %Y}.")
    items = list(memory.open_items)
    while items and len(" ".join(head + [open_items_line(items)])) > budget:
        items.pop()
    tail = [open_items_line(items)] if items else []
    lines = head
    if memory.last_summary:
        room = budget - len(" ".join(head + tail)) - len(" Last time: ")
        if room >= 40:
            lines = head + [f"Last time: {clip(memory.last_summary, room)}"]
    return " ".join(lines + tail)


def open_items_line(items: list[str]) -> str:
    return f"Open items: {'; '.join(items)}."


# App lifespan.

@asynccontextmanager
async def lifespan(conninfo: Callable[[], str]) -> AsyncIterator[None]:
    """Postgres when available, else memory; on shutdown, let summaries finish briefly."""
    global store
    if os.getenv("CALLER_MEMORY", "1") != "1":
        logger.info("Caller memory is off (CALLER_MEMORY=0).")
        yield
        return
    pool: AsyncConnectionPool | None = None
    try:
        pool = AsyncConnectionPool(conninfo(), min_size=1, max_size=4, open=False,
                                   kwargs={"autocommit": True})
        await pool.open(wait=True, timeout=5)
        postgres = PostgresStore(pool)
        await postgres.setup()
        store = postgres
    except Exception as error:
        logger.warning("Postgres unavailable for caller memory (%s); it is kept in memory "
                       "and lost on restart.", type(error).__name__)
        if pool is not None:
            await pool.close()
            pool = None
        store = InMemoryStore()
    try:
        yield
    finally:
        if tasks:
            _, late = await asyncio.wait(set(tasks), timeout=DRAIN_S)
            for task in late:
                task.cancel()
            await asyncio.gather(*late, return_exceptions=True)
        store = None
        calls.clear()
        if pool is not None:
            await pool.close()
