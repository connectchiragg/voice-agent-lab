"""Caller memory: keys, the summary written after a call, the prompt block, forget_me, timeouts."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from psycopg_pool import AsyncConnectionPool

from app import agent, memory, tools
from app.llm import ModelUsed

PHONE = "tel:+919812345678"
NOTES = {"name": "Asha", "summary": "Asha asked about the weather in Mysore. She wants a "
         "callback tomorrow about her trip.", "facts": ["prefers Celsius"],
         "open_items": ["wants a callback", "asked about Mysore weather"],
         "profile": "Asha plans a trip to Mysore and likes short answers."}


@pytest.fixture
def store(monkeypatch):
    store = memory.InMemoryStore()
    monkeypatch.setattr(memory, "store", store)
    monkeypatch.setattr(memory, "calls", {})
    monkeypatch.setattr(memory, "tasks", set())
    return store


def fake_llm(monkeypatch, *replies):
    """The summarizer's model: each request gets the next reply; requests are recorded."""
    requests = []
    queue = list(replies)

    async def stream_model(messages, tools=None, client=None, tool_choice=None):
        requests.append(messages)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield ModelUsed("fake")
        for index in range(0, len(reply), 7):
            yield reply[index:index + 7]

    monkeypatch.setattr(memory, "stream_model", stream_model)
    return requests


def said(*turns):
    """Stored history: alternating caller and assistant turns, with a tool exchange."""
    messages = []
    for index, text in enumerate(turns):
        role = "user" if index % 2 == 0 else "model"
        messages.append({"role": role, "parts": [{"text": text}], "status": "completed"})
    messages.insert(1, {"role": "tool", "parts": [{"text": '{"secret": "tool output"}'}],
                        "status": "completed", "tool_call_id": "c", "name": "get_weather"})
    return messages


async def one_call(caller_key, dialogue, conversation_id=None):
    """Begin and end a call, then wait for its background summary."""
    conversation_id = conversation_id or str(uuid4())
    await memory.begin_call(conversation_id, caller_key)

    async def load_dialogue(cid):
        assert cid == conversation_id
        return dialogue

    memory.end_call(conversation_id, load_dialogue)
    await asyncio.gather(*memory.tasks)
    return conversation_id


# Keys.

def test_phone_numbers_normalize_to_e164_keys():
    assert memory.caller_key_from_phone("+919812345678") == PHONE
    assert memory.caller_key_from_phone("+91 98123-45678") == PHONE
    assert memory.caller_key_from_phone("+1 (415) 555.0100") == "tel:+14155550100"
    for unknown in ("anonymous", "", None, "09812345678", "+0123456789", "+1234",
                    "sip:alice@example.com", "+9198123456789012345"):
        assert memory.caller_key_from_phone(unknown) is None


def test_emails_normalize_to_lowercase_keys():
    assert memory.caller_key_from_email(" Asha@Example.COM ") == "email:asha@example.com"
    for unknown in ("", None, "asha", "asha@", "a@b@c.com", "sip:asha@example.com",
                    "Asha <asha@example.com>"):
        assert memory.caller_key_from_email(unknown) is None


def test_web_identity_is_read_only_when_the_access_header_is_trusted(monkeypatch):
    headers = {memory.WEB_EMAIL_HEADER: "Asha@Example.com"}
    monkeypatch.delenv("CF_ACCESS_EMAIL_HEADER", raising=False)
    assert memory.web_caller_key(headers) is None
    monkeypatch.setenv("CF_ACCESS_EMAIL_HEADER", "1")
    assert memory.web_caller_key(headers) == "email:asha@example.com"
    assert memory.web_caller_key({}) is None


# The summary after a call.

def test_call_end_writes_summary_and_profile_in_the_background(store, monkeypatch):
    requests = fake_llm(monkeypatch, "Here you go: " + json.dumps(NOTES))

    async def scenario():
        cid = await one_call(PHONE, said("Hi, I'm Asha. Weather in Mysore?", "It's sunny.",
                                         "Call me back tomorrow.", "Sure."))
        return cid
    cid = asyncio.run(scenario())
    assert len(requests) == 1
    prompt = requests[0][1]["content"]
    assert "Caller: Hi, I'm Asha" in prompt and "Assistant: It's sunny." in prompt
    assert "tool output" not in prompt  # Tool traffic is not sent.
    assert "first call" in prompt
    caller = store.callers[PHONE]
    assert (caller.display_name, caller.call_count, caller.last_summary) == (
        "Asha", 1, NOTES["summary"])
    assert caller.open_items == NOTES["open_items"] and caller.profile == NOTES["profile"]
    key, record = store.summaries[cid]
    assert key == PHONE and record.facts == ["prefers Celsius"] and record.error is None
    assert memory.calls == {}


def test_the_next_summary_sees_the_earlier_notes_and_rolls_the_profile(store, monkeypatch):
    second = {**NOTES, "name": None, "summary": "Asha called back about her trip.",
              "open_items": [], "profile": "Asha's trip is booked."}
    requests = fake_llm(monkeypatch, json.dumps(NOTES), json.dumps(second))

    async def scenario():
        await one_call(PHONE, said("I'm Asha.", "Hello Asha."))
        await one_call(PHONE, said("It's me again.", "Welcome back."))
    asyncio.run(scenario())
    assert "Name: Asha" in requests[1][1]["content"]
    assert "wants a callback" in requests[1][1]["content"]
    caller = store.callers[PHONE]
    assert caller.display_name == "Asha"  # Kept when not stated again.
    assert (caller.call_count, caller.open_items, caller.profile) == (2, [], "Asha's trip is booked.")
    assert caller.last_summary == "Asha called back about her trip."


def test_a_failed_summary_is_recorded_once_without_retries(store, monkeypatch):
    requests = fake_llm(monkeypatch, "Sorry, I can't do JSON.")

    async def scenario():
        return await one_call(PHONE, said("Hello?", "Hi there."))
    cid = asyncio.run(scenario())
    assert len(requests) == 1
    _, record = store.summaries[cid]
    assert (record.summary, record.error) == (None, "ValueError")
    assert store.callers[PHONE].call_count == 1 and store.callers[PHONE].last_summary is None


def test_a_call_with_nothing_said_is_not_remembered(store, monkeypatch):
    requests = fake_llm(monkeypatch)
    asyncio.run(one_call(PHONE, [{"role": "model", "parts": [{"text": "Hello?"}],
                                  "status": "completed"}]))
    assert requests == [] and store.callers == {} and memory.calls == {}


def test_notes_are_trimmed_and_private_looking_names_dropped():
    notes = memory.parse_notes(json.dumps({
        "name": "call me at 98123", "summary": "word " * 400, "facts": ["x" * 500] * 9,
        "open_items": ["", "callback"], "profile": "p"}))
    assert notes.name is None and len(notes.summary) <= 600
    assert len(notes.facts) == 5 and all(len(fact) <= 120 for fact in notes.facts)
    assert notes.open_items == ["callback"]
    with pytest.raises(ValueError):
        memory.parse_notes('{"name": "Asha"}')


# The prompt block.

def remembered(**changes):
    values = dict(caller_key=PHONE, display_name="Asha",
                  first_seen=datetime(2026, 10, 1, tzinfo=timezone.utc),
                  last_seen=datetime(2026, 10, 14, 9, tzinfo=timezone.utc), call_count=3,
                  profile="Plans a trip.", open_items=["wants a callback"],
                  last_summary="Asha asked about the weather in Mysore.")
    return memory.CallerMemory(**{**values, **changes})


def test_block_has_name_date_summary_and_open_items():
    block = memory.render_block(remembered())
    assert "Name: Asha." in block and "Last call: 14 October 2026." in block
    assert "Last time: Asha asked about the weather in Mysore." in block
    assert block.endswith("Open items: wants a callback.")
    assert "Plans a trip" not in block  # The profile feeds the summarizer, not the prompt.


def test_block_stays_within_the_token_cap():
    long = remembered(display_name="A" * 60, last_summary="She talked at length. " * 80,
                      open_items=[f"open item number {index} " + "x" * 100 for index in range(5)])
    block = memory.render_block(long)
    assert memory.estimated_tokens(block) <= memory.MAX_BLOCK_TOKENS
    assert "Name: " in block and "Last call: " in block and "Open items: " in block
    short = memory.render_block(remembered(last_summary="She talked at length. " * 80,
                                           open_items=[]))
    assert memory.estimated_tokens(short) <= memory.MAX_BLOCK_TOKENS
    assert "Last time: She talked at length." in short and short.endswith(".")


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setattr(agent, "graph", agent.build_graph(InMemorySaver()))


def scripted_agent(monkeypatch, *replies):
    seen = []
    queue = list(replies)

    async def stream_model(messages, tools=None, tool_choice=None):
        seen.append(messages)
        for event in queue.pop(0):
            yield event

    monkeypatch.setattr(agent, "stream_model", stream_model)
    return seen


def agent_turn(conversation_id, text):
    async def collect():
        return [event["text"] async for event in agent.stream_conversation(conversation_id, text)
                if event["type"] == "delta"]
    return asyncio.run(collect())


def test_a_known_callers_memory_ends_the_system_prompt(store, graph, monkeypatch):
    store.callers[PHONE] = remembered()
    seen = scripted_agent(monkeypatch, ["Hi Asha!"], ["Hello!"])
    cid = str(uuid4())
    asyncio.run(memory.begin_call(cid, PHONE))
    other = str(uuid4())
    asyncio.run(memory.begin_call(other, None))
    agent_turn(cid, "hello")
    agent_turn(other, "hello")
    assert seen[0][0]["content"].endswith(memory.render_block(remembered()))
    assert "Caller memory" not in seen[1][0]["content"]


# forget_me.

def test_forget_me_deletes_memory_and_this_call_is_not_summarized(store, graph, monkeypatch):
    store.callers[PHONE] = remembered()
    store.summaries["old"] = (PHONE, memory.CallRecord("old", datetime.now(timezone.utc),
                                                       datetime.now(timezone.utc), "Old call."))
    requests = fake_llm(monkeypatch)
    assert tools.has_side_effects("forget_me")  # A speculative reply waits for the commit.
    cid = str(uuid4())

    async def scenario():
        await memory.begin_call(cid, PHONE)
        assert memory.prompt_block(cid)
        # The model cannot aim it at someone else.
        result = json.loads(await tools.run_tool("forget_me", '{"caller_key": "tel:+14155550100"}',
                                                 memory.caller_of(cid)))
        assert result == {"forgotten": True, "had_memory": True}
        assert memory.prompt_block(cid) == ""

        async def load_dialogue(_):
            return said("Forget me.", "Done, I've forgotten you.")
        memory.end_call(cid, load_dialogue)
        await asyncio.gather(*memory.tasks)
    asyncio.run(scenario())
    assert store.callers == {} and store.summaries == {} and requests == []


def test_forget_me_runs_through_the_agent_and_is_confirmed(store, graph, monkeypatch):
    from app.llm import ToolCallRequest
    store.callers[PHONE] = remembered()
    seen = scripted_agent(monkeypatch, [[ToolCallRequest("f", "forget_me", "{}")]],
                          ["Done, I've forgotten everything about you."])
    cid = str(uuid4())
    asyncio.run(memory.begin_call(cid, PHONE))
    assert agent_turn(cid, "please forget me") == ["Done, I've forgotten everything about you."]
    assert store.callers == {}
    assert "Caller memory" in seen[0][0]["content"]
    assert "Caller memory" not in seen[1][0]["content"]  # Gone from the very next step.
    assert json.loads(seen[1][-1]["content"])["forgotten"] is True


def test_forget_me_for_an_unknown_caller_explains_there_is_nothing(store):
    result = json.loads(asyncio.run(tools.run_tool("forget_me", "{}", None)))
    assert result["forgotten"] is False and "not identified" in result["reason"]


# Timeouts and unknown callers.

class SlowStore(memory.InMemoryStore):
    async def load(self, caller_key):
        await asyncio.sleep(0.5)
        return remembered()


def test_a_slow_load_is_abandoned_and_the_call_starts_without_memory(store, monkeypatch):
    monkeypatch.setattr(memory, "store", SlowStore())
    monkeypatch.setenv("CALLER_MEMORY_LOAD_TIMEOUT_MS", "50")
    cid = str(uuid4())

    async def scenario():
        started = time.perf_counter()
        await memory.begin_call(cid, PHONE)
        return time.perf_counter() - started
    assert asyncio.run(scenario()) < 0.2
    assert memory.prompt_block(cid) == ""
    assert memory.caller_of(cid) == PHONE  # The call is still summarized at the end.


def test_a_failing_load_starts_without_memory(store, monkeypatch):
    class BrokenStore(memory.InMemoryStore):
        async def load(self, caller_key):
            raise OSError("database down")
    monkeypatch.setattr(memory, "store", BrokenStore())
    cid = str(uuid4())
    asyncio.run(memory.begin_call(cid, PHONE))
    assert memory.prompt_block(cid) == ""


def test_an_unknown_caller_has_no_memory_and_leaves_none(store, monkeypatch):
    requests = fake_llm(monkeypatch)
    asyncio.run(one_call(None, said("Hi, I'm Asha.", "Hello.")))
    assert memory.calls == {} and store.callers == {} and requests == []


def test_memory_off_does_nothing(monkeypatch):
    monkeypatch.setattr(memory, "store", None)
    monkeypatch.setattr(memory, "calls", {})
    asyncio.run(memory.begin_call("c", PHONE))
    assert memory.calls == {} and memory.prompt_block("c") == ""


# Postgres, when a scratch database is given (it creates the tables if missing).

@pytest.mark.skipif(not os.getenv("CALLER_MEMORY_TEST_POSTGRES"),
                    reason="set CALLER_MEMORY_TEST_POSTGRES to a scratch database's conninfo")
def test_postgres_store_round_trip():
    async def scenario():
        async with AsyncConnectionPool(os.environ["CALLER_MEMORY_TEST_POSTGRES"], min_size=1,
                                       kwargs={"autocommit": True}) as pool:
            store = memory.PostgresStore(pool)
            await store.setup()
            await store.setup()  # Idempotent.
            key = f"email:{uuid4().hex}@example.com"
            start = datetime.now(timezone.utc)
            assert await store.load(key) is None
            first = memory.CallRecord(str(uuid4()), start, start + timedelta(minutes=2),
                                      "First call.", ["likes tea"], display_name="Asha",
                                      profile="Likes tea.", open_items=["callback"])
            await store.record_call(key, first)
            await store.record_call(key, first)  # A repeat is ignored.
            failed = memory.CallRecord(str(uuid4()), start + timedelta(hours=1),
                                       start + timedelta(hours=2), None, error="TimeoutError")
            await store.record_call(key, failed)
            loaded = await store.load(key)
            assert loaded is not None
            assert (loaded.display_name, loaded.call_count, loaded.profile, loaded.open_items,
                    loaded.last_summary) == ("Asha", 2, "Likes tea.", ["callback"], "First call.")
            assert loaded.last_seen == start + timedelta(hours=2)
            assert await store.forget(key) is True
            assert await store.load(key) is None and await store.forget(key) is False
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT count(*) FROM call_summaries WHERE caller_key = %s", (key,))
                assert (await cursor.fetchone())[0] == 0
    asyncio.run(scenario())
