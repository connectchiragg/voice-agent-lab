"""The groq backend of the LLM gateway: model table, translation, rate limiter, 429/413
handling, JSON retry, the free web search and the Anthropic fallback. No network: Groq is an
httpx.MockTransport, search a fake, time a fake clock."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app import groq_backend as gb
from app.research_jobs import price


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def chat(content="ok", tool_calls=None, finish="stop", prompt=100, completion=10):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "x", "model": "qwen/qwen3.8-27b", "choices": [{"index": 0, "message": message,
            "finish_reason": finish}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


class FakeGroq:
    """Answers queued (status, json, headers) and records each request body."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        status, body, headers = self.replies.pop(0) if self.replies else (200, chat(), {})
        return httpx.Response(status, json=body, headers=headers)


def transport(fake, clock=None, **kwargs):
    clock = clock or Clock()
    return gb.GroqTransport(gb.Limiters(clock=clock), inner=httpx.MockTransport(fake), sleep=clock.sleep, **kwargs)


# Model table.

def test_canonical_names_map_to_a_background_model_never_a_voice_model(monkeypatch):
    for name in ("GROQ_LLM_MODEL", "LLM_FALLBACK_MODEL", "VALR_GROQ_FAST_MODEL", "VALR_GROQ_STRONG_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert gb.groq_model("claude-haiku-5-5") == "qwen/qwen3.8-27b"
    assert gb.groq_model("claude-sonnet-5-5") == "qwen/qwen3.8-27b"
    assert gb.groq_model("openai/gpt-oss-safeguard-20b") == "openai/gpt-oss-safeguard-20b"
    monkeypatch.setenv("VALR_GROQ_FAST_MODEL", "x/fast")
    assert gb.groq_model("claude-haiku-5-5") == "x/fast"
    for voice in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        with pytest.raises(ValueError, match="live calls"):
            gb.groq_model(voice)
    monkeypatch.setenv("VALR_GROQ_STRONG_MODEL", "x/live")
    monkeypatch.setenv("GROQ_LLM_MODEL", "x/live")
    with pytest.raises(ValueError, match="live calls"):
        gb.groq_model("claude-sonnet-5-5")


def test_reasoning_is_off_on_the_free_tier_unless_enabled(monkeypatch):
    monkeypatch.delenv("VALR_GROQ_REASONING", raising=False)
    assert gb.reasoning_for("claude-sonnet-5-5", None, "high") == "none"
    monkeypatch.setenv("VALR_GROQ_REASONING", "1")
    assert gb.reasoning_for("claude-haiku-5-5") == "none"
    assert gb.reasoning_for("claude-sonnet-5-5") == "default"
    assert gb.reasoning_for("claude-sonnet-5-5", {"type": "disabled"}) == "none"
    assert gb.reasoning_for("claude-sonnet-5-5", None, "low") == "none"
    assert gb.reasoning_for("claude-haiku-5-5", None, "high") == "default"


def test_free_models_price_at_zero_and_claude_keeps_its_price():
    assert price("qwen/qwen3.8-27b", 10_000, 5_000, searches=3) == 0.0
    assert price("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    response = SimpleNamespace(valr={"backend": "groq", "model": "qwen/qwen3.8-27b"})
    assert gb.billed_model(response, "claude-haiku-5-5") == "qwen/qwen3.8-27b"
    assert gb.billed_model(SimpleNamespace(), "claude-haiku-5-5") == "claude-haiku-5-5"


# Sizes.

def test_durations_from_groq_headers():
    assert gb.parse_duration("1m26.4s") == pytest.approx(86.4)
    assert gb.parse_duration("232ms") == pytest.approx(0.232)
    assert gb.parse_duration("18") == 18
    assert gb.parse_duration(None) == 0


def test_fit_cuts_the_middle_of_the_longest_message():
    body = {"messages": [{"role": "system", "content": "rules"},
                         {"role": "user", "content": "HEAD " + "x" * 40_000 + " TAIL"}]}
    cut = gb.fit(body, 2000)
    text = body["messages"][1]["content"]
    assert cut > 30_000 and gb.estimate_tokens(body) <= 2000
    assert text.startswith("HEAD") and text.endswith("TAIL") and gb.CUT in text
    assert body["messages"][0]["content"] == "rules"
    assert gb.fit(body, 2000) == 0


# The limiter.

def test_requests_per_minute_wait_and_interactive_uses_the_reserve():
    clock = Clock()
    limiter = gb.ModelLimiter("m", gb.Limits(4, 1000, 100_000, 1_000_000, 7000, 1000, 100), 0.25, clock)
    for _ in range(3):  # Background may use 3 of 4 a minute.
        limiter.acquire(10, "background", 120, clock.sleep)
    assert clock.slept == []
    limiter.acquire(10, "interactive", 120, clock.sleep)  # The reserve: no wait.
    assert clock.slept == []
    limiter.acquire(10, "background", 120, clock.sleep)
    assert sum(clock.slept) >= 59  # Waited for the minute to roll over.


def test_tokens_per_minute_queue_and_settle_with_real_usage():
    clock = Clock()
    limiter = gb.ModelLimiter("m", gb.Limits(100, 1000, 8000, 1_000_000, 7000, 1000, 100), 0.25, clock)
    ticket, waited = limiter.acquire(5000, "background", 120, clock.sleep)
    assert waited == 0
    limiter.settle(ticket, 1000)  # Used far less than estimated.
    _, waited = limiter.acquire(4000, "background", 120, clock.sleep)
    assert waited == 0
    _, waited = limiter.acquire(3000, "background", 120, clock.sleep)
    assert 59 <= waited <= 61


def test_a_daily_limit_beyond_the_wait_raises_rate_limited():
    clock = Clock()
    limiter = gb.ModelLimiter("m", gb.Limits(100, 4, 100_000, 1_000_000, 7000, 1000, 100), 0.25, clock)
    for _ in range(3):
        limiter.acquire(10, "background", 120, clock.sleep)
    with pytest.raises(gb.RateLimited, match="requests per day"):
        limiter.acquire(10, "background", 120, clock.sleep)
    limiter.acquire(10, "interactive", 120, clock.sleep)  # The classifier still gets through.


def test_server_daily_headers_hold_back_background_work_only():
    clock = Clock()
    limiter = gb.ModelLimiter("m", gb.limits_for("qwen/qwen3.8-27b"), 0.25, clock)
    limiter.update({"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "7.5s",
                    "x-ratelimit-remaining-requests": "200", "x-ratelimit-reset-requests": "1m26.4s"})
    assert limiter.wait_for(2000, "interactive") == (0.0, "")  # Remaining tokens are not trusted.
    wait, why = limiter.wait_for(2000, "background")
    assert why == "server daily requests" and 86 <= wait <= 87


# The transport.

def test_429_waits_for_retry_after_then_succeeds():
    clock = Clock()
    fake = FakeGroq((429, {"error": {"message": "rate"}}, {"retry-after": "7"}), (200, chat(), {}))
    client = httpx.Client(transport=transport(fake, clock), base_url=gb.GROQ_BASE_URL)
    response = client.post("/chat/completions", json={"model": "qwen/qwen3.8-27b", "messages": []},
                           headers={gb.PRIORITY_HEADER: "interactive"})
    assert response.status_code == 200
    assert response.headers["x-valr-retries"] == "1" and float(response.headers["x-valr-queued-s"]) >= 7
    assert len(fake.bodies) == 2 and gb.PRIORITY_HEADER not in fake.headers[0]


def test_413_shrinks_the_request_and_retries():
    fake = FakeGroq((413, {"error": {"message": "Request too large"}}, {}), (200, chat(), {}))
    client = httpx.Client(transport=transport(fake), base_url=gb.GROQ_BASE_URL)
    big = "word " * 6000  # About 8,600 estimated tokens: shrunk before the first send.
    response = client.post("/chat/completions", json={"model": "qwen/qwen3.8-27b",
                                                      "messages": [{"role": "user", "content": big}]})
    assert response.status_code == 200
    first, second = (len(b["messages"][0]["content"]) for b in fake.bodies)
    assert first < len(big) and second < first
    assert int(response.headers["x-valr-shrunk-chars"]) > 0


def test_max_output_is_capped_and_booked_against_the_output_budget():
    clock = Clock()
    fake = FakeGroq(*[(200, chat(completion=600), {}) for _ in range(3)])
    t = transport(fake, clock)
    client = httpx.Client(transport=t, base_url=gb.GROQ_BASE_URL)
    first = client.post("/chat/completions", json={"model": "qwen/qwen3.8-27b", "messages": [],
                                                   "max_tokens": 5000}, headers={gb.PRIORITY_HEADER: "interactive"})
    assert fake.bodies[0]["max_completion_tokens"] == 1000 and "max_tokens" not in fake.bodies[0]
    assert first.headers["x-valr-output-capped"] == "5000"
    client.post("/chat/completions", json={"model": "qwen/qwen3.8-27b", "messages": [], "max_tokens": 5000})
    assert fake.bodies[1]["max_completion_tokens"] == 750  # Background share.
    assert clock.slept and sum(clock.slept) >= 59  # 600 used + 750 asked > 750: waited for the minute.
    limiter = t.limiters.get("qwen/qwen3.8-27b")
    assert limiter.snapshot()["minute_output"] == 600  # Settled to the reported usage.


def test_a_too_large_429_is_returned_not_retried():
    fake = FakeGroq((429, {"error": {"message": "Request too large for model on output tokens per minute"}}, {}))
    client = httpx.Client(transport=transport(fake), base_url=gb.GROQ_BASE_URL)
    response = client.post("/chat/completions", json={"model": "qwen/qwen3.8-27b", "messages": []})
    assert response.status_code == 429 and len(fake.bodies) == 1


def test_voice_models_are_refused_at_the_transport():
    client = httpx.Client(transport=transport(FakeGroq()), base_url=gb.GROQ_BASE_URL)
    with pytest.raises(ValueError, match="live calls"):
        client.post("/chat/completions", json={"model": "openai/gpt-oss-120b", "messages": []})


def test_an_exhausted_budget_answers_a_local_429_instead_of_hanging():
    fake = FakeGroq()
    t = transport(fake, max_wait=5)
    t.limiters.get("qwen/qwen3.8-27b").day_requests = 10_000
    client = httpx.Client(transport=t, base_url=gb.GROQ_BASE_URL)
    response = client.post("/chat/completions", json={"model": "qwen/qwen3.8-27b", "messages": []})
    assert response.status_code == 429 and fake.bodies == []
    assert response.json()["error"]["code"] == "valr_queue_limit"


# Free search.

DDG = """
<div class="result results_links"><a rel="nofollow" class="result__a" href="https://en.wikipedia.org/wiki/Stripe,_Inc.">Stripe, <b>Inc.</b> - Wikipedia</a>
<a class="result__snippet" href="x">Stripe is a payments &amp; finance company.</a></div>
<div class="result result--ad"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a><a class="result__snippet" href="y">ad</a></div>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fstripe.com%2Fcontact&rut=1">Contact Stripe</a>
<a class="result__snippet" href="z">Offices.</a></div>
"""


def test_duckduckgo_results_are_parsed_without_ads_and_redirects():
    results = gb.parse_ddg(DDG, 5)
    assert [r["url"] for r in results] == ["https://en.wikipedia.org/wiki/Stripe,_Inc.", "https://stripe.com/contact"]
    assert results[0]["title"] == "Stripe, Inc. - Wikipedia"
    assert results[0]["snippet"] == "Stripe is a payments & finance company."


def test_search_falls_back_to_wikipedia_when_duckduckgo_fails():
    def handler(request):
        if "duckduckgo" in request.url.host:
            return httpx.Response(403)
        return httpx.Response(200, json={"query": {"search": [{"title": "Faroe Islands", "snippet": "<b>Faroe</b>"}]}})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    results, engine = gb.web_search("faroe", 3, http)
    assert engine == "wikipedia" and results[0]["url"] == "https://en.wikipedia.org/wiki/Faroe_Islands"


def test_background_work_waits_while_interactive_work_is_queued():
    clock = Clock()
    limiter = gb.ModelLimiter("m", gb.Limits(100, 1000, 100_000, 1_000_000, 7000, 1000, 100), 0.25, clock)
    limiter.interactive_waiting = 1
    wait, why = limiter.wait_for(10, "background")
    assert wait > 0 and why == "interactive work first"
    assert limiter.wait_for(10, "interactive") == (0.0, "")
