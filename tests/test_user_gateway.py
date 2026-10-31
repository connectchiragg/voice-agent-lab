"""llm_gateway.for_current (app/user_gateway.py): the owner keeps the shared gateway and the
server's keys; a guest gets its own provider and key, or KeyMissing with zero HTTP calls; no
guest request ever carries a server key or reaches a server-key client. No network."""
import json

import httpx
import pytest

anthropic = pytest.importorskip("anthropic", reason="anthropic is only in the worker image")
from app import llm_gateway, tenant, user_gateway, user_keys  # noqa: E402
from app.user_gateway import NoServerKey, UserGateway  # noqa: E402

GUEST = "u_" + "3" * 20
KEY = "guest-key-made-up-9876"
SERVER = {"GROQ_API_KEY": "server-groq-SECRET", "ANTHROPIC_API_KEY": "server-anthropic-SECRET",
          "OPENROUTER_API_KEY": "server-openrouter-SECRET"}


@pytest.fixture(autouse=True)
def server_keys(monkeypatch):
    for name, value in SERVER.items():
        monkeypatch.setenv(name, value)
    user_keys.forget()
    user_gateway.forget()
    yield
    user_keys.forget()
    user_gateway.forget()


def reply(text="Hello.", tool=None):
    message = {"role": "assistant", "content": text}
    if tool:
        message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": tool, "arguments": "{}"}}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


class Recorder(httpx.BaseTransport):
    def __init__(self, answers=None):
        self.requests, self.answers = [], list(answers or [])

    def handle_request(self, request):
        request.read()
        self.requests.append(request)
        return httpx.Response(200, json=self.answers.pop(0) if self.answers else reply())

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def no_server_key(requests):
    for request in requests:
        sent = json.dumps(dict(request.headers)) + request.content.decode(errors="ignore")
        assert not any(value in sent for value in SERVER.values()), request.url
        assert not any(name.lower().startswith("x-valr-") for name in request.headers), request.url


# Which gateway.

def test_the_owner_and_no_workspace_get_the_shared_gateway(monkeypatch):
    shared = object()
    monkeypatch.setattr(llm_gateway, "shared", lambda: shared)
    for user in (tenant.OWNER, None):
        with tenant.as_user(user):
            assert llm_gateway.for_current() is shared


def test_a_guest_without_a_key_gets_key_missing_and_no_request(monkeypatch):
    def refuse():
        raise AssertionError("a guest must never reach the shared gateway")
    monkeypatch.setattr(llm_gateway, "shared", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a: pytest.fail("no HTTP for a guest"))
    user_keys.remember(GUEST, {"speech": user_keys.UserKey("deepgram", "", KEY)})
    with tenant.as_user(GUEST), pytest.raises(user_keys.KeyMissing, match="Settings → API keys"):
        llm_gateway.for_current()


def test_a_guest_gets_its_own_gateway_cached_per_key(monkeypatch):
    monkeypatch.setattr(llm_gateway, "shared", lambda: pytest.fail("not the shared gateway"))
    user_keys.remember(GUEST, {"llm": user_keys.UserKey("openai", "gpt-6-luna", KEY)})
    with tenant.as_user(GUEST):
        first = llm_gateway.for_current()
        assert isinstance(first, UserGateway) and first.chat_provider == "openai" and first.model_id == "gpt-6-luna"
        assert llm_gateway.for_current() is first
        user_keys.remember(GUEST, {"llm": user_keys.UserKey("openai", "gpt-6-luna", KEY + "x")})
        assert llm_gateway.for_current() is not first  # A new key: a new gateway.
    assert KEY not in repr(first)


# Request shaping, per provider.

@pytest.mark.parametrize("provider,url,model,limit_field", [
    ("openai", "https://api.openai.com/v1/chat/completions", "gpt-6-luna", "max_completion_tokens"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-3.1-flash-lite",
     "max_tokens"),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "google/gemini-3.1-flash-lite", "max_tokens"),
    ("groq", "https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b", "max_completion_tokens"),
])
def test_chat_providers_get_their_endpoint_model_and_key(provider, url, model, limit_field):
    transport = Recorder()
    gateway = UserGateway(provider, KEY, transport=transport)
    response = gateway.client(timeout=5, priority="interactive", backend="anthropic").messages.create(
        model="claude-sonnet-5-5", max_tokens=200, system="Be brief.", backend="anthropic",
        messages=[{"role": "user", "content": "hi"}], thinking={"type": "disabled"})
    assert response.content[0].text == "Hello." and response.model == model
    (request,) = transport.requests
    body = json.loads(request.content)
    assert str(request.url) == url and request.headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == model and body[limit_field] == 200
    assert "reasoning_format" not in body and "provider" not in body  # No owner pin, no Groq-only fields.
    no_server_key(transport.requests)


def test_json_mode_and_tools_keep_working_on_another_provider():
    transport = Recorder([reply('{"ok": true}'), reply("", tool="classify")])
    gateway = UserGateway("openai", KEY, transport=transport)
    client = gateway.client()
    out = client.messages.create(model="claude-haiku-5-5", max_tokens=50, json_output=True,
                                 messages=[{"role": "user", "content": "json please"}])
    assert out.content[0].text == '{"ok": true}'
    out = client.messages.create(model="claude-haiku-5-5", max_tokens=50, messages=[{"role": "user", "content": "go"}],
                                 tools=[{"name": "classify", "description": "x", "input_schema": {"type": "object"}}],
                                 tool_choice={"type": "any"})
    assert out.content[-1].type == "tool_use" and out.stop_reason == "tool_use"
    first, second = transport.bodies()
    assert first["response_format"] == {"type": "json_object"}
    assert second["tools"][0]["function"]["name"] == "classify" and second["tool_choice"] == "required"


def test_web_search_is_the_keyless_search_on_a_guests_chat_provider():
    transport = Recorder([reply("Juniper builds kites. [https://juniper.example/about]")])
    searches = []

    def search(query, count):
        searches.append(query)
        return [{"title": "About", "url": "https://juniper.example/about", "snippet": "Kites."}], "duckduckgo"
    gateway = UserGateway("gemini", KEY, transport=transport)
    gateway.search = search
    out = gateway.client().messages.create(
        model="claude-haiku-5-5", max_tokens=300, messages=[{"role": "user", "content": "Search: juniper kites"}],
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 1}])
    assert searches == ["juniper kites"]
    assert [b.type for b in out.content][:2] == ["server_tool_use", "web_search_tool_result"]
    assert out.content[2].citations[0].url == "https://juniper.example/about"
    no_server_key(transport.requests)


def test_anthropic_keys_go_direct_at_the_workspaces_model():
    calls = []

    class Direct:
        def __init__(self):
            self.messages = self
            self.beta = type("B", (), {"messages": self})()

        def create(self, **kwargs):
            calls.append(kwargs)
            return "message"
    gateway = UserGateway("anthropic", KEY, direct=Direct())
    out = gateway.client(backend="groq").messages.create(model="claude-opus-5-5", max_tokens=10, backend="groq",
                                                         json_output=True, messages=[{"role": "user", "content": "x"}])
    assert out == "message" and calls[0]["model"] == "claude-haiku-5-5"
    assert "backend" not in calls[0] and "json_output" not in calls[0]


def test_the_anthropic_client_carries_only_the_workspaces_key():
    gateway = UserGateway("anthropic", KEY)
    assert gateway._direct.api_key == KEY


def test_a_guests_gateway_has_no_server_key_client_and_never_falls_back():
    gateway = UserGateway("openai", KEY, transport=Recorder())
    assert gateway.fallback is False
    for method in (gateway.openrouter_client, lambda: gateway.direct_client().messages.create()):
        with pytest.raises(NoServerKey):
            method()

    class Failing(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(503, json={"error": {"message": "busy"}})
    failing = UserGateway("openai", KEY, transport=Failing())
    with pytest.raises(anthropic.InternalServerError):
        failing.client().messages.create(model="claude-haiku-5-5", max_tokens=5, messages=[{"role": "user", "content": "x"}])
    assert failing.stats.get("fallback", 0) == 0


def test_the_owner_gateway_still_serves_groq_as_before():
    transport = Recorder()
    limited = llm_gateway.groq_backend.GroqTransport(llm_gateway.groq_backend.Limiters(), inner=transport)
    gateway = llm_gateway.Gateway(mode="groq", groq_key="owner", groq_transport=limited, fallback=False)
    gateway.client().messages.create(model="claude-haiku-5-5", max_tokens=20, messages=[{"role": "user", "content": "x"}])
    body = transport.bodies()[0]
    assert body["model"] == llm_gateway.groq_backend.groq_model("claude-haiku-5-5")
    assert body["reasoning_effort"] == "none"  # Groq's own fields, unchanged for the owner.
