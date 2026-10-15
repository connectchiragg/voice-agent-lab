"""Phone call recordings (TELNYX_RECORD=1), for debugging calls.

Telnyx records each answered call (Call Control `record_start`, WAV, dual
channel: the caller on one channel, the assistant on the other) and keeps the
file. Its `call.recording.saved` webhook carries download URLs that expire
after 10 minutes, so we keep only the recording's id and metadata, in
`call_recordings`, and fetch a fresh URL with GET /v2/recordings/{id} when the
file is wanted (scripts/fetch_recording.py). Phone numbers are never stored.
"""
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("uvicorn.error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_recordings (
    recording_id    text PRIMARY KEY,
    call_control_id text NOT NULL,
    created_at      timestamptz NOT NULL,
    duration_ms     integer
);
CREATE INDEX IF NOT EXISTS call_recordings_by_time ON call_recordings (created_at DESC);
"""

pool: AsyncConnectionPool | None = None


def enabled() -> bool:
    return os.getenv("TELNYX_RECORD", "0") == "1"


@dataclass(frozen=True)
class Recording:
    recording_id: str
    call_control_id: str
    created_at: datetime
    duration_ms: int | None


async def save(recording: Recording) -> bool:
    """Store a recording's metadata; False without Postgres (it is then only logged)."""
    if pool is None:
        return False
    async with pool.connection() as connection:
        await connection.execute(
            "INSERT INTO call_recordings (recording_id, call_control_id, created_at, duration_ms) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (recording_id) DO UPDATE SET "
            "duration_ms = coalesce(excluded.duration_ms, call_recordings.duration_ms)",
            (recording.recording_id, recording.call_control_id, recording.created_at,
             recording.duration_ms))
    return True


@asynccontextmanager
async def lifespan(conninfo: Callable[[], str]) -> AsyncIterator[None]:
    """Postgres for recording metadata, only when recording is on."""
    global pool
    if not enabled():
        yield
        return
    try:
        pool = AsyncConnectionPool(conninfo(), min_size=1, max_size=1, open=False,
                                   kwargs={"autocommit": True})
        await pool.open(wait=True, timeout=5)
        async with pool.connection() as connection:
            await connection.execute(SCHEMA)
        logger.info("Phone calls are recorded (TELNYX_RECORD=1)")
    except Exception as error:
        logger.warning("Postgres unavailable for recordings (%s); recording ids are only logged.",
                       type(error).__name__)
        if pool is not None:
            await pool.close()
        pool = None
    try:
        yield
    finally:
        if pool is not None:
            await pool.close()
            pool = None
