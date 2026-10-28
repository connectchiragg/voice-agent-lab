"""Table creation runs once per process and database (app/schema.py), not per request."""
import asyncio

from app import calendar_sync, notifications, research_jobs, schema


class Info:
    def __init__(self, dsn):
        self.dsn = dsn


class Connection:
    """A psycopg-like connection: it records statements and transactions."""

    def __init__(self, dsn="dbname=valr"):
        self.info = Info(dsn)
        self.sql = []
        self.transactions = 0

    async def execute(self, query, params=None):
        self.sql.append(query)

        class Cursor:
            rowcount = 0

            async def fetchone(self):
                return None

            async def fetchall(self):
                return []
        return Cursor()

    def transaction(self):
        outer = self

        class Tx:
            async def __aenter__(self):
                outer.transactions += 1

            async def __aexit__(self, *exc):
                return False
        return Tx()


def ddl(connection):
    return [q for q in connection.sql if "CREATE TABLE" in q]


def test_setup_runs_its_ddl_once_per_process_and_database():
    schema.forget()

    async def main():
        first, second, other = Connection(), Connection(), Connection("dbname=other")
        for _ in range(3):
            await research_jobs.setup(first)
            await calendar_sync.setup(first)
            await notifications.Store(first).setup()
        await research_jobs.setup(second)  # Another pooled connection, same database.
        await research_jobs.setup(other)
        return first, second, other
    first, second, other = asyncio.run(main())
    assert len(ddl(first)) == 3  # One per schema, not one per call.
    assert ddl(second) == []
    assert len(ddl(other)) == 1
    assert any("pg_advisory_xact_lock" in q for q in first.sql)  # Serialised across processes.
    assert first.transactions == 3
    schema.forget()


def test_concurrent_first_setups_run_the_ddl_once():
    schema.forget()

    async def main():
        connection = Connection()
        await asyncio.gather(*(research_jobs.setup(connection) for _ in range(5)))
        return connection
    assert len(ddl(asyncio.run(main()))) == 1
    schema.forget()


def test_a_failed_setup_is_retried_next_time():
    schema.forget()
    calls = []

    async def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("deadlock")

    async def main():
        connection = Connection()
        try:
            await schema.once(connection, "t", boom)
        except RuntimeError:
            pass
        await schema.once(connection, "t", boom)
        await schema.once(connection, "t", boom)
    asyncio.run(main())
    assert len(calls) == 2
    schema.forget()


def test_test_stand_ins_without_a_dsn_always_run():
    calls = []

    class Fake:
        async def execute(self, query, params=None):
            calls.append(query)

    async def main():
        for _ in range(2):
            await research_jobs.setup(Fake())
    asyncio.run(main())
    assert len(calls) == 2
