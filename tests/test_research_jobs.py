"""VALR jobs: the callback-time estimator, pricing, limits and the spend tracker.

The Postgres tests need a scratch database: VALR_TEST_POSTGRES=<conninfo>
(for example `dbname=valr_test`); they drop and recreate the VALR tables.
"""
import asyncio
import os

import psycopg
import pytest

from app import research_jobs
from app.research_jobs import Booking, Limits, p90, price, promise_minutes


def test_the_promise_comes_from_the_p90_plus_dialling_and_is_two_to_five_minutes():
    assert p90([30.0] * 4) == research_jobs.SPIKE_P90_S  # Too few samples: the spike's p90.
    assert promise_minutes(research_jobs.SPIKE_P90_S) == 3  # 57 + 60 + 30 s -> 3 minutes.
    assert p90([float(s) for s in range(1, 21)]) == 18.0  # Nearest rank: 18th of 20.
    assert promise_minutes(5) == 2  # Never less than 2.
    assert promise_minutes(100) == 4
    assert promise_minutes(1000) == 5  # Never more than 5.
    # The job's wall-clock limit leaves time to dial within the promise.
    assert research_jobs.job_seconds(3) == 120


def test_prices_come_from_usage_tokens_and_searches():
    assert price("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert price("claude-sonnet-5-5", 0, 1_000_000) == pytest.approx(10.0)
    assert price("claude-haiku-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.01)
    assert price("claude-haiku-5-5", 0, 0, searches=3) == pytest.approx(0.03)


def test_callers_are_kept_as_short_hashes_never_numbers():
    key = "tel:+919812345678"
    assert research_jobs.caller_hash(key) == research_jobs.caller_hash(key)
    assert len(research_jobs.caller_hash(key)) == 12
    assert "9812345678" not in research_jobs.caller_hash(key)
    assert research_jobs.caller_hash(key) != research_jobs.caller_hash("tel:+919812345679")


def test_limits_have_owner_defaults_and_env_overrides(monkeypatch):
    for name in ("VALR_JOBS_PER_CALLER_DAY", "VALR_JOBS_PER_DAY", "VALR_MONTH_USD", "VALR_JOB_USD"):
        monkeypatch.delenv(name, raising=False)
    assert Limits.from_env() == Limits(3, 15, 20.0, 0.25)
    monkeypatch.setenv("VALR_MONTH_USD", "5")
    monkeypatch.setenv("VALR_JOBS_PER_CALLER_DAY", "1")
    assert Limits.from_env().month_usd == 5.0 and Limits.from_env().jobs_per_caller_day == 1


needs_postgres = pytest.mark.skipif(not os.getenv("VALR_TEST_POSTGRES"),
                                    reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")


def with_db(test):
    async def main():
        async with await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"],
                                                         autocommit=True) as connection:
            await connection.execute("DROP TABLE IF EXISTS research_jobs, research_spend")
            await research_jobs.setup(connection)
            await test(connection)
    asyncio.run(main())


def row(part="web", model="claude-haiku-5-5", searches=0, input_tokens=10_000):
    return dict(part=part, kind="agent", model=model, input_tokens=input_tokens,
                output_tokens=1000, cache_read=0, cache_write=0, searches=searches)


@needs_postgres
def test_bookings_stop_at_three_per_caller_and_fifteen_per_day():
    async def test(connection):
        limits = Limits(jobs_per_caller_day=3, jobs_per_day=5)
        for n in range(3):
            assert await research_jobs.reserve(connection, Booking(f"a{n}", "tel:+1555000001", "Coorg", 3),
                                               limits) is None
        assert await research_jobs.reserve(connection, Booking("a3", "tel:+1555000001", "Coorg", 3),
                                           limits) == "caller_day"
        for n in range(2):
            assert await research_jobs.reserve(connection, Booking(f"b{n}", f"tel:+155500000{n + 2}",
                                                                   "Austin", 3), limits) is None
        assert await research_jobs.reserve(connection, Booking("c", "tel:+1555000009", "Hampi", 3),
                                           limits) == "day"
        stored = await research_jobs.scalar(connection, "SELECT count(*) FROM research_jobs "
                                                        "WHERE caller_hash LIKE '%%+%%'")
        assert stored == 0  # Only hashes.
    with_db(test)


@needs_postgres
def test_spend_is_tracked_per_request_and_the_month_cap_refuses_new_jobs():
    async def test(connection):
        limits = Limits(month_usd=1.0, job_usd=0.25)
        assert await research_jobs.reserve(connection, Booking("j1", "tel:+1555000001", "Coorg", 3),
                                           limits) is None
        usd = await research_jobs.record_spend(connection, "j1", [row(searches=3), row("synth",
                                               "claude-sonnet-5-5")])
        assert usd == pytest.approx(0.0015 + 0.03 + 0.02 + 0.01)
        assert await research_jobs.job_spend(connection, "j1") == pytest.approx(usd)
        assert await research_jobs.month_spend(connection) == pytest.approx(usd)
        assert await research_jobs.day_spend(connection) == pytest.approx(usd)
        # $0.80 more this month: one more $0.25 job would pass the $1 cap.
        await research_jobs.record_spend(connection, "j1", [row(input_tokens=8_000_000)])
        assert await research_jobs.reserve(connection, Booking("j2", "tel:+1555000002", "Austin", 3),
                                           limits) == "month"
    with_db(test)


@needs_postgres
def test_the_promise_adapts_to_the_last_finished_jobs_and_results_load_by_id():
    async def test(connection):
        limits = Limits(jobs_per_caller_day=50, jobs_per_day=50)
        assert await research_jobs.current_promise(connection) == 3  # No history: the spike.
        for n in range(6):
            await research_jobs.reserve(connection, Booking(f"j{n}", f"tel:+15550000{n:02}", "X", 3), limits)
            await research_jobs.finish(connection, f"j{n}", "done", 200.0, "Spoken.",
                                       [{"fact": "F", "source_ids": ["S1"], "sensitive": False}],
                                       [{"id": "S1", "url": "https://example.org"}], {"page_chars": 2}, "")
        assert await research_jobs.current_promise(connection) == 5  # Slow jobs: a longer promise.
        job = await research_jobs.load(connection, "j0")
        assert job["spoken"] == "Spoken." and job["sources"][0]["url"] == "https://example.org"
        view = await research_jobs.overview(connection, limits)
        assert view["today_jobs"] == 6 and view["jobs"][0]["cap_hits"] == {"page_chars": 2}
    with_db(test)
