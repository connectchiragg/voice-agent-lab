"""VALR interview researchers: tools, the people classifier, merging and the guardrail.
No network: Anthropic, Gmail and Fathom are faked.

Needs the worker's dependencies: .venv-worker/bin/python -m pytest tests/test_interview_agents.py
"""
import json
import time
from types import SimpleNamespace

import pytest

agents = pytest.importorskip("app.interview_agents", reason="crewai is only in the worker image")
from app import interview_worker, private_sources  # noqa: E402
from app.interview_agents import (PrivateInputs, classify_people, interview_tools,  # noqa: E402
                                  slot_guardrail, subject)
from app.interview_types import Person, SlotEvent  # noqa: E402
from app.interview_workflow import InterviewPartResult  # noqa: E402
from app.research_agents import Ctx, Meter  # noqa: E402

EVENT = SlotEvent("e1", "Interview with Acme", "2026-10-20T10:30:00+00:00",
                  "2026-10-20T11:30:00+00:00", company="Acme", company_domain="acme.com",
                  attendees=[Person("dev@acme.com", "Dev One")], description="System design.")


def ctx(part):
    return Ctx(part, "Acme", Meter(part), time.monotonic() + 60, 1.0)


def tool_named(tools, name):
    return next(t for t in tools if t.name == name)


def test_researchers_never_see_email_addresses():
    text = subject(EVENT, [Person("dev@acme.com", "Dev One"), Person("cto@acme.com", "")])
    assert "Dev One (acme.com)" in text and "cto (acme.com)" in text and "@" not in text


def test_private_tools_use_gmail_fathom_history_and_pushes(monkeypatch):
    calls = []
    monkeypatch.setattr(private_sources, "gmail_search",
                        lambda http, token, query, status="": (calls.append((token, query)) or
                                                               (f"Thread [source_url: https://mail.google.com/mail/#all/t1] {query}",
                                                                ["https://mail.google.com/mail/#all/t1"])))
    monkeypatch.setattr(private_sources, "fathom_search",
                        lambda http, terms: ("Fathom: no matching recordings.", []))
    monkeypatch.setattr(private_sources, "history",
                        lambda company, terms: ("- earlier round [source_url: valr://history/f/1]",
                                                ["valr://history/f/1"]))
    c = ctx("private")
    tools = interview_tools("private", c, EVENT, [], PrivateInputs("tok", "connected",
                                                                     [json.dumps({"id": "p", "excerpt": "x"})]))
    out = tool_named(tools, "gmail_search").run(query="acme.com")
    assert "t1" in out and calls == [("tok", "acme.com")]
    rounds = tool_named(tools, "past_rounds").run(company="Acme")
    assert "earlier round" in rounds and "valr://pushed/p" in rounds
    assert {"https://mail.google.com/mail/#all/t1", "valr://history/f/1", "valr://pushed/p"} <= c.seen_urls


def test_linkedin_pages_are_never_opened():
    tools = interview_tools("round", ctx("round"), EVENT, [], PrivateInputs())
    assert "never opened" in tool_named(tools, "read_page").run(url="https://www.linkedin.com/in/someone")
    assert [t.name for t in interview_tools("interviewers", ctx("interviewers"), EVENT, [],
                                            PrivateInputs())] == ["web_search"]


def fake_anthropic(monkeypatch, reply):
    usage = SimpleNamespace(input_tokens=300, output_tokens=40, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, server_tool_use=None)
    response = SimpleNamespace(usage=usage, content=[SimpleNamespace(type="text", text=reply)],
                               stop_reason="end_turn")
    prompts = []
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: (prompts.append(kwargs["messages"][0]["content"]) or response)))
    monkeypatch.setattr(agents, "anthropic_client", lambda: client)
    return prompts


def test_classifier_keeps_interviewers_and_drops_a_talent_partner_found_in_a_signature(monkeypatch):
    prompts = fake_anthropic(monkeypatch, '{"roles": {"p0": "interviewer", "p1": "recruiter_hr"}}')
    manager = Person("ravi@acme.com", "Ravi Kumar", organizer=True)
    partner = Person("meera@acme.com", "Meera Iyer")
    roles = classify_people([manager, partner],
                            "- from Meera Iyer (acme.com): Meera Iyer | Talent Partner, Acme", Meter("x"),
                            "acme.com")
    assert roles == {"ravi@acme.com": "interviewer", "meera@acme.com": "recruiter_hr"}
    assert "Talent Partner" in prompts[0] and "@" not in prompts[0].split("Evidence")[0]


def test_classifier_failure_falls_back_by_domain(monkeypatch):
    fake_anthropic(monkeypatch, "not json")
    roles = classify_people([Person("a@acme.com", "A"), Person("b@gmail.com", "B")], "", Meter("x"),
                            "acme.com")
    assert roles == {"a@acme.com": "interviewer", "b@gmail.com": "unknown"}


def part(name, ok=True, researched=()):
    return InterviewPartResult(name, ok, json.dumps({"summary": name, "claims": []}), "ev",
                               [f"https://{name}.example"], researched=list(researched))


def test_a_failed_part_never_replaces_an_earlier_good_one():
    stored = {"company": {"ok": True, "findings": {"summary": "good"}}}
    merged = interview_worker.merge_parts(stored, [part("company", ok=False)], [])
    assert merged["company"]["findings"]["summary"] == "good"


def test_merge_overlays_new_parts_and_drops_removed_interviewers():
    stored = {"private": {"ok": True, "findings": {"summary": "old"}, "seen_urls": []},
              "interviewers_by_email": {"dev@acme.com": {"ok": True}, "a@acme.com,b@acme.com": {"ok": True}}}
    merged = interview_worker.merge_parts(stored, [part("round"), part("interviewers", researched=["new@acme.com"]),
                                                   part("company", ok=False)], ["dev@acme.com", "a@acme.com"])
    assert merged["private"]["findings"]["summary"] == "old"  # Kept.
    assert merged["round"]["ok"] and "company" in merged  # The failed part kept its evidence.
    assert set(merged["interviewers_by_email"]) == {"a@acme.com,b@acme.com", "new@acme.com"}
    assert interview_worker.interviewed(merged) == {"b@acme.com", "new@acme.com"}


def test_slot_guardrail_wants_140_to_230_spoken_words_and_sourced_notes():
    check = slot_guardrail({"https://a.example"}, Meter("synth"))
    note = SimpleNamespace(fact="f", source_urls=["https://a.example"])
    good = SimpleNamespace(pydantic=SimpleNamespace(spoken=" ".join(["word"] * 180), notes=[note]))
    assert check(good)[0]
    short = SimpleNamespace(pydantic=SimpleNamespace(spoken="Too short.", notes=[note]))
    assert not check(short)[0]
    digits = SimpleNamespace(pydantic=SimpleNamespace(spoken=" ".join(["word"] * 170) + " 40 LPA",
                                                      notes=[note]))
    assert not check(digits)[0]
