"""Notetakers: providers against fake HTTP, webhook signatures, matching, key moments, the
evaluation hand-off, and the dashboard routes. No network; every person and key is made up."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import notetakers
from app.notetakers import base, fathom, fireflies, granola, pipeline, store
from app.notetakers import signatures as routes
from app.notetakers.base import KeyRefused, Recording

OWNER = "owner@example.com"
GOOD = {"Cf-Access-Jwt-Assertion": "owner"}
FAKE_KEY = "fake-key-0123456789"


def run(coro):
    return asyncio.run(coro)


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


FATHOM_ITEM = {
    "recording_id": 4242, "title": "Orbit <> Candidate", "meeting_title": "Orbit Labs: technical round",
    "url": "https://fathom.video/calls/4242", "share_url": "https://fathom.video/share/tokABC123",
    "recording_start_time": "2026-10-16T09:00:00Z", "recording_end_time": "2026-10-16T09:58:00Z",
    "calendar_invitees": [{"name": "Ana Ruiz", "email": "ana.ruiz@orbit.example", "is_external": True},
                          {"name": "Me", "email": OWNER, "is_external": False}],
    "transcript": [{"speaker": {"display_name": "Ana Ruiz"}, "text": "How would you shard a busy Postgres table?",
                    "timestamp": "00:01:35"},
                   {"speaker": {"display_name": "Sam"}, "text": "By tenant, then by time.", "timestamp": "00:01:50"}],
    "default_summary": {"markdown_formatted": "## Notes\n- [Sharding question](https://fathom.video/share/tokABC123?timestamp=95)"},
}


# ---------- Fathom ----------

def test_fathom_lists_with_api_key_header_and_pages_by_cursor():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("cursor") == "c2":
            return httpx.Response(200, json={"items": [dict(FATHOM_ITEM, recording_id=4243)], "next_cursor": None})
        return httpx.Response(200, json={"items": [FATHOM_ITEM], "next_cursor": "c2"})

    async def go():
        async with client_for(handler) as http:
            return await fathom.list_recordings(FAKE_KEY, datetime(2026, 10, 15, tzinfo=timezone.utc),
                                                datetime(2026, 10, 17, tzinfo=timezone.utc), http)
    items = run(go())
    assert [r.external_id for r in items] == ["4242", "4243"]
    assert seen[0].headers["x-api-key"] == FAKE_KEY and seen[0].url.host == "api.fathom.ai"
    assert seen[0].url.params["created_after"] == "2026-10-15T00:00:00Z"
    first = items[0]
    assert first.share_url == "https://fathom.video/calls/4242"
    assert first.embed_url == ""  # The undocumented iframe is gone; the video plays from the download API.
    assert first.company == "Orbit Labs" and first.company_domain == "orbit.example"  # The title's spelling.
    assert first.interviewers == ["Ana Ruiz"] and first.invitee_hashes == [base.email_hash("ana.ruiz@orbit.example")]
    assert "ana.ruiz@orbit.example" not in json.dumps(first.public())  # Never an address stored.
    assert "ana.ruiz@orbit.example" in first.attendee_emails
    assert "email" not in json.dumps(first.public()) and "@" not in json.dumps(first.public())


def test_fathom_refused_key_and_one_recording_normalised():
    def refuse(request):
        return httpx.Response(401, json={})

    async def refused():
        async with client_for(refuse) as http:
            await fathom.verify(FAKE_KEY, http)
    with pytest.raises(KeyRefused) as error:
        run(refused())
    assert FAKE_KEY not in str(error.value)

    def handler(request):
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, json={"transcript": FATHOM_ITEM["transcript"]})
        return httpx.Response(200, json={"summary": FATHOM_ITEM["default_summary"]})

    async def one():
        async with client_for(handler) as http:
            return await fathom.get_recording(FAKE_KEY, "4242", http, listed=fathom.recording(FATHOM_ITEM))
    rec = run(one())
    assert rec.meeting["source"] == "fathom" and rec.meeting["turns"][0]["start"] == 95.0
    assert "Sharding question" in rec.meeting["vendor_summary"]
    assert all("@" not in p for p in rec.meeting["participants"])


def test_fathom_timestamp_links_and_no_embed():
    assert not hasattr(fathom, "embed_url")
    assert fathom.timestamp_url("https://fathom.video/calls/1", 95) == "https://fathom.video/calls/1?timestamp=95"


# ---------- Fireflies ----------

def test_fireflies_graphql_bearer_list_and_transcript():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append((request.headers["authorization"], body))
        if "transcripts(" in body["query"]:
            return httpx.Response(200, json={"data": {"transcripts": [{
                "id": "ff1", "title": "Kite: technical round", "date": 1760605200000, "duration": 45,
                "transcript_url": "https://app.fireflies.ai/view/ff1",
                "meeting_attendees": [{"displayName": "Lee Park", "email": "lee@kite.example"}]}]}})
        return httpx.Response(200, json={"data": {"transcript": {
            "id": "ff1", "title": "Kite: technical round", "date": 1760605200000, "duration": 45,
            "transcript_url": "https://app.fireflies.ai/view/ff1", "speakers": [{"name": "Lee Park"}],
            "summary": {"overview": "A coding round."},
            "sentences": [{"speaker_name": "Lee Park", "text": "Can you walk me through the complexity here?",
                           "start_time": 130.5}]}}})

    async def go():
        async with client_for(handler) as http:
            listed = await fireflies.list_recordings(FAKE_KEY, datetime(2026, 10, 1, tzinfo=timezone.utc),
                                                     datetime(2026, 10, 20, tzinfo=timezone.utc), http)
            one = await fireflies.get_recording(FAKE_KEY, "ff1", http)
            return listed, one
    listed, one = run(go())
    assert bodies[0][0] == f"Bearer {FAKE_KEY}" and bodies[0][1]["variables"]["limit"] == 50
    assert listed[0].start == "2025-10-16T09:00:00Z" and listed[0].end == "2025-10-16T09:45:00Z"
    assert listed[0].embed_url == "" and listed[0].share_url == "https://app.fireflies.ai/view/ff1"
    assert one.meeting["turns"][0]["start"] == 130.5 and one.meeting["vendor_summary"] == "A coding round."
    assert fireflies.timestamp_url(one.share_url, 130) == ""  # Not documented by Fireflies.


def test_fireflies_graphql_auth_error_is_a_refused_key():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"extensions": {"code": "auth_failed"}}]})

    async def go():
        async with client_for(handler) as http:
            await fireflies.verify(FAKE_KEY, http)
    with pytest.raises(KeyRefused):
        run(go())


# ---------- Granola ----------

GRANOLA_NOTE = {
    "id": "not_abcdefghijklmn", "title": "Vela: system design", "web_url": "https://notes.granola.ai/d/abc",
    "calendar_event": {"event_title": "Vela: system design", "scheduled_start_time": "2026-10-16T11:00:00Z",
                       "scheduled_end_time": "2026-10-16T12:00:00Z", "invitees": [{"email": "kim@vela.example"}]},
    "attendees": [{"name": "Kim Lee", "email": "kim@vela.example"}],
    "summary_markdown": "Design a URL shortener.",
    "transcript": [{"speaker": {"source": "speaker", "attribution": "them"}, "text": "How would you scale reads?",
                    "start_time": "2026-10-16T11:02:00Z", "end_time": "2026-10-16T11:02:05Z"},
                   {"speaker": {"source": "microphone", "attribution": "me"}, "text": "With a cache.",
                    "start_time": "2026-10-16T11:02:10Z", "end_time": "2026-10-16T11:02:12Z"}],
}


def test_granola_rest_list_and_413_transcript_fallback():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        if request.url.path == "/v1/notes":
            return httpx.Response(200, json={"notes": [{"id": GRANOLA_NOTE["id"], "title": "Vela"}],
                                             "hasMore": False, "cursor": None})
        if request.url.path.endswith("/transcript"):
            return httpx.Response(200, json={"transcript": GRANOLA_NOTE["transcript"], "hasMore": False})
        if request.url.params.get("include") == "transcript":
            return httpx.Response(413, json={"code": "TRANSCRIPT_TOO_LARGE"})
        return httpx.Response(200, json={k: v for k, v in GRANOLA_NOTE.items() if k != "transcript"})

    async def go():
        async with client_for(handler) as http:
            listed = await granola.list_recordings(FAKE_KEY, datetime(2026, 10, 1, tzinfo=timezone.utc),
                                                   datetime(2026, 10, 20, tzinfo=timezone.utc), http)
            return listed, await granola.get_recording(FAKE_KEY, GRANOLA_NOTE["id"], http)
    listed, one = run(go())
    assert calls[0].url.host == "public-api.granola.ai" and listed[0].external_id == GRANOLA_NOTE["id"]
    assert one.embed_url == "" and one.share_url == "https://notes.granola.ai/d/abc"
    assert [t["start"] for t in one.meeting["turns"]] == [0.0, 10.0]
    assert one.meeting["turns"][1]["speaker"] == "Me" and one.attendee_emails == ["kim@vela.example"]


def test_granola_exports_csv_json_and_markdown():
    csv_text = ('title,created_at,summary,transcript\n'
                '"Tessel screen",2026-10-10T10:00:00Z,"Intro call","Them: Why Tessel?\nMe: The product."\n')
    from_csv = granola.parse_export(csv_text, "granola.csv")
    assert from_csv[0].title == "Tessel screen" and from_csv[0].meeting["turns"][0]["text"] == "Why Tessel?"
    assert from_csv[0].external_id.startswith("export-")
    from_json = granola.parse_export(json.dumps({"notes": [GRANOLA_NOTE]}))
    assert from_json[0].external_id == GRANOLA_NOTE["id"] and from_json[0].meeting["turns"]
    md = granola.parse_export("# Lumen screen\nGood chat.\n## Transcript\nThem: Tell me about you?\nMe: Sure.", "n.md")
    assert md[0].title == "Lumen screen" and len(md[0].meeting["turns"]) == 2
    with pytest.raises(ValueError):
        granola.parse_export("{not json")


# ---------- Signatures ----------

SECRET_BYTES = b"fake-webhook-secret-bytes"
WHSEC = "whsec_" + base64.b64encode(SECRET_BYTES).decode()


def standard_headers(body: bytes, stamp: int | None = None, secret: bytes = SECRET_BYTES) -> dict:
    stamp = int(time.time()) if stamp is None else stamp
    sig = base64.b64encode(hmac.new(secret, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": "msg_1", "webhook-timestamp": str(stamp), "webhook-signature": f"v0,zzz v1,{sig}"}


def test_standard_webhook_signature():
    body = b'{"recording_id": 1}'
    assert routes.verify_standard_webhook(WHSEC, standard_headers(body), body)
    assert not routes.verify_standard_webhook(WHSEC, standard_headers(body), body + b" ")
    assert not routes.verify_standard_webhook(WHSEC, standard_headers(body, int(time.time()) - 400), body)
    assert not routes.verify_standard_webhook(WHSEC, standard_headers(body, secret=b"other"), body)
    headers = standard_headers(body)
    headers["webhook-signature"] = headers["webhook-signature"].replace("v1,", "v2,")
    assert not routes.verify_standard_webhook(WHSEC, headers, body)
    assert not routes.verify_standard_webhook("", standard_headers(body), body)


# ---------- Matching and moments ----------

def test_match_by_time_window_and_attendees():
    start, end = "2026-10-16T09:00:00+00:00", "2026-10-16T10:00:00+00:00"
    same_time_other_people = Recording("fathom", "a", "Standup", "2026-10-16T09:00:00Z", "2026-10-16T09:30:00Z",
                                       attendee_emails=["x@other.example"])
    right = Recording("fathom", "b", "Orbit Labs round", "2026-10-16T09:03:00Z", "2026-10-16T09:58:00Z",
                      attendee_emails=["ana.ruiz@orbit.example"])
    later = Recording("fathom", "c", "Orbit follow-up", "2026-10-16T13:00:00Z", "2026-10-16T13:30:00Z",
                      attendee_emails=["ana.ruiz@orbit.example"])
    assert base.match_score(later, start, end, {"ana.ruiz@orbit.example"}) == 0
    assert base.best_match([same_time_other_people, right, later], start, end, {"ana.ruiz@orbit.example"},
                           "Orbit Labs: technical round") is right
    assert base.best_match([later], start, end, set()) is None


def test_key_moments_from_summary_links_then_questions():
    from_summary = base.moments_from_summary(FATHOM_ITEM["default_summary"]["markdown_formatted"])
    assert from_summary == [{"label": "Sharding question", "seconds": 95,
                             "url": "https://fathom.video/share/tokABC123?timestamp=95"}]
    turns = [{"speaker": "Ana", "text": "How would you shard a busy Postgres table?", "start": 95},
             {"speaker": "Sam", "text": "Should I assume one region for this?", "start": 120},
             {"speaker": "Ana", "text": "What happens when a node fails during writes?", "start": 125},
             {"speaker": "Ana", "text": "And the backfill strategy for a lost day?", "start": 400}]
    moments = base.moments_from_turns(turns, {"Sam"})
    assert [m["seconds"] for m in moments] == [95, 400]  # The owner's own question is skipped; 60 s apart.
    assert base.clock(95) == "1:35" and base.clock(3725) == "1:02:05"


def test_only_https_links():
    assert not hasattr(base, "safe_embed")
    assert base.safe_link("javascript:alert(1)") == ""


def test_invitee_details_company_people_and_no_addresses():
    invitees = [{"name": "Maya Lin", "email": "maya@talent.quillworks.example", "is_external": True},
                {"name": "Dev Kapoor", "email": "dev@quillworks.example", "is_external": True},
                {"name": "Pat Friend", "email": "pat@gmail.com", "is_external": True},
                {"name": "Owner", "email": OWNER, "is_external": False},
                {"name": "Notetaker", "email": "bot@fathom.video", "is_external": True}]
    details = base.invitee_details(invitees, {OWNER})
    assert details["company"] == "Quillworks" and details["company_domain"] in ("quillworks.example", "talent.quillworks.example")
    assert details["interviewers"] == ["Maya Lin", "Dev Kapoor", "Pat Friend"]
    assert len(details["invitee_hashes"]) == 3 and "@" not in "".join(details["invitee_hashes"])
    only_freemail = base.invitee_details([{"name": "Pat", "email": "pat@gmail.com", "is_external": True}], {OWNER})
    assert only_freemail["company"] == "" and only_freemail["interviewers"] == ["Pat"]


@pytest.mark.parametrize("name, ok", [("L2- Sam", False), ("Round 2", False), ("Interview", False),
                                      ("r3", False), ("Sam Rivera", False), ("Quillworks", True),
                                      ("Orbit Labs", True), ("", False)])
def test_round_labels_and_the_owner_are_never_a_company(name, ok):
    assert base.is_company(name, {"sam", "rivera"}) is ok


# ---------- The evaluation hand-off ----------

class FakeStore:
    """In-memory stand-in for app/notetakers/store.py (same function names)."""

    def __init__(self):
        self.keys, self.recordings, self.events, self.media = {}, {}, {}, {}
        self.gone = {}
        self.clock = lambda: datetime.now(timezone.utc)

    def install(self, monkeypatch):
        async def setup(c):
            return None

        async def save_key(c, token_key, provider, api_key, account):
            from app import google_oauth
            self.keys[provider] = {"secret": google_oauth.encrypt(token_key, api_key), "account": account,
                                   "state": "connected"}

        async def delete_key(c, provider):
            return self.keys.pop(provider, None) is not None

        async def load_key(c, token_key, provider):
            from app import google_oauth
            row = self.keys.get(provider)
            return google_oauth.decrypt(token_key, row["secret"]) if row else None

        async def statuses(c):
            return {p: {"state": r["state"], "account": r["account"], "connected_at": None, "last_ok_at": None,
                        "last_error": None} for p, r in self.keys.items()}

        async def mark(c, provider, ok, error=""):
            return None

        async def upsert_recording(c, rec, origin, sealed=None):
            key = (rec["provider"], rec["external_id"])
            new = key not in self.recordings
            row = self.recordings.setdefault(key, {"eval_status": "pending"})
            row.update(rec, origin=origin)
            row["sealed"] = None if row["eval_status"] == "handed_off" else sealed or row.get("sealed")
            return new

        async def set_eval(c, provider, external_id, status, detail=""):
            self.recordings[(provider, external_id)]["eval_status"] = status

        async def set_event(c, provider, external_id, event_key, title="", company="", start=None):
            self.events[event_key] = (provider, external_id)
            self.recordings[(provider, external_id)].update(event_key=event_key, event_title=title,
                                                            event_company=company, event_start=start)

        async def recording(c, provider, external_id):
            row = self.recordings.get((provider, external_id))
            return None if row is None else {"provider": provider, "external_id": external_id,
                                             "title": row.get("title", ""), "started_at": None, "ended_at": None,
                                             "share_url": row.get("share_url", ""),
                                             "embed_url": row.get("embed_url", ""),
                                             "company": row.get("company", ""),
                                             "interviewers": json.dumps(row.get("interviewers") or [])}

        async def clear_sealed(c, provider, external_id):
            self.recordings[(provider, external_id)]["sealed"] = None

        async def many(c, keys):
            return {k: {**self.recordings[k], "provider": k[0], "external_id": k[1]}
                    for k in keys if k in self.recordings}

        async def set_details(c, rec):
            row = self.recordings.get((rec["provider"], rec["external_id"]))
            if row is None:
                return False
            row.update({k: rec[k] for k in ("company", "company_domain", "interviewers", "invitee_hashes")})
            return True

        async def get_media(c, provider, external_id):
            return self.media.get((provider, external_id))

        async def save_media(c, provider, external_id, media):
            self.media[(provider, external_id)] = {**{k: None for k in store.MEDIA_FIELDS}, **media,
                                                   "updated_at": self.clock()}

        async def between(c, start, end, limit=200):
            def when(r):
                value = r.get("start") or r.get("started_at")
                return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
            return [{"provider": p, "external_id": x, "title": r.get("title", ""), "started_at": when(r),
                     "ended_at": None, "share_url": r.get("share_url", ""), "embed_url": "",
                     "event_key": r.get("event_key"), "eval_status": r.get("eval_status", "pending"),
                     "event_title": r.get("event_title"), "event_company": r.get("event_company"),
                     "event_start": r.get("event_start"), "received_at": None, "company": r.get("company", ""),
                     "interviewers": json.dumps(r.get("interviewers") or [])}
                    for (p, x), r in self.recordings.items()]

        async def hide(c, provider, external_id, how="hidden"):
            self.gone[(provider, external_id)] = how

        async def hidden(c):
            return set(self.gone)

        async def delete_recording(c, provider, external_id):
            self.recordings.pop((provider, external_id), None)
            self.media.pop((provider, external_id), None)

        for name, fn in locals().items():
            if name != "self" and callable(fn):
                monkeypatch.setattr(store, name, fn)
        return self


def test_new_recording_is_handed_to_the_hook_once(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    from cryptography.fernet import Fernet
    monkeypatch.setenv("VALR_TOKEN_KEY", Fernet.generate_key().decode())
    handed = []

    async def hook(provider, external_id, meeting):
        handed.append((provider, external_id, bool(meeting and meeting["turns"])))
        return "queued"

    async def match(recording):
        return {"key": "ev-orbit", "title": "Orbit Labs: technical round", "company": "Orbit",
                "start": "2026-10-16T09:00:00+00:00"}

    rec = fathom.payload_recording(FATHOM_ITEM)
    first = run(pipeline.intake(object(), rec, "webhook", hook=hook, match=match))
    again = run(pipeline.intake(object(), rec, "webhook", hook=hook, match=match))
    assert first == {"new": True, "event_key": "ev-orbit", "eval": "handed_off"} and again["eval"] == "known"
    assert handed == [("fathom", "4242", True)]
    row = fake.recordings[("fathom", "4242")]
    assert row["eval_status"] == "handed_off" and row["sealed"] is None  # The transcript is dropped once taken.
    assert fake.events == {"ev-orbit": ("fathom", "4242")} and row["event_company"] == "Orbit"


def test_without_a_hook_the_recording_waits_and_a_failing_hook_is_recorded(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    monkeypatch.setenv(pipeline.HOOK_ENV, "app.no_such_module:evaluate")
    assert pipeline.resolve_hook() is None
    run(pipeline.intake(object(), Recording("fireflies", "ff9", "x"), "sync"))
    assert fake.recordings[("fireflies", "ff9")]["eval_status"] == "no_hook"

    def broken(provider, external_id, meeting):
        raise RuntimeError("boom")
    run(pipeline.intake(object(), Recording("fireflies", "ff10", "x"), "sync", hook=broken))
    assert fake.recordings[("fireflies", "ff10")]["eval_status"] == "failed"


# ---------- Routes ----------

class Verifier:
    async def email(self, token):
        return {"owner": OWNER, "guest": "guest@example.com"}.get(token)


class Pool:
    def connection(self):
        class Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False
        return Context()


@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")  # The worker's venv has no web stack: route tests run in the app's.
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from app import research, valr_dashboard, valr_view
    from app.main import app
    from tests.valr_sample import sample_view

    async def collect(pool, now=None):
        return sample_view()

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")  # Cloudflare Access only (app/auth).
    monkeypatch.setenv("VALR_TOKEN_KEY", Fernet.generate_key().decode())
    for name in ("FATHOM_API_KEY", "FIREFLIES_API_KEY", "GRANOLA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    test_client = TestClient(app)
    test_client.store = FakeStore().install(monkeypatch)
    return test_client


def action_headers(client) -> dict:
    page = client.get("/valr", headers=GOOD).text
    csrf = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    return {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}


def test_connect_checks_the_key_stores_it_encrypted_and_never_echoes_it(client, monkeypatch):
    checked = []

    async def verify(key, http):
        checked.append(key)
        if key != FAKE_KEY:
            raise KeyRefused("refused")
        return "Sample Account"

    monkeypatch.setattr(fireflies, "verify", verify)
    body = json.dumps({"api_key": FAKE_KEY})
    assert client.post("/valr/api/notetakers/fireflies/connect", content=body,
                       headers={**GOOD, "Content-Type": "application/json"}).status_code == 403  # No CSRF.
    headers = action_headers(client)
    assert client.post("/valr/api/notetakers/zoom/connect", content=body, headers=headers).status_code == 404
    bad = client.post("/valr/api/notetakers/fireflies/connect", content=json.dumps({"api_key": "wrong-key-123"}),
                      headers=headers)
    assert bad.status_code == 400 and "wrong-key-123" not in bad.text and "fireflies" not in client.store.keys
    ok = client.post("/valr/api/notetakers/fireflies/connect", content=body, headers=headers)
    assert ok.status_code == 200 and FAKE_KEY not in ok.text and ok.json()["state"] == "connected"
    stored = client.store.keys["fireflies"]["secret"]
    assert FAKE_KEY not in stored  # Fernet ciphertext only.
    assert run(notetakers.api_key(object(), "fireflies")) == FAKE_KEY
    gone = client.post("/valr/api/notetakers/fireflies/disconnect", content="{}", headers=headers)
    assert gone.status_code == 200 and "fireflies" not in client.store.keys


def test_connect_refuses_without_a_token_key(client, monkeypatch):
    async def verify(key, http):
        return ""
    monkeypatch.setattr(fathom, "verify", verify)
    monkeypatch.delenv("VALR_TOKEN_KEY")
    headers = action_headers(client)
    response = client.post("/valr/api/notetakers/fathom/connect", content=json.dumps({"api_key": FAKE_KEY}),
                           headers=headers)
    assert response.status_code == 503 and FAKE_KEY not in response.text


def test_fathom_webhook_needs_a_valid_signature_and_takes_the_recording_in(client, monkeypatch):
    taken = []

    async def intake(connection, recording, origin, hook=None, match=None):
        taken.append((recording.external_id, origin, bool(recording.meeting)))
        return {"new": True, "event_key": None, "eval": "no_hook"}

    monkeypatch.setattr(pipeline, "intake", intake)
    body = json.dumps(FATHOM_ITEM).encode()
    monkeypatch.delenv("FATHOM_WEBHOOK_SECRET", raising=False)
    assert client.post("/hooks/fathom", content=body, headers=standard_headers(body)).status_code == 503
    monkeypatch.setenv("FATHOM_WEBHOOK_SECRET", WHSEC)
    assert client.post("/hooks/fathom", content=body).status_code == 401
    assert client.post("/hooks/fathom", content=body + b" ", headers=standard_headers(body)).status_code == 401
    assert taken == []
    ok = client.post("/hooks/fathom", content=body, headers=standard_headers(body))
    assert ok.status_code == 200 and taken == [("4242", "webhook", True)]


def test_granola_webhook_fetches_the_named_note_and_fireflies_has_none(client, monkeypatch):
    taken = []

    async def intake(connection, recording, origin, hook=None, match=None):
        taken.append((recording.provider, recording.external_id))
        return {"new": True, "event_key": None, "eval": "no_hook"}

    async def get_gr(key, external_id, http, listed=None):
        assert key == FAKE_KEY
        return Recording("granola", external_id, "x")

    monkeypatch.setattr(pipeline, "intake", intake)
    monkeypatch.setattr(granola, "get_recording", get_gr)
    monkeypatch.setenv("GRANOLA_API_KEY", FAKE_KEY)
    monkeypatch.setenv("GRANOLA_WEBHOOK_SECRET", WHSEC)
    gbody = json.dumps({"event_type": "note.generated", "note_id": "not_abcdefghijklmn"}).encode()
    assert client.post("/hooks/granola", content=gbody).status_code == 401
    assert client.post("/hooks/granola", content=gbody, headers=standard_headers(gbody)).status_code == 200
    assert taken == [("granola", "not_abcdefghijklmn")]
    # One-time history only: no such hook (asked as the owner, past app/auth's gate).
    assert client.post("/hooks/fireflies", content=b"{}", headers=GOOD).status_code in (404, 405)


def test_recording_endpoint_returns_player_and_moments(client, monkeypatch):
    async def recording(connection, provider, external_id):
        return {"provider": provider, "external_id": external_id, "title": "Orbit", "started_at": None,
                "ended_at": None, "share_url": "https://fathom.video/calls/4242",
                "embed_url": "https://fathom.video/embed/tokABC123", "company": "Orbit",
                "interviewers": '["Ana Ruiz"]'}

    async def get_recording(key, external_id, http, listed=None):
        listed.meeting = fathom.normalize_fathom(FATHOM_ITEM)
        return listed

    monkeypatch.setattr(store, "recording", recording)
    monkeypatch.setattr(fathom, "get_recording", get_recording)
    monkeypatch.setenv("FATHOM_API_KEY", FAKE_KEY)
    assert client.get("/valr/api/recording?id=fathom:4242").status_code == 403  # Access login needed.
    data = client.get("/valr/api/recording?id=fathom:4242", headers=GOOD).json()
    assert data["found"] and data["player"] is True and data["id"] == "fathom:4242" and "embed_url" not in data
    assert data["company"] == "Orbit" and data["interviewers"] == ["Ana Ruiz"]
    assert data["moments"][0] == {"label": "Sharding question", "seconds": 95, "clock": "1:35",
                                  "url": "https://fathom.video/share/tokABC123?timestamp=95"}
    assert FAKE_KEY not in json.dumps(data)
    assert client.get("/valr/api/recording?id=zoom:1", headers=GOOD).status_code == 404


def test_setup_checklist_items_are_links_or_buttons_that_do_the_step(client):
    page = client.get("/valr/settings", headers=GOOD).text  # The checklist lives on Settings now.
    setup = page.split('id="setup"', 1)[1].split('id="g-conn"', 1)[0]
    cards = page.split('id="notetakers"', 1)[1]
    # Done steps say so; an open step either has its button or says what needs fixing.
    assert setup.count("Done") == 4 and "Connect Telegram" in setup and "4 of 5 done" in setup
    for name in ("fathom", "fireflies", "granola"):
        assert f'data-connect="{name}"' in cards and f'id="nt-{name}"' in cards
    assert 'type="password"' in cards and 'value="' not in cards.split('type="password"', 1)[1][:200]
    assert 'data-import="granola"' in cards
    assert "Done" in setup and "Finish setting up" in setup  # Telegram isn't linked in the sample.


def test_telegram_step_and_deep_link(client, monkeypatch):
    from app import telegram_api, valr_view
    from tests.valr_sample import sample_view

    async def collect(pool, now=None):
        view = sample_view()
        view["mode"] = "telegram"
        view["connections"]["telegram"] = {"configured": True, "linked": False, "linked_at": None}
        return view

    monkeypatch.setattr(valr_view, "collect", collect)
    assert 'data-action="tglink"' in client.get("/valr/settings", headers=GOOD).text
    assert telegram_api.start_link("example_valr_bot", "123456") == "https://t.me/example_valr_bot?start=123456"
    assert telegram_api.start_link("bad name!", "123456") == ""
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@example_valr_bot")
    assert run(telegram_api.username()) == "example_valr_bot"


def test_past_interview_page_plays_or_finds_the_recording(client, monkeypatch):
    from app.valr_ui import routes

    async def stored(interview):
        if interview["id"] == "fathom:1000":
            return None
        return {"provider": "fathom", "share_url": "https://fathom.video/calls/1001", "title": "Interview"}

    monkeypatch.setattr(routes, "load_recording", stored)
    page = client.get("/valr/interviews/past/fathom-1001", headers=GOOD).text
    assert "<iframe" not in page and "fathom.video/embed" not in page
    assert 'data-player="fathom:1001"' in page and 'data-moments-id="fathom:1001"' in page
    assert '<video controls preload="metadata" playsinline' in page
    assert 'href="https://fathom.video/calls/1001"' in page and "Open in Fathom" in page  # The fallback link.
    assert "valr-player.js" in page
    missing = client.get("/valr/interviews/past/fathom-1000", headers=GOOD).text
    assert 'data-moments-id="fathom:1000" data-full' in missing  # Asks /valr/api/recording?id=.


# ---------- The real store (needs VALR_TEST_POSTGRES, a scratch database) ----------

needs_postgres = pytest.mark.skipif(not __import__("os").getenv("VALR_TEST_POSTGRES"),
                                    reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")


@needs_postgres
def test_store_round_trip_on_postgres():
    import os
    import psycopg
    from cryptography.fernet import Fernet
    token_key = Fernet.generate_key().decode()

    async def main():
        async with await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"], autocommit=True) as c:
            await c.execute("DROP TABLE IF EXISTS notetaker_credentials, notetaker_recordings, recording_media")
            await store.setup(c)
            await store.save_key(c, token_key, "fathom", FAKE_KEY, "")
            cursor = await c.execute("SELECT secret FROM notetaker_credentials")
            assert FAKE_KEY not in (await cursor.fetchone())[0]
            assert await store.load_key(c, token_key, "fathom") == FAKE_KEY
            assert await store.load_key(c, Fernet.generate_key().decode(), "fathom") is None
            assert (await store.statuses(c))["fathom"]["state"] == "connected"
            rec = fathom.payload_recording(FATHOM_ITEM)
            assert await store.upsert_recording(c, rec.public(), "webhook", "sealed-1") is True
            assert await store.upsert_recording(c, rec.public(), "sync") is False
            await store.set_event(c, "fathom", "4242", "ev-orbit")
            row = await store.by_event(c, "ev-orbit")
            assert row["company"] == "Orbit" and store.names(row) == ["Ana Ruiz"]
            assert "orbit.example" not in json.dumps(store.names(row, "invitee_hashes"))
            assert [r["external_id"] for r in await store.pending(c)] == ["4242"]
            await store.set_eval(c, "fathom", "4242", "handed_off")
            await store.upsert_recording(c, rec.public(), "webhook", "sealed-2")
            cursor = await c.execute("SELECT sealed FROM notetaker_recordings")
            assert (await cursor.fetchone())[0] is None  # Never re-sealed after the hand-off.
            assert await store.pending(c) == []
            assert await store.delete_key(c, "fathom") is True
            await store.save_media(c, "fathom", "4242", {"status": "processing", "download_id": "dl_1"})
            assert (await store.get_media(c, "fathom", "4242"))["status"] == "processing"
            assert (await store.many(c, [("fathom", "4242"), ("fathom", "9")])).keys() == {("fathom", "4242")}
            await c.execute("DROP TABLE notetaker_credentials, notetaker_recordings, recording_media")
    asyncio.run(main())


def test_past_interviews_lists_events_and_stored_recordings(client, monkeypatch):
    from app import calendar_sync
    from app.interview_types import Person, SlotEvent, slot_workflow_id
    now = datetime.now(timezone.utc)
    recent = SlotEvent("ev-recent", "Orbit Labs: technical round", (now - timedelta(hours=30)).isoformat(),
                       (now - timedelta(hours=29)).isoformat(), company="Orbit",
                       attendees=[Person("ana.ruiz@orbit.example", "Ana Ruiz"), Person(OWNER, "Me", is_self=True)])
    future = SlotEvent("ev-future", "Kite: screen", (now + timedelta(hours=3)).isoformat(),
                       (now + timedelta(hours=4)).isoformat(), company="Kite")
    rows = [
        {"provider": "fathom", "external_id": "4242", "title": "Orbit <> Candidate", "started_at": now - timedelta(hours=30),
         "ended_at": now - timedelta(hours=29), "share_url": "https://fathom.video/calls/4242",
         "embed_url": "https://fathom.video/embed/tokABC123", "event_key": "ev-recent", "eval_status": "handed_off",
         "event_title": "Orbit Labs: technical round", "event_company": "Orbit", "event_start": now - timedelta(hours=30)},
        {"provider": "fathom", "external_id": "4100", "title": "Vela <> Candidate", "started_at": now - timedelta(days=20),
         "ended_at": None, "share_url": "https://fathom.video/calls/4100", "embed_url": "", "event_key": "ev-old",
         "eval_status": "no_hook", "event_title": "Vela: system design", "event_company": "Vela",
         "event_start": now - timedelta(days=20)},
        {"provider": "fathom", "external_id": "4050", "title": "Interview -L2- Sam", "started_at": now - timedelta(days=25),
         "ended_at": None, "share_url": "https://fathom.video/calls/4050", "embed_url": "", "event_key": None,
         "eval_status": "handed_off", "event_title": None, "event_company": "L2- Sam", "event_start": None,
         "company": "Quillworks", "company_domain": "quillworks.example", "interviewers": '["Maya Lin"]'},
        {"provider": "fireflies", "external_id": "ff1", "title": "Kite intro", "started_at": now - timedelta(days=40),
         "ended_at": None, "share_url": "https://app.fireflies.ai/view/ff1", "embed_url": "", "event_key": None,
         "eval_status": "no_hook", "event_title": None, "event_company": None, "event_start": None}]
    windows = []

    async def setup(c):
        return None

    async def upcoming(c, start, end):
        windows.append(end - start)
        return [recent, future]

    async def between(c, start, end, limit=200):
        return rows

    monkeypatch.setattr(calendar_sync, "setup", setup)
    monkeypatch.setattr(calendar_sync, "upcoming", upcoming)
    monkeypatch.setattr(store, "between", between)
    assert client.get("/valr/api/past-interviews").status_code == 403
    data = client.get("/valr/api/past-interviews?days=999", headers=GOOD).json()
    assert data["days"] == 180 and windows == [timedelta(days=3)]
    items = data["interviews"]
    assert [i["title"] for i in items] == ["Orbit Labs: technical round", "Vela: system design",
                                           "Interview -L2- Sam", "Kite intro"]
    first = items[0]
    assert first["ref"] == slot_workflow_id("ev-recent") and first["interviewers"] == ["Ana Ruiz"]
    assert first["recording"]["id"] == "fathom:4242"
    assert first["recording"]["player"] is True and "embed_url" not in first["recording"]
    assert items[1]["matched"] is True
    # An unmatched recording keeps the company and people Fathom's invitees gave; never "L2- Sam".
    assert items[2]["company"] == "Quillworks" and items[2]["interviewers"] == ["Maya Lin"]
    assert items[3]["matched"] is False and items[3]["ref"] is None
    assert "@" not in json.dumps(data)


def test_granola_is_optional_and_fathom_plays_without_a_sharing_hint(client, monkeypatch):
    from app import valr_view
    from tests.valr_sample import notetakers_view, sample_view

    async def collect(pool, now=None):
        view = sample_view()
        view["connections"]["notetakers"] = notetakers_view({"fathom": "connected", "granola": "refused"})
        return view

    monkeypatch.setattr(valr_view, "collect", collect)
    page = client.get("/valr/settings", headers=GOOD).text
    setup = page.split('id="setup"', 1)[1].split('id="g-conn"', 1)[0]
    assert "Connect a notetaker" in setup and 'data-connect-open="granola"' not in setup
    step = setup.split("Connect a notetaker", 1)[1].split("</li>", 1)[0]
    assert "Done" in step and "Needs attention" not in step
    cards = page.split('id="notetakers"', 1)[1]
    assert "needs a Granola Business or Enterprise plan" in cards and 'data-import="granola"' in cards
    assert "Anyone with the link" not in page and "help.fathom.video" not in page  # No longer needed.
    assert "Plays the recording right here" in cards


def test_granola_alone_never_leaves_a_setup_step_open(client, monkeypatch):
    from app import valr_view
    from tests.valr_sample import notetakers_view, sample_view

    async def collect(pool, now=None):
        view = sample_view(scenario="empty")
        view["connections"]["notetakers"] = notetakers_view({})
        return view

    monkeypatch.setattr(valr_view, "collect", collect)
    page = client.get("/valr/settings", headers=GOOD).text
    setup = page.split('id="setup"', 1)[1].split('id="g-conn"', 1)[0]
    assert "Granola" not in setup and "Optional" in page
    from app.valr_ui import attention
    assert not any("granola" in i["key"] for i in attention.items(sample_view(scenario="empty")))


def test_r1_searches_granola_with_the_stored_key():
    from app import private_sources

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        if request.url.path == "/v1/notes":
            return httpx.Response(200, json={"notes": [
                {"id": "not_a", "title": "Orbit Labs debrief", "created_at": "2026-10-10T10:00:00Z"},
                {"id": "not_b", "title": "Dentist", "created_at": "2026-10-11T10:00:00Z"}], "hasMore": False})
        return httpx.Response(200, json={"id": "not_a", "summary_text": "They asked about sharding."})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        text, urls = private_sources.granola_search(http, ["Orbit"], FAKE_KEY)
    assert urls == ["valr://granola/not_a"] and "sharding" in text and "Dentist" not in text
    assert private_sources.granola_search(None, ["Orbit"], None) == ("", [])


def test_r1_fathom_search_prefers_the_stored_key(monkeypatch):
    from app import private_sources
    monkeypatch.setenv("FATHOM_API_KEY", "env-key-should-lose")
    seen = []

    def handler(request):
        seen.append(request.headers["x-api-key"])
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        private_sources.fathom_search(http, ["Orbit"], FAKE_KEY)
    assert seen == [FAKE_KEY]


def test_worker_reads_dashboard_keys_for_r1(monkeypatch):
    from cryptography.fernet import Fernet
    pytest.importorskip("temporalio")
    from app import interview_worker
    fake = FakeStore().install(monkeypatch)
    token_key = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", token_key)
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    monkeypatch.setenv("GRANOLA_API_KEY", "env-granola")
    run(store.save_key(object(), token_key, "fathom", FAKE_KEY, ""))
    assert run(interview_worker.notetaker_keys(object())) == (FAKE_KEY, "env-granola")
    assert fake.keys["fathom"]["secret"] != FAKE_KEY


# ---------- Playing recordings: Fathom's download API, cached (app/notetakers/media.py) ----------

SIGNED = "https://streamkit.storage.googleapis.com/fake-bucket/rec.mp4?X-Goog-Signature=fakesig0123"


def download(status, **extra):
    body = {"download_id": "dl_fake1", "recording_id": 4242, "status": status, **extra}
    return body


def video(expires_in=timedelta(hours=24), url=SIGNED, now=None):
    now = now or datetime.now(timezone.utc)
    return {"url": url, "content_type": "video/mp4", "file_size_bytes": 84_000_000,
            "expires_at": (now + expires_in).isoformat().replace("+00:00", "Z")}


class FakeFathom:
    """Answers POST /download and GET /downloads/{id} from a script; counts calls."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.headers["x-api-key"] == FAKE_KEY
        code, body = self.answers.pop(0)
        return httpx.Response(code, json=body)


@pytest.fixture
def media_env(monkeypatch):
    from cryptography.fernet import Fernet
    from app.notetakers import media
    fake = FakeStore().install(monkeypatch)
    token = Fernet.generate_key().decode()

    def resolve(fathom_fake, now=None, refresh=False, key=FAKE_KEY):
        async def go():
            async with client_for(fathom_fake) as http:
                return await media.resolve(object(), "fathom", "4242", http, key, token, now=now, refresh=refresh)
        return run(go())
    return fake, resolve


def test_media_requests_then_polls_then_reuses_the_cached_url(media_env):
    fake, resolve = media_env
    api = FakeFathom((202, download("processing")), (200, download("completed", video=video())))
    first = resolve(api)
    assert first == {"status": "processing", "message": "Preparing the video… (first play can take a minute or two)"}
    assert api.calls == [("POST", "/external/v1/recordings/4242/download")]
    second = resolve(api)
    assert second["status"] == "completed" and second["url"] == SIGNED and second["content_type"] == "video/mp4"
    assert api.calls[-1] == ("GET", "/external/v1/recordings/4242/downloads/dl_fake1")
    stored = fake.media[("fathom", "4242")]
    assert stored["status"] == "completed" and SIGNED not in str(stored["url"])  # Sealed at rest.
    third = resolve(FakeFathom())  # No answers scripted: any call to Fathom would fail.
    assert third["url"] == SIGNED


def test_media_reuses_a_url_only_with_30_minutes_left(media_env):
    fake, resolve = media_env
    now = datetime.now(timezone.utc)
    resolve(FakeFathom((202, download("completed", video=video(timedelta(minutes=31), now=now)))), now=now)
    assert resolve(FakeFathom(), now=now)["url"] == SIGNED  # 31 min left: reused.
    later = now + timedelta(minutes=2)  # 29 min left: a fresh download.
    fresh = SIGNED.replace("fakesig0123", "fakesig4567")
    api = FakeFathom((202, download("completed", video=video(url=fresh, now=later))))
    assert resolve(api, now=later)["url"] == fresh and api.calls[0][0] == "POST"


def test_media_audio_only_recordings_play_too(media_env):
    _, resolve = media_env
    audio = dict(video(url="https://fathom-recording-data.storage.googleapis.com/a/rec.m4a?sig=fake"),
                 content_type="audio/mp4")
    got = resolve(FakeFathom((202, download("completed", audio=audio))))
    assert got["status"] == "completed" and got["content_type"] == "audio/mp4"


@pytest.mark.parametrize("code, status, words", [
    (403, "unavailable", "isn't downloadable for your account"), (404, "unavailable", "can't find"),
    (422, "unavailable", "no video or audio"), (429, "busy", "Fathom is busy, try again in a minute"),
    (401, "unavailable", "refused the API key"), (500, "failed", "didn't answer")])
def test_media_maps_fathom_errors_to_plain_messages(media_env, code, status, words):
    _, resolve = media_env
    got = resolve(FakeFathom((code, {"error": "x"})))
    assert got["status"] == status and words in got["message"] and "url" not in got


def test_media_remembers_an_unavailable_recording_but_not_a_rate_limit(media_env):
    fake, resolve = media_env
    assert resolve(FakeFathom((403, {})))["status"] == "unavailable"
    assert resolve(FakeFathom())["status"] == "unavailable"  # Cached: Fathom isn't asked again.
    fake.media.clear()
    api = FakeFathom((429, {}), (202, download("processing")))
    assert resolve(api) == {"status": "busy", "message": "Fathom is busy, try again in a minute."}
    assert resolve(api)["status"] == "processing"  # A 429 isn't remembered: the next ask goes through.


def test_media_refuses_an_unexpected_host_and_a_failed_generation(media_env):
    _, resolve = media_env
    odd = resolve(FakeFathom((202, download("completed", video=video(url="https://evil.example/x.mp4")))))
    assert odd["status"] == "failed" and "url" not in odd
    failed = resolve(FakeFathom((202, download("failed", failure_reason="generation_timeout"))))
    assert failed["status"] == "failed" and "took too long" in failed["message"]


def test_media_refresh_asks_for_a_new_url_after_a_minute(media_env):
    fake, resolve = media_env
    now = datetime.now(timezone.utc)
    resolve(FakeFathom((202, download("completed", video=video(now=now)))), now=now)
    assert resolve(FakeFathom(), now=now, refresh=True)["url"] == SIGNED  # Just made: not again.
    fake.media[("fathom", "4242")]["updated_at"] = now - timedelta(minutes=5)
    fresh = SIGNED.replace("fakesig0123", "fakesig9999")
    api = FakeFathom((202, download("completed", video=video(url=fresh, now=now))))
    assert resolve(api, now=now, refresh=True)["url"] == fresh and api.calls


def test_media_without_a_key_or_for_other_providers(media_env):
    from app.notetakers import media
    _, resolve = media_env
    assert resolve(FakeFathom(), key=None)["status"] == "unavailable"
    assert run(media.resolve(object(), "granola", "g1", None, FAKE_KEY, "k"))["status"] == "unavailable"


def test_media_never_logs_the_signed_url_or_the_key(media_env, caplog):
    import logging
    fake, resolve = media_env
    caplog.set_level(logging.DEBUG)
    api = FakeFathom((202, download("processing")), (200, download("completed", video=video())))
    resolve(api)
    resolve(api)
    resolve(FakeFathom())
    fake.media[("fathom", "4242")]["updated_at"] -= timedelta(minutes=5)
    resolve(FakeFathom((202, download("completed", video=video(url="https://evil.example/x.mp4?sig=fakesig0123")))),
            refresh=True)
    text = caplog.text
    assert "unexpected host: evil.example" in text  # The host only.
    assert "fakesig0123" not in text and SIGNED not in text and FAKE_KEY not in text


def test_media_route_is_owner_only_and_returns_the_answer(client, monkeypatch, caplog):
    import logging
    from app.auth import gate
    from app.notetakers import media
    assert gate.classify("/valr/api/recording/media") == "owner"

    async def resolve(connection, provider, external_id, http, key, token_key, now=None, refresh=False):
        assert (provider, external_id, key, refresh) == ("fathom", "4242", FAKE_KEY, True)
        return media.answer("completed", url=SIGNED, expires_at="2026-10-17T09:00:00+00:00", content_type="video/mp4")

    monkeypatch.setattr(media, "resolve", resolve)
    monkeypatch.setenv("FATHOM_API_KEY", FAKE_KEY)
    caplog.set_level(logging.DEBUG)
    assert client.get("/valr/api/recording/media?id=fathom:4242").status_code == 403
    assert client.get("/valr/api/recording/media?id=fathom:4242",
                      headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    response = client.get("/valr/api/recording/media?id=fathom:4242&refresh=1", headers=GOOD)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"ok": True, "status": "completed", "url": SIGNED,
                               "expires_at": "2026-10-17T09:00:00+00:00", "content_type": "video/mp4"}
    assert response.headers["referrer-policy"] == "no-referrer"
    for bad in ("zoom:1", "fathom:", "fathom:../x", "nothing"):
        assert client.get(f"/valr/api/recording/media?id={bad}", headers=GOOD).status_code == 404
    assert "fakesig0123" not in caplog.text and FAKE_KEY not in caplog.text


def test_new_matched_fathom_recording_prewarms_its_download(monkeypatch):
    from app.notetakers import media
    FakeStore().install(monkeypatch)
    warmed = []
    monkeypatch.setattr(media, "prewarm", lambda provider, external_id: warmed.append((provider, external_id)))

    async def match(recording):
        return {"key": "ev-orbit", "title": "Orbit", "company": "Orbit", "start": None}
    run(pipeline.intake(object(), fathom.payload_recording(FATHOM_ITEM), "webhook", hook=lambda *a: "ok", match=match))
    run(pipeline.intake(object(), fathom.payload_recording(FATHOM_ITEM), "webhook", hook=lambda *a: "ok", match=match))
    run(pipeline.intake(object(), Recording("fathom", "77", "No interview"), "sync", hook=lambda *a: "ok"))
    assert warmed == [("fathom", "4242")]  # Once, only when new and matched to an interview.


def test_backfill_fills_company_and_people_of_stored_recordings(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    fake.recordings[("fathom", "4242")] = {"eval_status": "handed_off", "title": "Orbit"}
    monkeypatch.setenv("FATHOM_API_KEY", FAKE_KEY)
    monkeypatch.delenv("VALR_TOKEN_KEY", raising=False)

    def handler(request):
        return httpx.Response(200, json={"items": [FATHOM_ITEM, dict(FATHOM_ITEM, recording_id=9999)],
                                         "next_cursor": None})

    async def go():
        async with client_for(handler) as http:
            return await notetakers.backfill_details(object(), http)
    assert run(go()) == {"listed": 2, "updated": 1}  # Only the stored one; nothing new is taken in.
    row = fake.recordings[("fathom", "4242")]
    assert row["company"] == "Orbit Labs" and row["interviewers"] == ["Ana Ruiz"]
    assert ("fathom", "9999") not in fake.recordings
