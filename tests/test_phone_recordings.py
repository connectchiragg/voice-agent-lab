"""Phone call recordings: the upload's auth and checks, encryption at rest, Range playback,
processing with a fake Deepgram, and the recording showing in Past interviews.
Audio here is fake bytes behind a real file signature; names are fictional."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import phone_recordings as phone
from app.company_resolver import Decision as CompanyDecision

TOKEN = "test-upload-token-0123456789abcdef"
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + bytes(range(256)) * 12_000  # ~3 MB, 3+ pieces


def run(coro):
    return asyncio.run(coro)


class FakePhone:
    """In-memory stand-in for phone_recordings' store functions."""

    def __init__(self):
        self.uploads, self.audio, self.token, self.how = {}, {}, None, {}

    def install(self, monkeypatch):
        async def setup(c):
            return None

        async def token_row(c):
            return dict(self.token) if self.token else None

        async def save_token(c, token, source, revealed):
            self.token = {"token_hash": phone.digest(token), "source": source, "created_at": None,
                          "revealed_at": datetime.now(timezone.utc) if revealed else None}

        async def mark_revealed(c):
            self.token["revealed_at"] = datetime.now(timezone.utc)

        async def begin_upload(c, upload_id):
            self.uploads[upload_id] = {k: None for k in phone.UPLOAD_FIELDS} | {
                "id": upload_id, "status": "receiving", "received_at": datetime.now(timezone.utc), "tries": 0,
                "size": 0, "kind": "", "title": "", "other_name": ""}

        async def put_chunk(c, upload_id, idx, sealed):
            self.audio[(upload_id, idx)] = sealed

        async def finish_upload(c, upload_id, size, kind, title, other_name, recorded_at, sha256=None):
            self.uploads[upload_id].update(status="uploaded", size=size, kind=kind, title=title,
                                           other_name=other_name, recorded_at=recorded_at, sha256=sha256)

        async def find_duplicate(c, sha256, upload_id):
            return next((u["id"] for u in self.uploads.values() if u.get("sha256") == sha256 and u["id"] != upload_id
                         and u["status"] in ("uploaded", "processing", "done")), None)

        async def release_failed(c, sha256):
            for u in self.uploads.values():
                if u.get("sha256") == sha256 and u["status"] == "failed":
                    u["sha256"] = None

        async def drop_upload(c, upload_id):
            self.uploads.pop(upload_id, None)
            for key in [k for k in self.audio if k[0] == upload_id]:
                del self.audio[key]

        async def upload(c, upload_id):
            row = self.uploads.get(upload_id)
            return dict(row) if row else None

        async def chunk(c, upload_id, idx):
            return self.audio.get((upload_id, idx))

        async def claim(c):
            waiting = [u for u in self.uploads.values() if u["status"] == "uploaded"]
            if not waiting:
                return None
            row = waiting[0]
            row.update(status="processing", tries=row["tries"] + 1)
            return dict(row)

        async def mark(c, upload_id, **values):
            self.uploads[upload_id].update(values)

        async def stuck(c, older):
            return None

        async def set_company_how(c, upload_id, how):
            self.how[upload_id] = how

        for name, fn in locals().items():
            if name != "self" and callable(fn):
                monkeypatch.setattr(phone, name, fn)
        return self


@pytest.fixture
def key(monkeypatch):
    from cryptography.fernet import Fernet
    value = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", value)
    return value


# Pure parts.

def test_multipart_parser_handles_any_split():
    body = (b"--XyZ\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nAcme screen\r\n"
            b"--XyZ\r\nContent-Disposition: form-data; name=\"file\"; filename=\"call.m4a\"\r\n"
            b"Content-Type: audio/x-m4a\r\n\r\n" + M4A[:5000] + b"\r\n--XyZ--\r\n")
    for step in (1, 7, 4096, len(body)):
        parser = phone.MultipartParser(b"XyZ")
        parts, data = [], {}
        for i in range(0, len(body), step):
            for event, value in parser.feed(body[i:i + step]):
                if event == "part":
                    parts.append(value)
                    data[value["name"]] = b""
                elif event == "data":
                    data[parts[-1]["name"]] += value
        assert parser.done and [p["name"] for p in parts] == ["title", "file"], step
        assert data["title"] == b"Acme screen" and data["file"] == M4A[:5000], step
        assert parts[1]["filename"] == "call.m4a" and parts[1]["content_type"] == "audio/x-m4a"


@pytest.mark.parametrize("head, name, kind", [
    (M4A[:16], "x.m4a", "m4a"), (b"RIFF\x00\x00\x00\x00WAVEfmt ", "x.wav", "wav"), (b"ID3\x04" + b"\x00" * 12, "", "mp3"),
    (b"\xff\xf1\x50\x80" + b"\x00" * 12, "x.aac", "aac"), (b"hello, not audio", "x.m4a", None),
    (M4A[:16], "x.exe", None)])
def test_audio_type_is_sniffed(head, name, kind):
    assert phone.kind_of(head, name, "") == kind


def test_encryption_round_trip_and_ranges(key):
    chunker = phone.Chunker()
    pieces = chunker.feed(M4A) + [chunker.rest()]
    sealed = [phone.seal_chunk(key, p) for p in pieces]
    assert len(sealed) == 3 and all(bytes(range(64)) not in s for s in sealed)  # Not the plaintext.

    async def get(idx):
        return sealed[idx] if idx < len(sealed) else None

    async def collect(start, end):
        return b"".join([part async for part in phone.read_range(get, key, start, end)])
    assert run(collect(0, len(M4A) - 1)) == M4A
    edge = phone.CHUNK
    assert run(collect(edge - 10, edge + 10)) == M4A[edge - 10:edge + 11]
    assert phone.byte_range("bytes=100-", 1000) == (100, 999) and phone.byte_range("bytes=-100", 1000) == (900, 999)
    assert phone.byte_range("", 1000) is None
    with pytest.raises(ValueError):
        phone.byte_range("bytes=2000-", 1000)


def test_the_owner_is_found_among_the_speakers():
    utterances = [{"speaker": 0, "transcript": "Hi, am I speaking with Sam?", "start": 0},
                  {"speaker": 1, "transcript": "Yes, this is Sam. Good to talk.", "start": 3}]
    assert phone.owner_speaker(utterances, "Sam Rivera") == 1
    greet = [{"speaker": 0, "transcript": "Hello Sam, thanks for joining", "start": 0},
             {"speaker": 1, "transcript": "Thanks", "start": 2}]
    assert phone.owner_speaker(greet, "Sam Rivera") == 1
    talky = [{"speaker": 0, "transcript": "Tell me about you", "start": 0},
             {"speaker": 1, "transcript": "I built a payments platform used by many teams", "start": 2}]
    assert phone.owner_speaker(talky, "") == 1


# The upload route.

@pytest.fixture
def client(monkeypatch, key):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from app import phone_web, research, valr_dashboard, valr_view
    from app.main import app
    from tests.test_notetakers import GOOD, OWNER, Pool, Verifier
    from tests.valr_sample import sample_view

    async def collect(pool, now=None):
        return sample_view()

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setattr(phone_web, "limiter", phone_web.RateLimit())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")
    monkeypatch.setenv("VALR_UPLOAD_TOKEN", TOKEN)
    test_client = TestClient(app)
    test_client.phone = FakePhone().install(monkeypatch)
    test_client.good = GOOD
    return test_client


def post(client, token=TOKEN, data=M4A, name="call.m4a", fields=None, headers=None):
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/hooks/recording-upload", files={"file": (name, data, "audio/x-m4a")},
                       data=fields or {}, headers={**auth, **(headers or {})})


def test_upload_needs_the_token(client):
    assert post(client, token=None).status_code == 401
    assert post(client, token="wrong-token").status_code == 401
    assert client.phone.uploads == {}
    ok = post(client, fields={"title": "Acme recruiter call", "name": "Riya Sen"})
    assert ok.status_code == 200 and ok.json()["ok"] is True and ok.json()["id"].startswith("phone:")
    row = next(iter(client.phone.uploads.values()))
    assert row["status"] == "uploaded" and row["size"] == len(M4A) and row["kind"] == "m4a"
    assert row["title"] == "Acme recruiter call" and row["other_name"] == "Riya Sen"


def test_upload_refuses_wrong_types_too_large_and_too_many(client, monkeypatch):
    assert post(client, data=b"just some text, not audio at all", name="notes.txt").status_code == 415
    monkeypatch.setenv("VALR_PHONE_MAX_MB", "1")
    assert post(client).status_code == 413
    assert client.phone.uploads == {} and client.phone.audio == {}  # Nothing half-stored stays.
    monkeypatch.setenv("VALR_PHONE_MAX_MB", "100")
    monkeypatch.setenv("VALR_UPLOAD_PER_HOUR", "2")
    from app import phone_web
    monkeypatch.setattr(phone_web, "limiter", phone_web.RateLimit())
    statuses = [post(client, token="nope", data=b"x").status_code for _ in range(3)]
    assert statuses == [401, 401, 429]


def test_audio_is_encrypted_at_rest_and_streams_with_ranges(client, key):
    upload_id = post(client).json()["id"].split(":", 1)[1]
    stored = [v for (u, _), v in sorted(client.phone.audio.items()) if u == upload_id]
    assert len(stored) == 3 and all(M4A[100:200] not in piece for piece in stored)
    assert client.get(f"/valr/api/recording/audio?id=phone:{upload_id}").status_code == 403  # Owner only.
    whole = client.get(f"/valr/api/recording/audio?id=phone:{upload_id}", headers=client.good)
    assert whole.status_code == 200 and whole.content == M4A and whole.headers["accept-ranges"] == "bytes"
    part = client.get(f"/valr/api/recording/audio?id=phone:{upload_id}",
                      headers={**client.good, "Range": f"bytes={phone.CHUNK - 5}-{phone.CHUNK + 4}"})
    assert part.status_code == 206 and part.content == M4A[phone.CHUNK - 5:phone.CHUNK + 5]
    assert part.headers["content-range"] == f"bytes {phone.CHUNK - 5}-{phone.CHUNK + 4}/{len(M4A)}"
    media = client.get(f"/valr/api/recording/media?id=phone:{upload_id}", headers=client.good).json()
    assert media["status"] == "completed" and media["url"] == f"/valr/api/recording/audio?id=phone:{upload_id}"


def test_the_token_is_shown_once_and_rotates(client):
    from tests.test_notifications import csrf_headers
    assert client.post("/valr/api/phone/token", content=b'{"action": "show"}').status_code == 403
    shown = client.post("/valr/api/phone/token", content=b'{"action": "show"}', headers=csrf_headers(client))
    assert shown.json()["token"] == TOKEN and client.phone.token["token_hash"] == phone.digest(TOKEN)
    assert TOKEN not in json.dumps(client.phone.token, default=str)  # Only the hash is stored.
    again = client.post("/valr/api/phone/token", content=b'{"action": "show"}', headers=csrf_headers(client))
    assert again.status_code == 409
    new = client.post("/valr/api/phone/token", content=b'{"action": "rotate"}', headers=csrf_headers(client)).json()["token"]
    assert new != TOKEN and post(client).status_code == 401 and post(client, token=new).status_code == 200


# Processing with a fake Deepgram, then Past interviews.

DEEPGRAM_ANSWER = {
    "metadata": {"duration": 95.0},
    "results": {"utterances": [
        {"speaker": 0, "start": 0.5, "end": 4.0, "transcript": "Hi, this is Riya from Acme. Am I speaking with Sam?"},
        {"speaker": 1, "start": 4.5, "end": 6.0, "transcript": "Yes, this is Sam."},
        {"speaker": 0, "start": 6.5, "end": 12.0, "transcript": "Tell me about a system you designed."},
        {"speaker": 1, "start": 12.5, "end": 40.0, "transcript": "I designed a ledger service with idempotent writes."}]}}


class Resolver:
    def __init__(self):
        self.evidence = []

    async def resolve(self, connection, ev, entity_id=""):
        self.evidence.append((ev, entity_id))
        return CompanyDecision("Acme", "", 0.9, "llm", "AI review: introduced as Acme")


def test_processing_transcribes_and_feeds_the_pipeline(client, key, monkeypatch):
    from app.notetakers import routes, store
    from tests.test_notetakers import FakeStore
    notes = FakeStore().install(monkeypatch)
    upload_id = post(client, fields={"name": "Riya Sen"}).json()["id"].split(":", 1)[1]
    seen = {}

    def deepgram(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, json=DEEPGRAM_ANSWER)

    handed = []

    async def hook(provider, external_id, meeting):
        handed.append((provider, external_id, meeting))
        return "queued for the post-call report"

    async def no_event(recording):
        return None

    resolver = Resolver()
    processor = phone.Processor(http=httpx.AsyncClient(transport=httpx.MockTransport(deepgram)),
                                deepgram=phone.DeepgramPrerecorded("dg-test-key"), resolver=resolver, hook=hook,
                                match=no_event, candidate="Sam Rivera")
    assert run(processor.run_once(object())) == upload_id
    assert seen["body"] == M4A and seen["auth"] == "Token dg-test-key"
    assert "model=nova-3" in seen["url"] and "diarize=true" in seen["url"] and "smart_format=true" in seen["url"]
    row = client.phone.uploads[upload_id]
    assert row["status"] == "done" and row["delete_after"] - row["processed_at"] == timedelta(days=7)
    assert "Riya" not in row["meeting"]  # The stored transcript is ciphertext.
    provider, external_id, meeting = handed[0]
    assert (provider, external_id) == ("phone", upload_id) and meeting["source"] == "phone"
    assert [t["speaker"] for t in meeting["turns"]] == ["Riya Sen", "Sam Rivera", "Riya Sen", "Sam Rivera"]
    assert resolver.evidence[0][0].transcript.startswith("Riya Sen: Hi, this is Riya from Acme")
    recorded = notes.recordings[("phone", upload_id)]
    assert recorded["company"] == "Acme" and recorded["eval_status"] == "handed_off"
    assert run(processor.run_once(object())) is None  # Nothing else waits.

    # Past interviews lists it, and it plays from VALR.
    async def between(c, start, end, limit=200):
        return [{"provider": p, "external_id": x, "title": r.get("title", ""), "started_at": phone.parse_when(r["start"]),
                 "ended_at": None, "share_url": r.get("share_url", ""), "embed_url": "", "event_key": None,
                 "eval_status": r["eval_status"], "event_title": None, "event_company": None, "event_start": None,
                 "company": r.get("company", ""), "interviewers": json.dumps(r.get("interviewers") or [])}
                for (p, x), r in notes.recordings.items()]

    async def nothing(*args, **kwargs):
        return []

    from app import calendar_sync

    async def setup(c):
        return None
    monkeypatch.setattr(store, "between", between)
    monkeypatch.setattr(calendar_sync, "setup", setup)
    monkeypatch.setattr(calendar_sync, "upcoming", nothing)
    items = client.get("/valr/api/past-interviews", headers=client.good).json()["interviews"]
    phone_item = next(i for i in items if i["recording"]["provider"] == "phone")
    assert phone_item["company"] == "Acme" and phone_item["recording"]["player"] is True
    assert phone_item["recording"]["label"] == "Phone call" and phone_item["interviewers"] == ["Riya Sen"]
    assert routes.recording_summary  # The route module is in use.


def test_a_failed_transcription_is_retried_then_given_up(client, key):
    post(client)

    def broken(request):
        return httpx.Response(500, json={})

    processor = phone.Processor(http=httpx.AsyncClient(transport=httpx.MockTransport(broken)),
                                deepgram=phone.DeepgramPrerecorded("dg-test-key"), resolver=Resolver(),
                                match=lambda r: None, candidate="Sam")
    for _ in range(phone.MAX_TRIES):
        assert run(processor.run_once(object())) is None
    row = next(iter(client.phone.uploads.values()))
    assert row["status"] == "failed" and row["error"] == "RuntimeError" and row["delete_after"] is not None


# Re-sends from the batch Shortcut, and what a file name tells.

def test_a_resent_file_is_a_duplicate_and_not_stored_again(client):
    import hashlib
    first = post(client, name="Riya Sen 2026-10-17 14-30-12.m4a").json()
    assert first["ok"] is True and first["duplicate"] is False
    again = post(client, name="copy of it.m4a")
    assert again.status_code == 200 and again.json() == {"ok": True, "result": "ok", "duplicate": True, "id": first["id"],
                                                         "message": "Already received. Nothing new to do."}
    assert len(client.phone.uploads) == 1 and {k[0] for k in client.phone.audio} == {first["id"].split(":")[1]}
    row = next(iter(client.phone.uploads.values()))
    assert row["sha256"] == hashlib.sha256(M4A).hexdigest()
    other = post(client, data=M4A[:-1] + b"x").json()  # Different bytes: a new recording.
    assert other["duplicate"] is False and len(client.phone.uploads) == 2
    row["status"] = "failed"  # A failed copy can be sent again.
    retry = post(client).json()
    assert retry["duplicate"] is False and row["sha256"] is None


def test_the_file_name_gives_the_time_and_name_but_never_a_number(client):
    post(client, name="Call recording - Riya Sen (+91 98765 43210) 17-10-2026 09.05.m4a")
    row = next(iter(client.phone.uploads.values()))
    assert row["other_name"] == "Riya Sen" and row["recorded_at"].isoformat() == "2026-10-17T09:05:00+05:30"
    client.phone.uploads.clear()
    post(client, data=M4A + b"1", name="+919876543210_20261017_143012.m4a")
    row = next(iter(client.phone.uploads.values()))
    assert row["other_name"] == "" and "98765" not in json.dumps(row, default=str)
    client.phone.uploads.clear()
    post(client, data=M4A + b"2", name="x.m4a", fields={"filename": "Arjun Rao 2026-10-16 18-00.m4a",
                                                        "name": "", "title": "Call +91 98765 43210"})
    row = next(iter(client.phone.uploads.values()))
    assert row["other_name"] == "Arjun Rao" and row["recorded_at"].hour == 18 and "98765" not in row["title"]


@pytest.mark.parametrize("filename, name, when", [
    ("Riya Sen 2026-10-17 14-30-12.m4a", "Riya Sen", "2026-10-17T14:30:12+05:30"),
    ("Rahul_K_2026_10_17_0930.mp3", "Rahul K", "2026-10-17T09:30:00+05:30"),
    ("Recording 2026-10-17 at 14.30.12.m4a", "", "2026-10-17T14:30:12+05:30"),
    ("9876543210.m4a", "", None), ("+91 98200 26101.m4a", "", None),
    ("Acme HR.m4a", "Acme HR", None), ("call_rec_20261317.m4a", "", None)])
def test_from_filename(monkeypatch, filename, name, when):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")
    got_name, got_when = phone.from_filename(filename)
    assert got_name == name and (got_when.isoformat() if got_when else None) == when


def test_the_shortcut_steps_page_is_owner_only_and_shows_the_batch_steps(client):
    assert client.get("/valr/settings/phone-help").status_code == 403
    assert client.get("/valr/settings/phone-help", headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    page = client.get("/valr/settings/phone-help", headers=client.good)
    assert page.status_code == 200 and "iPhone shortcut steps" in page.text
    for step in ("VALR Inbox", "Save to Files", "Repeat with Each", "Get Contents of URL", "Delete Files",
                 "Add to Home Screen", "<strong>If</strong>"):
        assert step in page.text, step
    settings = client.get("/valr/settings", headers=client.good).text
    assert 'href="/valr/settings/phone-help"' in settings


def test_help_markdown_is_escaped_and_nests_lists():
    from app import phone_help
    out = phone_help.render("## Hi <b>\n\n1. One **bold**\n   - inner `code`\n2. Two\n\n<script>x</script>")
    assert "<h3>Hi &lt;b&gt;</h3>" in out and "<script>" not in out and "&lt;script&gt;" in out
    assert out.count("<ol") == 1 and out.count("<ul") == 1 and "<code>code</code>" in out
    assert "<strong>bold</strong>" in out and out.index("<ul") < out.index("Two")
