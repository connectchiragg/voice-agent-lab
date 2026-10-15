"""Interview mode in the app: owner-only gating, the prompts, the tools and the webhooks."""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import (agent, calendar_sync, callbacks, interviews, memory, private_sources, tools,
                 valr_hooks)
from app.callbacks import TurnContext
from app.interview_types import Person, SlotEvent
from app.llm import to_chat_messages
from app.persona import ASSISTANT_NAME

OWNER_PHONE = "+15550100001"
OWNER = f"tel:{OWNER_PHONE}"
STRANGER = "tel:+15550100099"


@pytest.fixture(autouse=True)
def owner_env(monkeypatch):
    monkeypatch.setenv("VALR_OWNER_PHONE", OWNER_PHONE)
    monkeypatch.setenv("VALR_OWNER_EMAILS", "owner@example.com")
    monkeypatch.delenv("VALR_OWNER_KEYS", raising=False)
    monkeypatch.setattr(interviews, "profile_block",
                        lambda: "Private candidate context:\n## Profile\nChirag, Bengaluru. Comp: target "
                                "secret-figure.\n\n## Self-evaluation from recent interviews\nWeak: system design.")
    interviews.briefings.clear()
    memory.calls.clear()
    yield
    memory.calls.clear()
    interviews.briefings.clear()


def as_caller(conversation_id: str, caller_key: str | None) -> None:
    if caller_key:
        memory.calls[conversation_id] = memory.CallerCall(caller_key, datetime.now(timezone.utc))


def test_owner_keys_are_the_phone_and_the_access_emails():
    assert interviews.is_owner(OWNER) and interviews.is_owner("email:owner@example.com")
    assert not interviews.is_owner(STRANGER) and not interviews.is_owner(None)
    assert not interviews.is_owner("email:someone@example.com")


def test_only_the_owner_is_offered_the_interview_tools():
    owner_names = {s["function"]["name"] for s in tools.schemas_for(OWNER)}
    other_names = {s["function"]["name"] for s in tools.schemas_for(STRANGER)}
    assert {"get_calendar", "research_interviews"} <= owner_names
    assert not {"get_calendar", "research_interviews"} & other_names
    assert {s["function"]["name"] for s in tools.schemas_for(None)} == other_names


def test_owner_only_tools_refuse_anyone_else_in_code():
    result = json.loads(asyncio.run(tools.run_tool("get_calendar", '{"day": "tomorrow"}', STRANGER, "c")))
    assert "no tool" in result["error"]
    direct = asyncio.run(interviews.get_calendar("tomorrow", caller_key=STRANGER))
    assert direct == interviews.NOT_OWNER


def test_prompt_has_the_profile_only_for_the_owner():
    as_caller("owner-call", OWNER)
    as_caller("other-call", STRANGER)
    owner_prompt = interviews.prompt_note("owner-call")
    assert f"You are {ASSISTANT_NAME}, Chirag's personal interview assistant" in owner_prompt
    assert "secret-figure" in owner_prompt and "<profile>" in owner_prompt
    assert interviews.prompt_note("other-call") == "" and interviews.prompt_note("anon") == ""
    system = to_chat_messages([{"role": "user", "parts": [{"text": "hi"}], "status": "completed"}],
                              interviews.prompt_note("other-call"))[0]["content"]
    assert "secret-figure" not in system and "Chirag" not in system
    assert interviews.reply_max_tokens("owner-call") == interviews.OWNER_REPLY_MAX_TOKENS
    assert interviews.reply_max_tokens("other-call") is None


def block(minutes_ahead=15):
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead, seconds=30)
    return {"block_id": "briefing-x", "start_utc": start, "label": "your 4:00 PM interview with Acme",
            "slot_keys": ["e1"],
            "slots": [{"key": "e1", "title": "Interview with Acme", "start_utc": start,
                       "spoken": "Acme builds voice agents. Your interviewer is Dev One.",
                       "notes": [{"fact": "The recruiter said: bring a laptop.", "private": True},
                                 {"fact": "Glassdoor: rounds include system design."}]}]}


def test_briefing_prompt_and_greeting():
    as_caller("brief-call", OWNER)
    interviews.briefings["brief-call"] = block()
    prompt = interviews.prompt_note("brief-call")
    assert "<briefing>" in prompt and "Prepared brief: Acme builds voice agents" in prompt
    assert "(private: from his email or recordings)" in prompt
    assert "offer a quick quiz" in prompt
    # The evaluation part of the profile is shortened on briefing calls (token budget).
    assert len(prompt) < 4 * 2600
    greeting = interviews.greeting("brief-call", "")
    assert greeting.startswith(f"Hi Chirag, it's {ASSISTANT_NAME} with your briefing for your 4:00 PM "
                               "interview with Acme, starting in about 15 minutes")


def test_a_briefing_with_no_research_says_so():
    as_caller("empty", OWNER)
    interviews.briefings["empty"] = {**block(), "slots": []}
    assert "research is not ready yet" in interviews.prompt_note("empty")


def test_block_calls_route_greeting_and_prompt_to_interview_mode():
    as_caller("cb", OWNER)
    interviews.briefings["cb"] = block(60)
    callbacks.calls["cb"] = callbacks.CallbackCall("briefing-x", 1, "your 4:00 PM interview with Acme",
                                                   "block:briefing-x")
    try:
        assert callbacks.greeting("cb").startswith(f"Hi Chirag, it's {ASSISTANT_NAME}, calling back")
        assert callbacks.prompt_note("cb") == ""  # The briefing is in interviews.prompt_note.
    finally:
        callbacks.calls.pop("cb")


def test_parse_day():
    today = datetime(2026, 10, 16).date()  # A Friday.
    assert interviews.parse_day("tomorrow", today).isoformat() == "2026-10-17"
    assert interviews.parse_day("Monday", today).isoformat() == "2026-10-19"
    assert interviews.parse_day("2026-10-20", today).isoformat() == "2026-10-20"
    assert interviews.parse_day("someday", today) is None


def events_for(day):
    start = datetime(day.year, day.month, day.day, 10, 30, tzinfo=timezone.utc)
    return [SlotEvent("e1", "Interview with Acme", start.isoformat(),
                      (start + timedelta(hours=1)).isoformat(), company="Acme",
                      attendees=[Person("dev@acme.com", "Dev One"),
                                 Person("priya@acme.com", "Priya (Talent Partner)")])]


def test_research_interviews_needs_a_yes_on_the_next_turn(monkeypatch):
    async def day_events(day):
        return events_for(day)

    class FakeClient:
        def __init__(self):
            self.started = []

        async def start_workflow(self, *args, **kwargs):
            self.started.append(kwargs["id"])

        def get_workflow_handle(self, workflow_id):
            client = self

            class Handle:
                async def signal(self, *args, **kwargs):
                    client.started.append("signal:" + workflow_id)
            return Handle()

    client = FakeClient()

    async def temporal():
        return client

    monkeypatch.setattr(interviews, "day_events", day_events)
    monkeypatch.setattr(callbacks, "temporal", temporal)
    monkeypatch.setattr(interviews.research, "pool", object())
    proposal = asyncio.run(interviews.research_interviews(
        "tomorrow", caller_key=OWNER, conversation_id="c1", turn=TurnContext("research tomorrow", 1)))
    assert proposal["proposed"] and "Acme" in proposal["say"] and client.started == []
    refused = asyncio.run(interviews.research_interviews(
        "tomorrow", confirmed=True, caller_key=OWNER, conversation_id="c1", turn=TurnContext("no, wait", 2)))
    assert "Not started" in refused["error"] and client.started == []
    stranger = asyncio.run(interviews.research_interviews("tomorrow", caller_key=STRANGER,
                                                          conversation_id="c2"))
    assert stranger == interviews.NOT_OWNER


def test_calendar_labels_are_speakable_and_never_invented():
    zone = interviews.owner_zone()
    event = events_for(datetime(2026, 10, 20).date())[0]  # 10:30 UTC = 4 PM IST.
    assert interviews.describe(event, zone) == {
        "label": "Acme, with Dev, at four p.m.", "calendar_title": "Interview with Acme"}
    start = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)  # 11:30 AM IST.
    rohan = SlotEvent("s", "Chirag Goel and Rohan Mehta", start.isoformat(),
                        (start + timedelta(minutes=30)).isoformat(), company="Brightline",
                        company_domain="brightline.example",
                        attendees=[Person("rohan@brightline.example", "Rohan Mehta"),
                                   Person("owner@example.com", "Chirag Goel", is_self=True)])
    assert interviews.spoken_label(rohan, zone) == "Brightline, with Rohan, at eleven thirty a.m."
    northwind = SlotEvent("z", "Chirag -In Person Post offer connect at Northwind Bengaluru [In-person]",
                       "2026-10-16T07:15:00+00:00", "2026-10-16T08:15:00+00:00", company="Northwind")
    assert interviews.spoken_label(northwind, zone) == "Northwind, post-offer connect, at twelve forty-five p.m."
    nameless = SlotEvent("n", "Sync", "2026-10-16T03:35:00+00:00", "2026-10-16T04:00:00+00:00",
                         company_domain="acme-labs.io")
    assert interviews.spoken_label(nameless, zone) == "Acme Labs, at nine oh five a.m."


def test_time_words():
    def at(h, m):
        return datetime(2026, 1, 1, h, m)
    assert interviews.time_words(at(0, 0)) == "twelve a.m."
    assert interviews.time_words(at(12, 15)) == "twelve fifteen p.m."
    assert interviews.time_words(at(18, 30)) == "six thirty p.m."
    assert interviews.time_words(at(9, 47)) == "nine forty-seven a.m."


def test_the_promise_is_at_most_five_minutes_and_explainable():
    one = interviews.estimate(1)
    assert one.minutes <= 5 and not one.capped and "four researchers" in one.why
    three = interviews.estimate(3)
    assert three.minutes == 5 and not three.capped  # Two rounds of researchers in parallel.
    six = interviews.estimate(6)
    assert six.minutes == 5 and six.capped and "earliest interview goes first" in six.why
    cached = interviews.estimate(2, already_done=2)
    assert cached.minutes == 2 and "already researched" in cached.why
    assert all(interviews.estimate(n).research_s >= 90 for n in range(1, 7))


def test_proposal_says_the_labels_verbatim_with_the_capped_promise(monkeypatch):
    async def day_events(day):
        start = datetime(day.year, day.month, day.day, 6, 0, tzinfo=timezone.utc)
        return [SlotEvent(f"e{i}", f"Interview with Co{i}", (start + timedelta(hours=i)).isoformat(),
                          (start + timedelta(hours=i, minutes=30)).isoformat(), company=f"Co{i}")
                for i in range(6)]

    async def researched(keys):
        return 0

    async def temporal():
        return object()
    monkeypatch.setattr(interviews, "day_events", day_events)
    monkeypatch.setattr(interviews, "researched", researched)
    monkeypatch.setattr(callbacks, "temporal", temporal)
    monkeypatch.setattr(interviews.research, "pool", object())
    result = asyncio.run(interviews.research_interviews(
        "today", caller_key=OWNER, conversation_id="c9", turn=TurnContext("research today", 1)))
    assert result["say"].startswith("I see 6 interviews today: Co0, at eleven thirty a.m.; Co1, at "
                                    "twelve thirty p.m.;")
    assert "within five minutes, starting with the earliest one. Okay?" in result["say"]
    assert result["minutes"] == 5 and "why_minutes" in result
    assert interviews.proposals["c9"].estimate.capped
    interviews.proposals.clear()


def test_owner_greeting_uses_his_name():
    as_caller("g", OWNER)
    assert callbacks.greeting("g") == f"Hi Chirag, it's {ASSISTANT_NAME}. What can I help you prepare?"
    as_caller("h", STRANGER)
    assert callbacks.greeting("h") == f"Hi, this is {ASSISTANT_NAME}. How can I help?"


def test_a_failed_turn_is_continued_not_repeated():
    def msg(role, text="", **extra):
        return {"role": role, "parts": [{"text": text}], "status": "completed", **extra}
    asked = [msg("model", "Proposal. Okay?"), msg("user", "How did you get five?")]
    assert agent.unanswered(asked, "How did you get five?")
    after_tools = asked + [msg("model", "", tool_calls=[{"id": "1", "name": "x", "arguments": "{}"}]),
                           msg("tool", "{}", tool_call_id="1")]
    assert agent.unanswered(after_tools, "How did you get five?")
    answered = asked + [msg("model", "Four researchers in parallel.")]
    assert not agent.unanswered(answered, "How did you get five?")
    assert not agent.unanswered(asked, "Something else")


def test_owner_prompt_is_compact_on_normal_calls(monkeypatch):
    long_eval = "x" * 3000
    monkeypatch.setattr(interviews, "profile_block",
                        lambda: "Private:\n## Profile\nChirag.\n\n## Self-evaluation\n" + long_eval)
    as_caller("n", OWNER)
    normal = interviews.prompt_note("n")
    interviews.briefings["n"] = block()
    briefing = interviews.prompt_note("n")
    assert normal.count("x") <= interviews.EVALUATION_CHARS_ON_CALLS + 10  # + "x"s in the rules
    assert "<briefing>\n" not in normal and "<briefing>\n" in briefing
    assert briefing.count("x") <= interviews.EVALUATION_CHARS_ON_BRIEFING + 50


class FakePool:
    def __init__(self, state):
        self.state = state
        self.pushed = []

    def connection(self):
        pool = self

        class Context:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *args):
                return False
        return Context()

    async def execute(self, query, params=()):
        if query.startswith("INSERT INTO interview_context_push"):
            self.pushed.append(params)

        class Cursor:
            async def fetchone(self):
                return None
        return Cursor()


def hooks_client(monkeypatch, state):
    from fastapi import FastAPI
    pool = FakePool(state)
    monkeypatch.setattr(valr_hooks.research, "pool", pool)

    async def load_state(connection):
        return state

    async def setup(connection):
        return None
    monkeypatch.setattr(calendar_sync, "load_state", load_state)
    monkeypatch.setattr(calendar_sync, "setup", setup)
    synced = []

    async def sync_now(reason):
        synced.append(reason)
        return True
    monkeypatch.setattr(interviews, "sync_now", sync_now)
    app = FastAPI()
    app.include_router(valr_hooks.router)
    return TestClient(app), synced, pool


def test_gcal_webhook_checks_channel_token_and_resource(monkeypatch):
    state = {"channel_id": "valr-1", "channel_token": "secret", "resource_id": "res-1"}
    client, synced, _ = hooks_client(monkeypatch, state)
    good = {"X-Goog-Channel-ID": "valr-1", "X-Goog-Channel-Token": "secret",
            "X-Goog-Resource-ID": "res-1", "X-Goog-Resource-State": "exists"}
    assert client.post("/hooks/gcal", headers=good).status_code == 200 and synced == ["push"]
    assert client.post("/hooks/gcal", headers={**good, "X-Goog-Resource-State": "sync"}).status_code == 200
    assert synced == ["push"]  # The channel's first message is only acknowledged.
    for bad in ({"X-Goog-Channel-Token": "wrong"}, {"X-Goog-Channel-ID": "other"},
                {"X-Goog-Resource-ID": "res-2"}):
        assert client.post("/hooks/gcal", headers={**good, **bad}).status_code == 403
    assert synced == ["push"]


def test_context_push_needs_the_token_and_is_stored_encrypted(monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("VALR_TOKEN_KEY", key)
    monkeypatch.setenv("VALR_CONTEXT_TOKEN", "push-token")
    client, _, pool = hooks_client(monkeypatch, {})
    body = {"company": "Acme", "subject": "Round 2", "excerpt": "Bring a laptop"}
    assert client.post("/valr/context", json=body).status_code == 403  # No token, no Access login.
    response = client.post("/valr/context", json=body, headers={"Authorization": "Bearer push-token"})
    assert response.status_code == 200
    sealed = pool.pushed[0][2]
    assert "laptop" not in sealed
    from app import google_oauth
    assert json.loads(google_oauth.decrypt(key, sealed))["excerpt"] == "Bring a laptop"


# R1's private sources, with fake Gmail and Fathom.

def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/threads"):
        assert "newer_than:180d" in request.url.params["q"]
        return httpx.Response(200, json={"threads": [{"id": "t1"}]})
    body = ("Hi Chirag,\nYour system design round is on Tuesday at 4 pm.\nThe salary range is "
            "flexible.\nUnrelated chatter here.\nOn Mon, Asha wrote:\n> old quoted text about interview")
    return httpx.Response(200, json={"messages": [{"payload": {
        "mimeType": "text/plain", "body": {"data": b64(body)},
        "headers": [{"name": "Subject", "value": "Acme: next round"},
                    {"name": "From", "value": "Asha Rao <asha@acme.com>"},
                    {"name": "Date", "value": "Mon, 12 Oct 2026 10:00:00 +0530"}]}}]})


def test_gmail_search_returns_excerpts_without_addresses():
    with httpx.Client(transport=httpx.MockTransport(gmail_handler)) as http:
        text, urls = private_sources.gmail_search(http, "token", "acme.com")
    assert urls == ["https://mail.google.com/mail/#all/t1"]
    assert "system design round" in text and "salary range" in text
    assert "Asha Rao (acme.com)" in text and "asha@acme.com" not in text
    assert "Unrelated chatter" not in text and "old quoted" not in text
    assert private_sources.gmail_search(http, None, "x", "reconnect needed")[0] == \
        "Gmail: not available (reconnect needed)."


def test_fathom_matches_the_company_and_skips_without_a_key(monkeypatch):
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)
    assert private_sources.fathom_search(None, ["Acme"]) == ("Fathom: not configured.", [])
    monkeypatch.setenv("FATHOM_API_KEY", "k")

    def handler(request):
        assert request.headers["X-Api-Key"] == "k"
        return httpx.Response(200, json={"items": [
            {"meeting_title": "Acme intro", "share_url": "https://fathom.video/share/1",
             "recording_start_time": "2026-10-01T10:00:00Z",
             "default_summary": {"markdown_formatted": "## Summary\nThey asked about **RAG**."}},
            {"meeting_title": "Dentist", "share_url": "https://fathom.video/share/2"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        text, urls = private_sources.fathom_search(http, ["Acme"])
    assert urls == ["https://fathom.video/share/1"] and "RAG" in text and "Dentist" not in text


def test_history_and_pushed_context(tmp_path):
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps({"interviews": [
        {"source": "fireflies", "id": "x1", "company": "Acme", "date": "2026-10-01",
         "round_type": "Recruiter screen", "questions_asked": ["Why Acme?"],
         "weak_answers": [{"topic": "pay expectations"}], "interviewers": [{"name": "Asha"}]},
        {"source": "fathom", "id": "x2", "company": "Beta", "round_type": "technical"}]}))
    text, urls = private_sources.history("Acme", [], path)
    assert urls == ["valr://history/fireflies/x1"] and "Why Acme?" in text and "Beta" not in text
    assert private_sources.history("Acme", [], tmp_path / "missing.json")[1] == []
    text, urls = private_sources.pushed([json.dumps({"id": "p1", "subject": "Round 2", "excerpt": "Laptop"})])
    assert urls == ["valr://pushed/p1"] and "Laptop" in text


def test_agent_offers_tools_by_caller(monkeypatch):
    seen = []

    async def fake_stream(messages, tools=None, tool_choice=None, **options):
        seen.append(([t["function"]["name"] for t in tools or []], options))
        yield "ok"
    monkeypatch.setattr(agent, "stream_model", fake_stream)

    async def collect(caller):
        return [e async for e in agent.generate([{"role": "user", "parts": [{"text": "hi"}],
                                                  "status": "completed"}], True, None, "", caller, None)]
    asyncio.run(collect(STRANGER))
    asyncio.run(collect(OWNER))
    assert "get_calendar" not in seen[0][0] and "get_calendar" in seen[1][0]
