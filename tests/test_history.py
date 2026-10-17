"""History import (python -m app.notetakers history), the quiet evaluation backfill, the
re-analysis of derived-only interviews, kept transcripts and Fireflies playback. Fakes only:
no network, no Postgres; every person, company and key is made up."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app import post_call
from app.notetakers import base, fathom, fireflies, history, media, store, transcripts
from app.notetakers.base import Recording
from tests.test_notetakers import FAKE_KEY, GOOD, FakeStore, action_headers, client  # noqa: F401

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
SECRET_LINE = "We sharded the ledger by tenant id"


def run(coro):
    return asyncio.run(coro)


def mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class FakeConn:
    """Just enough SQL for recording_transcripts and calendar_history."""

    def __init__(self):
        self.transcripts, self.calendar = {}, {}

    async def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO recording_transcripts"):
            self.transcripts[(params[0], params[1])] = params[2]
        elif q.startswith("SELECT sealed FROM recording_transcripts"):
            return Cursor([(self.transcripts[params],)] if params in self.transcripts else [])
        elif q.startswith("DELETE FROM recording_transcripts"):
            self.transcripts.pop(params, None)
        elif q.startswith("INSERT INTO calendar_history"):
            new = params[0] not in self.calendar
            self.calendar[params[0]] = params
            return Cursor([(new,)])
        elif q.startswith("SELECT event_key, title, company, start_utc"):
            rows = [(k, p[1], p[2], p[3], p[4], p[5]) for k, p in self.calendar.items()]
            return Cursor(rows)
        return Cursor([])


@pytest.fixture
def token(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", key)
    return key


def meeting(source="fathom", mid="77"):
    return {"source": source, "id": mid, "title": "Orbit Labs: technical round",
            "turns": [{"speaker": "Ana Ruiz", "text": "How would you shard it?", "start": 95},
                      {"speaker": "Sam Candidate", "text": SECRET_LINE, "start": 110.5}]}


def rec(external_id, title="Orbit Labs: technical round", company="Orbit", invitees=("Ana Ruiz",),
        emails=("ana@orbit.example", "me@example.com"), provider="fathom", domain="orbit.example"):
    return Recording(provider, external_id, title, "2026-09-01T09:00:00Z", "2026-09-01T10:00:00Z",
                     f"https://fathom.video/calls/{external_id}", attendee_emails=list(emails), company=company,
                     company_domain=domain if company else "", interviewers=list(invitees),
                     invitee_hashes=[base.email_hash(e) for e in emails[:1]] if invitees else [])


# ---------- Paging and rate limits ----------

def test_fathom_history_pages_through_every_cursor_and_waits_on_429(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(fathom.asyncio, "sleep", sleep)
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        cursor = request.url.params.get("cursor")
        if cursor == "c2" and sum(1 for c in calls if c.get("cursor") == "c2") == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        page = {None: (["1", "2"], "c1"), "c1": (["3"], "c2"), "c2": (["4"], None)}[cursor]
        return httpx.Response(200, json={"items": [{"recording_id": i, "title": "x"} for i in page[0]],
                                         "next_cursor": page[1]})

    async def go():
        async with mock(handler) as http:
            return await fathom.list_recordings(FAKE_KEY, NOW - timedelta(days=90), NOW, http,
                                                max_pages=history.MAX_PAGES, pace_s=1.1)
    found = run(go())
    assert [r.external_id for r in found] == ["1", "2", "3", "4"]
    assert calls[0]["created_after"].startswith("2026-07-19")
    assert 7.0 in slept and slept.count(1.1) == 2  # One 429 back-off, a pause before each later page.


def test_fireflies_history_pages_by_skip(monkeypatch):
    seen = []

    def handler(request):
        skip = json.loads(request.content)["variables"]["skip"]
        seen.append(skip)
        n = 50 if skip < 100 else 3
        return httpx.Response(200, json={"data": {"transcripts": [
            {"id": f"ff{skip + i}", "title": "Screen", "date": 1758000000000} for i in range(n)]}})

    async def go():
        async with mock(handler) as http:
            return await fireflies.list_recordings(FAKE_KEY, NOW - timedelta(days=90), NOW, http,
                                                   max_pages=history.MAX_PAGES)
    assert len(run(go())) == 103 and seen == [0, 50, 100]


# ---------- The interview filter ----------

@pytest.mark.parametrize("recording, ok", [
    (rec("a"), True),                                                       # Title says round, outside person.
    (rec("b", title="Orbit <> Sam"), True),                                 # Classifier's "<>" title.
    (rec("c", title="Weekly sync", company="Orbit"), False),                # Internal title.
    (rec("d", title="Technical interview", company="", invitees=(), emails=("me@example.com",), domain=""), False),
    (rec("e", title="Chat", company="Orbit"), True),                        # An outside company's domain.
    (rec("f", title="Chat", company="", invitees=("Ana",), domain=""), False),  # No interview signal.
    (rec("g", emails=tuple(f"p{i}@orbit.example" for i in range(9))), False),  # Too many people.
])
def test_interview_filter(recording, ok):
    assert history.interview_like(recording)[0] is ok


# ---------- Import: idempotent, dry run, already scored ----------

@pytest.fixture
def importer(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    queued, requeued = [], []

    async def lister(provider, key, start, end, http):
        assert (end - start).days == 90
        return [rec("100"), rec("101", title="Weekly sync"), rec("102"), rec("103")] if provider == "fathom" else []

    async def queue(connection, recording):
        if recording.external_id in queued:
            return False
        queued.append(recording.external_id)
        return True

    async def requeue(connection, provider, external_id, title, url):
        requeued.append(external_id)
        return True

    async def reanalysed(connection):
        return {("fathom", x) for x in requeued}

    async def keep(connection, recording, key, http):
        return True
    monkeypatch.setattr(history, "reanalysed", reanalysed)
    monkeypatch.setattr(history, "keep_transcript", keep)
    interviews = {"fathom:103": {"impression": {"coverage": "transcript"}},
                  "fathom:900": {"impression": {"coverage": "derived"}, "title": "Old round"},
                  "fathom:901": {"impression": {"coverage": "transcript"}},
                  "phone:5": {"impression": {"coverage": "derived"}}}

    def go(dry_run=False):
        return run(history.import_recordings(object(), None, 90, dry_run=dry_run, now=NOW, keys={"fathom": FAKE_KEY},
                                             lister=lister, queue=queue, interviews=interviews, requeue=requeue))
    return fake, queued, requeued, go


def test_dry_run_counts_only(importer):
    fake, queued, requeued, go = importer
    counts = go(dry_run=True)
    assert (counts.total, counts.interview_like, counts.already_stored, counts.new) == (4, 3, 0, 3)
    assert counts.queued == 2 and counts.already_scored == 1 and counts.reanalyse == 1
    assert fake.recordings == {} and queued == [] and requeued == []


def test_import_is_idempotent_and_never_rescores(importer):
    fake, queued, requeued, go = importer
    first = go()
    assert first.new == 3 and first.queued == 2 and first.reanalyse == 1
    assert sorted(queued) == ["100", "102"] and requeued == ["900"]
    assert fake.recordings[("fathom", "100")]["origin"] == "history"
    assert fake.recordings[("fathom", "100")]["company"] == "Orbit"
    assert fake.recordings[("fathom", "103")]["eval_status"] == "handed_off"  # Scored already: not queued.
    second = go()
    assert (second.new, second.queued, second.reanalyse, second.already_stored) == (0, 0, 0, 3)
    assert sorted(queued) == ["100", "102"] and requeued == ["900"]


def test_hidden_recordings_stay_out(importer):
    fake, queued, _, go = importer
    fake.gone[("fathom", "100")] = "hidden"
    counts = go()
    assert counts.hidden == 1 and "100" not in queued


# ---------- The cost gate ----------

def test_cost_estimate_and_the_yes_gate(monkeypatch):
    calls = []

    async def fake_import(connection, http, days, dry_run=False, match=None):
        calls.append(dry_run)
        return history.Counts(total=80, interview_like=60, new=60, queued=55, reanalyse=5, providers={"fathom": 80})

    async def no_calendar(*a, **k):
        return {"skipped": "test"}
    monkeypatch.setattr(history, "import_recordings", fake_import)
    monkeypatch.setattr(history, "calendar_history", no_calendar)
    lines = []
    est = history.estimate(60)
    assert est["usd"] > history.ESTIMATE_GATE_USD and est["tokens"]["claude-sonnet-5-5"][0] > 60 * 30_000
    assert run(history.run(object(), None, 90, dry_run=False, yes=False, out=lines.append)) == 2
    assert calls == [True] and any("--yes" in line for line in lines)
    assert any("claude-sonnet-5-5" in line and "$" in line for line in lines)
    assert run(history.run(object(), None, 90, dry_run=True, yes=False, out=lines.append)) == 0
    assert calls == [True, True]
    assert run(history.run(object(), None, 90, dry_run=False, yes=True, out=lines.append)) == 0
    assert calls == [True, True, True, False]
    small = history.estimate(3)["usd"]
    assert 0 < small < history.ESTIMATE_GATE_USD


# ---------- The worker: history is quiet ----------

@pytest.fixture
def worker(monkeypatch):
    rows = {}
    statuses = {}

    async def setup(c):
        return None

    async def enqueue(c, provider, external_id, meeting=None, title="", url="", history=False):
        rows.setdefault((provider, external_id), {"history": history})
        return True

    async def set_status(c, provider, external_id, status, **fields):
        statuses[(provider, external_id)] = (status, fields)

    async def queued(c, limit=5, history=False):
        return [{"provider": p, "external_id": x, "sealed": None, "recording_url": "", "mode": r.get("mode", "score")}
                for (p, x), r in rows.items() if r.get("queued") and history][:limit]

    async def backlog(c):
        return sum(1 for r in rows.values() if r.get("queued"))

    async def room(c, need):
        return True

    async def save(c, provider, external_id, m):
        return True

    async def spend(c, rows_, now=None):
        return 0.0
    for name, fn in (("setup", setup), ("enqueue", enqueue), ("set_status", set_status), ("queued", queued),
                     ("history_backlog", backlog), ("record_history_spend", spend)):
        monkeypatch.setattr(post_call, name, fn)
    monkeypatch.setattr(transcripts, "save_quietly", save)

    async def no_delivery(*a, **k):
        raise AssertionError("history must not deliver a report")
    monkeypatch.setattr(post_call, "deliver", no_delivery)
    from app import notifications

    async def no_note(*a, **k):
        raise AssertionError("history must not create a notification")
    monkeypatch.setattr(notifications, "from_recording", no_note)
    return rows, statuses, room


def test_history_items_are_scored_without_telegram_push_or_notifications(worker):
    rows, statuses, room = worker
    rows[("fathom", "77")] = {"queued": True, "history": True}
    seen = []

    def process(m, history=False, refresh_coach=True, reanalyse=False):
        seen.append((history, refresh_coach, reanalyse))
        return {"status": "ready", "interview_id": "fathom:77", "report": None, "spend_rows": []}

    async def fetch(c, provider, external_id):
        return meeting()
    status = run(post_call.run_history(object(), fetch=fetch, process=process, room=room))
    assert status == "history" and statuses[("fathom", "77")][0] == "history"
    assert seen == [(True, True, False)]  # Last in the backlog: the coach read is refreshed.


def test_history_waits_for_the_month_cap_and_explains_a_missing_transcript(worker):
    rows, statuses, _ = worker
    rows[("fireflies", "ff1")] = {"queued": True, "history": True, "mode": "reanalyse"}

    async def full(c, need):
        return False

    async def nothing(c, provider, external_id):
        return None
    assert run(post_call.run_history(object(), fetch=nothing, room=full)) == "month_cap"
    assert statuses == {}

    async def room(c, need):
        return True
    assert run(post_call.run_history(object(), fetch=nothing, room=room)) == "failed"
    assert statuses[("fireflies", "ff1")] == ("failed", {"detail": "Couldn't read this recording's transcript from Fireflies."})


def test_reanalyse_items_reach_the_reanalysis(worker):
    rows, statuses, room = worker
    rows[("fathom", "900")] = {"queued": True, "history": True, "mode": "reanalyse"}
    rows[("fathom", "901")] = {"queued": True, "history": True}
    seen = []

    def process(m, history=False, refresh_coach=True, reanalyse=False):
        seen.append((refresh_coach, reanalyse))
        return {"status": "ready", "interview_id": "fathom:900", "report": None}

    async def fetch(c, provider, external_id):
        return meeting(mid=external_id)
    assert run(post_call.run_history(object(), fetch=fetch, process=process, room=room)) == "history"
    assert seen == [(False, True)]  # Not the last one: the coach read waits.


def test_profile_reanalyse_keeps_the_entry_and_adds_full_cues(monkeypatch, tmp_path):
    from app.profile import post_call as profile_post_call
    from app.profile.config import Settings
    perf = {"interviews": [{"interview_id": "fathom:77", "company": "Orbit", "round_type": "technical",
                            "outcome": "advanced", "date": "2026-09-23", "impression": {"coverage": "derived"}}]}
    path = tmp_path / "performance.json"
    path.write_text(json.dumps(perf))
    cfg = Settings(path, path, path, path, tmp_path / "p.csv", path, tmp_path, "Sam Candidate", "h", "s")
    written = {}
    monkeypatch.setattr(profile_post_call.rubric, "score_interview",
                        lambda m, a, llm, model, cand: {"interview_id": "fathom:77", "overall_score": 7,
                                                        "company": None, "date": "2026-09-01"})
    monkeypatch.setattr(profile_post_call.impressions, "analyze",
                        lambda m, llm, model, cand: {"coverage": "transcript", "cues": [{"kind": "x"}]})

    def add(cfg_, scored, llm, comp, refresh):
        written.update(scored, refresh=refresh)
        return True, "ok"
    monkeypatch.setattr(profile_post_call, "add_to_performance", add)
    out = profile_post_call.reanalyse(meeting(), cfg, object(), None, refresh_coach=False)
    assert out["status"] == "ready" and out["report"] is None
    assert written["impression"]["coverage"] == "transcript" and written["overall_score"] == 7
    assert (written["company"], written["outcome"], written["date"]) == ("Orbit", "advanced", "2026-09-23")
    assert written["refresh"] is False


# ---------- Transcripts: encrypted, owner only, deleted with the recording ----------

def test_transcript_round_trip_is_encrypted_and_never_logged(token, caplog):
    caplog.set_level(logging.DEBUG)
    conn = FakeConn()
    assert run(transcripts.save(conn, "fathom", "77", meeting())) is True
    sealed = conn.transcripts[("fathom", "77")]
    assert SECRET_LINE not in sealed and "Ana Ruiz" not in sealed
    data = run(transcripts.load(conn, "fathom", "77"))
    assert data["turns"][1] == {"speaker": "Sam Candidate", "text": SECRET_LINE, "start": 110.5}
    assert run(transcripts.load(conn, "fathom", "77", key=Fernet.generate_key().decode())) is None
    view = transcripts.view(data, {"sam candidate", "sam"})
    assert [t["who"] for t in view] == ["Ana Ruiz", "You"] and view[0]["clock"] == "1:35"
    text = transcripts.as_text(data, {"sam"})
    assert "[1:50] You: " + SECRET_LINE in text
    run(transcripts.delete(conn, "fathom", "77"))
    assert run(transcripts.load(conn, "fathom", "77")) is None
    assert SECRET_LINE not in caplog.text and "Ana Ruiz" not in caplog.text


def test_no_key_no_transcript(monkeypatch):
    monkeypatch.delenv("VALR_TOKEN_KEY", raising=False)
    conn = FakeConn()
    assert run(transcripts.save(conn, "fathom", "77", meeting())) is False and conn.transcripts == {}


def test_transcript_route_is_owner_only_and_renders_turns(client, monkeypatch, caplog):  # noqa: F811
    from app.auth import gate
    from app.notetakers import routes
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("VALR_CANDIDATE_NAME", "Sam Candidate")
    stored = {"title": "Orbit round", "turns": transcripts.turns_of(meeting())}

    async def load(c, provider, external_id, key=None):
        return stored if (provider, external_id) == ("fathom", "77") else None
    monkeypatch.setattr(transcripts, "load", load)
    assert gate.classify("/valr/api/recording/transcript") == "owner"
    assert client.get("/valr/api/recording/transcript?id=fathom:77").status_code == 403
    assert client.get("/valr/api/recording/transcript?id=fathom:77",
                      headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    body = client.get("/valr/api/recording/transcript?id=fathom:77", headers=GOOD).json()
    assert [t["who"] for t in body["turns"]] == ["Ana Ruiz", "You"] and body["turns"][0]["seconds"] == 95
    txt = client.get("/valr/api/recording/transcript.txt?id=fathom:77", headers=GOOD)
    assert txt.status_code == 200 and "attachment" in txt.headers["content-disposition"]
    assert SECRET_LINE in txt.text
    assert client.get("/valr/api/recording/transcript.txt?id=fathom:77").status_code == 403
    client.store.gone[("fathom", "77")] = "hidden"  # Hidden: kept, not shown.
    assert client.get("/valr/api/recording/transcript?id=fathom:77", headers=GOOD).status_code == 404
    assert SECRET_LINE not in caplog.text
    assert routes is not None


def test_past_page_has_the_folded_transcript_section(client):  # noqa: F811
    page = client.get("/valr/interviews/past/fathom-1005", headers=GOOD).text
    assert 'data-transcript-id="fathom:1005"' in page and "Find in transcript" in page
    assert "Download transcript (.txt)" in page and '<details class="fold" id="transcript"' in page


def test_deleting_a_phone_recording_deletes_its_transcript(monkeypatch, token):
    pytest.importorskip("fastapi")  # The route module: the app's venv only.
    from app import phone_recordings
    from app.notetakers import routes
    fake = FakeStore().install(monkeypatch)
    conn = FakeConn()
    run(transcripts.save(conn, "phone", "u1", meeting("phone", "u1")))
    run(transcripts.save(conn, "fathom", "77", meeting()))

    async def nothing(*a, **k):
        return None

    async def exists(c, table):
        return False
    monkeypatch.setattr(phone_recordings, "setup", nothing)
    monkeypatch.setattr(phone_recordings, "drop_upload", nothing)
    monkeypatch.setattr(routes, "_exists", exists)
    run(routes.remove(conn, "phone", "u1", "delete"))
    assert ("phone", "u1") not in conn.transcripts and ("phone", "u1") in fake.gone
    run(routes.remove(conn, "fathom", "77", "hide"))
    assert ("fathom", "77") in conn.transcripts  # Hide keeps it.


def test_every_evaluated_meeting_keeps_its_transcript(monkeypatch, token):
    conn = FakeConn()

    async def nothing(*a, **k):
        return True
    monkeypatch.setattr(post_call, "enqueue", nothing)
    monkeypatch.setattr(post_call, "set_status", nothing)

    def process(m):
        return {"status": "not_interview"}
    run(post_call.on_new_interview(meeting("phone", "u2"), connection=conn, process=process))
    assert run(transcripts.load(conn, "phone", "u2"))["turns"][1]["text"] == SECRET_LINE


# ---------- Calendar history: "No recording" entries ----------

def test_calendar_interviews_without_a_recording_are_kept_and_listed(monkeypatch):
    from app.interview_types import Person, SlotEvent
    FakeStore().install(monkeypatch)

    async def by_event(c, key):
        return None
    monkeypatch.setattr(store, "by_event", by_event)
    conn = FakeConn()
    events = [
        SlotEvent("ev1", "Orbit Labs - technical round", "2026-09-10T09:00:00+00:00", "2026-09-10T10:00:00+00:00",
                  attendees=[Person("ana@orbit.example", "Ana Ruiz"), Person("me@example.com", "Me", is_self=True)],
                  company="Orbit", company_domain="orbit.example"),
        SlotEvent("ev2", "Dentist", "2026-09-11T09:00:00+00:00", "2026-09-11T10:00:00+00:00"),
        SlotEvent("ev3", "Future interview round", "2026-11-11T09:00:00+00:00", "2026-11-11T10:00:00+00:00",
                  company="Orbit"),
    ]
    first = run(history.import_calendar(conn, events, {"me@example.com"}, now=NOW))
    again = run(history.import_calendar(conn, events, {"me@example.com"}, now=NOW))
    assert first == {"events": 3, "interviews": 1, "with_recording": 0, "no_recording": 1, "new": 1}
    assert again["new"] == 0
    stored = conn.calendar["ev1"]
    assert "ana@orbit.example" not in json.dumps([str(v) for v in stored])
    entries = run(history.calendar_entries(conn, NOW - timedelta(days=365), NOW))
    assert entries[0]["id"] == "calendar:ev1" and entries[0]["review"] == "no_recording"
    assert entries[0]["company"] == "Orbit" and entries[0]["interviewers"] == ["Ana"]


def test_past_interviews_show_no_recording_entries(client, monkeypatch):  # noqa: F811
    async def entries(c, start, end):
        return [{"id": "calendar:ev1", "recording_only": True, "no_recording": True, "date": "2026-09-10T09:00:00+00:00",
                 "title": "Orbit Labs - technical round", "company": "Quasarly", "round": "", "interviewers": ["Ana"],
                 "provider": "calendar", "review": "no_recording", "overall": None}]
    monkeypatch.setattr(history, "calendar_entries", entries)
    page = client.get("/valr/interviews/past", headers=GOOD).text
    assert "Quasarly" in page and "No recording" in page
    one = client.get("/valr/interviews/past/calendar-ev1", headers=GOOD).text
    assert "No recording of this interview" in one and "data-transcript-id" not in one


# ---------- Fireflies playback ----------

def ff_media(video="", audio="", host="cdn.fireflies.ai"):
    def handler(request):
        return httpx.Response(200, json={"data": {"transcript": {
            "id": "ff1", "video_url": f"https://{host}/v.mp4?sig=x" if video else None,
            "audio_url": f"https://{host}/a.mp3?sig=x" if audio else None}}})
    return handler


def resolve_ff(handler, token_key, refresh=False):
    async def go():
        async with mock(handler) as http:
            return await media.resolve(object(), "fireflies", "ff1", http, FAKE_KEY, token_key, now=NOW, refresh=refresh)
    return run(go())


def test_fireflies_plays_video_else_audio_and_caches_it(monkeypatch):
    fake = FakeStore().install(monkeypatch)
    fake.clock = lambda: NOW
    key = Fernet.generate_key().decode()
    out = resolve_ff(ff_media(video=True, audio=True), key)
    assert out["status"] == "completed" and out["content_type"] == "video/mp4" and "v.mp4" in out["url"]
    assert "v.mp4" not in json.dumps(str(fake.media[("fireflies", "ff1")]["url"]))  # Sealed.

    def fail(request):
        raise AssertionError("a cached URL must be reused")
    assert resolve_ff(fail, key)["url"] == out["url"]
    fake.media.clear()
    audio = resolve_ff(ff_media(audio=True), key)
    assert audio["status"] == "completed" and audio["content_type"] == "audio/mpeg"


def test_fireflies_without_media_or_on_a_strange_host(monkeypatch):
    FakeStore().install(monkeypatch)
    key = Fernet.generate_key().decode()
    none = resolve_ff(ff_media(), key)
    assert none["status"] == "unavailable" and "Pro plan" in none["message"]
    odd = resolve_ff(ff_media(video=True, host="evil.example"), key, refresh=True)
    assert odd["status"] == "failed"
    assert run(media.resolve(object(), "fireflies", "ff1", None, None, key))["status"] == "unavailable"
