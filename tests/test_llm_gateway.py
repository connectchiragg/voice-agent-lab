"""The background-Claude gateway: OpenRouter (BYOK) or Anthropic direct, fallback and the
is_byok guard. No network: both clients are fakes."""
from types import SimpleNamespace

import httpx
import pytest

anthropic = pytest.importorskip("anthropic", reason="anthropic is only in the worker image")
from app import llm_gateway  # noqa: E402
from app.llm_gateway import ByokRequired, Gateway, openrouter_model  # noqa: E402


class FakeClient:
    def __init__(self, is_byok=None, error=None):
        self.calls = []
        self.is_byok, self.error = is_byok, error
        self.messages = SimpleNamespace(create=self.create, stream=self.create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create, stream=self.create))

    def create(self, *args, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(usage=SimpleNamespace(is_byok=self.is_byok, cost=0), content=[])


def status_error(code=502):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/messages")
    return anthropic.APIStatusError("upstream", response=httpx.Response(code, request=request), body=None)


def gateway(openrouter, direct, **kwargs):
    return Gateway(mode="openrouter", openrouter_key="k", openrouter=openrouter, direct=direct, **kwargs)


def test_model_ids_map_to_openrouter_slugs():
    assert openrouter_model("claude-haiku-5-5") == "anthropic/claude-haiku-5.5"
    assert openrouter_model("claude-sonnet-5-5") == "anthropic/claude-sonnet-5.5"
    assert openrouter_model("anthropic/claude-opus-5.5") == "anthropic/claude-opus-5.5"


def test_openrouter_requests_are_pinned_to_anthropic_and_keep_the_callers_body():
    openrouter, direct = FakeClient(is_byok=True), FakeClient()
    client = gateway(openrouter, direct).client()
    client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[],
                           tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
                           extra_body={"output_config": {"effort": "low"}})
    sent = openrouter.calls[0]
    assert sent["model"] == "anthropic/claude-haiku-5.5"
    assert sent["extra_body"] == {"output_config": {"effort": "low"},
                                  "provider": {"only": ["anthropic"], "allow_fallbacks": False}}
    assert sent["tools"][0]["max_uses"] == 1
    assert sent["thinking"] == {"type": "adaptive", "display": "omitted"}  # Anthropic's default.
    assert direct.calls == []


def test_crewai_output_format_moves_to_output_config_and_string_fallbacks_are_dropped():
    openrouter = FakeClient(is_byok=True)
    gw = gateway(openrouter, FakeClient())
    gw.client().beta.messages.create(
        model="claude-sonnet-5-5", max_tokens=10, messages=[], betas=["structured-outputs-2025-11-13"],
        thinking={"type": "adaptive"},
        extra_body={"output_format": {"type": "json_schema", "schema": {}}, "output_config": {"effort": "high"},
                    "fallbacks": "default"})
    sent = openrouter.calls[0]
    assert sent["extra_body"]["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": {}}}
    assert "output_format" not in sent["extra_body"] and "fallbacks" not in sent["extra_body"]
    assert sent["thinking"] == {"type": "adaptive"}  # The caller's choice is kept.
    assert gw.stats["openrouter_fallbacks_dropped"] == 1


def test_anthropic_mode_goes_direct_unchanged():
    openrouter, direct = FakeClient(), FakeClient()
    gw = Gateway(mode="anthropic", openrouter_key="", openrouter=openrouter, direct=direct)
    gw.client().messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    assert openrouter.calls == [] and direct.calls[0]["model"] == "claude-haiku-5-5"
    assert "extra_body" not in direct.calls[0]


def test_an_openrouter_error_falls_back_to_anthropic_direct_when_enabled():
    openrouter, direct = FakeClient(error=status_error()), FakeClient()
    gw = gateway(openrouter, direct, fallback=True)
    gw.client().messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    assert direct.calls[0]["model"] == "claude-haiku-5-5" and "extra_body" not in direct.calls[0]
    assert gw.stats["fallback"] == 1


def test_an_openrouter_error_raises_when_fallback_is_off():
    gw = gateway(FakeClient(error=status_error(402)), FakeClient(), fallback=False)
    with pytest.raises(anthropic.APIStatusError):
        gw.client().messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])


def test_byok_guard_refuse_trips_and_sends_later_calls_direct(caplog):
    openrouter, direct = FakeClient(is_byok=False), FakeClient()
    gw = gateway(openrouter, direct, fallback=True, guard="refuse")
    client = gw.client()
    client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    assert gw.tripped and "VALR BYOK ALERT" in caplog.text
    client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    assert len(openrouter.calls) == 1 and len(direct.calls) == 1


def test_byok_guard_refuse_without_fallback_raises_on_later_calls():
    gw = gateway(FakeClient(is_byok=False), FakeClient(), fallback=False, guard="refuse")
    client = gw.client()
    client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    with pytest.raises(ByokRequired):
        client.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])


def test_byok_guard_warn_and_off_keep_using_openrouter(caplog):
    for guard, alerted in (("warn", True), ("off", False)):
        caplog.clear()
        openrouter = FakeClient(is_byok=False)
        gw = gateway(openrouter, FakeClient(), guard=guard)
        for _ in range(2):
            gw.client().messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
        assert len(openrouter.calls) == 2 and not gw.tripped
        assert ("VALR BYOK ALERT" in caplog.text) is alerted


def test_byok_true_counts_and_does_not_alert(caplog):
    gw = gateway(FakeClient(is_byok=True), FakeClient())
    gw.client().messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[])
    assert gw.stats["openrouter_byok"] == 1 and "ALERT" not in caplog.text


def test_mode_defaults_and_config_errors(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert Gateway().mode == "openrouter"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert Gateway().mode == "anthropic"
    monkeypatch.setenv("LLM_GATEWAY", "openrouter")
    with pytest.raises(ValueError):
        Gateway()
    assert "OPENROUTER_API_KEY" in llm_gateway.required_key_missing()
    monkeypatch.setenv("LLM_GATEWAY", "bedrock")
    with pytest.raises(ValueError):
        Gateway()
    with pytest.raises(ValueError):
        Gateway(mode="anthropic", guard="maybe")
