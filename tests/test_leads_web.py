"""The Gmail push webhook (Pub/Sub OIDC), and the leads page and JSON behind the owner's Access login."""
import base64
import json
import time

import pytest

pytest.importorskip("fastapi", reason="the web routes run in the app's venv")

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from fastapi.testclient import TestClient

from app import google_oidc, leads, leads_web, research, valr_dashboard, valr_view
from tests.leads_sample import NOW, sample_touches

AUDIENCE = "https://valr.example/hooks/gmail"
SA = "push@project.iam.gserviceaccount.example"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OWNER = "owner@example.com"
GOOD = {"Cf-Access-Jwt-Assertion": "owner"}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def token(claims=None, key=KEY, kid="k1", alg="RS256"):
    now = int(time.time())
    body = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": SA, "email_verified": True,
            "iat": now, "exp": now + 3600, **(claims or {})}
    head = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = b64(json.dumps(body).encode())
    signature = key.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), SHA256())
    return f"{head}.{payload}.{b64(signature)}"


def push_body(email="me@example.com", history=1234, subscription="projects/p/subscriptions/s"):
    data = base64.b64encode(json.dumps({"emailAddress": email, "historyId": history}).encode()).decode()
    return json.dumps({"message": {"data": data, "messageId": "1"}, "subscription": subscription})


def test_oidc_check():
    keys = {"k1": KEY.public_key()}
    assert google_oidc.check(token(), keys, AUDIENCE, SA)["email"] == SA
    assert google_oidc.check(token({"iss": "accounts.google.com"}), keys, AUDIENCE, SA) is not None
    for bad in (token({"aud": "https://elsewhere.example"}), token({"email": "x@example.com"}),
                token({"email_verified": False}), token({"exp": int(time.time()) - 3600}),
                token({"iss": "https://evil.example"}), token(key=OTHER), token(kid="k2"),
                token(alg="HS256")):
        assert google_oidc.check(bad, keys, AUDIENCE, SA) is None


@pytest.fixture
def hook(monkeypatch):
    from app.main import app
    started = []

    async def start_sync(reason, history_id=0):
        started.append((reason, history_id))
        return monkeypatch.temporal_up

    monkeypatch.temporal_up = True
    verifier = google_oidc.PushVerifier(AUDIENCE, SA, keys={"k1": KEY.public_key()}, fetched_at=time.monotonic())
    monkeypatch.setattr(leads_web, "push_verifier", lambda: verifier)
    monkeypatch.setattr(leads_web, "start_sync", start_sync)
    monkeypatch.setenv("VALR_LEADS", "1")
    monkeypatch.setenv("VALR_GMAIL_ACCOUNT", "me@example.com")
    monkeypatch.setenv("VALR_GMAIL_SUBSCRIPTION", "projects/p/subscriptions/s")
    client = TestClient(app)
    client.started = started
    return client


def post(client, body, bearer):
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post("/hooks/gmail", content=body, headers=headers)


def test_push_starts_a_sync(hook):
    assert post(hook, push_body(), token()).status_code == 204
    assert hook.started == [("push", 1234)]


def test_push_without_a_valid_token_is_refused(hook):
    for bearer in (None, "nonsense", token(key=OTHER), token({"aud": "https://elsewhere.example"})):
        assert post(hook, push_body(), bearer).status_code == 403
    assert hook.started == []


def test_push_for_another_account_or_subscription_is_acknowledged_and_ignored(hook):
    assert post(hook, push_body(email="someone@example.com"), token()).status_code == 204
    assert post(hook, push_body(subscription="projects/p/subscriptions/other"), token()).status_code == 204
    assert post(hook, "not json", token()).status_code == 204
    assert hook.started == []


def test_push_while_leads_are_off_starts_nothing(hook, monkeypatch):
    monkeypatch.setenv("VALR_LEADS", "0")
    assert post(hook, push_body(), token()).status_code == 204 and hook.started == []


def test_push_with_temporal_down_asks_pubsub_to_retry(hook, monkeypatch):
    monkeypatch.temporal_up = False
    assert post(hook, push_body(), token()).status_code == 503


class Verifier:
    async def email(self, value):
        return {"owner": OWNER, "guest": "guest@example.com"}.get(value)


@pytest.fixture
def page(monkeypatch):
    from app.main import app

    async def collect(pool, now=None):
        view = leads.graph(leads.build(sample_touches(), NOW), NOW)
        view["gmail"] = leads_web.gmail_view({}, {}, NOW)
        return view

    async def state(pool, now=None):
        from tests.valr_sample import sample_view
        return sample_view()

    monkeypatch.setattr(leads_web, "collect", collect)  # The Leads page reads it through LEADS_SOURCE.
    monkeypatch.setattr(valr_view, "collect", state)
    monkeypatch.setattr(research, "pool", object())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")  # Cloudflare Access only (app/auth).
    return TestClient(app)


def test_page_and_json_need_the_owner(page):
    for path in ("/valr/leads", "/valr/api/leads"):
        assert page.get(path).status_code == 403
        assert page.get(path, headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
        response = page.get(path, headers=GOOD)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = page.get("/valr/api/leads", headers=GOOD).json()
    assert {"leads", "contacts", "links", "stages", "gmail"} <= set(data)
    html = page.get("/valr/leads", headers=GOOD).text
    assert "Juniper Robotics" in html and "/static/leads-globe.js" in html  # The app's Leads page, real data.
    for tag in ('id="leads-data">', 'id="leads-raw">'):
        assert tag in html and "<" not in html.split(tag, 1)[1].split("</script>")[0]


def test_sync_action_needs_csrf(page, monkeypatch):
    started = []

    async def start_sync(reason, history_id=0):
        started.append(reason)
        return True

    monkeypatch.setattr(leads_web, "start_sync", start_sync)
    headers = {**GOOD, "Content-Type": "application/json"}
    assert page.post("/valr/api/leads/sync", headers=headers, content="{}").status_code == 403
    csrf = page.get("/valr/leads", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    response = page.post("/valr/api/leads/sync", headers={**headers, "X-CSRF-Token": csrf}, content="{}")
    assert response.status_code == 200 and started == ["manual"]


def test_every_page_links_to_leads(page):
    assert 'href="/valr/leads"' in page.get("/valr/performance", headers=GOOD).text


class Pool:
    def connection(self):
        class Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False
        return Context()


def test_overrides_from_the_panel(page, monkeypatch):
    from app import leads_store
    stored = []

    async def current(connection, now):
        return leads.build(sample_touches(), NOW)

    async def add_override(connection, lead_id, kind, value="", touch_ids=None, threads=None, by=""):
        stored.append((lead_id, kind, value, sorted(touch_ids or []), by))

    monkeypatch.setattr(leads_web, "current", current)
    monkeypatch.setattr(leads_store, "add_override", add_override)
    monkeypatch.setattr(research, "pool", Pool())
    ids = {l.company: l.id for l in leads.build(sample_touches(), NOW)}
    csrf = page.get("/valr/leads", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}

    def send(lead, body):
        return page.post(f"/valr/api/leads/{lead}/override", headers=headers, content=json.dumps(body))

    assert page.post(f"/valr/api/leads/{ids['Kite']}/override", headers={**GOOD, "Content-Type": "application/json"},
                     content='{"action": "dismiss"}').status_code == 403  # No CSRF token.
    assert send(ids["Nimbus"], {"action": "dismiss"}).json()["message"] == "Nimbus dismissed."
    assert send(ids["Kite"], {"action": "stage", "stage": "offer"}).status_code == 200
    assert send(ids["Kite"], {"action": "stage", "stage": "hired"}).status_code == 400
    assert send(ids["Vela"], {"action": "merge", "into": ids["Kite"]}).json()["message"] == "Vela merged into Kite."
    assert send(ids["Vela"], {"action": "merge", "into": ids["Vela"]}).status_code == 400
    assert send("lead-nope", {"action": "dismiss"}).status_code == 404
    assert send(ids["Kite"], {"action": "explode"}).status_code == 400
    assert [(s[1], s[2]) for s in stored] == [("dismiss", ""), ("stage", "offer"), ("merge", ids["Kite"])]
    assert stored[0][3] == ["gmail:n1"] and stored[0][4] == OWNER
