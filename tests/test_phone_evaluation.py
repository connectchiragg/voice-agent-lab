"""Phone recordings and the coach's evaluation: the hand-off works in the research worker (no
groq, no fastapi there), the app imports none of the worker-only packages, re-evaluation picks
up recordings whose hand-off failed, and every stored recording shows in Past interviews (list,
detail page, the notification's link) and can be deleted (phone) or hidden (notetakers).
Names are fictional."""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import phone_recordings as phone

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = "1d873eb56328d8cd"
MEETING = {"source": "phone", "id": UPLOAD, "title": "Phone call with Maheku", "date": "2026-10-17T09:00:00Z",
           "duration": 5.0, "participants": ["Maheku", "Sam"], "vendor_summary": None, "scheduled_duration": None,
           "turns": [{"speaker": "Maheku", "text": "Hello?", "start": 0.0}, {"speaker": "Sam", "text": "Hi.", "start": 2.0}]}


def run(coro):
    return asyncio.run(coro)


def python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)


# Blocks imports the way a missing package fails in the other image.
BLOCKER = """
import sys
class Block:
    def __init__(self, names): self.names = names
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.names or name in self.names:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None
sys.meta_path.insert(0, Block(set(BLOCKED)))
"""


# ---------- The worker context ----------

WORKER_HAND_OFF = """
import asyncio, json, os, sys
import psycopg
os.environ.setdefault("POSTGRES_PASSWORD", "test")  # The worker's env has it.

class Cursor:
    def __init__(self, row): self.row = row
    async def fetchone(self): return self.row
    async def fetchall(self): return []

class Conn:
    def __init__(self): self.sql = []
    async def execute(self, sql, params=None):
        self.sql.append(sql)
        return Cursor((1,) if "RETURNING" in sql else None)
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False

opened = []
async def connect(*args, **kwargs):
    opened.append(Conn())
    return opened[-1]
psycopg.AsyncConnection.connect = connect

from app import phone_recordings
from app.notetakers import pipeline

async def main():
    mine = Conn()
    bound = await pipeline.hand_off(mine, "phone", "abc", MEETING, phone_recordings.default_hook(mine))
    resolved = await pipeline.hand_off(Conn(), "phone", "def", MEETING)  # VALR_EVAL_HOOK's, as in production
    queued_here = any("INSERT INTO post_call_reports" in s for s in mine.sql)
    queued_there = any("INSERT INTO post_call_reports" in s for c in opened for s in c.sql)
    print(json.dumps({"bound": bound, "resolved": resolved, "queued_here": queued_here,
                      "queued_there": queued_there, "research": "app.research" in sys.modules,
                      "groq": "groq" in sys.modules, "fastapi": "fastapi" in sys.modules}))
asyncio.run(main())
"""


def test_the_phone_hand_off_queues_the_evaluation_in_the_worker_without_the_web_stack():
    """The live bug: in the research worker, the hook imported app.research -> groq (app-only)."""
    code = (f"BLOCKED = ['groq', 'fastapi', 'av', 'starlette']\n{BLOCKER}\n"
            f"MEETING = {MEETING!r}\n{WORKER_HAND_OFF}")
    done = python(code)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out == {"bound": "handed_off", "resolved": "handed_off", "queued_here": True, "queued_there": True,
                   "research": False, "groq": False, "fastapi": False}


def test_the_processor_hands_phone_recordings_to_the_default_hook_on_its_connection(monkeypatch):
    from app.notetakers import pipeline
    from app.profile import notetaker_hook
    monkeypatch.delenv(pipeline.HOOK_ENV, raising=False)
    seen = []

    async def evaluate(provider, external_id, meeting, connect=None, fetch=None, connection=None):
        seen.append((provider, external_id, connection))
        return "queued for the post-call report"

    monkeypatch.setattr(notetaker_hook, "evaluate_recording", evaluate)
    conn = object()
    hook = phone.default_hook(conn)
    assert run(hook("phone", UPLOAD, MEETING)) == "queued for the post-call report"
    assert seen == [("phone", UPLOAD, conn)]
    monkeypatch.setenv(pipeline.HOOK_ENV, "somewhere.else:evaluate")
    assert phone.default_hook(conn) is None  # A configured hook is resolved by the pipeline.


def test_a_failing_hook_logs_the_exception_class_and_the_missing_module_only(monkeypatch, caplog):
    from app.notetakers import pipeline, store
    from tests.test_notetakers import FakeStore
    notes = FakeStore().install(monkeypatch)
    run(store.upsert_recording(None, {"provider": "phone", "external_id": UPLOAD}, "upload"))

    async def broken(provider, external_id, meeting):
        raise ModuleNotFoundError("No module named 'groq' while reading Hello? from Maheku", name="groq")

    with caplog.at_level("WARNING"):
        assert run(pipeline.hand_off(None, "phone", UPLOAD, MEETING, broken)) == "failed"
    assert "ModuleNotFoundError: groq" in caplog.text and "Maheku" not in caplog.text and "Hello" not in caplog.text
    assert notes.recordings[("phone", UPLOAD)]["eval_status"] == "failed"


# ---------- The app context ----------

WORKER_ONLY = {"app.interview_agents", "app.research_agents", "app.llm_gateway"}  # crewai / anthropic


def test_the_app_imports_no_worker_only_package():
    """With the app's requirements: every module but the worker-only ones imports, and the
    app's entry point and the upload/evaluation path pull in neither crewai nor anthropic."""
    code = ("BLOCKED = ['crewai', 'anthropic']\n" + BLOCKER + """
import importlib, json, pkgutil, app
bad = []
for m in pkgutil.walk_packages(app.__path__, "app."):
    try:
        importlib.import_module(m.name)
    except ModuleNotFoundError as error:
        bad.append(m.name)
import app.main, app.phone_web, app.notetakers.pipeline, app.profile.notetaker_hook, app.post_call
print(json.dumps(bad))
""")
    pytest.importorskip("fastapi")  # The app's venv.
    done = python(code)
    assert done.returncode == 0, done.stderr[-2000:]
    assert set(json.loads(done.stdout.strip().splitlines()[-1])) <= WORKER_ONLY


# ---------- Re-evaluation ----------

class Conn:
    def __init__(self, ids):
        self.ids, self.sql = ids, []

    async def execute(self, sql, params=None):
        self.sql.append((sql, params))
        ids = self.ids

        class Cursor:
            async def fetchall(self):
                return [(i,) for i in ids] if "FROM notetaker_recordings" in sql else []

            async def fetchone(self):
                return None
        return Cursor()


def test_reevaluation_hands_unevaluated_phone_recordings_to_the_evaluation_again(monkeypatch):
    from app import post_call
    from app.notetakers import pipeline
    from app.notetakers import store
    from tests.test_notetakers import FakeStore
    monkeypatch.delenv(pipeline.HOOK_ENV, raising=False)
    notes = FakeStore().install(monkeypatch)
    for upload_id, status in ((UPLOAD, "failed"), ("gone0000", "failed")):
        run(store.upsert_recording(None, {"provider": "phone", "external_id": upload_id}, "upload"))
        notes.recordings[("phone", upload_id)]["eval_status"] = status

    async def sealed(connection, upload_id):
        return MEETING if upload_id == UPLOAD else None  # The other one's transcript is gone.

    queued = []

    async def enqueue(connection, provider, external_id, meeting=None, title="", recording_url=""):
        queued.append((provider, external_id, bool(meeting)))
        return True

    async def nothing(connection):
        return None

    monkeypatch.setattr(phone, "sealed_meeting", sealed)
    monkeypatch.setattr(post_call, "enqueue", enqueue)
    monkeypatch.setattr(post_call, "setup", nothing)
    conn = Conn([UPLOAD, "gone0000"])
    assert run(phone.reevaluate(conn)) == [(UPLOAD, "handed_off"), ("gone0000", "no_transcript")]
    assert queued == [("phone", UPLOAD, True)]
    assert notes.recordings[("phone", UPLOAD)]["eval_status"] == "handed_off"
    select = next(params for sql, params in conn.sql if "FROM notetaker_recordings" in sql)
    assert select == ("phone", ["pending", "no_hook", "failed"])
    # One by id: a failed post-call report is cleared so it is queued again.
    queued.clear()
    assert run(phone.reevaluate(conn, UPLOAD)) == [(UPLOAD, "handed_off")]
    assert any("DELETE FROM post_call_reports" in sql and params == ("phone", UPLOAD) for sql, params in conn.sql)


def test_the_worker_loop_picks_up_unevaluated_recordings_once_at_start(monkeypatch):
    calls = []
    stop = asyncio.Event()

    async def reevaluate(connection, upload_id=None):
        calls.append(upload_id)
        return []

    async def nothing(*args):
        return 0

    async def run_once(self, connection, now=None):
        if len(calls) and not stop.is_set():
            self.passes = getattr(self, "passes", 0) + 1
            if self.passes >= 2:
                stop.set()
        return None

    class Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    async def connect():
        return Ctx()

    monkeypatch.setattr(phone, "reevaluate", reevaluate)
    monkeypatch.setattr(phone, "setup", nothing)
    monkeypatch.setattr(phone, "purge", nothing)
    monkeypatch.setattr(phone.Processor, "run_once", run_once)
    monkeypatch.setenv("VALR_PHONE_LOOP_S", "0.01")
    run(asyncio.wait_for(phone.loop(connect, stop), 5))
    assert calls == [None]  # Twice round the loop, one pick-up.


def test_the_post_call_queue_reads_a_phone_transcript_from_valr(monkeypatch):
    from app import post_call

    async def queued(connection, limit=5):
        return [{"provider": "phone", "external_id": UPLOAD, "sealed": None, "recording_url": ""}]

    async def sealed(connection, upload_id):
        return MEETING

    seen = []

    async def on_new(meeting, connection=None, recording_url="", process=None, bot=None):
        seen.append(meeting["id"])
        return "sent"

    def fetch(provider, external_id):
        raise AssertionError("a phone recording is never fetched from a provider")

    monkeypatch.setattr(post_call, "queued", queued)
    monkeypatch.setattr(phone, "sealed_meeting", sealed)
    monkeypatch.setattr(post_call, "on_new_interview", on_new)
    assert run(post_call.run_queue(object(), fetch=fetch)) == 1 and seen == [UPLOAD]


# ---------- Past interviews: every stored recording, its page, the notification link ----------

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from tests.test_notetakers import FakeStore
    from tests.test_phone_recordings import FakePhone
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from app import phone_web, post_call, research, valr_dashboard, valr_view
    from app.main import app
    from tests.test_notetakers import GOOD, OWNER, Pool, Verifier
    from tests.valr_sample import sample_view

    async def collect(pool, now=None):
        return sample_view()

    async def statuses(connection):
        return {}

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setattr(phone_web, "limiter", phone_web.RateLimit())
    monkeypatch.setattr(post_call, "statuses", statuses)
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")
    monkeypatch.setenv("VALR_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VALR_UPLOAD_TOKEN", "test-upload-token-0123456789abcdef")
    test_client = TestClient(app)
    test_client.phone = FakePhone().install(monkeypatch)
    test_client.notes = FakeStore().install(monkeypatch)
    test_client.good = GOOD
    start = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
    rows = {("phone", UPLOAD): {"title": "Phone call with Maheku", "interviewers": ["Maheku"], "company": ""},
            ("fathom", "77001"): {"title": "Orbit Labs: technical round", "company": "Orbit Labs"},
            ("fireflies", "01HFIREFLY9"): {"title": "Coffee chat", "company": "Nimbus"}}
    for key, row in rows.items():
        test_client.notes.recordings[key] = {"provider": key[0], "external_id": key[1], "eval_status": "handed_off",
                                             "started_at": start, **row}
    return test_client


def test_a_phone_recording_without_an_evaluation_is_listed_with_its_review_state(client):
    page = client.get("/valr/interviews/past", headers=client.good)
    assert page.status_code == 200
    html = page.text
    assert "Call with Maheku" in html and "Review pending" in html
    assert f'href="/valr/interviews/past/phone-{UPLOAD}"' in html
    assert "Orbit Labs" in html and "Nimbus" in html  # Fathom and Fireflies recordings too.


def test_a_review_that_cannot_be_made_says_so(client, monkeypatch):
    from app import post_call

    async def statuses(connection):
        return {("phone", UPLOAD): "not_interview"}

    monkeypatch.setattr(post_call, "statuses", statuses)
    html = client.get(f"/valr/interviews/past/phone-{UPLOAD}", headers=client.good).text
    assert "Review not available" in html


def test_the_phone_recording_page_has_the_player_and_a_pending_review(client):
    page = client.get(f"/valr/interviews/past/phone-{UPLOAD}", headers=client.good)
    assert page.status_code == 200
    html = page.text
    assert f'data-player="phone:{UPLOAD}"' in html and f'data-moments-id="phone:{UPLOAD}"' in html
    assert "Review pending…" in html and "Call with Maheku" in html
    assert "We couldn" in html  # The company is unknown: the page offers "Set company".
    assert f'data-recording-remove="delete" data-id="phone:{UPLOAD}"' in html and "Delete recording" in html


@pytest.mark.parametrize("provider,external_id", [("phone", UPLOAD), ("fathom", "77001"), ("fireflies", "01HFIREFLY9")])
def test_the_notification_link_opens_the_recording_page(client, monkeypatch, provider, external_id):
    from app import notifications
    notes = []

    async def add(store, note, now):
        notes.append(note)
        return 1

    monkeypatch.setattr(notifications, "add", add)
    run(notifications.from_recording(None, provider, external_id, "", "Maheku", datetime.now(timezone.utc)))
    page = client.get(notes[0]["link"], headers=client.good)
    assert page.status_code == 200 and f'data-moments-id="{provider}:{external_id}"' in page.text
    if provider != "phone":
        assert 'data-recording-remove="hide"' in page.text and "Delete recording" not in page.text


def test_a_raw_provider_link_with_other_characters_still_resolves(client):
    client.notes.recordings[("granola", "not_ab12")] = {"provider": "granola", "external_id": "not_ab12",
                                                        "eval_status": "pending", "title": "Chat",
                                                        "started_at": datetime(2026, 10, 1, tzinfo=timezone.utc)}
    assert client.get("/valr/interviews/past/granola-not_ab12", headers=client.good).status_code == 200


# ---------- Delete (phone) and Hide (notetakers) ----------

class SqlConn:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, params=None):
        self.sql.append((sql, params))

        class Cursor:
            async def fetchone(self):
                return (True,)

            async def fetchall(self):
                return []
        return Cursor()


class SqlPool:
    def __init__(self):
        self.conn = SqlConn()

    def connection(self):
        conn = self.conn

        class Context:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return Context()


def headers(client):
    from tests.test_notetakers import action_headers
    return action_headers(client)


def test_delete_removes_the_phone_recording_everywhere_and_the_file_can_be_sent_again(client, monkeypatch):
    from app import research
    from tests.test_phone_recordings import M4A, post
    first = post(client).json()
    upload_id = first["id"].split(":", 1)[1]
    client.phone.uploads[upload_id]["status"] = "done"
    client.notes.recordings[("phone", upload_id)] = {"provider": "phone", "external_id": upload_id,
                                                     "eval_status": "handed_off", "title": "Phone call",
                                                     "started_at": datetime(2026, 10, 17, tzinfo=timezone.utc)}
    assert post(client).json()["duplicate"] is True
    action = headers(client)
    refused = client.post("/valr/api/recordings/remove", json={"id": f"phone:{upload_id}", "action": "delete"},
                          headers={**client.good, "Content-Type": "application/json"})
    assert refused.status_code == 403  # No CSRF token.
    pool = SqlPool()
    monkeypatch.setattr(research, "pool", pool)
    done = client.post("/valr/api/recordings/remove", json={"id": f"phone:{upload_id}", "action": "delete"},
                       headers=action)
    assert done.status_code == 200 and done.json()["message"] == "Recording deleted."
    assert upload_id not in client.phone.uploads and not [k for k in client.phone.audio if k[0] == upload_id]
    assert ("phone", upload_id) not in client.notes.recordings
    assert client.notes.gone[("phone", upload_id)] == "deleted"
    deletes = [(sql, params) for sql, params in pool.conn.sql if sql.startswith("DELETE")]
    assert ("DELETE FROM notifications WHERE source_id = %s", (f"phone:{upload_id}",)) in deletes
    assert ("DELETE FROM post_call_reports WHERE provider = %s AND external_id = %s", ("phone", upload_id)) in deletes
    again = post(client, data=M4A).json()
    assert again["duplicate"] is False  # The same file can be sent again.


def test_hide_keeps_a_notetaker_recording_but_takes_it_off_the_list(client, monkeypatch):
    from app import research
    action = headers(client)
    pool = SqlPool()
    monkeypatch.setattr(research, "pool", pool)
    wrong = client.post("/valr/api/recordings/remove", json={"id": "fathom:77001", "action": "delete"}, headers=action)
    assert wrong.status_code == 400
    done = client.post("/valr/api/recordings/remove", json={"id": "fathom:77001", "action": "hide"}, headers=action)
    assert done.status_code == 200 and ("fathom", "77001") in client.notes.recordings
    assert client.notes.gone[("fathom", "77001")] == "hidden"
    from tests.test_notetakers import Pool
    monkeypatch.setattr(research, "pool", Pool())
    html = client.get("/valr/interviews/past", headers=client.good).text
    assert "Orbit Labs" not in html and "Call with Maheku" in html
