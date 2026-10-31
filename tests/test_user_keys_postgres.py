"""user_keys on a real Postgres (app/user_keys.py, app/tenant.py): the table gets owner_id and
row-level security, the upsert's conflict target works, and one workspace can neither read nor
overwrite another's key, whatever its WHERE clause says. Skipped without a local Postgres."""
import asyncio

import pytest
from cryptography.fernet import Fernet

from tests.test_tenant_postgres import ADMIN, build, database, reachable  # noqa: F401 - the fixture.

psycopg = pytest.importorskip("psycopg")
from app import tenant, user_keys  # noqa: E402

pytestmark = pytest.mark.skipif(not reachable(), reason="no local Postgres superuser (VALR_TEST_PG)")
GUEST, OTHER = "u_" + "6" * 20, "u_" + "7" * 20
KEY, OTHER_KEY = "sk-guest-made-up-aaaa", "sk-other-made-up-bbbb"


def test_one_workspace_cannot_read_or_overwrite_anothers_key(database, monkeypatch):
    monkeypatch.setenv("VALR_TOKEN_KEY", Fernet.generate_key().decode())
    user_keys.forget()

    async def main():
        pool = await build(database)
        try:
            with tenant.as_user(tenant.OWNER):
                async with pool.connection() as connection:
                    await user_keys.setup(connection)
            for user, key in ((GUEST, KEY), (OTHER, OTHER_KEY)):
                with tenant.as_user(user):
                    async with pool.connection() as connection:
                        await user_keys.save(connection, "llm", "openai", "", key, "ok")
                        await user_keys.save(connection, "llm", "openai", "gpt-6-luna", key, "ok")  # The upsert.
            with tenant.as_user(GUEST):
                async with pool.connection() as connection:
                    assert (await user_keys.load(connection))["llm"].key == KEY
                    cursor = await connection.execute("SELECT count(*) FROM user_keys WHERE owner_id = %s", (OTHER,))
                    assert (await cursor.fetchone())[0] == 0
                    cursor = await connection.execute(
                        "UPDATE user_keys SET last4 = 'gone' WHERE owner_id = %s", (OTHER,))
                    assert cursor.rowcount == 0
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await connection.execute(
                            "INSERT INTO user_keys (owner_id, purpose, provider, ciphertext, last4, status) "
                            "VALUES (%s, 'speech', 'deepgram', '\\x00', '0000', 'ok')", (OTHER,))
            with tenant.as_user(OTHER):
                async with pool.connection() as connection:
                    found = await user_keys.load(connection)
                    assert found["llm"].key == OTHER_KEY and found["llm"].model == "gpt-6-luna"
                    assert (await user_keys.public(connection))["llm"]["last4"] == "bbbb"
            with tenant.as_user(tenant.OWNER):
                async with pool.connection() as connection:
                    assert await user_keys.load(connection) == {}  # The owner keeps the server's keys.
            with tenant.as_user(None):
                async with pool.connection() as connection:
                    cursor = await connection.execute("SELECT count(*) FROM user_keys")
                    assert (await cursor.fetchone())[0] == 0  # No workspace: fails closed.
        finally:
            await pool.close()
    asyncio.run(main())
