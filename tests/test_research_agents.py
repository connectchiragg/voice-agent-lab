"""VALR research agents: caps enforced in code, the ClaudeLLM subclass, guardrails and the
claim check. No network: Anthropic and web pages are faked.

Needs the worker's dependencies (Python 3.13 with crewai):
.venv-worker/bin/python -m pytest tests/test_research_agents.py
"""
import time
from types import SimpleNamespace

import httpx
import pytest

agents = pytest.importorskip("app.research_agents", reason="crewai is only in the worker image")
from app.research_agents import (CAPS, Claim, ClaudeLLM, Ctx, Findings, JobStop, Meter,  # noqa: E402
                                 Note, Synthesis)


def ctx(budget=1.0, seconds=60.0, part="web"):
    return Ctx(part, "Coorg", Meter(part), time.monotonic() + seconds, budget)


def search_response(urls=("https://a.example/1", "https://b.example/2"), results=9):
    usage = SimpleNamespace(input_tokens=12_000, output_tokens=500, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0,
                            server_tool_use=SimpleNamespace(web_search_requests=1))
    found = [SimpleNamespace(title=f"R{n}", url=f"https://r.example/{n}", page_age="") for n in range(results)]
    blocks = [SimpleNamespace(type="web_search_tool_result", content=found)]
    blocks += [SimpleNamespace(type="text", text=f"Fact {n}.",
                               citations=[SimpleNamespace(url=url, cited_text="cited")])
               for n, url in enumerate(urls)]
    return SimpleNamespace(content=blocks, usage=usage)


class FakeAnthropic:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response, self.error = response, error
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def test_searches_are_capped_in_code_deduplicated_and_metered(monkeypatch):
    fake = FakeAnthropic(search_response())
    monkeypatch.setattr(agents, "_anthropic", fake)
    c = ctx()
    out = agents.web_search(c, "coorg best time", "web")
    assert "Fact 0. [source_url: https://a.example/1]" in out
    assert out.count("https://r.example/") == CAPS.results_per_call  # Top results only.
    assert fake.calls[0]["tools"][0] == {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
    assert agents.web_search(c, "Coorg best time ", "web").startswith("(Same search")  # No second charge.
    for query in ("b", "c"):
        agents.web_search(c, query, "web")
    assert agents.web_search(c, "d", "web").startswith("LIMIT")
    assert len(fake.calls) == CAPS.searches_per_researcher
    assert c.meter.cap_hits["searches_per_researcher"] == 1
    assert c.meter.cap_hits["results_per_call"] == 3
    assert c.meter.rows[0]["searches"] == 1 and c.meter.usd() > 0.01
    assert "https://a.example/1" in c.seen_urls


def test_a_search_that_would_pass_the_budget_is_refused_before_it_runs(monkeypatch):
    fake = FakeAnthropic(search_response())
    monkeypatch.setattr(agents, "_anthropic", fake)
    c = ctx(budget=0.015)  # Soft stop at 70%: below one search's reserved estimate.
    assert agents.web_search(c, "q", "web").startswith("STOP (budget)")
    assert fake.calls == [] and c.stopped == "budget"


def test_pages_are_truncated_and_capped(monkeypatch):
    page = "<html><script>x()</script><body>" + "word " * 5000 + "</body></html>"
    monkeypatch.setattr(agents, "_http", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html"}))))
    c = ctx()
    out = agents.read_page(c, "https://a.example/page")
    assert len(out) < CAPS.page_chars + 200 and "x()" not in out
    assert c.meter.cap_hits["page_chars"] == 1
    for n in range(CAPS.pages_per_researcher - 1):
        agents.read_page(c, f"https://a.example/{n}")
    assert agents.read_page(c, "https://a.example/more").startswith("LIMIT")


def test_deadline_and_budget_stop_tools_softly_then_llm_calls_hard():
    c = ctx(seconds=CAPS.soft_stop_s - 1)
    assert c.check() == "deadline"  # Soft: tools refuse, the agent writes up.
    assert c.check(hard=True) == ""  # The deadline itself has not passed yet.
    c.killed = True
    llm = ClaudeLLM(model="claude-haiku-5-5", max_tokens=100, effort="low").bind(c, c.meter)
    with pytest.raises(JobStop):
        llm.call("hello")


def test_claude_llm_sends_effort_drops_forced_tool_choice_and_meters_usage():
    meter = Meter("synth")
    llm = ClaudeLLM(model="claude-sonnet-5-5", max_tokens=100, effort="medium").bind(None, meter)
    sent = []

    def create(*args, **kwargs):
        sent.append(kwargs)
    client = SimpleNamespace(messages=SimpleNamespace(create=create, stream=create),
                             beta=SimpleNamespace(messages=SimpleNamespace(create=create, stream=create)))
    llm._with_effort(client)
    client.beta.messages.create(model="m", extra_body={"output_format": {"type": "json_schema"}})
    assert sent[0]["extra_body"] == {"output_format": {"type": "json_schema"},
                                     "output_config": {"effort": "medium"}}
    params = llm._prepare_completion_params(
        [{"role": "user", "content": "hi"}],
        tools=[{"name": "only", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
        available_functions={"only": lambda: None})
    assert "tool_choice" not in params  # Claude Sonnet 5.5 rejects forced tool use.
    llm._track_token_usage_internal({"input_tokens": 1000, "output_tokens": 100,
                                     "cached_prompt_tokens": 400, "cache_creation_tokens": 100})
    assert meter.rows[0] | {} == dict(part="synth", kind="agent", model="claude-sonnet-5-5",
                                      input_tokens=500, output_tokens=100, cache_read=400,
                                      cache_write=100, searches=0)


def test_findings_and_notes_are_truncated_to_their_limits():
    findings = Findings(summary="word " * 500,
                        claims=[Claim(claim="c " * 100, source_url="https://a") for _ in range(20)])
    assert len(findings.summary.split()) == CAPS.findings_summary_words
    assert len(findings.claims) == CAPS.findings_max_claims
    assert len(findings.claims[0].claim.split()) == CAPS.claim_max_words
    notes = Synthesis(spoken="s", notes=[Note(fact="f", source_urls=["https://a"])] * 30).notes
    assert len(notes) == CAPS.notes_max


def test_the_synthesis_guardrail_wants_speakable_sourced_text():
    meter = Meter("synth")
    check = agents.synthesis_guardrail({"https://a.example/1"}, meter)
    good = " ".join(["word"] * 100) + ". Want more on any of that?"
    ok = SimpleNamespace(pydantic=Synthesis(spoken=good, notes=[Note(fact="f", source_urls=["https://a.example/1/"])]))
    assert check(ok)[0] is True
    for spoken, notes, problem in (
            (good.replace("word", "5", 1), [], "Write numbers"),
            ("Too short.", [], "words"),
            (good, [Note(fact="made up", source_urls=["https://elsewhere"])], "no source URL")):
        verdict, message = check(SimpleNamespace(pydantic=Synthesis(spoken=spoken, notes=notes)))
        assert verdict is False and problem in message


def test_the_claim_check_drops_unsupported_sentences_and_fails_open(monkeypatch):
    spoken = ("Coorg works for a weekend. It is two hundred sixty kilometres from Bengaluru. "
              "Tripadvisor rates it three point nine. Want more on any of that?")
    parts = [{"part": "web", "ok": True, "evidence": "",
              "findings": {"claims": [{"claim": "Coorg is 260 km from Bengaluru", "source_url": "u"}]}}]
    verdict = SimpleNamespace(content=[SimpleNamespace(type="text", text='{"unsupported": [3, 4]}')],
                              usage=SimpleNamespace(input_tokens=900, output_tokens=20,
                                                    cache_read_input_tokens=0, cache_creation_input_tokens=0,
                                                    server_tool_use=None))
    monkeypatch.setattr(agents, "_anthropic", FakeAnthropic(verdict))
    meter = Meter("synth")
    checked = agents.check_claims(spoken, parts, meter)
    assert "Tripadvisor" not in checked and checked.endswith("Want more on any of that?")
    assert meter.cap_hits["claim_check_dropped"] == 1 and meter.rows[0]["kind"] == "claim_check"
    monkeypatch.setattr(agents, "_anthropic", FakeAnthropic(error=RuntimeError("down")))
    assert agents.check_claims(spoken, parts, Meter("synth")) == spoken


# Groq's free tier (LLM_GATEWAY=groq).

def test_meter_reads_openai_style_usage_and_groq_models_are_free():
    meter = Meter("web")
    meter.record("agent", "qwen/qwen3.8-27b", {"prompt_tokens": 3000, "completion_tokens": 200, "total_tokens": 3200})
    assert meter.rows[0]["input_tokens"] == 3000 and meter.rows[0]["output_tokens"] == 200
    assert meter.usd() == 0.0


def test_the_crew_runs_on_groq_through_the_gateway(monkeypatch):
    import json as _json

    from app import groq_backend, llm_gateway
    bodies = []

    def handler(request):
        bodies.append(_json.loads(request.content))
        return httpx.Response(200, json={"id": "x", "model": "qwen/qwen3.8-27b", "choices": [{
            "index": 0, "message": {"role": "assistant", "content": "All done."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}})
    transport = groq_backend.GroqTransport(groq_backend.Limiters(), inner=httpx.MockTransport(handler),
                                           sleep=lambda s: pytest.fail("the crew should not wait here"))
    monkeypatch.setenv("LLM_GATEWAY", "groq")
    monkeypatch.setenv("VALR_GROQ_REASONING", "1")
    monkeypatch.setattr(llm_gateway, "_shared", llm_gateway.Gateway(mode="groq", groq_key="k",
                                                                    groq_transport=transport))
    meter = Meter("synth")
    llm = agents.make_llm("claude-sonnet-5-5", 3000, "medium").bind(None, meter)
    assert isinstance(llm, agents.GroqLLM) and llm.model == "qwen/qwen3.8-27b"
    assert llm.call("Say done.") == "All done."
    sent = bodies[0]
    assert sent["model"] == "qwen/qwen3.8-27b" and sent["reasoning_effort"] == "default"
    assert sent["reasoning_format"] == "hidden" and sent["max_completion_tokens"] == 750  # Capped.
    assert "max_tokens" not in sent
    assert meter.rows[0]["model"] == "qwen/qwen3.8-27b" and meter.rows[0]["input_tokens"] == 50
    low = agents.make_llm("claude-haiku-5-5", 1500, "low").bind(None, meter)
    low.call("Hi")
    assert bodies[1]["reasoning_effort"] == "none" and "reasoning_format" not in bodies[1]
    monkeypatch.setenv("LLM_GATEWAY", "anthropic")
    assert isinstance(agents.make_llm("claude-haiku-5-5", 1500, "low"), ClaudeLLM)
