"""Sessions and one-time login states, in the app's Postgres (research.pool).

    valr_sessions      token_hash (SHA-256 of the cookie's random bytes), email, created_at,
                       expires_at. The token itself is never stored or logged.
    valr_login_states  state_hash, nonce, next_path, created_at: one row per sign-in started,
                       deleted when Google comes back (so a state works once) or after
                       STATE_TTL_S.

Expired rows are swept whenever a sign-in starts or a session is created.
"""
from typing import Any, Protocol

from app.auth import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS valr_sessions (
    token_hash  text PRIMARY KEY,
    email       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS valr_sessions_expires ON valr_sessions (expires_at);
CREATE TABLE IF NOT EXISTS valr_login_states (
    state_hash  text PRIMARY KEY,
    nonce       text NOT NULL,
    next_path   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""


class Store(Protocol):
    async def save_state(self, state: str, nonce: str, next_path: str) -> None: ...

    async def take_state(self, state: str) -> tuple[str, str] | None:
        """(nonce, next_path) once, while fresh; None after that."""

    async def create_session(self, email: str) -> str:
        """A new session; returns the cookie token."""

    async def session_email(self, token: str) -> str | None: ...

    async def end_session(self, token: str) -> None: ...


_ready_pool: Any = None  # The pool the tables were created in (once per process).


class PostgresStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def _ready(self, connection: Any) -> None:
        global _ready_pool
        if _ready_pool is self.pool:
            return
        await connection.execute(SCHEMA)
        _ready_pool = self.pool

    async def save_state(self, state: str, nonce: str, next_path: str) -> None:
        async with self.pool.connection() as connection:
            await self._ready(connection)
            await connection.execute(
                "DELETE FROM valr_login_states WHERE created_at < now() - make_interval(secs => %s)",
                (core.STATE_TTL_S,))
            await connection.execute(
                "INSERT INTO valr_login_states (state_hash, nonce, next_path) VALUES (%s, %s, %s)",
                (core.token_hash(state), nonce, next_path))

    async def take_state(self, state: str) -> tuple[str, str] | None:
        async with self.pool.connection() as connection:
            await self._ready(connection)
            cursor = await connection.execute(
                "DELETE FROM valr_login_states WHERE state_hash = %s "
                "AND created_at >= now() - make_interval(secs => %s) RETURNING nonce, next_path",
                (core.token_hash(state), core.STATE_TTL_S))
            row = await cursor.fetchone()
        return (row[0], row[1]) if row else None

    async def create_session(self, email: str) -> str:
        token = core.new_token()
        async with self.pool.connection() as connection:
            await self._ready(connection)
            await connection.execute("DELETE FROM valr_sessions WHERE expires_at < now()")
            await connection.execute(
                "INSERT INTO valr_sessions (token_hash, email, expires_at) "
                "VALUES (%s, %s, now() + make_interval(secs => %s))",
                (core.token_hash(token), email, core.SESSION_TTL_S))
        return token

    async def session_email(self, token: str) -> str | None:
        async with self.pool.connection() as connection:
            await self._ready(connection)
            cursor = await connection.execute(
                "SELECT email FROM valr_sessions WHERE token_hash = %s AND expires_at > now()",
                (core.token_hash(token),))
            row = await cursor.fetchone()
        return row[0] if row else None

    async def end_session(self, token: str) -> None:
        async with self.pool.connection() as connection:
            await self._ready(connection)
            await connection.execute("DELETE FROM valr_sessions WHERE token_hash = %s",
                                     (core.token_hash(token),))


def backend() -> Store | None:
    """The store over the app's Postgres pool; None while Postgres is unavailable."""
    from app import research
    return PostgresStore(research.pool) if research.pool is not None else None
