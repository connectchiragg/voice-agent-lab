"""Browser calls in a guest's workspace (app/user_voice.py): the owner's engines and Groq stay as
they are; a guest's call needs its own Deepgram and LLM keys, runs Flux and Aura on its key and
streams replies from its own provider. No network: httpx.MockTransport. Keys are made up."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm, main, tenant, user_keys, user_voice
from app.auth import Identity, gate
from app.groq_api import ProviderError
from app.llm import ModelUsed, ToolCallRequest
from tests.test_pipeline import make_engines

GUEST = "u_" + "4" * 20
LLM_KEY = user_keys.UserKey("openai", "gpt-6-luna", "sk-guest-made-up-1111")
SPEECH_KEY = user_keys.UserKey("deepgram", "", "dg-guest-made-up-2222")
KEYS = {"llm": LLM_KEY, "speech": SPEECH_KEY}
MESSAGES = [{"role": "system", "content": "You are VALR."}, {"role": "user", "content": "weather?"},
            {"role": "system", "content": "Local time: 4 pm."}]
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather.",
                                            "parameters": {"type": "object", "properties": {}}}}]


def run(coro):
    return asyncio.run(coro)


async def collect(stream):
    return [event async for event in stream]


@pytest.fixture(autouse=True)
def fresh():
    user_keys.forget()
    user_voice._pinned.clear()
    yield
    user_keys.forget()
    user_voice._pinned.clear()


def sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def client_for(text, seen, status=200):
    def handle(request):
        request.read()
        seen.append(request)
        return httpx.Response(status, text=text, headers={"content-type": "text/event-stream"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


# Before the call.

def test_the_owner_needs_no_lookup(monkeypatch):
    async def load(*args, **kwargs):
        raise AssertionError("the owner's call never reads user keys")
    monkeypatch.setattr(user_keys, "load_current", load)
    for user in (tenant.OWNER, None):
        with tenant.as_user(user):
            assert run(user_voice.prepare()) is None
            assert run(user_voice.route()) is None


@pytest.mark.parametrize("keys,message", [({}, "Voice needs a Deepgram key"),
                                          ({"llm": LLM_KEY}, "Voice needs a Deepgram key"),
                                          ({"speech": SPEECH_KEY}, "Add your API key in Settings → API keys")])
def test_a_guest_without_both_keys_cannot_call(monkeypatch, keys, message):
    async def load(*args, **kwargs):
        return keys
    monkeypatch.setattr(user_keys, "load_current", load)
    with tenant.as_user(GUEST), pytest.raises(user_keys.KeyMissing, match=message):
        run(user_voice.prepare())


def test_a_guests_engines_use_its_deepgram_key_and_skip_the_groq_gate():
    engines, _ = make_engines(allowed=False)
    guest = user_voice.for_guest(engines, KEYS)
    listener, speaker = guest.new_listener(lambda event: None), guest.new_speaker()
    for settings in (listener.settings, speaker.settings):
        assert settings.api_key == SPEECH_KEY.key and settings.host == user_voice.GUEST_DEEPGRAM_HOST
    assert guest.may_speculate() and guest.reply is engines.reply
    assert not engines.may_speculate()  # The owner's engines are untouched.


def test_keys_are_pinned_while_a_call_is_open(monkeypatch):
    async def load(*args, **kwargs):
        raise AssertionError("pinned: no lookup during the call")
    monkeypatch.setattr(user_keys, "load_current", load)
    with tenant.as_user(GUEST):
        pinned = user_voice.opened(KEYS)
        assert run(user_voice.route()) == LLM_KEY
    user_voice.closed(pinned)
    assert GUEST not in user_voice._pinned


def test_without_an_llm_key_the_reply_says_what_to_add(monkeypatch):
    async def load(*args, **kwargs):
        return {"speech": SPEECH_KEY}
    monkeypatch.setattr(user_keys, "load_current", load)
    with tenant.as_user(GUEST), pytest.raises(ProviderError, match="Settings → API keys"):
        run(user_voice.route())


# The reply stream.

def test_an_openai_compatible_stream_yields_text_then_tool_calls():
    seen = []
    text = sse({"choices": [{"delta": {"content": "Let me "}}]}, {"choices": [{"delta": {"content": "check."}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "get_weather",
                                                                                         "arguments": "{\"ci"}}]}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ty\": \"Pune\"}"}}]}}]})
    events = run(collect(user_voice.stream(LLM_KEY, MESSAGES, TOOLS, "required", 400, client_for(text, seen))))
    assert events == [ModelUsed("openai:gpt-6-luna"), "Let me ", "check.",
                      [ToolCallRequest("t1", "get_weather", '{"city": "Pune"}')]]
    (request,) = seen
    body = json.loads(request.content)
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {LLM_KEY.key}"
    assert body["model"] == "gpt-6-luna" and body["stream"] is True and body["max_completion_tokens"] == 400
    assert body["reasoning_effort"] == "low" and body["tool_choice"] == "required" and body["tools"] == TOOLS


def test_gemini_gets_one_system_message_and_max_tokens():
    seen = []
    key = user_keys.UserKey("gemini", "gemini-3.1-flash-lite", "AIza-made-up-3333")
    run(collect(user_voice.stream(key, MESSAGES, None, None, 300, client_for(sse(), seen))))
    body = json.loads(seen[0].content)
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == "You are VALR.\n\nLocal time: 4 pm."
    assert body["max_tokens"] == 300 and "max_completion_tokens" not in body


def test_an_anthropic_stream_is_shaped_and_parsed_for_the_messages_api():
    seen = []
    key = user_keys.UserKey("anthropic", "claude-haiku-5-5", "sk-ant-made-up-4444")
    history = MESSAGES[:2] + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t0", "type": "function", "function": {
            "name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t0", "content": "Sunny."}]
    text = sse({"type": "message_start", "message": {}},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Sunny"}},
               {"type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}}},
               {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
               {"type": "message_stop"})
    events = run(collect(user_voice.stream(key, history, TOOLS, "required", 200, client_for(text, seen))))
    assert events == [ModelUsed("anthropic:claude-haiku-5-5"), "Sunny", [ToolCallRequest("t1", "get_weather", "{}")]]
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == key.key and "authorization" not in request.headers
    assert body["system"] == "You are VALR." and body["tool_choice"] == {"type": "any"}
    assert body["tools"] == [{"name": "get_weather", "description": "Weather.",
                              "input_schema": {"type": "object", "properties": {}}}]
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "weather?"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t0", "name": "get_weather", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "Sunny."}]}]


@pytest.mark.parametrize("status,code,words", [(401, 503, "rejected your API key"), (429, 429, "rate limit"),
                                               (404, 503, "couldn't use the model"), (500, 502, "unavailable")])
def test_provider_refusals_become_clear_errors(status, code, words):
    with pytest.raises(ProviderError, match=words) as caught:
        run(collect(user_voice.stream(LLM_KEY, MESSAGES, None, None, 100, client_for("{}", [], status))))
    assert caught.value.status_code == code


def test_stream_model_serves_a_guest_from_its_own_key_never_groq(monkeypatch):
    seen = []
    monkeypatch.setattr(user_voice, "http", lambda: client_for(sse({"choices": [{"delta": {"content": "Hi."}}]}), seen))
    monkeypatch.setattr(llm, "groq_client", lambda: pytest.fail("a guest's reply must not use the owner's Groq"))
    requests_before = len(llm.groq_api.recent_requests)
    with tenant.as_user(GUEST):
        pinned = user_voice.opened(KEYS)
        events = run(collect(llm.stream_model([{"role": "user", "content": "hi"}])))
        user_voice.closed(pinned)
    assert events == [ModelUsed("openai:gpt-6-luna"), "Hi."]
    assert len(seen) == 1 and len(llm.groq_api.recent_requests) == requests_before  # Not counted as the owner's.


# The call surfaces.

@pytest.fixture
def as_guest(monkeypatch):
    async def guest(connection):
        return Identity("guest@example.com", "session")
    monkeypatch.setattr(gate, "identify", guest)
    monkeypatch.setenv("VALR_OWNER_EMAILS", "owner@example.com")
    monkeypatch.setenv("VALR_OPEN_SIGNUP", "1")

    async def load(*args, **kwargs):
        assert tenant.get() == tenant.user_id("guest@example.com", {"owner@example.com"})
        return {}
    monkeypatch.setattr(user_keys, "load_current", load)
    engines, _ = make_engines()
    monkeypatch.setattr(main.app.state, "engines", engines, raising=False)


def test_the_call_socket_tells_a_guest_without_keys_what_to_add(as_guest):
    start = {"type": "start", "sample_rate": 16000, "encoding": "pcm_f32le", "echo_cancelled": True}
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(start)
        assert socket.receive_json() == {"type": "error", "error": user_keys.ADD_SPEECH_KEY}


def test_no_turn_credentials_for_a_guest_without_keys(as_guest, monkeypatch):
    from app import webrtc
    monkeypatch.setattr(webrtc, "call_ice_servers", lambda: pytest.fail("no TURN credentials for this guest"))
    response = TestClient(main.app).get("/webrtc/ice-servers")
    assert response.status_code == 409 and response.json()["detail"] == user_keys.ADD_SPEECH_KEY
