"""Serving every workspace from the workers: the list, a scope per workspace, ids and locks.

VALR's background work (the research worker's loops, its Temporal workflows and activities, the
Google push webhooks) used to run as the owner only. Each workspace (app/tenant.py) now gets the
same service, under its own scope:

- `listing`: every workspace that has signed in (`valr_users`, global, no row-level security),
  the owner's first and always present (a database where nobody has signed in yet is the owner's).
- `each`: a periodic loop's pass over the workspaces, each under `tenant.as_user(uid)`, at most
  CONCURRENCY at once, each within a time budget; one workspace's failure or timeout is logged
  (by workspace id and error type only) and never stops the others.
- `scoped_id` and `lock_key`: Temporal workflow/schedule ids and Postgres advisory-lock keys per
  workspace. The owner keeps today's ids and keys, so his running schedules and in-flight
  workflows are not orphaned, and a worker on the old code still excludes a new one.
- `prepare` and `emails`: who "you" are in a workspace's mail and calendar. The owner: his
  configured addresses (VALR_OWNER_EMAILS, VALR_GMAIL_ACCOUNT), as before. A guest: the address
  they signed in with and the Google account they connected (always the same: app/google_oauth).
- `caller_key`: the key a workspace's research bookings and workflows carry: "owner" for the
  owner (app/owner.KEY), `workspace:<uid>` for a guest.

Guests never get the owner-only work: Telnyx dials and callbacks, phone recordings, the Mac app
and the file-based profile/performance pipeline stay the owner's (README, "Workspaces").
Nothing here logs an address: a workspace is named by its id ("owner" or "u_<hash>").
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from app import tenant

logger = logging.getLogger("valr")

OWNER = tenant.OWNER
CONCURRENCY = 2  # Workspaces served at once by one loop pass.
BUDGET_S = 120.0  # A workspace's share of one pass; past it, it is cut off until the next pass.
EMAILS_FRESH_S = 300.0
GOOGLE = "google_gmail"  # app/google_oauth.PROVIDER (not imported: that module pulls in cryptography).


def owner(user: str | None) -> bool:
    """No workspace chosen means the owner (old in-flight workflows, the owner's CLIs)."""
    return user in (None, "", OWNER)


def scope(user: str | None) -> Any:
    """`with scope(input.user):` an activity runs in the workspace its input names; an input
    from before multi-tenancy names none and is the owner's."""
    return tenant.as_user(user or OWNER)


def is_owner() -> bool:
    """Is the current scope the owner's? No scope counts as the owner (as app/owner.env does):
    the owner's CLIs and start-up. Guards the owner's server files and environment values."""
    return owner(tenant.get())


def scoped_id(base: str, user: str | None = None) -> str:
    """A Temporal workflow or schedule id for this workspace (default: the current one).
    The owner keeps `base`; a guest's is `base-<uid>`."""
    user = tenant.get() if user is None else user
    return base if owner(user) else f"{base}-{user}"


def lock_key(base: int, user: str | None = None) -> int:
    """A pg_advisory_lock key for this workspace (default: the current one): the owner keeps
    `base`; a guest's is a signed 64-bit hash of (base, uid), so workspaces never wait on
    each other."""
    user = tenant.get() if user is None else user
    if owner(user):
        return base
    digest = hashlib.sha256(f"{base}:{user}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def listing(connection: Any) -> list[str]:
    """Every workspace, the owner's first (and always there). valr_users is global, so this
    needs no scope; a database without it (sign-in never used) is the owner's alone."""
    cursor = await connection.execute("SELECT to_regclass('public.valr_users') IS NOT NULL")
    exists = await cursor.fetchone()
    users: list[str] = []
    if exists and exists[0]:
        cursor = await connection.execute(
            "SELECT id FROM valr_users ORDER BY id <> %s, created_at", (OWNER,))
        users = [str(row[0]) for row in await cursor.fetchall()]
    return [OWNER] + [user for user in users if user != OWNER]


async def users(connect: Callable[[], Awaitable[Any]]) -> list[str]:
    """`listing` on a connection of its own; just the owner when the database is unreachable."""
    try:
        async with await connect() as connection:
            return await listing(connection)
    except Exception as error:  # noqa: BLE001 - the owner is still served
        logger.warning("VALR workspaces unavailable (%s): serving the owner only", type(error).__name__)
        return [OWNER]


Work = Callable[[str], Awaitable[Any]]


async def each(work: Work, *, label: str, connect: Callable[[], Awaitable[Any]] | None = None,
               only: Iterable[str] | None = None, concurrency: int = CONCURRENCY,
               budget_s: float = BUDGET_S) -> dict[str, str]:
    """One pass of a periodic loop: `await work(uid)` for every workspace (or `only` these),
    each under `tenant.as_user(uid)`, at most `concurrency` at once, each within `budget_s`.
    -> {uid: "ok" | what `work` returned when it is a string (e.g. "skipped") | "timeout" |
    "failed"}. A failure is logged by workspace id and error type, never raised."""
    chosen = list(only) if only is not None else await users(connect) if connect else [OWNER]
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(user: str) -> str:
        async with gate:
            with tenant.as_user(user):
                try:
                    result = await asyncio.wait_for(work(user), timeout=budget_s)
                except asyncio.TimeoutError:
                    logger.warning("VALR %s: workspace %s took over %g s; cut off until the next pass",
                                   label, user, budget_s)
                    return "timeout"
                except Exception as error:  # noqa: BLE001 - one workspace never stops the others
                    logger.warning("VALR %s: workspace %s failed (%s)", label, user, type(error).__name__)
                    return "failed"
        return result if isinstance(result, str) else "ok"

    outcomes = await asyncio.gather(*(one(user) for user in chosen))
    return dict(zip(chosen, outcomes))


async def google_connected(connection: Any) -> bool:
    """Has the current workspace a working Google grant (Gmail + Calendar)? One cheap query."""
    cursor = await connection.execute(
        "SELECT 1 FROM oauth_tokens WHERE provider = %s AND status = 'connected'", (GOOGLE,))
    return await cursor.fetchone() is not None


async def connected(connect: Callable[[], Awaitable[Any]]) -> list[str]:
    """The workspaces with Google connected, on one connection (one query each). The owner is
    always in: he may use the ICS URL, and his work ran unconditionally before."""
    out = [OWNER]
    try:
        async with await connect() as connection:
            for user in await listing(connection):
                if owner(user):
                    continue
                with tenant.as_user(user):
                    if await google_connected(connection):
                        out.append(user)
    except Exception as error:  # noqa: BLE001 - e.g. no oauth_tokens table yet: the owner only
        logger.info("VALR workspaces: Google state unreadable (%s)", type(error).__name__)
    return out


# Who "you" are in a workspace: {uid: (addresses, loaded at)}.
_emails: dict[str, tuple[frozenset[str], float]] = {}


def owner_emails() -> set[str]:
    """The owner's addresses: VALR_OWNER_EMAILS and VALR_GMAIL_ACCOUNT."""
    return {e.strip().lower() for e in os.getenv("VALR_OWNER_EMAILS", "").split(",") if e.strip()} \
        | ({os.getenv("VALR_GMAIL_ACCOUNT", "").strip().lower()} - {""})


def emails(user: str | None = None) -> set[str]:
    """The current workspace's own addresses (lower case): the owner's configured ones, or the
    guest's sign-in address and connected Google account once `prepare` has loaded them (an
    empty set before that: nobody on an invite is taken for the guest)."""
    user = tenant.get() if user is None else user
    if owner(user):
        return owner_emails()
    return set(_emails.get(user, (frozenset(), 0.0))[0])


def remember(user: str, addresses: Iterable[str]) -> None:
    _emails[user] = (frozenset(a.strip().lower() for a in addresses if a and a.strip()), time.monotonic())


def forget() -> None:
    """Tests: drop the cached addresses."""
    _emails.clear()


async def prepare(connection: Any, force: bool = False) -> None:
    """Load the current guest's addresses (sign-in email and connected Google account) at most
    every EMAILS_FRESH_S; nothing for the owner. Never raises: on a problem the last ones stay."""
    user = tenant.get()
    if owner(user):
        return
    loaded = _emails.get(user, (frozenset(), 0.0))[1]
    if not force and loaded and time.monotonic() - loaded < EMAILS_FRESH_S:
        return
    found: list[str] = []
    try:
        cursor = await connection.execute("SELECT email FROM valr_users WHERE id = %s", (user,))
        row = await cursor.fetchone()
        if row and row[0]:
            found.append(str(row[0]))
        cursor = await connection.execute("SELECT account FROM oauth_tokens WHERE provider = %s", (GOOGLE,))
        row = await cursor.fetchone()
        if row and row[0]:
            found.append(str(row[0]))
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR workspace %s: addresses unavailable (%s)", user, type(error).__name__)
        return
    remember(user, found)


def caller_key(user: str | None = None) -> str:
    """The key a workspace's research bookings and workflows carry (limits, `owner_key`):
    "owner" for the owner (app/owner.KEY), `workspace:<uid>` for a guest. Stable, the same in
    the app and the worker, and no address in Temporal's history."""
    user = tenant.get() if user is None else user
    return OWNER if owner(user) else f"workspace:{user}"


# Every workspace's Temporal schedules (app/research_worker.ensure_sweep, app/lead_worker.lead_schedules).
SCHEDULES = ("valr-calendar-sweep", "valr-gmail-poll", "valr-gmail-watch")


def schedule_ids(user: str | None = None) -> list[str]:
    return [scoped_id(name, user) for name in SCHEDULES]


async def drop_schedules(client: Any, user: str | None = None) -> int:
    """Delete a guest's schedules (Delete my data); never the owner's. -> how many went."""
    user = tenant.get() if user is None else user
    if owner(user):
        return 0
    dropped = 0
    for name in schedule_ids(user):
        try:
            await client.get_schedule_handle(name).delete()
            dropped += 1
        except Exception:  # noqa: BLE001 - never created (Google never connected, leads off)
            continue
    return dropped


__all__ = ["OWNER", "caller_key", "connected", "each", "emails", "google_connected", "is_owner", "listing",
           "lock_key", "owner", "prepare", "schedule_ids", "scope", "scoped_id", "users"]
