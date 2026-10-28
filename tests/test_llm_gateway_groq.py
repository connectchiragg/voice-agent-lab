"""The LLM gateway's groq backend: translation to chat completions and back, JSON retry, web
search emulation, fallback, per-call backend override and limits for callers. No network
(fake Groq transport, fake search). Needs the worker's dependencies (anthropic)."""
import json
from types import SimpleNamespace

import pytest

from app import groq_backend as gb

anthropic = pytest.importorskip("anthropic", reason="anthropic is only in the worker image")
from app import llm_gateway  # noqa: E402
from tests.test_groq_backend import Clock, FakeGroq, chat, transport  # noqa: E402


def gateway(fake, search=None, **kwargs):
    clock = Clock()
    return llm_gateway.Gateway(mode="groq", groq_key="k", groq_transport=transport(fake, clock),
                               search=search, **kwargs)


def test_a_messages_call_is_translated_and_comes_back_in_anthropic_shape():
    fake = FakeGroq((200, chat('{"keep": true}', prompt=870, completion=20), {}))
    client = gateway(fake).client(timeout=60, priority="interactive")
    response = client.messages.create(model="claude-haiku-5-5", max_tokens=300, system="Classify.",
                                      messages=[{"role": "user", "content": "An email. Reply in JSON."}],
                                      thinking={"type": "disabled"}, temperature=0, json_output=True)
    body = fake.bodies[0]
    assert body["model"] == "qwen/qwen3.8-27b" and body["reasoning_effort"] == "none"
    assert body["messages"] == [{"role": "system", "content": "Classify."},
                                {"role": "user", "content": "An email. Reply in JSON."}]
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 300 and body["temperature"] == 0  # Under the cap.
    assert "thinking" not in body and "json_output" not in body
    assert response.content[0].type == "text" and response.content[0].text == '{"keep": true}'
    assert response.usage.input_tokens == 870 and response.usage.output_tokens == 20
    assert response.stop_reason == "end_turn" and response.model == "qwen/qwen3.8-27b"
    assert response.valr["backend"] == "groq" and response.valr["rate_limited"] is False


def test_sonnet_class_reasons_hidden_when_enabled_and_a_schema_becomes_structured_output(monkeypatch):
    monkeypatch.setenv("VALR_GROQ_REASONING", "1")
    fake = FakeGroq((200, chat('{"a": 1}'), {}))
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    gateway(fake).client().messages.create(
        model="claude-sonnet-5-5", max_tokens=1000, system="s", messages=[{"role": "user", "content": "u"}],
        extra_body={"output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema}}})
    body = fake.bodies[0]
    assert body["reasoning_effort"] == "default" and body["reasoning_format"] == "hidden"
    assert body["max_completion_tokens"] == 750  # Background share of the 1,000 output tokens a minute.
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["response_format"]["json_schema"]["strict"] is False


def test_invalid_json_is_asked_for_once_more():
    fake = FakeGroq((200, chat("Sure! here it is"), {}), (200, chat('{"novelty": "new"}'), {}))
    response = gateway(fake).client().messages.create(
        model="claude-haiku-5-5", max_tokens=50, messages=[{"role": "user", "content": "JSON please"}],
        json_output=True)
    assert len(fake.bodies) == 2 and response.content[0].text == '{"novelty": "new"}'
    assert response.usage.input_tokens == 200  # Both attempts are metered.


def test_a_json_validate_failure_retries_in_json_object_mode():
    fake = FakeGroq((400, {"error": {"message": "json_validate_failed", "code": "json_validate_failed"}}, {}),
                    (200, chat('{"a": 1}'), {}))
    gateway(fake).client().messages.create(model="claude-haiku-5-5", max_tokens=50, json_output={"type": "object"},
                                           messages=[{"role": "user", "content": "json"}])
    assert fake.bodies[0]["response_format"]["type"] == "json_schema"
    assert fake.bodies[1]["response_format"] == {"type": "json_object"}


def test_tool_use_blocks_and_custom_tools_round_trip():
    call = [{"id": "c2", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "b"}'}}]
    fake = FakeGroq((200, chat(None, tool_calls=call, finish="tool_calls"), {}))
    response = gateway(fake).client().messages.create(
        model="claude-haiku-5-5", max_tokens=100,
        tools=[{"name": "lookup", "description": "Look up.", "input_schema": {"type": "object"}}],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": "find a"},
                  {"role": "assistant", "content": [{"type": "text", "text": "looking"},
                                                    {"type": "tool_use", "id": "c1", "name": "lookup", "input": {"q": "a"}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "A!"}]}])
    body = fake.bodies[0]
    assert body["tools"][0]["function"]["name"] == "lookup" and body["tool_choice"] == "required"
    assert body["messages"][1]["tool_calls"][0]["function"]["arguments"] == '{"q": "a"}'
    assert body["messages"][2] == {"role": "tool", "tool_call_id": "c1", "content": "A!"}
    block = response.content[0]
    assert block.type == "tool_use" and block.input == {"q": "b"} and response.stop_reason == "tool_use"


RESULTS = [{"title": "Stripe", "url": "https://stripe.com/about", "snippet": "Stripe builds payments."},
           {"title": "Wiki", "url": "https://en.wikipedia.org/wiki/Stripe", "snippet": "Founded 2010."}]


def test_web_search_becomes_a_free_search_and_cited_text_blocks():
    searched = []

    def search(query, limit):
        searched.append(query)
        return RESULTS, "duckduckgo"
    answer = "Stripe builds payment software. [https://stripe.com/about]\nIt was founded in 2010. [https://en.wikipedia.org/wiki/Stripe]\nUnsourced line."
    fake = FakeGroq((200, chat(answer), {}))
    response = gateway(fake, search).client().messages.create(
        model="claude-haiku-5-5", max_tokens=500, system="You search.",
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        messages=[{"role": "user", "content": "Search: stripe founded"}])
    assert searched == ["stripe founded"] and len(fake.bodies) == 1  # "Search:" needs no query call.
    sent = fake.bodies[0]["messages"]
    assert "square brackets" in sent[0]["content"] and sent[-1]["role"] == "tool"
    assert "https://stripe.com/about" in sent[-1]["content"]
    kinds = [b.type for b in response.content]
    assert kinds == ["server_tool_use", "web_search_tool_result", "text", "text", "text"]
    assert response.content[1].content[0].url == "https://stripe.com/about"
    cited = response.content[2]
    assert cited.text == "Stripe builds payment software." and cited.citations[0].url == "https://stripe.com/about"
    assert response.content[4].citations is None
    assert response.usage.server_tool_use.web_search_requests == 1


def test_web_search_without_a_query_asks_the_model_for_one_and_keeps_json():
    pick = [{"id": "q", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "acme corp"}'}}]
    fake = FakeGroq((200, chat(None, tool_calls=pick, finish="tool_calls"), {}),
                    (200, chat('{"company": "Acme", "source_url": "https://stripe.com/about"}'), {}))
    response = gateway(fake, lambda q, n: (RESULTS, "duckduckgo")).client().messages.create(
        model="claude-haiku-5-5", max_tokens=300, thinking={"type": "disabled"}, json_output=True,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        messages=[{"role": "user", "content": "Which company is this? Answer with JSON."}])
    assert fake.bodies[0]["tool_choice"] == "required"
    assert fake.bodies[1]["response_format"] == {"type": "json_object"} and "tools" not in fake.bodies[1]
    assert json.loads(response.content[-1].text)["company"] == "Acme"


class Direct:
    def __init__(self):
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def test_groq_errors_raise_anthropic_errors_without_fallback_by_default(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_FALLBACK", raising=False)
    fake = FakeGroq((503, {"error": {"message": "over capacity"}}, {}))
    gw = gateway(fake)
    assert gw.fallback is False
    with pytest.raises(anthropic.InternalServerError):
        gw.client().messages.create(model="claude-haiku-5-5", max_tokens=5, messages=[])


def test_fallback_to_anthropic_when_enabled_keeps_the_callers_kwargs():
    direct = Direct()
    fake = FakeGroq((503, {"error": {"message": "over capacity"}}, {}))
    gw = gateway(fake, direct=direct, fallback=True)
    response = gw.client().messages.create(model="claude-haiku-5-5", max_tokens=5, messages=[], json_output=True)
    assert direct.calls == [{"model": "claude-haiku-5-5", "max_tokens": 5, "messages": []}]
    assert response.valr["fallback"] is True and gw.stats["fallback"] == 1


def test_a_call_can_ask_for_anthropic_and_limits_are_exposed():
    direct = Direct()
    gw = gateway(FakeGroq(), direct=direct)
    gw.client().messages.create(model="claude-sonnet-5-5", max_tokens=5, messages=[], backend="anthropic",
                                json_output=True)
    assert direct.calls == [{"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": []}]
    limits = gw.limits("claude-sonnet-5-5")
    assert limits["model"] == "qwen/qwen3.8-27b" and limits["tpm"] == 8000 and limits["context"] == 131_072
    assert gw.input_budget("claude-sonnet-5-5") == 6300
    assert gw.limits("claude-sonnet-5-5", backend="anthropic")["tpm"] is None


def test_the_openai_client_for_crewai_shares_the_rate_limited_transport():
    fake = FakeGroq((200, chat("hi"), {}))
    gw = gateway(fake)
    reply = gw.openai_client("background").chat.completions.create(
        model="qwen/qwen3.8-27b", messages=[{"role": "user", "content": "hi"}])
    assert reply.choices[0].message.content == "hi"
    assert gb.PRIORITY_HEADER not in fake.headers[0]
    assert gw.limits("claude-haiku-5-5")["usage"]["minute_requests"] == 1  # One limiter for both paths.


def test_default_mode_prefers_groq_when_its_key_is_set(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert llm_gateway.default_mode() == "groq" and llm_gateway.required_key_missing() == ""
    monkeypatch.delenv("GROQ_API_KEY")
    assert llm_gateway.default_mode() == "openrouter"
    monkeypatch.setenv("LLM_GATEWAY", "groq")
    assert "GROQ_API_KEY" in llm_gateway.required_key_missing()


def test_a_feature_can_run_on_another_backend(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_RESEARCH", "anthropic")
    monkeypatch.delenv("LLM_GATEWAY_PROFILE", raising=False)
    assert llm_gateway.feature_backend("research") == "anthropic" and llm_gateway.feature_backend("profile") is None
    direct, fake = Direct(), FakeGroq()
    gw = gateway(fake, direct=direct)
    gw.client(backend=llm_gateway.feature_backend("research")).messages.create(
        model="claude-haiku-5-5", max_tokens=5, messages=[],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}])
    assert direct.calls[0]["tools"][0]["type"] == "web_search_20250305" and fake.bodies == []
    monkeypatch.setenv("LLM_GATEWAY_PROFILE", "bogus")
    with pytest.raises(ValueError):
        llm_gateway.feature_backend("profile")
