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
                             "phone_uploads, phone_audio, phone_upload_token")
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
            assert [r["kind"] for r in await store.pending()] == ["offer"]
            assert len(await store.unlinked()) == 1
            await store.update(first, lead_id="lead-kite", link="/valr/leads?lead=lead-kite")
            assert len(await store.recent_for_lead("lead-kite", now - timedelta(minutes=1))) == 1
            assert await store.unread_count(now - timedelta(days=1)) == 1
            assert len(await store.listing(True, now - timedelta(days=1))) == 1
            assert await store.mark_read([first], now) == 1 and await store.unread_count(now - timedelta(days=1)) == 0
            assert await store.mark_read(None, now) == 0
            await store.save_lead_states({"a": ("screening", "you"), "b": ("applied", "them")})
            await store.save_lead_states({"a": ("interviewing", "nobody")})
            assert await store.lead_states() == {"a": ("interviewing", "nobody")}
            assert (await store.switches()).digest is True
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
