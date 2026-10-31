"""Every workspace on the web side (app/workspaces.py): connecting Google stores the grant in the
signed-in user's own workspace and an OAuth state cannot cross workspaces; the Gmail and Calendar
pushes reach the right workspace; a guest's pages show none of the owner's phone line, devices or
data; a guest's Delete my data wipes only theirs, conversations and schedules included. Every
address, name and number is made up."""
import asyncio
import base64
import io
import json
import time
import zipfile
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi", reason="the pages run in the app's venv")
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import (auth, calendar_sync, google_oauth, interviews, leads_web, owner, research, settings_web,  # noqa: E402
                 tenant, valr_dashboard, valr_hooks, valr_view, workspaces)
from app.auth import core, gate  # noqa: E402
from app.valr_ui import routes  # noqa: E402
from tests.valr_sample import sample_view  # noqa: E402

OWNER = "owner@example.com"
GUEST = "guest@elsewhere.example"
GUEST_ID = tenant.user_id(GUEST, frozenset({OWNER}))
KEY = Fernet.generate_key().decode()


@pytest.fixture
def who(monkeypatch):
    """Sign in as `who.email` (a VALR session), the owner unless a test says otherwise."""
    box = {"email": OWNER}

    async def identify(connection):
        return core.Identity(box["email"], "session")
    monkeypatch.setattr(gate, "identify", identify)
    monkeypatch.setattr(auth, "identify", identify)
    monkeypatch.setenv("VALR_OWNER_EMAILS", OWNER)
    monkeypatch.delenv("VALR_ADMIN_EMAILS", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("VALR_TOKEN_KEY", KEY)
    monkeypatch.setenv("VALR_GMAIL_ACCOUNT", "owner.mail@example.com")
    owner.reset()
    yield box
    owner.reset()


def client():
    from app.main import app
    return TestClient(app, base_url="https://valr.example", follow_redirects=False)


class Rows:
    """A pool whose connections answer per workspace: {(user, sql fragment): rows}; records
    every statement with the scope it ran in."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.log: list[tuple[str | None, str, tuple]] = []

    def connection(self):
        pool = self

        class Connection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, sql, params=()):
                pool.log.append((tenant.get(), sql, params))
                rows = next((value for (user, part), value in pool.answers.items()
                             if user == tenant.get() and part in sql), [])
                if "to_regclass" in sql:
                    rows = [(name,) for name in params[0]]

                class Cursor:
                    async def fetchone(self):
                        return rows[0] if rows else None

                    async def fetchall(self):
                        return rows
                return Cursor()

            def transaction(self):
                class Transaction:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *exc):
                        return False
                return Transaction()

        class Context:
            async def __aenter__(self):
                return Connection()

            async def __aexit__(self, *exc):
                return False
        return Context()


# Connecting Google.

def test_the_oauth_state_names_its_workspace_and_cannot_be_forged():
    state = google_oauth.make_state(KEY, user=GUEST_ID)
    assert google_oauth.state_user(KEY, state) == GUEST_ID
    body, mac = state.split(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    data["u"] = tenant.OWNER  # Claim the owner's workspace with the guest's signature.
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=") + "." + mac
    assert google_oauth.state_user(KEY, forged) is None
    assert google_oauth.state_user(KEY, google_oauth.make_state(KEY, now=time.time() - 3600, user=GUEST_ID)) is None
    # A state from before workspaces carried no user: the owner's, as it was.
    old = json.dumps({"n": "x", "exp": int(time.time()) + 60}).encode()
    import hashlib
    import hmac
    sig = hmac.new(google_oauth._state_key(KEY), old, hashlib.sha256).digest()
    legacy = (base64.urlsafe_b64encode(old).decode().rstrip("=") + "."
              + base64.urlsafe_b64encode(sig).decode().rstrip("="))
    assert google_oauth.state_user(KEY, legacy) == tenant.OWNER


def test_a_guest_connects_their_own_google_into_their_own_workspace(who, monkeypatch):
    stored, synced = [], []

    async def exchange(settings, code, http):
        return "refresh-1", " ".join(google_oauth.SCOPES), GUEST

    async def store(connection, settings, account, refresh, scope):
        stored.append((tenant.get(), account))

    async def nothing(connection):
        return None

    async def sync_now(reason):
        synced.append((reason, tenant.get()))
        return True
    monkeypatch.setattr(google_oauth, "exchange_code", exchange)
    monkeypatch.setattr(google_oauth, "store", store)
    monkeypatch.setattr(google_oauth, "setup", nothing)
    monkeypatch.setattr(interviews, "sync_now", sync_now)
    monkeypatch.setattr(research, "pool", Rows())
    who["email"] = GUEST
    web = client()
    start = web.get("/oauth/google/start")
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["login_hint"] == [GUEST]  # Not the owner's VALR_GMAIL_ACCOUNT.
    assert google_oauth.state_user(KEY, query["state"][0]) == GUEST_ID
    done = web.get("/oauth/google/callback", params={"code": "c", "state": query["state"][0]})
    assert done.headers["location"] == "/valr/settings?connected=google#google"
    assert stored == [(GUEST_ID, GUEST)] and synced == [("connected", GUEST_ID)]


def test_an_oauth_state_cannot_attach_a_token_to_another_workspace(who, monkeypatch):
    stored = []

    async def exchange(settings, code, http):
        return "refresh-1", " ".join(google_oauth.SCOPES), OTHER_ACCOUNT

    async def store(connection, settings, account, refresh, scope):
        stored.append((tenant.get(), account))
    OTHER_ACCOUNT = "owner.mail@example.com"
    monkeypatch.setattr(google_oauth, "exchange_code", exchange)
    monkeypatch.setattr(google_oauth, "store", store)
    monkeypatch.setattr(research, "pool", Rows())
    web = client()
    # The owner starts a connect; a guest's browser finishes it (a link passed on): refused.
    owner_state = parse_qs(urlsplit(web.get("/oauth/google/start").headers["location"]).query)["state"][0]
    who["email"] = GUEST
    back = web.get("/oauth/google/callback", params={"code": "c", "state": owner_state})
    assert back.headers["location"] == "/valr/settings?google_error=expired#google"
    # And the other way: a guest's state finished in the owner's browser.
    guest_state = parse_qs(urlsplit(web.get("/oauth/google/start").headers["location"]).query)["state"][0]
    who["email"] = OWNER
    back = web.get("/oauth/google/callback", params={"code": "c", "state": guest_state})
    assert back.headers["location"] == "/valr/settings?google_error=expired#google"
    assert stored == []


def test_a_guest_cannot_connect_someone_elses_google_account(who, monkeypatch):
    stored = []

    async def exchange(settings, code, http):
        return "refresh-1", " ".join(google_oauth.SCOPES), "owner.mail@example.com"

    async def store(*args):
        stored.append(tenant.get())
    monkeypatch.setattr(google_oauth, "exchange_code", exchange)
    monkeypatch.setattr(google_oauth, "store", store)
    monkeypatch.setattr(research, "pool", Rows())
    who["email"] = GUEST
    web = client()
    state = parse_qs(urlsplit(web.get("/oauth/google/start").headers["location"]).query)["state"][0]
    back = web.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert back.headers["location"] == "/valr/settings?google_error=account#google" and stored == []


def test_guests_may_act_in_their_own_workspace_but_not_start_a_browser_call(who, monkeypatch):
    calls = []

    async def sync_now(reason):
        calls.append(("calendar", tenant.get()))
        return True

    async def start_sync(reason, history_id=0):
        calls.append(("gmail", tenant.get()))
        return True
    monkeypatch.setattr(interviews, "sync_now", sync_now)
    monkeypatch.setattr(leads_web, "start_sync", start_sync)
    who["email"] = GUEST
    headers = {"Content-Type": "application/json", "X-CSRF-Token": valr_dashboard.csrf_token(GUEST),
               "Origin": "https://valr.example"}
    web = client()
    assert web.post("/valr/api/google/sync", content="{}", headers=headers).status_code == 200
    assert calls == [("calendar", GUEST_ID), ("gmail", GUEST_ID)]
    offer = web.post("/webrtc/offer", json={"type": "offer", "sdp": "x", "echo_cancelled": True}, headers=headers)
    assert offer.status_code == 403  # Browser calls stay the owner's.
    for path in ("/valr/api/call-now", "/valr/api/phone/token"):
        assert web.post(path, content="{}", headers=headers).status_code == 403, path


# The pushes.

def push(email, history_id=7):
    data = base64.b64encode(json.dumps({"emailAddress": email, "historyId": history_id}).encode()).decode()
    return json.dumps({"subscription": "projects/p/subscriptions/s", "message": {"data": data}})


@pytest.fixture
def gmail_hook(who, monkeypatch):
    started = []

    class Verifier:
        async def verify(self, header):
            return {"email": "push@example.iam.gserviceaccount.com"}

    async def start_sync(reason, history_id=0):
        started.append((reason, history_id, tenant.get()))
        return True
    monkeypatch.setattr(leads_web, "push_verifier", lambda: Verifier())
    monkeypatch.setattr(leads_web, "start_sync", start_sync)
    monkeypatch.setenv("VALR_LEADS", "1")
    monkeypatch.setenv("VALR_GMAIL_SUBSCRIPTION", "projects/p/subscriptions/s")
    pool = Rows({(GUEST_ID, "FROM oauth_tokens"): [(GUEST,)]})
    monkeypatch.setattr(research, "pool", pool)
    return started, pool


def test_a_gmail_push_starts_the_sync_of_the_workspace_that_connected_that_account(gmail_hook):
    started, pool = gmail_hook
    web = client()
    assert "/hooks/gmail" not in gate.OWNER_HOOKS and "/hooks/gcal" not in gate.OWNER_HOOKS
    for body in (push("owner.mail@example.com", 5), push(GUEST.upper(), 6), push("stranger@nowhere.example")):
        assert web.post("/hooks/gmail", content=body, headers={"Authorization": "Bearer t"}).status_code == 204
    assert started == [("push", 5, tenant.OWNER), ("push", 6, GUEST_ID)]  # The stranger's is ignored.
    # The lookup asked only the address's own workspace, in its scope.
    assert {scope for scope, sql, _ in pool.log if "oauth_tokens" in sql} == {
        GUEST_ID, tenant.user_id("stranger@nowhere.example", frozenset({OWNER}))}


def test_a_gmail_push_for_an_account_a_guest_no_longer_has_is_ignored(gmail_hook, monkeypatch):
    started, _ = gmail_hook
    monkeypatch.setattr(research, "pool", Rows())  # Disconnected: no token row.
    assert client().post("/hooks/gmail", content=push(GUEST), headers={"Authorization": "Bearer t"}).status_code == 204
    assert started == []


def test_a_calendar_push_reaches_the_workspace_its_channel_belongs_to(who, monkeypatch):
    synced = []
    channel = f"valr-{GUEST_ID}-0123456789abcdef"
    states = {GUEST_ID: {"channel_id": channel, "channel_token": "tok", "resource_id": "res"},
              tenant.OWNER: {"channel_id": "valr-00112233aabbccdd", "channel_token": "otok", "resource_id": "ores"}}

    async def load_state(connection):
        return states.get(tenant.get(), {})

    async def nothing(connection):
        return None

    async def sync_now(reason):
        synced.append(tenant.get())
        return True
    monkeypatch.setattr(calendar_sync, "load_state", load_state)
    monkeypatch.setattr(calendar_sync, "setup", nothing)
    monkeypatch.setattr(interviews, "sync_now", sync_now)
    monkeypatch.setattr(research, "pool", Rows())
    web = client()
    good = {"x-goog-channel-id": channel, "x-goog-channel-token": "tok", "x-goog-resource-id": "res",
            "x-goog-resource-state": "exists"}
    assert web.post("/hooks/gcal", headers=good).status_code == 200
    assert web.post("/hooks/gcal", headers={**good, "x-goog-channel-token": "otok"}).status_code == 403
    mine = {"x-goog-channel-id": "valr-00112233aabbccdd", "x-goog-channel-token": "otok",
            "x-goog-resource-id": "ores", "x-goog-resource-state": "exists"}
    assert web.post("/hooks/gcal", headers=mine).status_code == 200
    # A guest's channel id with the owner's secrets is checked against the guest's state: refused.
    assert web.post("/hooks/gcal", headers={**mine, "x-goog-channel-id": channel}).status_code == 403
    assert synced == [GUEST_ID, tenant.OWNER]
    assert valr_hooks is not None


# A guest's pages.

# The owner's phone line, number, devices and phone-only controls, as the pages mark them.
OWNER_SECRETS = ('id="dial-in"', 'href="tel:', "/valr/downloads/mac", "phone-help", "Call my phone",
                 'data-action="callnow"', "data-call-split", "data-phone-edit", "data-phone-action",
                 'data-mode="call"', 'id="pf-phone-title"')


def guest_view():
    view = sample_view()
    view.update(guest=True, dials={}, performance=valr_view.performance(None), pay_target=None, owner_name="")
    view["setup"] = {"phone_on_file": False, "test_call_done": False, "valr_number": ""}
    return view


def test_a_guests_pages_show_empty_states_not_the_owners_line_devices_or_data(who, monkeypatch):
    async def collect(pool, now=None):
        return guest_view()

    async def notes():
        return {"items": [], "unread": 0, "leads": {}}

    async def leads_():
        return {"leads": [], "contacts": [], "links": [], "stages": [], "generated_at": None, "gmail": {}}

    async def nothing(*args, **kwargs):
        return None
    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(routes, "load_notes", notes)
    monkeypatch.setattr(routes, "load_leads", leads_)
    monkeypatch.setattr(owner, "refresh", nothing)
    monkeypatch.setattr(research, "pool", Rows())
    monkeypatch.setenv("VALR_PUBLIC_NUMBER", "+12288888007")
    who["email"] = GUEST
    web = client()
    settings = web.get("/valr/settings").text
    for secret in OWNER_SECRETS:
        assert secret not in settings, secret
    assert "available in your workspace" in settings and "Phone call uploads aren" in settings
    assert 'id="google"' in settings and 'id="notetakers"' in settings  # Theirs to connect.
    for path in ("/valr", "/valr/profile", "/valr/progress", "/valr/interviews"):
        page = web.get(path)
        assert page.status_code == 200, path
        for secret in OWNER_SECRETS:
            assert secret not in page.text, (path, secret)
    who["email"] = OWNER  # The same view for the owner (no guest flag) keeps his line and buttons.

    async def owner_collect(pool, now=None):
        return sample_view()
    monkeypatch.setattr(valr_view, "collect", owner_collect)
    mine = web.get("/valr/settings").text
    for marker in ('id="dial-in"', 'href="tel:', "data-call-split", "data-phone-action", 'data-mode="call"'):
        assert marker in mine, marker  # The markers are real: the owner's page has each.


# Delete my data, as a guest.

def test_a_guests_delete_wipes_their_workspace_conversations_and_schedules_only(who, monkeypatch):
    dropped = []
    pool = Rows({(GUEST_ID, "FROM call_summaries"): [("conv-1",), ("conv-2",)]})

    async def disconnect(connection, settings, http):
        pool.log.append((tenant.get(), "revoke", ()))
        return {"revoked": True, "stopped": True}

    async def nothing(connection):
        return None

    async def drop(client, user=None):
        dropped.append(tenant.get())
        return 3

    class Temporal:
        pass

    async def temporal():
        return Temporal()
    from app import callbacks
    monkeypatch.setattr(settings_web, "pool", lambda: pool)
    monkeypatch.setattr(google_oauth, "disconnect", disconnect)
    monkeypatch.setattr(google_oauth, "setup", nothing)
    monkeypatch.setattr(workspaces, "drop_schedules", drop)
    monkeypatch.setattr(callbacks, "temporal", temporal)
    monkeypatch.setattr(settings_web, "scorecard_files", lambda: (_ for _ in ()).throw(AssertionError("owner files")))
    who["email"] = GUEST
    headers = {"Content-Type": "application/json", "X-CSRF-Token": valr_dashboard.csrf_token(GUEST),
               "Origin": "https://valr.example"}
    done = client().post("/valr/api/data/delete", content='{"confirm": "DELETE"}', headers=headers)
    assert done.status_code == 200 and "Max-Age=0" in done.headers["set-cookie"]
    sql = [(scope, text, params) for scope, text, params in pool.log]
    assert all(scope == GUEST_ID for scope, _, _ in sql)
    assert not any(text.startswith("TRUNCATE") for _, text, _ in sql)  # Never everyone's tables.
    assert [text for _, text, _params in sql][0] == "revoke"  # Google is revoked first.
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert (GUEST_ID, f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (["conv-1", "conv-2"],)) in sql
    assert (GUEST_ID, "DELETE FROM valr_users WHERE id = %s", (GUEST_ID,)) in sql
    assert (GUEST_ID, "DELETE FROM valr_sessions WHERE lower(email) = %s", (GUEST,)) in sql  # Theirs only.
    assert dropped == [GUEST_ID]


def test_a_guests_export_has_no_owner_files(who, monkeypatch):
    pool = Rows()
    monkeypatch.setattr(settings_web, "pool", lambda: pool)

    async def rows(connection):
        return {"notifications": []}
    monkeypatch.setattr(settings_web, "export_rows", rows)
    monkeypatch.setattr(settings_web, "scorecard_files", lambda: (_ for _ in ()).throw(AssertionError("owner files")))
    who["email"] = GUEST
    archive = zipfile.ZipFile(io.BytesIO(client().get("/valr/api/export").content))
    assert not any(name.startswith("scorecards/") for name in archive.namelist())


def test_drop_schedules_never_touches_the_owners():
    deleted = []

    class Handle:
        def __init__(self, name):
            self.name = name

        async def delete(self):
            deleted.append(self.name)

    class Client:
        def get_schedule_handle(self, name):
            return Handle(name)
    assert asyncio.run(workspaces.drop_schedules(Client(), tenant.OWNER)) == 0 and deleted == []
    assert asyncio.run(workspaces.drop_schedules(Client(), GUEST_ID)) == 3
    assert all(name.endswith(GUEST_ID) for name in deleted)
