"""The Home Screen app and phone push: the manifest and icons, the service worker's headers, the
owner-only push API with CSRF, the sender (pywebpush mocked: payload, no PII, 410 deletes),
the device list (remove one, test one) and the push pass. All names and addresses are fictional."""
import asyncio
import json
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import notifications as n
from app import webpush
from tests.test_notifications import MemoryStore, ist

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
APPLE = "https://web.push.apple.com/QGx-fictional-endpoint"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("VALR_NOTIFY_QUIET", "VALR_NOTIFY_QUIET_HOURS", "VALR_NOTIFY_DELIVERY", "VALR_VAPID_SUBJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")
    public, private = webpush.new_keys()
    monkeypatch.setenv("VALR_VAPID_PUBLIC", public)
    monkeypatch.setenv("VALR_VAPID_PRIVATE", private)
    monkeypatch.setenv("VALR_OWNER_EMAILS", "owner@example.com")


class Subs:
    """webpush.Store in memory."""

    def __init__(self, endpoints=(APPLE,)):
        self.rows = [{"id": i + 1, "endpoint": e, "p256dh": "BPk" + "x" * 84, "auth": "auth-secret-1",
                      "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", "created_at": NOW,
                      "last_ok_at": None, "failures": 0} for i, e in enumerate(endpoints)]

    async def setup(self):
        return None

    async def save(self, endpoint, p256dh, auth, user_agent):
        self.rows = [r for r in self.rows if r["endpoint"] != endpoint]
        self.rows.append({"id": len(self.rows) + 100, "endpoint": endpoint, "p256dh": p256dh, "auth": auth,
                          "user_agent": user_agent, "created_at": NOW, "last_ok_at": None, "failures": 0})

    async def remove(self, endpoint):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["endpoint"] != endpoint]
        return before - len(self.rows)

    async def all(self):
        return [dict(r) for r in self.rows]

    async def count(self):
        return len(self.rows)

    async def ok(self, sub_id, now):
        next(r for r in self.rows if r["id"] == sub_id)["last_ok_at"] = now

    async def failed(self, sub_id):
        next(r for r in self.rows if r["id"] == sub_id)["failures"] += 1

    async def delete(self, sub_id):
        before = len(self.rows)
        self.rows = [r for r in self.rows if r["id"] != sub_id]
        return before - len(self.rows)


class Post:
    """Stands in for pywebpush.webpush: records each call, answers with `status` per endpoint."""

    def __init__(self, status=None):
        self.calls, self.status = [], status or {}

    def __call__(self, **kwargs):
        from pywebpush import WebPushException
        self.calls.append(kwargs)
        kwargs["vapid_claims"]["aud"] = "mutated"  # Like the real one: the next call must get fresh claims.
        code = self.status.get(kwargs["subscription_info"]["endpoint"])
        if code:
            response = type("Response", (), {"status_code": code, "text": ""})()
            raise WebPushException(f"Push failed: {code}", response=response)
        return type("Response", (), {"status_code": 201})()


def sender(subs=None, post=None):
    return webpush.Sender(subs or Subs(), webpush.Vapid.from_env(), post or Post())


# The installable app.

def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_is_valid_json_with_the_required_fields_and_real_icons():
    pytest.importorskip("fastapi")  # The worker image has no FastAPI.
    from app import webpush_web
    manifest = json.loads(json.dumps(webpush_web.MANIFEST))
    assert manifest["name"] == "VALR" and manifest["short_name"] == "VALR"
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/valr" and manifest["scope"] == "/"
    assert manifest["theme_color"].startswith("#") and manifest["background_color"].startswith("#")
    assert manifest["theme_color"] in (ROOT / "static/valr-ds.css").read_text()  # From the design tokens.
    sizes = {(i["sizes"], i["purpose"]) for i in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for item in manifest["icons"]:
        width, height = png_size(ROOT / item["src"].lstrip("/"))
        assert f"{width}x{height}" == item["sizes"]
    assert png_size(ROOT / "static/icons/apple-touch-icon.png") == (180, 180)


def test_vapid_keys_command_prints_env_lines_that_pywebpush_accepts(capsys):
    from py_vapid import Vapid
    assert webpush.main(["webpush", "keys"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines() if not line.startswith("#"))
    assert set(out) == {"VALR_VAPID_PUBLIC", "VALR_VAPID_PRIVATE", "VALR_VAPID_SUBJECT"}
    assert out["VALR_VAPID_SUBJECT"] == "mailto:owner@example.com"
    key = Vapid.from_string(private_key=out["VALR_VAPID_PRIVATE"])
    from cryptography.hazmat.primitives import serialization
    public = key.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert webpush.b64url(public) == out["VALR_VAPID_PUBLIC"] and len(public) == 65
    assert webpush.main(["webpush"]) == 2


def test_only_real_push_services_are_accepted():
    assert webpush.valid_endpoint(APPLE) and webpush.valid_endpoint("https://fcm.googleapis.com/fcm/send/x")
    assert webpush.valid_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    for bad in ("http://web.push.apple.com/x", "https://evil.example/web.push.apple.com", "https://push.apple.com.evil.io/x",
                "https://169.254.169.254/latest", ""):
        assert not webpush.valid_endpoint(bad), bad


# Sending.

def test_sender_payload_headers_and_fresh_claims():
    post = Post()
    subs = Subs([APPLE, "https://fcm.googleapis.com/fcm/send/abc"])
    result = run(sender(subs, post).send(webpush.payload("Action needed · Acme", "Acme sent you an offer.",
                                                         "/valr/leads?lead=l1", "valr-1", 3, urgent=True), NOW))
    assert result.delivered == 2 and len(post.calls) == 2
    call = post.calls[0]
    assert call["ttl"] == webpush.TTL_S and call["headers"] == {"Urgency": "high"}
    assert call["vapid_private_key"] == webpush.Vapid.from_env().private
    assert post.calls[1]["vapid_claims"] == {"sub": "mailto:owner@example.com", "aud": "mutated"}  # Fresh dict each time.
    data = json.loads(call["data"])
    assert data == {"title": "Action needed · Acme", "body": "Acme sent you an offer.", "url": "/valr/leads?lead=l1",
                    "tag": "valr-1", "urgent": True, "badge": 3}
    assert all(r["last_ok_at"] == NOW for r in subs.rows)


def test_410_deletes_the_subscription_and_500_counts_a_failure():
    gone, broken = APPLE, "https://fcm.googleapis.com/fcm/send/broken"
    subs = Subs([gone, broken, "https://fcm.googleapis.com/fcm/send/fine"])
    result = run(sender(subs, Post({gone: 410, broken: 500})).send(webpush.payload("x", "y"), NOW))
    assert (result.delivered, result.gone, result.failed) == (1, 1, 1)
    assert [r["endpoint"] for r in subs.rows] == [broken, "https://fcm.googleapis.com/fcm/send/fine"]
    assert subs.rows[0]["failures"] == 1
    subs = Subs([APPLE])
    run(sender(subs, Post({APPLE: 404})).send(webpush.payload("x", "y"), NOW))
    assert subs.rows == []


def test_push_text_carries_no_pay_phones_or_emails():
    note = {"kind": "reply_needed", "importance": "immediate", "company": "Acme",
            "action": "Confirm the ₹45 LPA CTC and call +91 98765 43210 or mail riya@acme.example",
            "thread_id": "t1", "link": "/valr/leads?lead=l1", "source_id": "gmail:1"}
    message = n.push_message(note, 4)
    text = message["title"] + " " + message["body"]
    for leak in ("45", "LPA", "98765", "riya@", "acme.example", "+91"):
        assert leak not in text, leak
    assert message["title"] == "Action needed · Acme" and message["urgent"] is True and message["badge"] == 4
    assert len(message["body"]) <= webpush.BODY_MAX and message["url"] == "/valr/leads?lead=l1"
    plain = n.push_message({"kind": "rejection", "importance": "digest", "company": "Tessel", "thread_id": "t9"})
    assert plain["title"] == "Not moving forward · Tessel" and plain["urgent"] is False and "badge" not in plain
    assert plain["tag"] == n.push_message({"kind": "reply", "importance": "digest", "thread_id": "t9"})["tag"]
    assert webpush.payload("x", "y", "https://evil.example/")["url"] == "/valr"


# The pass in the worker, and the delivery switch.

def note(store, source, kind="offer", at=NOW, **extra):
    return run(n.add(store, {"source_id": source, "kind": kind, "title": "x", "company": "Arcworks", **extra}, at))


def test_each_immediate_item_is_pushed_once_digest_kinds_wait_for_the_digest():
    store, post = MemoryStore(), Post()
    morning = ist(8, 30)  # Before the 09:00 digest.
    note(store, "gmail:a", "offer", morning)
    note(store, "gmail:b", "reply", morning, thread_id="t2")
    result = run(n.dispatch(store, morning, sender(post=post)))
    assert result["pushed"] == 1 and len(post.calls) == 1  # The reply waits for the 09:00 digest.
    assert json.loads(post.calls[0]["data"])["title"] == "Action needed · Arcworks"
    assert json.loads(post.calls[0]["data"])["badge"] == 2
    assert run(n.dispatch(store, morning + timedelta(minutes=1), sender(post=post)))["pushed"] == 0
    assert run(n.dispatch(store, ist(9, 5), sender(post=post)))["digest"] == 1  # The reply, in the 09:00 push.
    assert json.loads(post.calls[-1]["data"])["title"] == "VALR · 1 update"


def test_one_device_can_be_tested_alone():
    subs, post = Subs([APPLE, "https://fcm.googleapis.com/fcm/send/fictional"]), Post()
    result = run(sender(subs, post).send(webpush.payload("t", "b"), NOW, only=2))
    assert result.delivered == 1 and [c["subscription_info"]["endpoint"] for c in post.calls] == [
        "https://fcm.googleapis.com/fcm/send/fictional"]


def test_device_view_never_carries_the_endpoint_or_keys():
    view = webpush.device_view(Subs().rows[0])
    assert view["label"] == "iPhone" and view["key"] == webpush.endpoint_key(APPLE) and len(view["key"]) == 16
    assert APPLE not in json.dumps(view) and "auth" not in view and "p256dh" not in view


def test_push_respects_quiet_hours_reads_and_old_rows():
    store, post = MemoryStore(), Post()
    night = ist(2)
    note(store, "gmail:a", "offer", night)
    note(store, "gmail:b", "interview", night, interview_at=night + timedelta(hours=6), thread_id="t2")
    note(store, "gmail:c", "reply", night, thread_id="t3")
    store.by_source("gmail:c")["read_at"] = night  # Already seen in the app.
    store.rows.append({**store.by_source("gmail:a"), "id": 99, "source_id": "old", "push_state": None})  # Pre-push row.
    result = run(n.dispatch(store, night, sender(post=post)))
    assert result == {"pushed": 1, "held": 1, "digest": 0}
    assert store.by_source("gmail:c")["push_state"] == "skipped" and store.by_source("old")["push_state"] is None
    assert run(n.dispatch(store, ist(8, 1), sender(post=post)))["pushed"] == 1
    assert len(post.calls) == 2


def test_without_vapid_keys_nothing_is_pushed_and_items_stay_in_the_bell(monkeypatch):
    monkeypatch.delenv("VALR_VAPID_PRIVATE")
    assert webpush.Vapid.from_env() is None and webpush.public_key() == ""
    store = MemoryStore()
    note(store, "gmail:a", "offer")
    result = run(n.dispatch(store, NOW, None))
    assert result["pushed"] == 0 and store.rows[0]["push_state"] == "skipped" and not store.rows[0]["read_at"]


def test_an_upgrade_in_the_same_thread_is_pushed_again():
    store, post = MemoryStore(), Post()
    morning = ist(8, 30)
    note(store, "gmail:a", "reply", morning, thread_id="t1")
    run(n.dispatch(store, morning, sender(post=post)))
    assert post.calls == []  # A reply waits for the digest...
    note(store, "gmail:b", "offer", morning + timedelta(minutes=2), thread_id="t1")
    run(n.dispatch(store, morning + timedelta(minutes=2), sender(post=post)))
    tags = [json.loads(c["data"])["tag"] for c in post.calls]  # ...an offer in the same thread is pushed now,
    assert tags == [webpush.tag_for("t1")] and len(store.rows) == 1  # once, tagged by the thread.


# The HTTP side.

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app import research, valr_dashboard, valr_view
    from app.main import app
    from tests.test_notetakers import GOOD, OWNER, Pool, Verifier
    from tests.valr_sample import sample_view

    async def collect(pool, now=None):
        return sample_view()

    subs, memory, post = Subs([]), MemoryStore(), Post()

    class Store(MemoryStore):
        def __init__(self, connection):
            self.__dict__ = memory.__dict__

        async def listing(self, unread_only, since, limit=100):
            return []

    class PushStore(Subs):
        def __init__(self, connection):
            self.__dict__ = subs.__dict__

    real_sender = webpush.Sender

    def mocked_sender(store, vapid, post_=None):
        return real_sender(store, vapid, post)

    monkeypatch.setattr(n, "Store", Store)
    monkeypatch.setattr(webpush, "Store", PushStore)
    monkeypatch.setattr(webpush, "Sender", mocked_sender)
    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")
    test_client = TestClient(app)
    test_client.good, test_client.subs, test_client.pushes, test_client.memory = GOOD, subs, post, memory
    return test_client


def csrf_headers(client):
    page = client.get("/valr", headers=client.good).text
    token = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    return {**client.good, "Content-Type": "application/json", "X-CSRF-Token": token}


SUB = {"endpoint": APPLE, "expirationTime": None, "keys": {"p256dh": "BPk" + "y" * 84, "auth": "auth-secret-2"}}


def test_manifest_and_service_worker_are_public_with_the_right_headers(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["start_url"] == "/valr"
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
    assert sw.headers["content-type"].startswith("text/javascript") and sw.headers["cache-control"] == "no-cache"
    assert "showNotification" in sw.text and "notificationclick" in sw.text
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200
    assert client.get("/static/offline.html").status_code == 200
    ico = client.get("/favicon.ico")  # Public, with no cookie: the Signal V at 16, 32 and 48 px.
    assert ico.status_code == 200 and ico.headers["content-type"] == "image/x-icon" and ico.content[:4] == b"\x00\x00\x01\x00"


def test_subscribe_unsubscribe_and_test_are_owner_only_with_csrf(client):
    body = json.dumps(SUB)
    for path in ("/valr/api/push/subscribe", "/valr/api/push/unsubscribe", "/valr/api/push/test", "/valr/api/push/remove"):
        assert client.post(path, content=body).status_code == 403  # No login.
        assert client.post(path, content=body, headers={"Cf-Access-Jwt-Assertion": "guest",
                                                        "Content-Type": "application/json"}).status_code == 403
        no_csrf = client.post(path, content=body, headers={**client.good, "Content-Type": "application/json"})
        assert no_csrf.status_code == 403, path
        cross = client.post(path, content=body, headers={**csrf_headers(client), "Origin": "https://evil.example"})
        assert cross.status_code == 403, path
    assert client.get("/valr/api/push").status_code == 403
    assert client.post("/valr/api/push/test", content=b"{}", headers=csrf_headers(client)).status_code == 409
    done = client.post("/valr/api/push/subscribe", content=body, headers=csrf_headers(client)).json()
    assert done["devices"] == 1 and done["configured"] is True and client.subs.rows[0]["endpoint"] == APPLE
    bad = client.post("/valr/api/push/subscribe", content=json.dumps({**SUB, "endpoint": "https://evil.example/x"}),
                      headers=csrf_headers(client))
    assert bad.status_code == 400
    sent = client.post("/valr/api/push/test", content=b"{}", headers=csrf_headers(client)).json()
    assert sent["delivered"] == 1 and json.loads(client.pushes.calls[0]["data"])["title"] == "VALR test notification ✅"
    status = client.get("/valr/api/push", headers=client.good).json()
    assert status["devices"] == 1 and status["public_key"] == webpush.public_key() and "delivery" not in status
    assert len(status["device_list"]) == 1 and APPLE not in json.dumps(status)
    off = client.post("/valr/api/push/unsubscribe", content=json.dumps({"endpoint": APPLE}), headers=csrf_headers(client))
    assert off.json()["devices"] == 0


def test_a_lost_device_is_removed_by_id(client):
    client.subs.rows = Subs([APPLE, "https://fcm.googleapis.com/fcm/send/fictional"]).rows
    gone = client.post("/valr/api/push/remove", content=b'{"id": 1}', headers=csrf_headers(client))
    assert gone.status_code == 200 and gone.json()["devices"] == 1
    assert [r["id"] for r in client.subs.rows] == [2]
    again = client.post("/valr/api/push/remove", content=b'{"id": 1}', headers=csrf_headers(client))
    assert again.status_code == 404
    assert client.post("/valr/api/push/remove", content=b'{"id": "x"}', headers=csrf_headers(client)).status_code == 400
    one = client.post("/valr/api/push/test", content=b'{"id": 2}', headers=csrf_headers(client)).json()
    assert one["delivered"] == 1


def test_alert_times_are_saved_next_to_the_switches(client):
    saved = client.post("/valr/api/notifications/settings", content=b'{"quiet_start": "22:00", "digest_at": "18:00"}',
                        headers=csrf_headers(client))
    assert saved.json()["switches"]["quiet_start"] == "22:00" and client.memory.settings["digest_at"] == "18:00"
    for bad in (b'{"digest_at": "25:00"}', b'{"delivery": "push"}', b'{"quiet_start": "07:00", "quiet_end": "07:00"}'):
        assert client.post("/valr/api/notifications/settings", content=bad, headers=csrf_headers(client)).status_code == 400
    assert client.get("/valr/api/notifications", headers=client.good).json()["switches"]["digest_at"] == "18:00"


def test_a_sample_digest_needs_a_device(client):
    assert client.post("/valr/api/notifications/test", content=b'{"kind": "digest"}',
                       headers=csrf_headers(client)).status_code == 409
    client.subs.rows = Subs().rows
    sent = client.post("/valr/api/notifications/test", content=b'{"kind": "digest"}', headers=csrf_headers(client))
    assert sent.status_code == 200 and json.loads(client.pushes.calls[-1]["data"])["tag"] == "valr-digest-sample"


def test_pages_carry_the_install_tags_and_settings_has_the_card(client):
    page = client.get("/valr", headers=client.good).text
    for tag in ('rel="manifest" href="/manifest.webmanifest"', 'name="apple-mobile-web-app-capable" content="yes"',
                'name="apple-mobile-web-app-status-bar-style"', 'name="apple-mobile-web-app-title" content="VALR"',
                'rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"', 'name="theme-color"', "valr-pwa.js"):
        assert tag in page, tag
    settings = client.get("/valr/settings", headers=client.good).text
    assert 'id="phone-push"' in settings and "Phone notifications" in settings and "Add to Home Screen" in settings
    assert "data-pwa-delivery" not in settings and "Enable notifications" in settings
    assert "data-pwa-device-list" in settings and "Where alerts go" not in settings
