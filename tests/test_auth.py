"""VALR's own sign-in (app/auth): the login state and nonce, the ID token checks, owner-only
sessions, the cookie, safe redirects, same-origin, the route allowlist, the three modes and
sign-out. Google and Postgres are fakes: no network."""
import base64
import json
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip("fastapi")  # The worker's venv has no web stack: these run in the app's.
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from app import google_oauth, memory, research, valr_dashboard, valr_view
from app.auth import core, gate, routes, store
from tests.valr_sample import sample_view

OWNER = "owner@example.com"
STRANGER = "someone@elsewhere.example"
CLIENT_ID = "client-123.apps.example.com"
SITE = "https://valr.example.com"
HOST = "valr.example.com"
GOOD_ACCESS = {"Cf-Access-Jwt-Assertion": "owner"}


class FakeStore:
    """store.PostgresStore's contract, in memory, with a settable clock."""

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.states: dict[str, tuple[str, str, float]] = {}
        self.sessions: dict[str, tuple[str, float]] = {}

    async def save_state(self, state, nonce, next_path):
        self.states[core.token_hash(state)] = (nonce, next_path, self.now)

    async def take_state(self, state):
        row = self.states.pop(core.token_hash(state), None)
        if row is None or row[2] < self.now - core.STATE_TTL_S:
            return None
        return row[0], row[1]

    async def create_session(self, email):
        self.sessions = {k: v for k, v in self.sessions.items() if v[1] > self.now}
        token = core.new_token()
        self.sessions[core.token_hash(token)] = (email, self.now + core.SESSION_TTL_S)
        return token

    async def session_email(self, token):
        row = self.sessions.get(core.token_hash(token))
        return row[0] if row and row[1] > self.now else None

    async def end_session(self, token):
        self.sessions.pop(core.token_hash(token), None)


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


def id_token(**overrides) -> str:
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234",
              "email": OWNER, "email_verified": True, "exp": time.time() + 3600,
              "nonce": "the-nonce", "name": "Ada Example"} | overrides
    claims = {k: v for k, v in claims.items() if v is not None}
    part = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


class FakeGoogle:
    """Google's token endpoint: answers a code with an ID token for `email`."""

    def __init__(self) -> None:
        self.email = OWNER
        self.overrides: dict = {}
        self.requests: list[dict] = []
        self.nonce = ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == core.TOKEN_URL
        form = parse_qs(request.content.decode())
        self.requests.append(form)
        return httpx.Response(200, json={"access_token": "unused", "id_token": id_token(
            email=self.email, nonce=self.nonce, **self.overrides)})


@pytest.fixture
def env(monkeypatch):
    fake_store, google = FakeStore(), FakeGoogle()
    monkeypatch.setenv("VALR_OWNER_EMAILS", f" {OWNER.upper()} , other-owner@example.com")
    monkeypatch.delenv("VALR_ADMIN_EMAILS", raising=False)
    monkeypatch.setenv("VALR_AUTH_MODE", "both")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", f"{SITE}/oauth/google/callback")
    monkeypatch.setenv("VALR_TOKEN_KEY", "test-token-key")
    monkeypatch.setattr(store, "backend", lambda: fake_store)
    monkeypatch.setattr(routes, "http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(google.handler)))
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())

    async def collect(pool, now=None):
        return sample_view()

    async def never_store(*args, **kwargs):  # A login must never write the Gmail grant.
        raise AssertionError("google_oauth.store called during sign-in")

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(google_oauth, "store", never_store)
    return fake_store, google


@pytest.fixture
def client(env):
    from app.main import app
    return TestClient(app, base_url=SITE, follow_redirects=False)


def sign_in(client, google, email=OWNER, next_path="/valr/leads"):
    """The whole flow: /login/google, then Google's redirect back to the shared callback."""
    started = client.get("/login/google", params={"next": next_path})
    assert started.status_code == 303
    query = parse_qs(urlsplit(started.headers["location"]).query)
    google.email, google.nonce = email, query["nonce"][0]
    return client.get("/oauth/google/callback", params={"code": "code-1", "state": query["state"][0]}), query


def cookie_of(response) -> str:
    header = response.headers["set-cookie"]
    return header.split(";", 1)[0].split("=", 1)[1]


# State, nonce and replay.

def test_login_asks_google_for_identity_only_with_a_one_time_state_and_nonce(client, env):
    fake_store, google = env
    started = client.get("/login/google", params={"next": "/valr/leads"})
    url = urlsplit(started.headers["location"])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == core.AUTH_URL
    assert query["scope"] == "openid email profile" and query["prompt"] == "select_account"
    assert query["client_id"] == CLIENT_ID and query["redirect_uri"] == f"{SITE}/oauth/google/callback"
    assert "access_type" not in query and "gmail" not in started.headers["location"]
    assert query["state"].startswith("login.") and len(query["nonce"]) >= 32
    assert started.headers["cache-control"] == "no-store"
    stored = list(fake_store.states.values())
    assert len(stored) == 1 and stored[0][:2] == (query["nonce"], "/valr/leads")
    assert query["state"] not in json.dumps(list(fake_store.states))  # Stored hashed.
    second = parse_qs(urlsplit(client.get("/login/google").headers["location"]).query)
    assert second["state"] != [query["state"]] and second["nonce"] != [query["nonce"]]


def test_a_state_works_once_and_expires(client, env):
    fake_store, google = env
    done, query = sign_in(client, google)
    assert done.status_code == 303 and done.headers["location"] == "/valr/leads"
    replay = client.get("/oauth/google/callback", params={"code": "code-1", "state": query["state"][0]})
    assert replay.headers["location"] == "/login?error=expired" and "set-cookie" not in replay.headers
    assert len(google.requests) == 1  # The replay never reached Google.
    forged = client.get("/oauth/google/callback", params={"code": "c", "state": "login.made-up"})
    assert forged.headers["location"] == "/login?error=expired"
    started = client.get("/login/google")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    fake_store.now += core.STATE_TTL_S + 1
    late = client.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert late.headers["location"] == "/login?error=expired"


def test_a_cancelled_sign_in_uses_up_the_state(client, env):
    fake_store, google = env
    started = client.get("/login/google")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    cancelled = client.get("/oauth/google/callback", params={"error": "access_denied", "state": state})
    assert cancelled.headers["location"] == "/login?error=cancelled" and not fake_store.states
    assert not google.requests


def test_a_nonce_mismatch_is_refused(client, env):
    fake_store, google = env
    started = client.get("/login/google")
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    google.nonce = "another-flow's-nonce"
    done = client.get("/oauth/google/callback", params={"code": "c", "state": state})
    assert done.headers["location"] == "/login?error=failed" and not fake_store.sessions


# The ID token's claims.

def test_claim_checks():
    now = time.time()
    good = json.loads(base64.urlsafe_b64decode(id_token().split(".")[1] + "=="))
    assert core.check_claims(good | {"email": "Owner@Example.com"}, CLIENT_ID, "the-nonce", now) == OWNER
    assert core.check_claims(good | {"iss": "accounts.google.com"}, CLIENT_ID, "the-nonce", now) == OWNER
    assert core.check_claims(good | {"aud": [CLIENT_ID, "x"], "azp": CLIENT_ID}, CLIENT_ID,
                             "the-nonce", now) == OWNER
    for bad in ({"iss": "https://evil.example"}, {"aud": "another-client"}, {"aud": [CLIENT_ID, "x"]},
                {"exp": now - 3600}, {"exp": "soon"}, {"exp": None}, {"email_verified": False},
                {"email_verified": None}, {"email": None}, {"email": "not-an-address"},
                {"nonce": "other"}, {"nonce": None}):
        claims = {k: v for k, v in (good | bad).items() if v is not None}
        with pytest.raises(core.LoginError):
            core.check_claims(claims, CLIENT_ID, "the-nonce", now)
    with pytest.raises(core.LoginError):
        core.check_claims(good, CLIENT_ID, "", now)  # No nonce stored: never matches.
    for broken in ("", "a.b", "a.!!!.c", "a." + base64.urlsafe_b64encode(b"[1]").decode() + ".c"):
        with pytest.raises(core.LoginError):
            core.decode_payload(broken)


def test_a_bad_token_from_google_is_no_session(client, env):
    fake_store, google = env
    for overrides in ({"aud": "someone-elses-client"}, {"email_verified": False},
                      {"exp": time.time() - 3600}, {"iss": "https://evil.example"}):
        google.overrides = overrides
        done, _ = sign_in(client, google)
        assert done.headers["location"] == "/login?error=failed", overrides
        assert "set-cookie" not in done.headers and not fake_store.sessions


# Owner only.

def test_another_google_account_gets_the_private_page_and_no_session(client, env, caplog):
    fake_store, google = env
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        done, _ = sign_in(client, google, email=STRANGER)
    assert done.status_code == 303 and done.headers["location"] == "/private"
    assert "set-cookie" not in done.headers and not fake_store.sessions
    assert "elsewhere.example" in caplog.text and STRANGER not in caplog.text  # Domain only.
    page = client.get("/private")
    assert page.status_code == 403 and "VALR is private" in page.text and STRANGER not in page.text
    assert client.get("/valr").headers["location"].startswith("/login")


def test_the_owner_gets_a_session_and_the_gmail_grant_is_untouched(client, env):
    fake_store, google = env
    done, _ = sign_in(client, google, email="other-owner@example.com")  # Any listed owner.
    assert done.headers["location"] == "/valr/leads"
    assert google.requests[0]["redirect_uri"] == [f"{SITE}/oauth/google/callback"]
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    page = client.get("/valr", headers=cookie)
    assert page.status_code == 200 and 'aria-label="Account"' in page.text
    assert "other-owner@example.com" not in page.text  # Owner-only app: no address on show.
    assert 'action="/logout"' in page.text and "Sign out" in page.text  # The top bar's account menu.


def test_a_session_ends_when_the_owner_leaves_the_list(client, env, monkeypatch):
    fake_store, google = env
    done, _ = sign_in(client, google)
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    assert client.get("/valr/api/state", headers=cookie).status_code == 200
    monkeypatch.setenv("VALR_OWNER_EMAILS", "other-owner@example.com")
    assert client.get("/valr/api/state", headers=cookie).status_code == 401


# The cookie: random, hashed at rest, attributes, expiry.

def test_cookie_is_random_hashed_at_rest_and_expires(client, env):
    fake_store, google = env
    done, _ = sign_in(client, google)
    header = done.headers["set-cookie"]
    token = cookie_of(done)
    assert header.startswith("__Host-valr=") and len(token) == 64 and int(token, 16) >= 0
    for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=Lax", f"Max-Age={30 * 86400}"):
        assert attribute in header
    assert "Domain=" not in header
    assert list(fake_store.sessions) == [core.token_hash(token)] and token not in str(fake_store.sessions)
    assert core.token_hash(token) != token and len(core.token_hash(token)) == 64
    cookie = {"Cookie": f"{core.COOKIE}={token}"}
    assert client.get("/valr/api/state", headers=cookie).status_code == 200
    fake_store.now += core.SESSION_TTL_S + 1
    assert client.get("/valr/api/state", headers=cookie).status_code == 401
    assert core.cookie_token({core.COOKIE: "x" * 64}) is None  # Malformed: not even looked up.
    assert core.cookie_token({core.COOKIE: token.upper()}) is None
    assert core.cookie_token({core.COOKIE: token}) == token


def test_expired_sessions_are_swept_when_a_new_one_is_made(env):
    import asyncio
    fake_store, _ = env
    asyncio.run(fake_store.create_session(OWNER))
    fake_store.now += core.SESSION_TTL_S + 1
    asyncio.run(fake_store.create_session(OWNER))
    assert len(fake_store.sessions) == 1


def test_postgres_store_sql_hashes_tokens_and_deletes_states_as_it_reads_them():
    import asyncio

    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        def __init__(self):
            self.calls = []

        async def execute(self, sql, params=()):
            self.calls.append((" ".join(sql.split()), params))
            return Cursor(("nonce", "/valr") if sql.startswith("DELETE FROM valr_login_states WHERE state_hash")
                          else ("owner@example.com",))

    connection = Connection()

    class OnePool:
        def connection(self):
            class Context:
                async def __aenter__(self):
                    return connection

                async def __aexit__(self, *exc):
                    return False
            return Context()

    pg = store.PostgresStore(OnePool())
    token = asyncio.run(pg.create_session(OWNER))
    assert asyncio.run(pg.take_state("login.abc")) == ("nonce", "/valr")
    asyncio.run(pg.end_session(token))
    sql = "\n".join(call[0] for call in connection.calls)
    params = json.dumps([call[1] for call in connection.calls])
    assert "CREATE TABLE IF NOT EXISTS valr_sessions" in sql and sql.count("CREATE TABLE") == 2  # Once.
    assert "DELETE FROM valr_sessions WHERE expires_at < now()" in sql
    assert "DELETE FROM valr_login_states WHERE state_hash = %s" in sql and "RETURNING" in sql
    assert token not in params and core.token_hash(token) in params
    assert "login.abc" not in params


# Redirects after sign-in.

@pytest.mark.parametrize("value,expected", [
    ("/valr/leads", "/valr/leads"), ("/valr/interviews/slot-ab12?x=1", "/valr/interviews/slot-ab12?x=1"),
    ("/call", "/call"), (None, "/valr"), ("", "/valr"), ("//evil.example/x", "/valr"),
    ("https://evil.example", "/valr"), ("/\\evil.example", "/valr"), ("valr", "/valr"),
    ("/valr\nSet-Cookie: x", "/valr"), ("javascript:alert(1)", "/valr"), ("/login?next=/valr", "/valr"),
    ("/logout", "/valr"), ("/" + "a" * 600, "/valr"), ("/valr/<script>", "/valr"),
])
def test_safe_next(value, expected):
    assert core.safe_next(value) == expected


def test_next_survives_the_round_trip_through_login(client, env):
    fake_store, google = env
    bounced = client.get("/valr/leads?stage=offer")
    assert bounced.status_code == 303
    assert bounced.headers["location"] == "/login?next=/valr/leads%3Fstage%3Doffer"
    login = client.get(bounced.headers["location"])
    assert login.status_code == 200 and 'href="/login/google?next=/valr/leads%3Fstage%3Doffer"' in login.text
    done, _ = sign_in(client, google, next_path="/valr/leads?stage=offer")
    assert done.headers["location"] == "/valr/leads?stage=offer"
    done, _ = sign_in(client, google, next_path="//evil.example")
    assert done.headers["location"] == "/valr"


# Same-origin on every POST and WebSocket.

def test_posts_and_sockets_must_be_same_origin(client, env):
    fake_store, google = env
    done, _ = sign_in(client, google)
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    json_headers = {"Content-Type": "application/json"}
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Sec-Fetch-Site": "same-site"}, {"Origin": "null"}):
        response = client.post("/valr/api/dials", content='{"paused": true}',
                               headers=cookie | json_headers | headers)
        assert response.status_code == 403 and response.json()["detail"] == "Cross-origin request refused."
        assert client.post("/logout", headers=cookie | headers).status_code == 403
    assert fake_store.sessions  # The cross-site logouts did nothing.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/voice", headers=cookie | {"Origin": "https://evil.example"}):
            pass
    # Same origin, but no CSRF token: the dashboard's own check still answers.
    same = client.post("/valr/api/dials", content='{"paused": true}',
                       headers=cookie | json_headers | {"Origin": SITE, "Sec-Fetch-Site": "same-origin"})
    assert same.status_code == 403 and "stale" in same.json()["detail"]
    assert core.same_origin({"origin": SITE}, HOST) and core.same_origin({}, HOST)
    assert not core.same_origin({"origin": "https://valr.example.com.evil.example"}, HOST)


def test_csrf_still_guards_actions_for_a_session(client, env, monkeypatch):
    fake_store, google = env
    from app import interview_store
    flags = {}

    async def setup(connection):
        return None

    async def set_flag(connection, name, enabled, until=None):
        flags[name] = enabled

    monkeypatch.setattr(interview_store, "setup", setup)
    monkeypatch.setattr(interview_store, "set_flag", set_flag)
    done, _ = sign_in(client, google)
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    page = client.get("/valr", headers=cookie).text
    csrf = page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    ok = client.post("/valr/api/dials", content='{"paused": true}',
                     headers=cookie | {"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": SITE})
    assert ok.status_code == 200 and flags == {interview_store.DIALS_PAUSED: True}


# Every route is either public or the owner's.

def route_paths(routes_):
    for route in routes_:
        if hasattr(route, "effective_candidates"):  # FastAPI's included routers.
            yield from route_paths(route.effective_candidates())
        elif hasattr(route, "original_router"):
            yield from route_paths(route.original_router.routes)
        else:  # An included route's own path (a WebSocket's effective path is "").
            path = getattr(getattr(route, "original_route", route), "path", None)
            if path is not None:
                yield path


def test_every_route_is_public_or_needs_the_owner():
    from app.main import app
    paths = set(route_paths(app.routes))
    assert {"/valr", "/ws/voice", "/webrtc/offer", "/telnyx/webhook", "/login", "/static"} <= paths
    unclassified = sorted(path for path in paths if gate.classify(path) is None)
    assert not unclassified, f"Add these to app/auth/gate.py's PUBLIC or OWNER lists: {unclassified}"
    public = sorted(path for path in paths if gate.classify(path) == "public")
    assert public == sorted({"/healthz", "/hooks/fathom", "/hooks/gcal", "/hooks/gmail", "/hooks/granola",
                             "/hooks/recording-upload", "/manifest.webmanifest", "/sw.js", "/favicon.ico",
                             "/login", "/login/google",
                             "/logout", "/oauth/google/callback", "/private", "/static",
                             "/telnyx/webhook", "/ws/telnyx"}), public
    for path in ("/", "/call", "/ws/voice", "/webrtc/offer", "/webrtc/ice-servers", "/oauth/google/start", "/logout/all",
                 "/valr/profile", "/valr/api/profile", "/valr/api/profile/phone", "/valr/api/google/disconnect",
                 "/valr/api/export", "/valr/api/data/delete", "/valr/profile/photo", "/valr/api/push/remove",
                 "/valr", "/valr/api/state", "/valr/api/dials", "/valr/interviews/{ref}", "/valr/context",
                 "/valr/api/reach", "/valr/api/prep", "/valr/api/leads", "/valr/api/leads/sync",
                 "/valr/api/leads/{lead_id}/override", "/valr/api/recording", "/valr/api/past-interviews",
                 "/valr/interviews/past/{slug}", "/valr/leads", "/valr/api/notetakers/{provider}/connect",
                 "/valr/api/notifications", "/valr/api/notifications/read", "/valr/api/notifications/settings",
                 "/valr/api/recording/audio", "/valr/api/phone/status", "/valr/api/phone/token",
                 "/valr/settings/phone-help", "/valr/downloads/mac",
                 "/valr/api/push", "/valr/api/push/subscribe", "/valr/api/push/unsubscribe", "/valr/api/push/test"):
        assert path in paths and gate.classify(path) == "owner", path
    assert gate.classify("/valrx") is None and gate.classify("/staticx") is None
    assert gate.classify("/static/icons/apple-touch-icon.png") == "public"  # The Home Screen icons.
    assert gate.classify("/sw.js.map") is None and gate.classify("/manifest.webmanifest/x") is None
    assert gate.classify("/hooks/gmail") == "public" and gate.classify("/hooks/fathom") == "public"


def test_owner_routes_refuse_strangers_and_public_ones_answer(client, env):
    for path in ("/", "/call", "/valr", "/valr/settings", "/oauth/google/start"):
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"].startswith("/login?next="), path
    for path in ("/valr/api/state", "/webrtc/ice-servers"):
        response = client.get(path)
        assert response.status_code == 401 and response.json()["login"] == "/login", path
    assert client.post("/webrtc/offer", json={"type": "offer", "sdp": "x", "echo_cancelled": True}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/voice"):
            pass
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/valr-auth.css").status_code == 200
    assert client.post("/telnyx/webhook", content=b"{}").status_code != 401  # Its own signature check.


def test_context_push_keeps_its_bearer_token(client, env, monkeypatch):
    monkeypatch.setenv("VALR_CONTEXT_TOKEN", "push-secret")
    monkeypatch.delenv("VALR_TOKEN_KEY")  # The handler then stops at "not configured" (503).
    assert client.post("/valr/context", content=b"{}").status_code == 401
    wrong = client.post("/valr/context", content=b"{}", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 403  # The handler: wrong token and no owner.
    right = client.post("/valr/context", content=b"{}", headers={"Authorization": "Bearer push-secret"})
    assert right.status_code == 503  # Past the gate and the token check.


def test_the_leads_page_and_its_json_accept_the_session_owner(client, env, monkeypatch):
    fake_store, google = env
    from app import leads_web
    from app.valr_ui import contracts
    from app.valr_ui import routes as page_routes
    from tests.valr_sample_extras import sample_leads

    async def collect(pool):
        return sample_leads()

    async def load_leads():
        return contracts.leads(sample_leads())

    monkeypatch.setattr(leads_web, "collect", collect)
    monkeypatch.setattr(page_routes, "load_leads", load_leads)
    for path in ("/valr/leads", "/valr/api/leads", "/valr/api/reach", "/valr/api/past-interviews"):
        response = client.get(path)
        assert response.status_code in (303, 401), path  # No one signed in.
    done, _ = sign_in(client, google)
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    assert client.get("/valr/leads", headers=cookie).status_code == 200
    assert client.get("/valr/api/leads", headers=cookie).status_code == 200


def test_the_gmail_grant_callback_still_needs_the_owner(client, env):
    response = client.get("/oauth/google/callback", params={"code": "c", "state": "eyJuIjoiIn0.sig"})
    assert response.status_code == 403


# The three modes.

def test_both_mode_accepts_access_or_a_session(client, env):
    fake_store, google = env
    assert client.get("/valr/api/state", headers=GOOD_ACCESS).status_code == 200
    assert client.get("/valr/api/state", headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 401
    done, _ = sign_in(client, google)
    assert client.get("/valr/api/state", headers={"Cookie": f"{core.COOKIE}={cookie_of(done)}"}).status_code == 200


def test_both_mode_holds_access_logins_to_the_owner_list(client, env):
    assert client.get("/valr/api/state", headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 401


def test_app_mode_ignores_access(client, env, monkeypatch):
    fake_store, google = env
    monkeypatch.setenv("VALR_AUTH_MODE", "app")
    assert client.get("/valr/api/state", headers=GOOD_ACCESS).status_code == 401
    assert client.get("/valr", headers=GOOD_ACCESS).headers["location"] == "/login?next=/valr"
    done, _ = sign_in(client, google)
    assert client.get("/valr/api/state", headers={"Cookie": f"{core.COOKIE}={cookie_of(done)}"}).status_code == 200


def test_access_mode_is_todays_behaviour(client, env, monkeypatch):
    fake_store, google = env
    done, _ = sign_in(client, google)  # Made while in both mode.
    monkeypatch.setenv("VALR_AUTH_MODE", "access")
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    assert client.get("/valr/api/state", headers=cookie).status_code == 403  # Sessions ignored.
    assert client.get("/valr", headers=cookie).status_code == 403  # 403, not a redirect.
    assert client.get("/valr/api/state", headers=GOOD_ACCESS).status_code == 200
    assert client.get("/login/google").headers["location"] == "/login?error=off"


def test_an_unknown_mode_means_both(monkeypatch):
    monkeypatch.setenv("VALR_AUTH_MODE", "nonsense")
    assert core.mode() == "both"
    monkeypatch.delenv("VALR_AUTH_MODE")
    assert core.mode() == "both"


# Sign-out.

def test_logout_deletes_the_session_and_clears_the_cookie(client, env):
    fake_store, google = env
    done, _ = sign_in(client, google)
    cookie = {"Cookie": f"{core.COOKIE}={cookie_of(done)}"}
    out = client.post("/logout", headers=cookie | {"Origin": SITE})
    assert out.status_code == 303 and out.headers["location"] == "/login?signed_out=1"
    assert out.headers["set-cookie"].startswith("__Host-valr=;") and "Max-Age=0" in out.headers["set-cookie"]
    assert not fake_store.sessions
    assert client.get("/valr", headers=cookie).headers["location"].startswith("/login")
    assert "signed out" in client.get("/login?signed_out=1").text
    assert client.get("/logout").status_code == 405  # A link cannot sign anyone out.


def test_login_page_sends_a_signed_in_owner_on(client, env):
    fake_store, google = env
    done, _ = sign_in(client, google)
    again = client.get("/login?next=/valr/settings", headers={"Cookie": f"{core.COOKIE}={cookie_of(done)}"})
    assert again.status_code == 303 and again.headers["location"] == "/valr/settings"
    page = client.get("/login?error=expired").text
    assert "Sign in with Google" in page and "expired" in page and "only its owner" in page


def test_a_browser_call_is_remembered_under_the_signed_in_owner():
    import asyncio
    token = core.current_email.set(OWNER)
    try:
        assert asyncio.run(memory.web_caller_key({})) == memory.caller_key_from_email(OWNER)
    finally:
        core.current_email.reset(token)


@pytest.mark.skipif(not __import__("os").getenv("VALR_TEST_POSTGRES"),
                    reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")
def test_postgres_store_against_a_real_database():
    import asyncio
    import os

    import psycopg

    class OnePool:
        def __init__(self, connection):
            self.connection_ = connection

        def connection(self):
            outer = self

            class Context:
                async def __aenter__(self):
                    return outer.connection_

                async def __aexit__(self, *exc):
                    return False
            return Context()

    async def main():
        async with await psycopg.AsyncConnection.connect(os.environ["VALR_TEST_POSTGRES"],
                                                         autocommit=True) as connection:
            await connection.execute("DROP TABLE IF EXISTS valr_sessions, valr_login_states")
            saved = store._ready_pool
            pg = store.PostgresStore(OnePool(connection))
            await pg.save_state("login.s1", "n1", "/valr/leads")
            assert await pg.take_state("login.s1") == ("n1", "/valr/leads")
            assert await pg.take_state("login.s1") is None  # Once.
            await pg.save_state("login.s2", "n2", "/valr")
            await connection.execute("UPDATE valr_login_states SET created_at = now() - interval '11 minutes'")
            assert await pg.take_state("login.s2") is None  # Expired.
            token = await pg.create_session(OWNER)
            assert await pg.session_email(token) == OWNER
            assert await pg.session_email(core.new_token()) is None
            cursor = await connection.execute("SELECT token_hash FROM valr_sessions")
            assert [row[0] for row in await cursor.fetchall()] == [core.token_hash(token)]
            await connection.execute("UPDATE valr_sessions SET expires_at = now() - interval '1 second'")
            assert await pg.session_email(token) is None
            await pg.create_session(OWNER)  # Sweeps the expired one.
            cursor = await connection.execute("SELECT count(*) FROM valr_sessions")
            assert (await cursor.fetchone())[0] == 1
            fresh = await pg.create_session(OWNER)
            await pg.end_session(fresh)
            assert await pg.session_email(fresh) is None
            store._ready_pool = saved
    asyncio.run(main())


def test_safari_form_post_with_null_origin_is_same_origin_only_with_fetch_metadata():
    from app.auth import core
    assert core.same_origin({"origin": "null", "sec-fetch-site": "same-origin"}, "valr.example.com")
    assert not core.same_origin({"origin": "null", "sec-fetch-site": "cross-site"}, "valr.example.com")
    assert not core.same_origin({"origin": "null"}, "valr.example.com")


def test_sign_in_page_has_link_preview_tags():
    from app.auth import pages
    html = pages.login_page("/valr")
    for tag in ('property="og:title"', 'property="og:description"',
                'content="https://valr.haciensus.com/static/og.jpg"', 'name="twitter:card"'):
        assert tag in html
