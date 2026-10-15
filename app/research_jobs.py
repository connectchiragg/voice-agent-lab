"""VALR (Voice Agent Lab Research): job records, spend, limits and the callback-time promise.

Shared by the app (booking, the dashboard) and the research worker (spend, results).
Only the standard library and psycopg: the app image never loads an LLM SDK for this.

Tables (in the app's Postgres, created if missing by whichever process starts first):

- `research_jobs`: one row per booked job. The caller is kept only as a short hash;
  the phone number lives in the Temporal workflow input, never here.
- `research_spend`: one row per Anthropic request, priced from the usage the API
  returned (tokens, cache reads and writes, web searches). Month and day totals
  come from here, so a crashed job's spend still counts.

Cost controls enforced in code (environment overrides, see `Limits`):

- per job: VALR_JOB_USD (default $0.25) across all its requests;
- per caller per UTC day: VALR_JOBS_PER_CALLER_DAY (3); everyone: VALR_JOBS_PER_DAY (15);
- per calendar month (UTC): VALR_MONTH_USD ($20). A new job is refused once the month's
  spend plus one job's budget would pass it.

`python -m app.research_jobs` prints today's and this month's spend and jobs.
"""
import asyncio
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_jobs (
    job_id           text PRIMARY KEY,
    caller_hash      text NOT NULL,
    topic            text NOT NULL,
    status           text NOT NULL,
    promised_minutes integer NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    duration_s       real,
    spend_usd        numeric(10, 5) NOT NULL DEFAULT 0,
    spoken           text,
    notes            jsonb,
    sources          jsonb,
    cap_hits         jsonb,
    detail           text
);
CREATE INDEX IF NOT EXISTS research_jobs_by_time ON research_jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS research_jobs_by_caller ON research_jobs (caller_hash, created_at DESC);
CREATE TABLE IF NOT EXISTS research_spend (
    id            bigserial PRIMARY KEY,
    job_id        text NOT NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    part          text NOT NULL,
    kind          text NOT NULL,
    model         text NOT NULL,
    input_tokens  integer NOT NULL,
    output_tokens integer NOT NULL,
    cache_read    integer NOT NULL,
    cache_write   integer NOT NULL,
    searches      integer NOT NULL,
    usd           numeric(10, 5) NOT NULL
);
CREATE INDEX IF NOT EXISTS research_spend_by_time ON research_spend (at);
CREATE INDEX IF NOT EXISTS research_spend_by_job ON research_spend (job_id);
"""
LOCK_KEY = 7_202_610_16  # pg_advisory_xact_lock key serializing bookings.

# Anthropic list prices, $ per million tokens: input, output, 5-minute cache write, cache read
# (checked 2026-10-16, platform.claude.com pricing). Web search: $10 per 1,000; web fetch: free.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}
SEARCH_USD = 0.01

# The promise ("I'll call you back in about N minutes"): from the p90 of recent jobs'
# research time, plus dialling and ringing, plus a margin; whole minutes, 2 to 5.
SPIKE_P90_S = 57.0  # Phase 1 spike, 9 jobs (Haiku researchers, Sonnet synthesizer): max 59 s.
MIN_SAMPLES = 5  # Fewer finished jobs than this: use the spike's p90.
P90_WINDOW = 20  # The last 20 finished jobs.
DIAL_MARGIN_S = 60  # Placing the call and ringing (Telnyx rings for at most 30 s).
SAFETY_S = 30
MIN_MINUTES, MAX_MINUTES = 2, 5


def caller_hash(caller_key: str) -> str:
    """An anonymised caller id for counting and display; never the number."""
    return hashlib.sha256(("valr:" + caller_key).encode()).hexdigest()[:12]


def price(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
          cache_write: int = 0, searches: int = 0) -> float:
    """Estimated $ for one request. `input_tokens` excludes cached tokens (as the API reports)."""
    p_in, p_out, p_write, p_read = PRICES.get(model, PRICES["claude-sonnet-5-5"])
    tokens = (input_tokens * p_in + output_tokens * p_out + cache_write * p_write
              + cache_read * p_read) / 1e6
    return tokens + searches * SEARCH_USD


def p90(durations: list[float]) -> float:
    """Nearest-rank 90th percentile; the spike's value with too few samples."""
    if len(durations) < MIN_SAMPLES:
        return SPIKE_P90_S
    ordered = sorted(durations)
    return ordered[math.ceil(0.9 * len(ordered)) - 1]


def promise_minutes(p90_s: float) -> int:
    """Minutes to promise: ceil((p90 + dialling + margin) / 60), clamped to 2..5."""
    minutes = math.ceil((p90_s + DIAL_MARGIN_S + SAFETY_S) / 60)
    return max(MIN_MINUTES, min(MAX_MINUTES, minutes))


def job_seconds(minutes: int) -> int:
    """The job's wall-clock limit: the promise minus the time to dial and ring."""
    return minutes * 60 - DIAL_MARGIN_S


@dataclass(frozen=True)
class Limits:
    jobs_per_caller_day: int = 3
    jobs_per_day: int = 15
    month_usd: float = 20.0
    job_usd: float = 0.25

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(int(os.getenv("VALR_JOBS_PER_CALLER_DAY", cls.jobs_per_caller_day)),
                   int(os.getenv("VALR_JOBS_PER_DAY", cls.jobs_per_day)),
                   float(os.getenv("VALR_MONTH_USD", cls.month_usd)),
                   float(os.getenv("VALR_JOB_USD", cls.job_usd)))


# Refusals, for the agent to explain (keys are logged; texts go to the model).
REFUSALS = {
    "month": "Research is paused for the rest of this month: its spending limit is reached. "
             "Apologize and offer to help another way.",
    "caller_day": "This caller has used today's research requests. Apologize and suggest "
                  "trying again tomorrow.",
    "day": "Today's research requests are used up. Apologize and suggest trying again tomorrow.",
}


@dataclass
class Booking:
    job_id: str
    caller_key: str
    topic: str
    promised_minutes: int


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


async def scalar(connection: Any, query: str, params: tuple[Any, ...] = ()) -> Any:
    cursor = await connection.execute(query, params)
    row = await cursor.fetchone()
    return row[0] if row else None


async def month_spend(connection: Any) -> float:
    return float(await scalar(connection,
        "SELECT coalesce(sum(usd), 0) FROM research_spend "
        "WHERE at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'") or 0)


async def day_spend(connection: Any) -> float:
    return float(await scalar(connection,
        "SELECT coalesce(sum(usd), 0) FROM research_spend "
        "WHERE at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'") or 0)


TODAY = "created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


async def refusal(connection: Any, caller_key: str, limits: Limits) -> str | None:
    """Why a new job may not be booked now ("month", "caller_day", "day"), or None."""
    if await month_spend(connection) + limits.job_usd > limits.month_usd:
        return "month"
    mine = await scalar(connection, f"SELECT count(*) FROM research_jobs WHERE caller_hash = %s "
                                    f"AND {TODAY}", (caller_hash(caller_key),))
    if mine >= limits.jobs_per_caller_day:
        return "caller_day"
    if await scalar(connection, f"SELECT count(*) FROM research_jobs WHERE {TODAY}") >= limits.jobs_per_day:
        return "day"
    return None


async def reserve(connection: Any, booking: Booking, limits: Limits) -> str | None:
    """Check the limits and record the job in one transaction; returns a refusal or None.

    The advisory lock serializes bookings, so two calls cannot both take the last slot.
    """
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        reason = await refusal(connection, booking.caller_key, limits)
        if reason is not None:
            return reason
        await connection.execute(
            "INSERT INTO research_jobs (job_id, caller_hash, topic, status, promised_minutes) "
            "VALUES (%s, %s, %s, 'booked', %s)",
            (booking.job_id, caller_hash(booking.caller_key), booking.topic,
             booking.promised_minutes))
    return None


async def recent_durations(connection: Any) -> list[float]:
    cursor = await connection.execute(
        "SELECT duration_s FROM research_jobs WHERE duration_s IS NOT NULL "
        "AND status IN ('done', 'partial') ORDER BY finished_at DESC LIMIT %s", (P90_WINDOW,))
    return [float(row[0]) for row in await cursor.fetchall()]


async def current_promise(connection: Any) -> int:
    return promise_minutes(p90(await recent_durations(connection)))


async def record_spend(connection: Any, job_id: str, rows: list[dict[str, Any]]) -> float:
    """Store one row per request; returns their total $ (also added to the job's total)."""
    total = 0.0
    async with connection.transaction():
        for row in rows:
            usd = price(row["model"], row["input_tokens"], row["output_tokens"],
                        row.get("cache_read", 0), row.get("cache_write", 0), row.get("searches", 0))
            total += usd
            await connection.execute(
                "INSERT INTO research_spend (job_id, part, kind, model, input_tokens, "
                "output_tokens, cache_read, cache_write, searches, usd) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (job_id, row["part"], row["kind"], row["model"], row["input_tokens"],
                 row["output_tokens"], row.get("cache_read", 0), row.get("cache_write", 0),
                 row.get("searches", 0), usd))
        await connection.execute("UPDATE research_jobs SET spend_usd = spend_usd + %s "
                                 "WHERE job_id = %s", (total, job_id))
    return total


async def job_spend(connection: Any, job_id: str) -> float:
    return float(await scalar(connection, "SELECT coalesce(sum(usd), 0) FROM research_spend "
                                          "WHERE job_id = %s", (job_id,)) or 0)


async def mark_started(connection: Any, job_id: str) -> None:
    await connection.execute("UPDATE research_jobs SET status = 'running', started_at = now() "
                             "WHERE job_id = %s AND started_at IS NULL", (job_id,))


async def finish(connection: Any, job_id: str, status: str, duration_s: float | None,
                 spoken: str, notes: list[dict[str, Any]], sources: list[dict[str, Any]],
                 cap_hits: dict[str, int], detail: str) -> None:
    await connection.execute(
        "UPDATE research_jobs SET status = %s, finished_at = now(), duration_s = %s, spoken = %s, "
        "notes = %s::jsonb, sources = %s::jsonb, cap_hits = %s::jsonb, detail = %s "
        "WHERE job_id = %s",
        (status, duration_s, spoken, json.dumps(notes), json.dumps(sources),
         json.dumps(cap_hits), detail[:500], job_id))


async def set_detail(connection: Any, job_id: str, detail: str) -> None:
    await connection.execute("UPDATE research_jobs SET detail = %s WHERE job_id = %s",
                             (detail[:500], job_id))


async def load(connection: Any, job_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        "SELECT job_id, topic, status, spoken, notes, sources FROM research_jobs WHERE job_id = %s",
        (job_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return {"job_id": row[0], "topic": row[1], "status": row[2], "spoken": row[3] or "",
            "notes": row[4] or [], "sources": row[5] or []}


async def overview(connection: Any, limits: Limits, recent: int = 25) -> dict[str, Any]:
    """Today's and this month's spend and job counts, and the latest jobs (for the dashboard)."""
    cursor = await connection.execute(
        "SELECT job_id, caller_hash, topic, status, promised_minutes, created_at, duration_s, "
        "spend_usd, spoken, notes, sources, cap_hits, detail FROM research_jobs "
        "ORDER BY created_at DESC LIMIT %s", (recent,))
    names = ("job_id", "caller", "topic", "status", "promised_minutes", "created_at",
             "duration_s", "spend_usd", "spoken", "notes", "sources", "cap_hits", "detail")
    jobs = [dict(zip(names, row)) for row in await cursor.fetchall()]
    for job in jobs:
        job["spend_usd"] = float(job["spend_usd"] or 0)
    return {
        "today_usd": await day_spend(connection),
        "month_usd": await month_spend(connection),
        "today_jobs": await scalar(connection, f"SELECT count(*) FROM research_jobs WHERE {TODAY}"),
        "month_jobs": await scalar(connection,
            "SELECT count(*) FROM research_jobs WHERE created_at >= "
            "date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"),
        "limits": limits,
        "promise_minutes": await current_promise(connection),
        "jobs": jobs,
    }


def conninfo() -> str:
    from psycopg.conninfo import make_conninfo
    return make_conninfo(host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
                         port=int(os.getenv("POSTGRES_PORT", "5432")), dbname="voice_agent",
                         user="voice_agent", password=os.environ["POSTGRES_PASSWORD"],
                         connect_timeout=5, options="-c statement_timeout=5000")


async def print_overview() -> None:
    import psycopg
    limits = Limits.from_env()
    async with await psycopg.AsyncConnection.connect(conninfo(), autocommit=True) as connection:
        await setup(connection)
        view = await overview(connection, limits, recent=10)
    now = datetime.now(timezone.utc)
    print(f"VALR spend at {now:%Y-%m-%d %H:%M} UTC")
    print(f"  today:      ${view['today_usd']:.4f}  ({view['today_jobs']} of {limits.jobs_per_day} jobs)")
    print(f"  this month: ${view['month_usd']:.4f} of ${limits.month_usd:.2f}  ({view['month_jobs']} jobs)")
    print(f"  per job:    ${limits.job_usd:.2f} cap; promise now {view['promise_minutes']} minutes")
    for job in view["jobs"]:
        duration = "-" if job["duration_s"] is None else f"{job['duration_s']:.0f}s"
        print(f"  {job['created_at']:%m-%d %H:%M} {job['caller']} {job['status']:<10} {duration:>5} "
              f"${job['spend_usd']:.4f}  {job['topic'][:50]}")


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["spend"]):
        sys.exit("usage: python -m app.research_jobs [spend]")
    asyncio.run(print_overview())
