"""Table creation (each module's `setup`) once per process and database, not on every request.

Every page load and worker activity used to run its module's `CREATE TABLE IF NOT EXISTS ...;
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` again. ALTER TABLE takes an ACCESS EXCLUSIVE lock
even when the column exists, so two of them racing each other and the advisory-lock transactions
(research_jobs.reserve, lead_worker) deadlocked in production (DeadlockDetected, QueryCanceled and
a 503 on /valr/leads). Now each schema runs once per process: serialised in the process by a lock
per schema, across processes by a Postgres advisory lock held for the DDL's transaction. It stays
idempotent, so a second process (or a restart) running it again is harmless.

Only real connections (psycopg's, with `info.dsn`) are remembered; the tests' in-memory stand-ins
run their setup every time, as before.
"""
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

LOCK_KEY = 0x56414C52_5343  # "VALR" "SC": the DDL lock, distinct from the bookings' lock keys.

_ready: set[tuple[str, str]] = set()
_locks: dict[str, asyncio.Lock] = {}


def database(connection: Any) -> str | None:
    """The connection's database (its DSN, password-free), or None for a test stand-in."""
    dsn = getattr(getattr(connection, "info", None), "dsn", None)
    return dsn if isinstance(dsn, str) else None


async def once(connection: Any, name: str, run: Callable[[], Awaitable[Any]]) -> None:
    """Run `run` (the DDL of schema `name`) unless this process already did on this database."""
    db = database(connection)
    if db is None:
        await run()
        return
    key = (name, db)
    if key in _ready:
        return
    lock = _locks.setdefault(name, asyncio.Lock())
    async with lock:
        if key in _ready:
            return
        from app import tenant
        real = isinstance(connection, tenant.psycopg.AsyncConnection)
        async with connection.transaction():
            if real:  # DDL as the database owner, for this transaction only; queries run as valr_app.
                await connection.execute("SET LOCAL ROLE NONE")
            await connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            await run()
            if real:
                await tenant.migrate(connection)  # New per-user tables get owner_id and row-level security.
        _ready.add(key)


def forget() -> None:
    """Tests that drop tables: the next setup creates them again."""
    _ready.clear()
