"""Settings › API keys (app/user_keys_web.py, app/valr_ui/keys.py): a guest saves, tests and
removes its keys; a rejected key is not saved; no response ever carries the key; the owner's
workspace uses the server's keys. The store is in memory here (its SQL: tests/test_user_keys.py
and tests/test_tenant_postgres.py). Keys and addresses are made up."""
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import auth, main, tenant, user_keys, user_keys_web, valr_dashboard
from app.auth import Identity, gate
from tests.test_valr_pages import Pool

GUEST_EMAIL, OWNER_EMAIL = "guest@example.com", "owner@example.com"
KEY = "sk-guest-made-up-key-7777"


@pytest.fixture
def store(monkeypatch):
    """user_keys in memory, keyed by workspace (so a test can see what each one holds)."""
    rows: dict[str, dict] = {}
    monkeypatch.setenv("VALR_TOKEN_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VALR_OWNER_EMAILS", OWNER_EMAIL)

    async def save(connection, purpose, provider, model, key, status):
        rows.setdefault(tenant.get(), {})[purpose] = {"provider": provider, "model": model, "key": key, "status": status}

    async def public(connection):
        mine = rows.get(tenant.get(), {})
        return {p: ({"provider": r["provider"], "label": r["provider"], "model": r["model"], "custom_model": bool(r["model"]),
                     "last4": user_keys.last4(r["key"]), "status": r["status"], "checked_at": None} if (r := mine.get(p)) else None)
                for p in ("llm", "speech")}

    async def load(connection):
        return {p: user_keys.UserKey(r["provider"], r["model"], r["key"]) for p, r in rows.get(tenant.get(), {}).items()
                if r["status"] != "rejected"}

    async def remove(connection, purpose):
        return rows.get(tenant.get(), {}).pop(purpose, None) is not None

    async def set_status(connection, purpose, status):
        rows[tenant.get()][purpose]["status"] = status
    for name, fn in (("save", save), ("public", public), ("load", load), ("remove", remove), ("set_status", set_status)):
        monkeypatch.setattr(user_keys, name, fn)
    monkeypatch.setattr(user_keys_web, "pool", lambda: Pool())
    return rows


def sign_in(monkeypatch, email):
    async def who(connection):
        return Identity(email, "session")
    monkeypatch.setattr(gate, "identify", who)
    monkeypatch.setattr(auth, "identify", who)
    return {"Content-Type": "application/json", "X-CSRF-Token": valr_dashboard.csrf_token(email)}


def provider_answers(monkeypatch, status):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(status, json={})
    monkeypatch.setattr(user_keys_web, "_http", httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return seen


def guest_id():
    return tenant.user_id(GUEST_EMAIL, {OWNER_EMAIL})


def test_a_guest_saves_a_checked_key_and_never_sees_it_again(store, monkeypatch):
    headers = sign_in(monkeypatch, GUEST_EMAIL)
    seen = provider_answers(monkeypatch, 200)
    client = TestClient(main.app)
    response = client.post("/valr/api/keys", headers=headers,
                           json={"purpose": "llm", "provider": "openai", "key": f"  {KEY} ", "model": ""})
    assert response.status_code == 200, response.text
    assert KEY not in response.text and response.json()["keys"]["llm"]["last4"] == "7777"
    assert response.json()["message"] == "Key saved and working."
    assert str(seen[0].url) == "https://api.openai.com/v1/models"
    assert store[guest_id()]["llm"] == {"provider": "openai", "model": "", "key": KEY, "status": "ok"}
    view = client.get("/valr/api/keys").json()
    assert view["owner"] is False and KEY not in str(view) and view["keys"]["speech"] is None


def test_a_rejected_key_is_not_saved(store, monkeypatch):
    headers = sign_in(monkeypatch, GUEST_EMAIL)
    provider_answers(monkeypatch, 401)
    response = TestClient(main.app).post("/valr/api/keys", headers=headers,
                                         json={"purpose": "speech", "provider": "deepgram", "key": KEY})
    assert response.status_code == 400 and "rejected" in response.json()["detail"]
    assert store == {}


def test_an_unreachable_provider_saves_the_key_unchecked(store, monkeypatch):
    headers = sign_in(monkeypatch, GUEST_EMAIL)
    provider_answers(monkeypatch, 503)
    response = TestClient(main.app).post("/valr/api/keys", headers=headers,
                                         json={"purpose": "llm", "provider": "gemini", "key": KEY, "model": "gemini-3.8-flash"})
    assert response.status_code == 200 and store[guest_id()]["llm"]["status"] == "unchecked"
    assert store[guest_id()]["llm"]["model"] == "gemini-3.8-flash"


@pytest.mark.parametrize("body", [
    {"purpose": "llm", "provider": "deepgram", "key": KEY},          # Not an LLM provider.
    {"purpose": "speech", "provider": "openai", "key": KEY},          # Not a speech provider.
    {"purpose": "llm", "provider": "openai", "key": "sk with spaces 1234"},
    {"purpose": "llm", "provider": "openai", "key": KEY, "model": "gpt 6; drop"},
    {"purpose": "llm", "provider": "openai", "key": "short"},
    {"purpose": "other", "provider": "openai", "key": KEY},
])
def test_bad_requests_are_refused_without_a_provider_call(store, monkeypatch, body):
    headers = sign_in(monkeypatch, GUEST_EMAIL)
    seen = provider_answers(monkeypatch, 200)
    assert TestClient(main.app).post("/valr/api/keys", headers=headers, json=body).status_code == 400
    assert seen == [] and store == {}


def test_test_rechecks_and_remove_deletes(store, monkeypatch):
    headers = sign_in(monkeypatch, GUEST_EMAIL)
    provider_answers(monkeypatch, 200)
    client = TestClient(main.app)
    client.post("/valr/api/keys", headers=headers, json={"purpose": "llm", "provider": "anthropic", "key": KEY})
    provider_answers(monkeypatch, 401)
    tested = client.post("/valr/api/keys/test", headers=headers, json={"purpose": "llm"})
    assert tested.json()["status"] == "rejected" and store[guest_id()]["llm"]["status"] == "rejected"
    assert KEY not in tested.text
    removed = client.post("/valr/api/keys/remove", headers=headers, json={"purpose": "llm"})
    assert removed.status_code == 200 and removed.json()["keys"]["llm"] is None
    assert client.post("/valr/api/keys/test", headers=headers, json={"purpose": "llm"}).status_code == 404


def test_every_action_needs_csrf_and_the_same_origin(store, monkeypatch):
    headers = sign_in(monkeypatch, GUEST_EMAIL)
    client = TestClient(main.app)
    for path in ("/valr/api/keys", "/valr/api/keys/test", "/valr/api/keys/remove"):
        assert client.post(path, json={"purpose": "llm"}).status_code == 403, path
        cross = {**headers, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        assert client.post(path, json={"purpose": "llm"}, headers=cross).status_code == 403, path
    assert store == {}


def test_one_workspace_never_sees_anothers_key(store, monkeypatch):
    provider_answers(monkeypatch, 200)
    client = TestClient(main.app)
    client.post("/valr/api/keys", headers=sign_in(monkeypatch, GUEST_EMAIL),
                json={"purpose": "llm", "provider": "openai", "key": KEY})
    sign_in(monkeypatch, "other@example.com")
    assert client.get("/valr/api/keys").json()["keys"] == {"llm": None, "speech": None}


def test_the_owner_uses_the_servers_keys(store, monkeypatch):
    headers = sign_in(monkeypatch, OWNER_EMAIL)
    seen = provider_answers(monkeypatch, 200)
    client = TestClient(main.app)
    assert client.get("/valr/api/keys").json()["owner"] is True
    response = client.post("/valr/api/keys", headers=headers, json={"purpose": "llm", "provider": "openai", "key": KEY})
    assert response.status_code == 400 and response.json()["detail"] == user_keys_web.OWNER_NOTE
    assert seen == [] and store == {}


def test_the_settings_row_has_the_forms_for_a_guest_and_one_line_for_the_owner():
    from app.valr_ui import keys
    with tenant.as_user(tenant.OWNER):
        owner_row = keys.row()
    assert "Uses the server's keys." in owner_row and "<form" not in owner_row
    with tenant.as_user("u_" + "5" * 20):
        guest_row = keys.row()
    assert guest_row.count("data-keys-form") == 2 and 'type="password"' in guest_row
    assert "subscriptions can&#x27;t be used" in guest_row or "subscriptions can't be used" in guest_row
    assert 'value="anthropic"' in guest_row and 'value="deepgram"' in guest_row and "valr-keys.js" in guest_row
