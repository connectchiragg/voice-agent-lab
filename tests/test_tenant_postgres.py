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


# The workers serve every workspace (app/workspaces.py), on the real tables.

async def register(pool, users: dict[str, str]) -> None:
    """valr_users (global, no row-level security) as app/auth/store.py creates it."""
    from app.auth import store as auth_store
    with tenant.as_user(tenant.OWNER):
        async with pool.connection() as connection:
            await schema.once(connection, "valr_auth", lambda: connection.execute(auth_store.SCHEMA))
            for user, email in users.items():
                await connection.execute("INSERT INTO valr_users (id, email) VALUES (%s, %s)", (user, email))


def test_the_notifications_loop_serves_each_workspace_its_own_items_on_real_postgres(database, monkeypatch):
    """One pass of the research worker's notifications loop: each workspace's pending item goes to
    that workspace's devices only, under its own scope, and is marked sent in its own rows."""
    from datetime import datetime, timezone

    from app import notifications, webpush, workspaces
    sent: list[tuple[str | None, str]] = []
    stop = asyncio.Event()

    class Sender:
        async def subscribers(self):
            return 1

        async def send(self, message, now=None, only=None):
            sent.append((tenant.get(), message["url"]))
            if len(sent) >= 2:
                stop.set()
            return webpush.Result(delivered=1)

    async def sender(connection):
        return Sender()

    async def connect():
        return await tenant.connect(database, autocommit=True)
    monkeypatch.setattr(webpush, "sender", sender)
    monkeypatch.setenv("VALR_NOTIFY_LOOP_S", "0.05")
    monkeypatch.setenv("VALR_NOTIFY_QUIET_HOURS", "0")  # Else it waits when run at night.

    async def main():
        pool = await build(database)
        try:
            await register(pool, {tenant.OWNER: "owner@example.com", GUEST: "guest@elsewhere.example"})
            now = datetime.now(timezone.utc)
            for user in (tenant.OWNER, GUEST):
                with tenant.as_user(user):
                    async with pool.connection() as connection:
                        store = notifications.Store(connection)
                        await store.setup()
                        await notifications.add(store, {
                            "source_id": f"test:{user}", "kind": "offer", "title": f"Offer for {user}", "body": "b",
                            "company": "", "link": f"/valr/leads?for={user}", "gmail_url": "", "thread_id": None, "action": ""}, now)
            with tenant.as_user(None):  # The worker's own scope: none (fail closed).
                await asyncio.wait_for(notifications.loop(connect, stop), 20)
            assert sorted(sent) == sorted([(user, f"/valr/leads?for={user}") for user in (tenant.OWNER, GUEST)])
            for user in (tenant.OWNER, GUEST):
                with tenant.as_user(user):
                    async with pool.connection() as connection:
                        cursor = await connection.execute("SELECT source_id, push_state FROM notifications")
                        assert await cursor.fetchall() == [(f"test:{user}", "sent")]
            assert await workspaces.users(connect) == [tenant.OWNER, GUEST]
        finally:
            await pool.close()
    asyncio.run(main())


def test_only_workspaces_with_google_connected_get_background_gmail_and_calendar_work(database):
    from app import workspaces
    third = "u_" + "1" * 20

    async def connect():
        return await tenant.connect(database, autocommit=True)

    async def main():
        pool = await build(database)
        try:
            await register(pool, {GUEST: "guest@elsewhere.example", third: "third@elsewhere.example"})
            with tenant.as_user(GUEST):
                async with pool.connection() as connection:
                    await connection.execute(
                        "INSERT INTO oauth_tokens (provider, account, refresh_token, scope, status) "
                        "VALUES ('google_gmail', 'guest@elsewhere.example', 'sealed', 's', 'connected')")
                    await workspaces.prepare(connection)
                    assert workspaces.emails() == {"guest@elsewhere.example"}  # Theirs, read in their scope.
            assert await workspaces.connected(connect) == [tenant.OWNER, GUEST]  # Not the third: nothing connected.
            with tenant.as_user(third):
                async with pool.connection() as connection:
                    await workspaces.prepare(connection)
                    assert workspaces.emails() == {"third@elsewhere.example"}
                    cursor = await connection.execute("SELECT count(*) FROM oauth_tokens")
                    assert (await cursor.fetchone())[0] == 0  # The guest's token is invisible here.
        finally:
            await pool.close()
            workspaces.forget()
    asyncio.run(main())


def test_a_guests_view_holds_none_of_the_owners_files_or_settings_on_real_postgres(database, monkeypatch, tmp_path):
    """valr_view.collect (every page's data) as a guest, with the owner's server files and settings
    in place: none of them reaches the guest's view."""
    import json
    from app import valr_view
    perf = tmp_path / "performance.json"
    perf.write_text(json.dumps({"schema_version": 3, "interviews": [
        {"interview_id": "fathom:1", "date": "2026-10-01", "company": "Secretco", "overall_score": 7}]}))
    monkeypatch.setenv("VALR_PERFORMANCE_PATH", str(perf))
    monkeypatch.setenv("VALR_PERFORMANCE_FALLBACK", str(perf))
    monkeypatch.setenv("VALR_OWNER_NAME", "Ownername Example")
    monkeypatch.setenv("VALR_CANDIDATE_NAME", "Ownername Example")
    monkeypatch.setenv("VALR_OWNER_PHONE", "+15550100042")
    monkeypatch.setenv("VALR_PUBLIC_NUMBER", "+15550100077")
    monkeypatch.setenv("FATHOM_API_KEY", "owner-fathom-key")
    with tenant.as_user(tenant.OWNER):
        assert "Secretco" in json.dumps(valr_view.profile_files()[0])  # The owner's view would have it.

    async def main():
        pool = await build(database)
        try:
            with tenant.as_user(GUEST):
                view = await valr_view.collect(pool)
        finally:
            await pool.close()
        return view
    view = asyncio.run(main())
    text = json.dumps(view, default=str)
    for secret in ("Secretco", "Ownername", "0100042", "0100077", "owner-fathom-key"):
        assert secret not in text, secret
    assert view["guest"] and not view["setup"]["phone_on_file"] and view["dials"] == {}
    assert not view["connections"]["fathom"]["configured"]  # The server's Fathom key is the owner's.
