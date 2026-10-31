"""Tenancy against a real Postgres (app/tenant.py): row-level security keeps workspaces apart.

Runs only when a local Postgres accepts the superuser connection in VALR_TEST_PG (default
"host=/tmp dbname=postgres", the Homebrew socket); it creates and drops a scratch database.
The app's own setup functions build the tables, so the migration is tested on the real DDL.
"""
import asyncio
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from app import google_oauth, interview_store, leads_store, owner, schema, tenant  # noqa: E402
from app.notetakers import store as notetaker_store  # noqa: E402

ADMIN = os.getenv("VALR_TEST_PG", "host=/tmp dbname=postgres")
GUEST = "u_" + "0" * 20


def reachable() -> bool:
    try:
        with psycopg.connect(ADMIN, connect_timeout=2) as connection:
            return bool(connection.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not reachable(), reason="no local Postgres superuser (VALR_TEST_PG)")


@pytest.fixture
def database():
    name = f"valr_tenant_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    schema.forget()
    tenant._role_ready.clear()
    owner.reset()
    yield psycopg.conninfo.make_conninfo(ADMIN, dbname=name)
    schema.forget()
    owner.reset()
    with psycopg.connect(ADMIN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def build(conninfo: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(conninfo, min_size=1, max_size=1, open=False,
                               connection_class=tenant.TenantConnection, kwargs={"autocommit": True})
    await pool.open(wait=True, timeout=5)
    with tenant.as_user(tenant.OWNER):
        async with pool.connection() as connection:
            for setup in (leads_store.setup, interview_store.setup, google_oauth.setup, notetaker_store.setup,
                          owner.setup):
                await setup(connection)
    return pool


async def count(pool, table: str, user: str | None) -> int:
    with tenant.as_user(user):
        async with pool.connection() as connection:
            cursor = await connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            return (await cursor.fetchone())[0]


def test_each_workspace_sees_only_its_own_rows(database):
    async def main():
        pool = await build(database)
        try:
            for user, flag in ((tenant.OWNER, "owner-flag"), (GUEST, "guest-flag")):
                with tenant.as_user(user):
                    async with pool.connection() as connection:
                        await connection.execute(
                            "INSERT INTO valr_flags (name, enabled) VALUES ('dials_paused', %s) "
                            f"ON CONFLICT {tenant.conflict('name')} DO UPDATE SET enabled = excluded.enabled",
                            (user == GUEST,))
                        await owner.setup(connection)  # The profile row, at this user's scope.
                        await owner.save(connection, display_name=flag)
            # The same key in two workspaces: two rows, each visible only to its owner.
            for user, enabled in ((tenant.OWNER, False), (GUEST, True)):
                with tenant.as_user(user):
                    async with pool.connection() as connection:
                        cursor = await connection.execute("SELECT name, enabled, owner_id FROM valr_flags")
                        assert await cursor.fetchall() == [("dials_paused", enabled, user)]
                        cursor = await connection.execute("SELECT display_name FROM owner_profile WHERE id = 1")
                        assert (await cursor.fetchone())[0] == ("guest-flag" if user == GUEST else "owner-flag")
            # The cache is per workspace too.
            with tenant.as_user(GUEST):
                assert owner.name() == "guest-flag"
            with tenant.as_user(tenant.OWNER):
                assert owner.name() == "owner-flag"
            # No user: every per-user table is empty and refuses writes (fails closed).
            assert await count(pool, "valr_flags", None) == 0
            with tenant.as_user(None):
                async with pool.connection() as connection:
                    with pytest.raises((psycopg.errors.NotNullViolation, psycopg.errors.InsufficientPrivilege)):
                        await connection.execute("INSERT INTO valr_flags (name, enabled) VALUES ('x', true)")
            # Writing another workspace's id is refused by the policy.
            with tenant.as_user(GUEST):
                async with pool.connection() as connection:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await connection.execute("INSERT INTO valr_flags (owner_id, name, enabled) "
                                                 "VALUES ('owner', 'sneaky', true)")
                    # A guest's DELETE of everything only empties its own workspace.
                    await connection.execute("DELETE FROM valr_flags")
                    await connection.execute("DELETE FROM owner_profile")
            assert await count(pool, "valr_flags", tenant.OWNER) == 1
            assert await count(pool, "owner_profile", tenant.OWNER) == 1
            # And the role cannot TRUNCATE or turn security off.
            with tenant.as_user(GUEST):
                async with pool.connection() as connection:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await connection.execute("TRUNCATE valr_flags")
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await connection.execute("ALTER TABLE valr_flags DISABLE ROW LEVEL SECURITY")
        finally:
            await pool.close()
    asyncio.run(main())


def test_a_rolled_back_transaction_never_leaves_the_connection_as_someone_else(database):
    async def main():
        pool = await build(database)  # One connection: every checkout reuses it.
        try:
            with tenant.as_user(tenant.OWNER):
                async with pool.connection() as connection:
                    await connection.execute("INSERT INTO valr_flags (name, enabled) VALUES ('mine', true)")
            with tenant.as_user(GUEST):
                async with pool.connection() as connection:
                    with pytest.raises(RuntimeError):
                        async with connection.transaction():
                            await connection.execute("SELECT count(*) FROM valr_flags")
                            raise RuntimeError("roll back")
                    cursor = await connection.execute("SELECT count(*) FROM valr_flags")
                    assert (await cursor.fetchone())[0] == 0  # Still the guest, not the owner.
            assert await count(pool, "valr_flags", tenant.OWNER) == 1
        finally:
            await pool.close()
    asyncio.run(main())


def test_the_migration_keeps_the_owners_existing_rows(database):
    """A database from before tenancy: its rows become the owner's, its keys per user."""
    async def main():
        async with await psycopg.AsyncConnection.connect(database, autocommit=True) as connection:
            await connection.execute(leads_store.SCHEMA)
            await connection.execute("INSERT INTO gmail_state (id) VALUES (1)")
        pool = await build(database)
        try:
            assert await count(pool, "gmail_state", tenant.OWNER) == 1
            assert await count(pool, "gmail_state", GUEST) == 0
            with tenant.as_user(GUEST):
                async with pool.connection() as connection:
                    await leads_store.setup(connection)  # The guest's own state row, same id = 1.
            assert await count(pool, "gmail_state", GUEST) == 1
            assert await count(pool, "gmail_state", tenant.OWNER) == 1
        finally:
            await pool.close()
    asyncio.run(main())
