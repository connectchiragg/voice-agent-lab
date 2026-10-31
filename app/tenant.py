"""Who the data belongs to: one VALR workspace per Google account, isolated by Postgres.

Every per-user table (`TABLES`) carries `owner_id` and row-level security:

    owner_id text NOT NULL DEFAULT nullif(current_setting('valr.user', true), '')
    POLICY tenant USING / WITH CHECK (owner_id = nullif(current_setting('valr.user', true), ''))

so a query only ever sees and writes the current user's rows, whatever its WHERE clause says.
The app connects as the database owner (a superuser in production, which bypasses row-level
security), so every connection switches to the role `valr_app` (NOLOGIN, no BYPASSRLS) when it
opens. DDL runs as the owner again, for its own transaction only (`schema.once`).

The current user is a ContextVar (`current`), set by the sign-in gate for a request and by the
workers for each user they serve. `TenantConnection`'s cursors bind it to `valr.user` before a
statement whenever it differs from what the connection holds. With no user, `valr.user` is ''
and every per-user table looks empty and refuses inserts: a forgotten scope fails closed.

Inside a transaction the user is bound with is_local (`SET LOCAL`), never cached: a rollback
would otherwise restore a pooled connection's previous user under a cache that says otherwise.

The owner's existing rows become `OWNER` ("owner", the same stable key as app/owner.KEY).
Anyone else is `u_` + the first 20 hex digits of sha256(lower-cased email).
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import psycopg
from psycopg import AsyncConnection, AsyncCursor, pq, sql

logger = logging.getLogger("valr")

OWNER = "owner"
ROLE = "valr_app"
SETTING = "valr.user"

current: ContextVar[str | None] = ContextVar("valr_user", default=None)

# Per-user tables -> their key before tenancy (None: no primary key to widen). The key becomes
# (owner_id, <key>); every ON CONFLICT target on it names owner_id first.
TABLES: dict[str, tuple[str, ...] | None] = {
    "briefing_blocks": ("block_id",),
    "calendar_events": ("key",),
    "calendar_history": ("event_key",),
    "calendar_state": ("id",),
    "call_recordings": ("recording_id",),
    "call_summaries": ("call_id",),
    "callers": ("caller_key",),
    "company_overrides": ("entity_id",),
    "company_resolutions": ("fingerprint",),
    "gmail_state": ("id",),
    "heads_up_log": ("block_id",),
    "interview_context_push": None,
    "lead_emails": ("message_id",),
    "lead_overrides": None,
    "lead_touchpoints": ("id",),
    "leads": ("id",),
    "notetaker_credentials": ("provider",),
    "notetaker_hidden": ("provider", "external_id"),
    "notetaker_recordings": ("provider", "external_id"),
    "notifications": None,
    "notify_settings": ("id",),
    "oauth_tokens": ("provider",),
    "owner_profile": ("id",),
    "phone_audio": ("upload_id", "idx"),
    "phone_upload_token": ("id",),
    "phone_uploads": ("id",),
    "post_call_reports": ("provider", "external_id"),
    "prep_checks": ("slot_ref", "item"),
    "push_subscriptions": None,
    "recording_media": ("provider", "external_id"),
    "recording_transcripts": ("provider", "external_id"),
    "research_jobs": ("job_id",),
    "research_spend": None,
    "slot_context": ("key",),
    "user_keys": ("purpose",),
    "valr_flags": ("name",),
}
# Unique indexes that are per user: (table, index name, columns after owner_id).
UNIQUE_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("notifications", "notifications_owner_source", ("source_id",)),
    ("phone_uploads", "phone_uploads_owner_sha256", ("sha256",)),
    ("push_subscriptions", "push_subscriptions_owner_endpoint", ("endpoint",)),
)
# Singleton tables (CHECK id = 1): one row per user now.
SINGLETONS = ("calendar_state", "gmail_state", "notify_settings", "owner_profile", "phone_upload_token")


def user_id(email: str, owner_emails: frozenset[str] | set[str]) -> str:
    """The workspace of a signed-in email: OWNER for the owner's addresses, else u_<hash>."""
    lowered = email.strip().lower()
    if lowered in owner_emails:
        return OWNER
    return "u_" + hashlib.sha256(lowered.encode()).hexdigest()[:20]


def get() -> str | None:
    return current.get()


def is_owner() -> bool:
    return current.get() == OWNER


@contextmanager
def as_user(user: str | None) -> Iterator[None]:
    """Run the block as `user` (a worker serving each user in turn)."""
    token = current.set(user)
    try:
        yield
    finally:
        current.reset(token)


# Connections.

BIND = "SELECT set_config('valr.user', %s, %s)"


class TenantCursor(AsyncCursor):
    async def _bind(self) -> None:
        connection: TenantConnection = self.connection  # type: ignore[assignment]
        user = current.get() or ""
        idle = connection.info.transaction_status == pq.TransactionStatus.IDLE
        if idle and connection.bound_user == user:
            return
        await AsyncCursor.execute(self, BIND, (user, not idle))
        if idle:
            connection.bound_user = user

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        await self._bind()
        return await super().execute(query, params, **kwargs)

    async def executemany(self, query: Any, params_seq: Any, **kwargs: Any) -> None:
        await self._bind()
        await super().executemany(query, params_seq, **kwargs)

    async def stream(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        await self._bind()
        async for row in super().stream(query, params, **kwargs):
            yield row


class TenantConnection(AsyncConnection):
    """A psycopg connection that runs as `valr_app` and binds the current user per statement."""
    bound_user: str | None = None

    @classmethod
    async def connect(cls, conninfo: str = "", **kwargs: Any) -> "TenantConnection":
        kwargs.setdefault("cursor_factory", TenantCursor)
        connection = await super().connect(conninfo, **kwargs)
        if not isinstance(connection, AsyncConnection):  # A test's stand-in for psycopg.
            return connection
        await prepare_role(connection)
        await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ROLE)))
        if not connection.autocommit:
            await connection.commit()
        connection.bound_user = None
        return connection


async def connect(conninfo: str, **kwargs: Any) -> TenantConnection:
    """psycopg.AsyncConnection.connect for VALR data: as valr_app, bound to the current user."""
    return await TenantConnection.connect(conninfo, **kwargs)


_role_ready: set[str] = set()


async def prepare_role(connection: AsyncConnection) -> None:
    """Create valr_app and grant it the tables, once per process and database (as the owner)."""
    dsn = connection.info.dsn
    if dsn in _role_ready:
        return
    cursor = AsyncCursor(connection)
    await cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
    if await cursor.fetchone() is None:
        await cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(ROLE)))
        logger.info("VALR tenancy: created role %s", ROLE)
    await cursor.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(ROLE)))
    await grant(cursor)
    if not connection.autocommit:
        await connection.commit()
    _role_ready.add(dsn)


async def grant(cursor: AsyncCursor) -> None:
    role = sql.Identifier(ROLE)
    for statement in (
        "GRANT USAGE ON SCHEMA public TO {}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}",
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}",
    ):
        await cursor.execute(sql.SQL(statement).format(role))


# Migration: run as the owner inside schema.once's transaction, after a module's DDL.

USER_EXPR = "nullif(current_setting('valr.user', true), '')"


async def migrate(connection: Any) -> None:
    """Give every existing per-user table owner_id, its wider key and row-level security."""
    cursor = AsyncCursor(connection)
    await cursor.execute(
        "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s)", (list(TABLES),))
    done = {name: secured for name, secured in await cursor.fetchall()}
    for table, secured in done.items():
        if secured:
            continue
        await secure(cursor, table)
        logger.info("VALR tenancy: %s is per user now", table)
    await grant(cursor)


async def secure(cursor: AsyncCursor, table: str) -> None:
    ident = sql.Identifier(table)
    await cursor.execute(sql.SQL(
        "ALTER TABLE {} ADD COLUMN IF NOT EXISTS owner_id text NOT NULL DEFAULT {}").format(
        ident, sql.Literal(OWNER)))
    await cursor.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN owner_id SET DEFAULT " + USER_EXPR).format(ident))
    if table in SINGLETONS:
        await cursor.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT IF EXISTS {}").format(
            ident, sql.Identifier(f"{table}_id_check")))
    key = TABLES[table]
    if key:
        await cursor.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT IF EXISTS {}").format(
            ident, sql.Identifier(f"{table}_pkey")))
        await cursor.execute(sql.SQL("ALTER TABLE {} ADD PRIMARY KEY (owner_id, {})").format(
            ident, sql.SQL(", ").join(map(sql.Identifier, key))))
    for index_table, name, columns in UNIQUE_INDEXES:
        if index_table == table:
            await unique_per_user(cursor, table, name, columns)
    await cursor.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident))
    await cursor.execute(sql.SQL("DROP POLICY IF EXISTS tenant ON {}").format(ident))
    await cursor.execute(sql.SQL(
        "CREATE POLICY tenant ON {} USING (owner_id = " + USER_EXPR + ") WITH CHECK (owner_id = "
        + USER_EXPR + ")").format(ident))


async def unique_per_user(cursor: AsyncCursor, table: str, name: str, columns: tuple[str, ...]) -> None:
    """Replace any single-column unique index/constraint on `columns` with (owner_id, columns)."""
    await cursor.execute(
        "SELECT i.relname, con.conname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
        "JOIN pg_class t ON t.oid = x.indrelid LEFT JOIN pg_constraint con ON con.conindid = x.indexrelid "
        "WHERE t.relname = %s AND x.indisunique AND NOT x.indisprimary "
        "AND (SELECT array_agg(a.attname::text ORDER BY k.n) FROM unnest(x.indkey) WITH ORDINALITY k(attnum, n) "
        "     JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum) = %s",
        (table, list(columns)))
    for index, constraint in await cursor.fetchall():
        if constraint:
            await cursor.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                sql.Identifier(table), sql.Identifier(constraint)))
        else:
            await cursor.execute(sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(index)))
    await cursor.execute(sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} (owner_id, {})").format(
        sql.Identifier(name), sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))))


def conflict(*columns: str) -> str:
    """An ON CONFLICT target on a per-user key: `(owner_id, a, b)`."""
    return "(" + ", ".join(("owner_id",) + columns) + ")"


__all__ = ["OWNER", "TABLES", "TenantConnection", "as_user", "connect", "conflict", "current", "get",
           "is_owner", "migrate", "user_id", "psycopg"]
