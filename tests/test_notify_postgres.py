"""The notifications and phone-recordings SQL against a real scratch Postgres (skipped unless
VALR_TEST_POSTGRES is a conninfo, e.g. "dbname=valr_notify_test"). Drops its own tables first."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("VALR_TEST_POSTGRES"),
                                reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")


def run(coro):
    return asyncio.run(coro)


async def connect():
    import psycopg
    connection = await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"], autocommit=True)
    await connection.execute("DROP TABLE IF EXISTS notifications, notify_lead_state, notify_settings, "
                             "phone_uploads, phone_audio, phone_upload_token, push_subscriptions")
    return connection


def test_notification_store_sql():
    from app import notifications as n

    async def go():
        async with await connect() as c:
            store = n.Store(c)
            await store.setup()
            now = datetime.now(timezone.utc)
            first = await n.add(store, {"source_id": "gmail:1", "kind": "reply", "title": "Kite replied",
                                        "company": "Kite", "thread_id": "t1"}, now)
            assert first and await n.add(store, {"source_id": "gmail:2", "kind": "reply", "title": "x",
                                                 "thread_id": "t1"}, now) is None
            assert await n.add(store, {"source_id": "gmail:3", "kind": "offer", "title": "Kite sent an offer",
                                       "thread_id": "t1"}, now) == first
            assert await store.insert({"source_id": "gmail:1", "kind": "reply", "importance": "digest",
                                       "title": "dup"}) is None
            assert [r["kind"] for r in await store.push_pending()] == ["offer"]
            assert len(await store.unlinked()) == 1
            await store.update(first, lead_id="lead-kite", link="/valr/leads?lead=lead-kite")
            assert len(await store.recent_for_lead("lead-kite", now - timedelta(minutes=1))) == 1
            assert await store.unread_count(now - timedelta(days=1)) == 1
            assert len(await store.listing(True, now - timedelta(days=1))) == 1
            assert await store.mark_read([first], now) == 1 and await store.unread_count(now - timedelta(days=1)) == 0
            assert await store.mark_read(None, now) == 0
            # Items of the removed (inferred) kinds stored by an older version: hidden.
            for kind in n.REMOVED:
                await store.insert({"source_id": f"{kind}:x", "kind": kind, "importance": "digest", "title": "old"})
            assert await store.unread_count(now - timedelta(days=1)) == 0
            assert await store.listing(False, now - timedelta(days=1)) != []
            assert all(r["kind"] not in n.REMOVED for r in await store.listing(False, now - timedelta(days=1)))
            assert all(r["kind"] not in n.REMOVED for r in await store.push_pending())
            await store.setup()
            cursor = await c.execute("SELECT count(*) FROM notifications WHERE kind = ANY(%s) AND (read_at IS NULL "
                                     "OR push_state = 'pending')", (list(n.REMOVED),))
            assert (await cursor.fetchone())[0] == 0
            assert (await store.switches()).digest is True
            # The unread pass: items by Gmail message id; read, then unread again.
            items = await store.email_items(now - timedelta(days=1))
            assert set(items) == {"1"} and items["1"]["read_at"] is not None
            assert await store.mark_unread([items["1"]["id"]]) == 1
            assert (await store.email_items(now - timedelta(days=1)))["1"]["read_at"] is None
            await store.save_switches(digest=False)
            await store.save_switches(last_digest_on=now.date())
            switches = await store.switches()
            assert switches.digest is False and switches.last_digest_on == now.date()
            assert await store.try_lock()
            await store.unlock()
            await store.purge(now + timedelta(seconds=1))
            assert await store.listing(False, now - timedelta(days=1)) == []
    run(go())


def test_phone_store_sql(monkeypatch):
    from cryptography.fernet import Fernet
    from app import phone_recordings as phone
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", key)
    monkeypatch.setenv("VALR_UPLOAD_TOKEN", "seed-token-for-tests")

    async def go():
        async with await connect() as c:
            await phone.setup(c)
            assert await phone.current_hash(c) == phone.digest("seed-token-for-tests")
            assert await phone.reveal(c) == "seed-token-for-tests"
            with pytest.raises(phone.AlreadyShown):
                await phone.reveal(c)
            new = await phone.rotate(c)
            assert await phone.current_hash(c) == phone.digest(new)
            await phone.begin_upload(c, "u1")
            data = b"\x00\x00\x00\x20ftypM4A " + os.urandom(phone.CHUNK + 1000)
            chunker = phone.Chunker()
            pieces = chunker.feed(data) + [chunker.rest()]
            for i, piece in enumerate(pieces):
                await phone.put_chunk(c, "u1", i, phone.seal_chunk(key, piece))
            await phone.finish_upload(c, "u1", len(data), "m4a", "Acme call", "Riya Sen", None)

            async def get(idx):
                return await phone.chunk(c, "u1", idx)
            back = b"".join([p async for p in phone.read_range(get, key, 0, len(data) - 1)])
            assert back == data
            claimed = await phone.claim(c)
            assert claimed["id"] == "u1" and claimed["tries"] == 1 and await phone.claim(c) is None
            now = datetime.now(timezone.utc)
            await phone.mark(c, "u1", status="done", processed_at=now, delete_after=now - timedelta(seconds=1))
            state = await phone.status(c)
            assert state["token"] and state["last_status"] == "done" and state["waiting"] == 0
            assert await phone.purge(c, now) == 1 and await phone.chunk(c, "u1", 0) is None
            assert (await phone.upload(c, "u1"))["audio_deleted_at"] is not None
            await phone.stuck(c, now)
            await phone.drop_upload(c, "u1")
            assert await phone.upload(c, "u1") is None
    run(go())


def test_push_columns_migrate_old_rows_and_the_subscription_store():
    from app import notifications as n
    from app import webpush

    async def go():
        async with await connect() as c:
            # The table as valr-notify created it, with a row from before phone push.
            old_schema = n.SCHEMA.split("-- Phone push")[0]
            await c.execute(old_schema)
            await c.execute("INSERT INTO notifications (source_id, kind, importance, title) "
                            "VALUES ('gmail:old', 'offer', 'immediate', 'Old')")
            store = n.Store(c)
            await store.setup()
            await store.setup()  # Idempotent.
            now = datetime.now(timezone.utc)
            await n.add(store, {"source_id": "gmail:new", "kind": "reply", "title": "Kite replied"}, now)
            assert [r["source_id"] for r in await store.push_pending()] == ["gmail:new"]  # The old row: never.
            await store.save_switches(digest_at="18:30", quiet_start="22:00", quiet_end="07:00")
            saved = (await store.switches()).view()
            assert (saved["digest_at"], saved["quiet_start"], saved["quiet_end"]) == ("18:30", "22:00", "07:00")
            subs = webpush.Store(c)
            await subs.setup()
            await subs.save("https://web.push.apple.com/a", "p" * 40, "a" * 16, "iPhone")
            await subs.save("https://web.push.apple.com/a", "q" * 40, "b" * 16, "iPhone")  # Same device: updated.
            rows = await subs.all()
            assert await subs.count() == 1 and rows[0]["p256dh"] == "q" * 40
            await subs.failed(rows[0]["id"])
            await subs.ok(rows[0]["id"], now)
            assert (await subs.all())[0]["failures"] == 0
            assert await subs.remove("https://web.push.apple.com/a") == 1
            await subs.save("https://web.push.apple.com/b", "p" * 40, "a" * 16, "")
            await subs.delete((await subs.all())[0]["id"])
            assert await subs.count() == 0
    run(go())


def test_phone_duplicate_by_sha256_sql():
    from app import phone_recordings as phone

    async def go():
        async with await connect() as c:
            await phone.setup(c)
            for upload_id in ("d1", "d2"):
                await phone.begin_upload(c, upload_id)
            await phone.finish_upload(c, "d1", 10, "m4a", "", "", None, "abc")
            assert await phone.find_duplicate(c, "abc", "d2") == "d1"
            assert await phone.find_duplicate(c, "abc", "d1") is None
            await phone.mark(c, "d1", status="failed")
            assert await phone.find_duplicate(c, "abc", "d2") is None
            await phone.release_failed(c, "abc")
            await phone.finish_upload(c, "d2", 10, "m4a", "", "", None, "abc")  # The unique index allows it now.
            assert (await phone.upload(c, "d1"))["status"] == "failed"
    run(go())
