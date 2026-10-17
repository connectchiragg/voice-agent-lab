"""Settings and Profile actions (app/settings_web.py) and the flows behind them: profile and
phone (set up, change, remove, errors), the photo, Google disconnect and sync, sign out of all
devices, export and delete, upload tokens on and off, the push heads-up. Owner only, with
CSRF. Every name, number and address is made up."""
import asyncio
import base64
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytest.importorskip("fastapi", reason="the pages run in the app's venv")

from app import google_oauth, interview_store, notifications, owner, settings_web  # noqa: E402
from tests.test_valr_pages import GOOD, OWNER, client  # noqa: E402,F401

PHONE = "+919800000001"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def memory_profile(monkeypatch):
    """owner_profile in memory: the API layer is tested here, the SQL in tests/test_owner.py."""
    row = {"display_name": "Sam Rivera", "phone_e164": PHONE}
    owner.reset()
    owner.apply(dict(row))
    photo = {}

    async def save(connection, **values):
        if "photo" in values:
            photo["data"] = values.pop("photo")
        owner.apply({**owner._row, **values})
        return owner._row

    async def set_phone(connection, e164):
        return await save(connection, phone_e164=e164, phone_removed=False)

    async def remove_phone(connection):
        return await save(connection, phone_e164=None, phone_removed=True)

    async def get_photo(connection):
        data = photo.get("data")
        return (data, owner._row.get("photo_type") or "image/png", owner.photo_etag()) if data else None

    async def nothing(*args, **kwargs):
        return None
    monkeypatch.setattr(owner, "save", save)
    monkeypatch.setattr(owner, "set_phone", set_phone)
    monkeypatch.setattr(owner, "remove_phone", remove_phone)
    monkeypatch.setattr(owner, "photo", get_photo)
    monkeypatch.setattr(owner, "setup", nothing)
    monkeypatch.setattr(owner, "refresh", nothing)
    yield photo
    owner.reset()


def headers(client):
    csrf = client.get("/valr", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    return {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}


ACTIONS = ("/valr/api/profile", "/valr/api/profile/photo", "/valr/api/profile/photo/remove", "/valr/api/profile/phone",
           "/valr/api/profile/phone/remove", "/valr/api/google/disconnect", "/valr/api/google/sync",
           "/valr/api/data/delete", "/logout/all")


def test_every_action_is_owner_only_with_csrf(client):
    good = headers(client)
    for path in ACTIONS:
        assert client.post(path, content="{}").status_code in (401, 403), path
        assert client.post(path, content="{}", headers={**GOOD, "Content-Type": "application/json"}).status_code == 403, path
        assert client.post(path, content="{}", headers={**good, "Cf-Access-Jwt-Assertion": "guest"}).status_code == 403, path
        assert client.post(path, content="{}", headers={**good, "Origin": "https://evil.example"}).status_code == 403, path
    for path in ("/valr/api/profile", "/valr/api/profile/phone", "/valr/api/export", "/valr/profile/photo", "/valr/profile"):
        assert client.get(path).status_code in (401, 403), path
        assert client.get(path, headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403, path


# Profile.

def test_profile_save_validates_each_field(client):
    good = headers(client)
    bad = client.post("/valr/api/profile", content=json.dumps({"display_name": "<b>", "timezone": "Mars/Base"}),
                      headers=good)
    assert bad.status_code == 400 and set(bad.json()["errors"]) == {"display_name", "timezone"}
    assert owner.name() == "Sam Rivera"  # Nothing saved when any field is wrong.
    ok = client.post("/valr/api/profile", content=json.dumps({
        "display_name": "Alex  Kim", "recording_name": "Alex K", "name_pronunciation": "Al-ex",
        "timezone": "Europe/London"}), headers=good).json()
    assert ok["message"] == "Profile saved." and ok["display_name"] == "Alex Kim"
    assert (owner.first_name(), owner.recording_name(), owner.zone_name(), owner.pronounce()) == (
        "Alex", "Alex K", "Europe/London", "Al-ex")
    from app import interviews
    assert " Alex, " in interviews.owner_greeting("call-1")  # The next call, no restart.
    assert client.post("/valr/api/profile", content="{}", headers=good).status_code == 400
    assert client.post("/valr/api/profile", content='{"email": "x"}', headers=good).status_code == 400
    page = client.get("/valr/profile", headers=GOOD).text
    assert 'value="Alex Kim"' in page and '<option value="Europe/London" selected>' in page and OWNER in page


def test_photo_upload_serve_and_remove(client, memory_profile):
    good = headers(client)
    assert client.get("/valr/profile/photo", headers=GOOD).status_code == 404
    svg = base64.b64encode(b"<svg onload=alert(1)>").decode()
    assert client.post("/valr/api/profile/photo", content=json.dumps({"image": svg}), headers=good).status_code == 415
    assert client.post("/valr/api/profile/photo", content=json.dumps({"image": "not base64!!"}),
                       headers=good).status_code == 400
    big = base64.b64encode(PNG + b"\x00" * (2 * 1024 * 1024)).decode()
    assert client.post("/valr/api/profile/photo", content=json.dumps({"image": big}), headers=good).status_code == 413
    data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    done = client.post("/valr/api/profile/photo", content=json.dumps({"image": data_url}), headers=good).json()
    assert done["photo"] and memory_profile["data"] == PNG
    served = client.get("/valr/profile/photo", headers=GOOD)
    assert served.content == PNG and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff" and served.headers["cache-control"].startswith("private")
    etag = served.headers["etag"]
    assert client.get("/valr/profile/photo", headers={**GOOD, "If-None-Match": etag}).status_code == 304
    top = client.get("/valr", headers=GOOD).text
    assert f'/valr/profile/photo?v={owner.photo_etag()}' in top  # The avatar in the top bar, every page.
    assert 'href="/valr/profile"' in top  # The avatar menu links to Profile.
    client.post("/valr/api/profile/photo/remove", content="{}", headers=good)
    assert not owner.photo_etag() and ">SR</span>" in client.get("/valr", headers=GOOD).text


# Phone.

def test_phone_change_validate_and_remove(client, monkeypatch):
    good = headers(client)
    flags = {interview_store.AUTO_DIAL: True}

    async def auto_dial(connection):
        return flags[interview_store.AUTO_DIAL]

    async def set_flag(connection, name, enabled, until=None):
        flags[name] = enabled
    monkeypatch.setattr(interview_store, "auto_dial", auto_dial)
    monkeypatch.setattr(interview_store, "set_flag", set_flag)
    shown = client.get("/valr/api/profile/phone", headers=GOOD).json()
    assert shown == {"ok": True, "masked": "+91 ••••• •0001", "set": True, "source": "db", "country": "+91"}
    for raw, why in (("12345", "country code"), ("+91 2223456789", "mobile"), ("+1 800 555 0123", "toll-free")):
        refused = client.post("/valr/api/profile/phone", content=json.dumps({"phone": raw}), headers=good)
        assert refused.status_code == 400 and why in refused.json()["detail"], raw
    assert owner.phone() == PHONE  # A bad number leaves the old one.
    changed = client.post("/valr/api/profile/phone", content=json.dumps({"phone": "98000 00002"}), headers=good).json()
    assert changed["masked"] == "+91 ••••• •0002" and owner.phone() == "+919800000002"
    assert "9800000002" not in json.dumps(changed)
    removed = client.post("/valr/api/profile/phone/remove", content="{}", headers=good).json()
    assert owner.phone() == "" and removed["set"] is False and removed["reach"] == "heads_up"
    assert flags[interview_store.AUTO_DIAL] is False  # Automatic calls fell back to the heads-up.
    page = client.get("/valr/settings", headers=GOOD).text
    assert "Add your phone so VALR can call you." in page and 'data-mode="call" aria-pressed="false" disabled' in page


def test_an_env_phone_is_shown_as_from_server_settings(client, monkeypatch):
    owner.apply({"display_name": "Sam Rivera"})
    monkeypatch.setenv("VALR_OWNER_PHONE", "+919800000003")
    page = client.get("/valr/settings", headers=GOOD).text
    assert "+91 ••••• •0003" in page and "from server settings" in page and "9800000003" not in page


# Google.

def test_google_disconnect_revokes_and_deletes(client, monkeypatch):
    calls = []

    async def disconnect(connection, settings, http):
        calls.append(settings)
        return {"revoked": True, "stopped": True}

    async def nothing(connection):
        return None
    monkeypatch.setattr(google_oauth, "disconnect", disconnect)
    monkeypatch.setattr(google_oauth, "setup", nothing)
    done = client.post("/valr/api/google/disconnect", content="{}", headers=headers(client)).json()
    assert done["message"] == "Google disconnected." and len(calls) == 1


def test_google_disconnect_steps(monkeypatch):
    """Stop the channels, revoke at Google, then delete the token, even when Google says no."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    settings = google_oauth.OAuthSettings("client", "secret", "https://valr.example/cb", key)
    sealed = google_oauth.encrypt(key, "refresh-token-fictional")
    sql = []

    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        async def execute(self, statement, params=()):
            sql.append(" ".join(statement.split())[:40])
            if statement.startswith("SELECT refresh_token FROM"):
                return Cursor((sealed,))
            if statement.startswith("SELECT refresh_token, status"):
                return Cursor((sealed, "connected"))
            if "FROM calendar_state" in statement:
                return Cursor(("tok", "chan-1", "res-1", "ct", None, "google", None, None, None))
            return Cursor(None)
    seen = []

    def google(request):
        seen.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "access-fictional"})
        if request.url.path == "/revoke":
            assert b"refresh-token-fictional" in request.content
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(google))
    result = run(google_oauth.disconnect(Connection(), settings, http))
    assert result == {"revoked": True, "stopped": True}
    assert "/revoke" in seen and "/calendar/v3/channels/stop" in seen and "/gmail/v1/users/me/stop" in seen
    assert any(s.startswith("DELETE FROM oauth_tokens") for s in sql)


def test_google_callback_returns_to_settings_with_the_problem_in_the_card(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("VALR_TOKEN_KEY", Fernet.generate_key().decode())
    back = client.get("/oauth/google/callback?error=access_denied&state=x", headers=GOOD, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/valr/settings?google_error=cancelled#google"
    page = client.get("/valr/settings?google_error=cancelled", headers=GOOD).text
    assert "You cancelled on Google" in page
    assert "<p>" not in client.get("/oauth/google/callback?state=x&code=y", headers=GOOD, follow_redirects=False).text


# Account and data.

def test_sign_out_of_all_devices(client, monkeypatch):
    from app.auth import store
    ended = []

    class Backend:
        async def end_all(self):
            ended.append(1)
            return 3
    monkeypatch.setattr(store, "backend", lambda: Backend())
    done = client.post("/logout/all", content="{}", headers=headers(client))
    assert done.status_code == 200 and done.json()["ended"] == 3 and ended == [1]
    assert "Max-Age=0" in done.headers["set-cookie"] and done.json()["next"].startswith("/login")


def test_export_is_a_zip_without_secrets(client, monkeypatch):
    async def export_rows(connection):
        return {"owner_profile": [settings_web.clean({"display_name": "Sam Rivera", "photo": "bytes", "phone_e164": PHONE})],
                "oauth_tokens": [settings_web.clean({"account": OWNER, "refresh_token": "secret-fictional"})],
                "notetaker_credentials": [settings_web.clean({"provider": "fathom", "secret": "k"})]}
    monkeypatch.setattr(settings_web, "export_rows", export_rows)
    monkeypatch.setattr(settings_web, "scorecard_files", lambda: [])
    response = client.get("/valr/api/export", headers=GOOD)
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="valr-data-')
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert {"README.txt", "owner_profile.json", "oauth_tokens.json"} <= names
    text = "".join(archive.read(n).decode() for n in names)
    assert "secret-fictional" not in text and '"photo"' not in text and '"secret"' not in text


def test_delete_needs_the_word_and_wipes_every_table(client, monkeypatch):
    executed = []

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        async def fetchall(self):
            return self.rows

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Connection:
        async def execute(self, sql, params=()):
            executed.append(sql)
            if "to_regclass" in sql:
                return Cursor([(name,) for name in params[0] if name != "call_recordings"])
            return Cursor([])

        def transaction(self):
            return Transaction()

    class Pool:
        def connection(self):
            class Context:
                async def __aenter__(self):
                    return Connection()

                async def __aexit__(self, *exc):
                    return False
            return Context()

    async def disconnect(connection, settings, http):
        executed.append("revoke")
        return {"revoked": True, "stopped": True}

    async def nothing(connection):
        return None
    monkeypatch.setattr(settings_web, "pool", lambda: Pool())
    monkeypatch.setattr(google_oauth, "disconnect", disconnect)
    monkeypatch.setattr(google_oauth, "setup", nothing)
    monkeypatch.setattr(settings_web, "scorecard_files", lambda: [])
    good = headers(client)
    for body in ("{}", '{"confirm": "delete"}', '{"confirm": "yes"}'):
        assert client.post("/valr/api/data/delete", content=body, headers=good).status_code == 400
    assert executed == []
    done = client.post("/valr/api/data/delete", content='{"confirm": "DELETE"}', headers=good)
    assert done.status_code == 200 and "Max-Age=0" in done.headers["set-cookie"]
    truncate = next(sql for sql in executed if sql.startswith("TRUNCATE"))
    assert executed.index("revoke") < executed.index(truncate)  # Google first, while the token exists.
    for table in ("notifications", "owner_profile", "valr_sessions", "recording_transcripts", "callers"):
        assert table in truncate
    assert "call_recordings" not in truncate  # Only tables that exist.


# Upload tokens: on and off.

def test_upload_token_turns_off_and_back_on(monkeypatch):
    from app import phone_recordings as phone
    row = {}

    class Cursor:
        def __init__(self, value):
            self.value = value

        async def fetchone(self):
            return self.value

    class Connection:
        async def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO phone_upload_token"):
                if "'off'" in sql:
                    row.update(token_hash=params[0], source="off", revealed_at=datetime.now(timezone.utc))
                else:
                    row.update(token_hash=params[0], source=params[1], revealed_at=datetime.now(timezone.utc))
            if sql.startswith("SELECT token_hash"):
                return Cursor((row["token_hash"], row["source"], None, row["revealed_at"]) if row else None)
            return Cursor(None)
    monkeypatch.delenv("VALR_UPLOAD_TOKEN", raising=False)
    first = run(phone.rotate(Connection()))
    assert phone.token_matches(first, run(phone.current_hash(Connection())))
    run(phone.revoke(Connection()))
    assert not phone.token_matches(first, run(phone.current_hash(Connection())))
    assert not phone.token_matches(phone.OFF, run(phone.current_hash(Connection())))  # Not even the marker.
    with pytest.raises(phone.AlreadyShown):
        run(phone.reveal(Connection()))
    again = run(phone.rotate(Connection()))  # "Turn uploads on".
    assert phone.token_matches(again, run(phone.current_hash(Connection())))


# The heads-up: a push and a bell item.

def test_heads_up_note_reads_like_the_owner_asked():
    from app import interview_worker
    from app.interview_types import SlotEvent, slot_workflow_id
    now = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
    event = SlotEvent(key="ev-1", title="Nextiva: technical interview", start_utc=(now + timedelta(minutes=30)).isoformat(),
                      end_utc=(now + timedelta(minutes=75)).isoformat(), company="Nextiva")
    note = interview_worker.heads_up_note("briefing-x", [event], now)
    assert note["title"] == "Nextiva interview in 30 min" and note["body"] == "Tap for your briefing call."
    assert note["link"] == f"/valr/interviews/{slot_workflow_id('ev-1')}?call=ready" and note["kind"] == "briefing"
    assert notifications.importance("briefing") == "immediate"
    two = interview_worker.heads_up_note("briefing-x", [event, event], now)
    assert two["title"] == "Nextiva interview in 30 min (+1 more)"


def test_notify_now_keeps_it_in_the_bell_without_a_phone(monkeypatch):
    from tests.test_notifications import MemoryStore, PushSender
    memory, sender = MemoryStore(), PushSender(devices=0)

    class Store(MemoryStore):
        def __init__(self, connection):
            self.__dict__ = memory.__dict__

    async def get_sender(connection):
        return sender
    from app import webpush
    monkeypatch.setattr(notifications, "Store", Store)
    monkeypatch.setattr(webpush, "sender", get_sender)
    note = {"source_id": "heads_up:b1", "kind": "briefing", "title": "Nextiva interview in 30 min",
            "body": "Tap for your briefing call.", "link": "/valr/interviews/slot-x?call=ready"}
    assert run(notifications.notify_now(None, dict(note))) == "no_device"
    assert memory.rows[0]["push_state"] == "skipped" and not memory.rows[0]["read_at"]
    sender.devices = 1
    assert run(notifications.notify_now(None, dict(note))) == "duplicate"  # Once per block.
    assert run(notifications.notify_now(None, {**note, "source_id": "heads_up:b2"})) == "sent"
    assert sender.sent[0]["title"] == "Nextiva interview in 30 min" and sender.sent[0]["urgent"]
