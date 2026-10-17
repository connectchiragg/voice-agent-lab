"""Phone calls in the bell within seconds of their upload: the item at upload, the worker woken by
NOTIFY, the same item made "ready" (silently), the review's push replacing the call's on the
phone, failures shown, the past page while transcribing, and the latency series. All names and
numbers are made up."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import notifications as n
from app import phone_recordings as phone
from tests.test_notifications import MemoryStore, PushSender
from tests.test_phone_recordings import FakePhone, Resolver, post  # noqa: F401 - fixtures below
from tests.test_phone_recordings import client, key  # noqa: F401

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)  # 11:30 IST: no quiet hours.


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("VALR_NOTIFY_QUIET", "VALR_NOTIFY_IMMEDIATE", "VALR_NOTIFY_DIGEST", "VALR_NOTIFY_QUIET_HOURS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")


def test_a_call_is_in_the_bell_silently_and_its_only_push_is_the_coach_review():
    store, sender = MemoryStore(), PushSender()
    assert run(n.from_call_upload(store, "ab12cd34", "Priya Raman", NOW)) is not None
    row = store.by_source("phone:ab12cd34")
    assert row["title"] == "Call with Priya Raman recorded" and row["read_at"] == NOW  # No red dot.
    assert row["link"] == "/valr/interviews/past/phone-ab12cd34" and row["arrived_at"] == NOW
    assert run(n.dispatch(store, NOW, sender=sender))["pushed"] == 0 and sender.sent == []
    # Processed: the same item says so, still without a push.
    later = NOW + timedelta(seconds=40)
    assert run(n.from_recording(store, "phone", "ab12cd34", "Northwind Labs", "Priya Raman", later)) is None
    row = store.by_source("phone:ab12cd34")
    assert row["title"] == "Call with Northwind Labs is ready" and row["ready_at"] == later and len(store.rows) == 1
    assert run(n.dispatch(store, later, sender=sender))["pushed"] == 0 and sender.sent == []
    assert run(n.from_recording(store, "phone", "ab12cd34", "Northwind Labs", "Priya Raman", later)) is None  # A re-run.
    # A recording with no upload item (a notetaker) is silent too.
    run(n.from_recording(store, "fathom", "f1", "Northwind Labs", "", later))
    assert store.by_source("fathom:f1")["push_state"] == "skipped"
    assert run(n.dispatch(store, later, sender=sender))["pushed"] == 0


def test_a_number_never_reaches_a_title_or_a_push():
    store, sender = MemoryStore(), PushSender()
    run(n.from_call_upload(store, "n1", "+91 98765 43210", NOW))
    assert store.by_source("phone:n1")["title"] == "New call recording"
    run(n.from_call_upload(store, "n2", "Dev 555 0100 222", NOW))
    run(n.dispatch(store, NOW, sender=sender))
    assert all(not any(ch.isdigit() for ch in m["title"] + m["body"]) for m in sender.sent)


def test_a_recording_without_an_upload_item_is_new_and_pushed():
    store = MemoryStore()
    assert run(n.from_recording(store, "fathom", "77001", "Northwind Labs", "", NOW)) is not None
    assert store.by_source("fathom:77001")["ready_at"] == NOW


def test_a_call_that_fails_for_good_says_so():
    store = MemoryStore()
    run(n.from_call_upload(store, "f1", "Priya Raman", NOW))
    assert run(n.call_failed(store, "phone", "f1", "Priya Raman", NOW))
    row = store.by_source("phone:f1")
    assert row["title"] == "Call with Priya Raman: transcription failed" and row["action"] == "failed"
    assert not run(n.call_failed(store, "phone", "missing", "", NOW))


def test_the_latency_report_has_a_phone_series():
    store = MemoryStore()
    for i, (bell, ready) in enumerate([(1, 40), (2, 55), (3, None)]):
        arrived = NOW - timedelta(seconds=60)
        run(store.insert({"source_id": f"phone:c{i}", "kind": "recording", "importance": "immediate", "title": "x",
                          "created_at": arrived + timedelta(seconds=bell), "arrived_at": arrived,
                          "push_sent_at": arrived + timedelta(seconds=bell + 1), "push_state": "sent",
                          "ready_at": arrived + timedelta(seconds=ready) if ready else None}))
    report = run(n.latency_report(store, NOW))
    assert report["count"] == 0  # Emails are their own series.
    calls = report["phone"]
    assert calls["count"] == 3 and calls["bell_p50_s"] == 2.0 and calls["bell_p95_s"] == 3.0 and calls["met"] is True
    assert calls["push_p95_s"] == 4.0 and calls["ready_count"] == 2 and calls["ready_p95_s"] == 55.0


class Conn:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, params=None):
        self.sql.append(sql)


def test_announce_wakes_the_worker_without_a_push(monkeypatch):
    store, pushed = MemoryStore(), []

    async def dispatch_now(connection, tries=5, wait=0.5):
        pushed.append(connection)
    monkeypatch.setattr(n, "Store", lambda connection: store)
    monkeypatch.setattr(n, "dispatch_now", dispatch_now)
    conn = Conn()
    run(phone.announce(conn, "ab12", "Priya Raman", NOW))
    assert conn.sql == [f"NOTIFY {phone.WAKE_CHANNEL}"] and pushed == []
    assert store.by_source("phone:ab12")["title"] == "Call with Priya Raman recorded"

    class Broken(Conn):
        async def execute(self, sql, params=None):
            raise RuntimeError("database gone")
    monkeypatch.setattr(n, "Store", lambda connection: (_ for _ in ()).throw(RuntimeError("no store")))
    run(phone.announce(Broken(), "cd34", "", NOW))  # Never raises: the upload is stored either way.


def test_the_upload_announces_the_call(client, monkeypatch):  # noqa: F811
    seen = []

    async def announce(connection, upload_id, other_name, now=None):
        seen.append((upload_id, other_name))
    monkeypatch.setattr(phone, "announce", announce)
    upload_id = post(client, fields={"name": "Priya Raman"}).json()["id"].split(":", 1)[1]
    assert seen == [(upload_id, "Priya Raman")]


def test_a_final_failure_updates_the_calls_item(client, key, monkeypatch):  # noqa: F811
    import httpx
    failed = []

    async def call_failed(store, provider, external_id, other, now):
        failed.append((provider, external_id, other))
        return True

    async def setup(self):
        return None
    monkeypatch.setattr(n, "call_failed", call_failed)
    monkeypatch.setattr(n.Store, "setup", setup)
    upload_id = post(client, fields={"name": "Priya Raman"}).json()["id"].split(":", 1)[1]
    processor = phone.Processor(http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                                deepgram=phone.DeepgramPrerecorded("dg-test-key"), resolver=Resolver(),
                                match=lambda r: None, candidate="Sam")
    for attempt in range(phone.MAX_TRIES):
        run(processor.run_once(object()))
        assert len(failed) == (1 if attempt == phone.MAX_TRIES - 1 else 0)  # Only when it gives up.
    assert failed == [("phone", upload_id, "Priya Raman")]


def test_the_past_page_says_transcribing_until_the_call_is_processed(client, monkeypatch):  # noqa: F811
    from app.valr_ui import routes
    states = {"phone-ab12": "processing", "phone-dead": "failed"}

    async def state(slug):
        return states.get(slug)
    monkeypatch.setattr(routes, "phone_upload_state", state)
    page = client.get("/valr/interviews/past/phone-ab12", headers=client.good)
    assert page.status_code == 200 and "Transcribing this call" in page.text and 'http-equiv="refresh"' in page.text
    failed = client.get("/valr/interviews/past/phone-dead", headers=client.good)
    assert failed.status_code == 200 and "could not be transcribed" in failed.text
    assert client.get("/valr/interviews/past/phone-nope", headers=client.good).status_code == 404


def test_the_phone_upload_state_reads_the_upload(client, monkeypatch):  # noqa: F811
    from app.valr_ui import routes
    upload_id = post(client).json()["id"].split(":", 1)[1]
    assert run(routes.phone_upload_state(f"phone-{upload_id}")) == "uploaded"
    assert run(routes.phone_upload_state("phone-unknown1")) is None
    assert run(routes.phone_upload_state("fathom-77001")) is None
    assert run(routes.phone_upload_state("phone-bad;id")) is None


def test_without_a_listener_the_loop_waits_its_poll_interval():
    stop = asyncio.Event()

    async def main():
        start = asyncio.get_running_loop().time()
        assert await phone.wait_for_upload(None, 0.2, stop) is False
        return asyncio.get_running_loop().time() - start
    assert 0.15 < run(main()) < 1.5


needs_postgres = pytest.mark.skipif(not os.getenv("VALR_TEST_POSTGRES"),
                                    reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")


@needs_postgres
def test_a_notify_wakes_the_worker_within_a_second():
    import psycopg

    async def connect():
        return await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"], autocommit=True)

    async def main():
        stop = asyncio.Event()
        listener = await phone.listen(connect)
        assert listener is not None
        loop_time = asyncio.get_running_loop().time
        start = loop_time()
        waiting = asyncio.ensure_future(phone.wait_for_upload(listener, 30, stop))
        await asyncio.sleep(0.2)
        async with await connect() as app_side:
            await phone.announce(app_side, "pg1", "", NOW)  # NOTIFY first; the bell part needs no tables here.
        woke = await asyncio.wait_for(waiting, 5)
        await listener.close()
        return woke, loop_time() - start
    woke, took = run(main())
    assert woke is True and took < 2  # Not the 30 s poll.


@needs_postgres
def test_the_worker_loop_processes_an_upload_right_after_its_notify(monkeypatch):
    import psycopg

    async def connect():
        return await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"], autocommit=True)

    passes = []

    async def run_once(self, connection, now=None):
        passes.append(asyncio.get_running_loop().time())
        return None

    async def nothing(*args, **kwargs):
        return []
    monkeypatch.setattr(phone.Processor, "run_once", run_once)
    monkeypatch.setattr(phone, "reevaluate", nothing)
    monkeypatch.setenv("VALR_PHONE_LOOP_S", "30")

    async def main():
        stop = asyncio.Event()
        task = asyncio.ensure_future(phone.loop(connect, stop))
        while not passes:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        sent = asyncio.get_running_loop().time()
        async with await connect() as app_side:
            await app_side.execute(f"NOTIFY {phone.WAKE_CHANNEL}")
        while len(passes) < 2:
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)
        return passes[1] - sent
    assert run(asyncio.wait_for(main(), 10)) < 2
