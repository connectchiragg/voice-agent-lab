"""Caller memory: keys, the summary written after a call, the prompt block, forget_me, timeouts."""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
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
    deleted = []

    async def delete_thread(thread_id):
        deleted.append(thread_id)
    store = memory.InMemoryStore(delete_thread)
    store.deleted = deleted  # Conversations (checkpoint threads) deleted, in order.
    monkeypatch.setattr(memory, "store", store)
    monkeypatch.setattr(memory, "calls", {})
    monkeypatch.setattr(memory, "active", set())
    monkeypatch.setattr(memory, "tasks", set())
    monkeypatch.setattr(memory.groq_api, "near_rate_limit", lambda: False)
    return store


def fake_llm(monkeypatch, *replies):
    """The summarizer's model: each request gets the next reply; requests are recorded."""
    requests = []
    queue = list(replies)

    async def stream_model(messages, tools=None, client=None, tool_choice=None, max_tokens=None):
        assert max_tokens == memory.SUMMARY_MAX_TOKENS  # Its own cap, not a spoken reply's.
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


def test_a_greeting_alone_is_no_visit_and_its_conversation_is_deleted(store, monkeypatch):
    requests = fake_llm(monkeypatch)
    cid = asyncio.run(one_call(PHONE, [{"role": "model", "parts": [{"text": "Hi Asha, this is "
                                        "VALR. How can I help?"}], "status": "completed"}]))
    assert requests == [] and store.callers == {} and store.deleted == [cid]


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


def facts(block):
    """The block without its fixed rules: what the token cap applies to."""
    assert block.startswith(memory.BLOCK_RULES + " ")
    return block[len(memory.BLOCK_RULES) + 1:]


def test_block_has_name_date_summary_open_items_and_profile():
    block = memory.render_block(remembered())
    assert facts(block) == ("Name: Asha. Last call: 14 October 2026. Last time: Asha asked "
                            "about the weather in Mysore. Open items: wants a callback. "
                            "About them: Plans a trip.")


def test_block_stays_within_the_token_cap_without_counting_the_rules():
    long = remembered(display_name="Anantharamakrishnan Venkatasubramanian", last_summary="She talked at length. " * 80,
                      profile="Profile sentence. " * 50,
                      open_items=[f"open item number {index} " + "x" * 100 for index in range(5)])
    block = facts(memory.render_block(long))
    assert memory.estimated_tokens(block) <= memory.MAX_BLOCK_TOKENS
    assert "Name: " in block and "Last call: " in block and "Open items: " in block
    short = facts(memory.render_block(remembered(last_summary="She talked at length. " * 80,
                                                 open_items=[])))
    assert memory.estimated_tokens(short) <= memory.MAX_BLOCK_TOKENS
    assert "Last time: She talked at length." in short and short.endswith(".")
    # Rules are outside the cap: a block using all 150 tokens is longer than 150 with them.
    assert memory.estimated_tokens(memory.render_block(long)) > memory.MAX_BLOCK_TOKENS


def test_the_profile_is_trimmed_first():
    full = remembered(last_summary="She asked about the weather. " * 6, profile="Likes tea. " * 6)
    tight = facts(memory.render_block(full, max_tokens=60))
    assert "Last time: She asked about the weather." in tight and "About them" not in tight
    roomy = facts(memory.render_block(full, max_tokens=150))
    assert "About them: Likes tea." in roomy
    assert roomy.index("Last time") < roomy.index("Open items") < roomy.index("About them")


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
    # The recorded call's conversation goes with the memory; this call's once it ends.
    assert store.deleted == ["old", cid]


def test_forget_me_deletes_the_conversations_of_past_calls(graph, monkeypatch):
    store = memory.InMemoryStore(agent.delete_thread)
    monkeypatch.setattr(memory, "store", store)
    monkeypatch.setattr(memory, "calls", {})
    monkeypatch.setattr(memory, "active", set())
    monkeypatch.setattr(memory, "tasks", set())
    monkeypatch.setattr(memory.groq_api, "near_rate_limit", lambda: False)
    fake_llm(monkeypatch, json.dumps(NOTES))
    past, stranger = str(uuid4()), str(uuid4())

    async def scenario():
        for cid in (past, stranger):
            await agent.save_history(cid, said("I'm Asha.", "Hello Asha."))
        await one_call(PHONE, await agent.load_history(past), past)
        assert store.summaries[past][0] == PHONE
        assert await memory.forget(PHONE) is True
        return await agent.load_history(past), await agent.load_history(stranger)
    gone, kept = asyncio.run(scenario())
    assert gone == [] and len(kept) == 3 and store.callers == {}


def test_forget_during_a_waiting_summary_stores_nothing_and_deletes_that_call(store, monkeypatch):
    requests = fake_llm(monkeypatch)
    monkeypatch.setattr(memory, "SUMMARY_RETRY_S", 0.01)

    async def scenario():
        await memory.begin_call("live", None)  # Someone else's call keeps the summary waiting.
        cid = str(uuid4())
        await memory.begin_call(cid, PHONE)

        async def load_dialogue(_):
            return said("I'm Asha.", "Hello.")
        memory.end_call(cid, load_dialogue)
        await asyncio.sleep(0.05)
        await memory.forget(PHONE)
        await asyncio.gather(*memory.tasks)
        return cid
    cid = asyncio.run(scenario())
    assert requests == [] and store.summaries == {} and store.deleted == [cid]


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


# The summary waits for quiet: no live call, Groq not near its limit.

def test_the_summary_waits_until_no_call_is_live(store, monkeypatch):
    requests = fake_llm(monkeypatch, json.dumps(NOTES))
    monkeypatch.setattr(memory, "SUMMARY_RETRY_S", 0.01)

    async def scenario():
        await memory.begin_call("live", None)
        cid = str(uuid4())
        await memory.begin_call(cid, PHONE)

        async def load_dialogue(_):
            return said("I'm Asha.", "Hello.")
        memory.end_call(cid, load_dialogue)
        await asyncio.sleep(0.1)
        assert requests == [] and memory.tasks  # Still waiting.
        memory.end_call("live", load_dialogue)  # Unknown caller: nothing to summarize.
        await asyncio.gather(*memory.tasks)
    asyncio.run(scenario())
    assert len(requests) == 1 and store.callers[PHONE].display_name == "Asha"


def test_the_summary_retries_after_a_rate_limit_and_waits_near_the_limit(store, monkeypatch):
    from app.groq_api import ProviderError
    requests = fake_llm(monkeypatch, ProviderError(429, "rate limited"), json.dumps(NOTES))
    monkeypatch.setattr(memory, "SUMMARY_RETRY_S", 0.01)
    near = iter([True, False, True, True, False])  # Checked before each attempt.
    monkeypatch.setattr(memory.groq_api, "near_rate_limit", lambda: next(near, False))
    cid = asyncio.run(one_call(PHONE, said("I'm Asha.", "Hello.")))
    assert len(requests) == 2
    _, record = store.summaries[cid]
    assert record.summary == NOTES["summary"] and record.error is None


def test_the_summary_gives_up_after_waiting_if_still_near_the_rate_limit(store, monkeypatch):
    requests = fake_llm(monkeypatch)
    monkeypatch.setattr(memory, "SUMMARY_RETRY_S", 0.01)
    monkeypatch.setenv("CALLER_MEMORY_SUMMARY_WAIT_S", "0.05")
    monkeypatch.setattr(memory.groq_api, "near_rate_limit", lambda: True)
    cid = asyncio.run(one_call(PHONE, said("I'm Asha.", "Hello.")))
    assert requests == []
    _, record = store.summaries[cid]
    assert (record.summary, record.error) == (None, "RateLimitWait")


def test_after_waiting_a_long_live_call_alone_does_not_stop_the_summary(store, monkeypatch):
    requests = fake_llm(monkeypatch, json.dumps(NOTES))
    monkeypatch.setattr(memory, "SUMMARY_RETRY_S", 0.01)
    monkeypatch.setenv("CALLER_MEMORY_SUMMARY_WAIT_S", "0.05")

    async def scenario():
        await memory.begin_call("long call", None)
        return await one_call(PHONE, said("I'm Asha.", "Hello."))
    cid = asyncio.run(scenario())
    assert len(requests) == 1 and store.summaries[cid][1].summary == NOTES["summary"]


# The phone greeting.

def test_the_greeting_names_a_known_caller_only(store):
    store.callers[PHONE] = remembered()
    asyncio.run(memory.begin_call("known", PHONE))
    asyncio.run(memory.begin_call("unknown", None))
    store.callers["tel:+14155550100"] = remembered(caller_key="tel:+14155550100", display_name=None)
    asyncio.run(memory.begin_call("nameless", "tel:+14155550100"))
    assert memory.greeting("known") == "Hi Asha, this is VALR. How can I help?"
    assert memory.greeting("unknown") == memory.GREETING == "Hi, this is VALR. How can I help?"
    assert memory.greeting("nameless") == memory.GREETING
    memory.calls["known"].forgotten = True
    assert memory.greeting("known") == memory.GREETING


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
            # The recorded calls' conversations are deleted with the memory; others are not.
            async with AsyncPostgresSaver.from_conn_string(
                    os.environ["CALLER_MEMORY_TEST_POSTGRES"]) as saver:
                await saver.setup()
                await store.setup()
                assert store.checkpoints
                graph = agent.build_graph(saver)
                stranger = str(uuid4())
                for thread in (first.call_id, failed.call_id, stranger):
                    await graph.aupdate_state(agent.config(thread), {
                        "messages": said("Hello", "Hi"), "updated_at": agent.now()}, as_node="agent")
                assert await store.forget(key) is True
                for thread, left in ((first.call_id, 0), (failed.call_id, 0), (stranger, 3)):
                    state = await graph.aget_state(agent.config(thread))
                    assert len(state.values.get("messages", [])) == left
                await saver.adelete_thread(stranger)
            assert await store.load(key) is None and await store.forget(key) is False
            async with pool.connection() as connection:
                cursor = await connection.execute(
                    "SELECT count(*) FROM call_summaries WHERE caller_key = %s", (key,))
                assert (await cursor.fetchone())[0] == 0
    asyncio.run(scenario())


def test_the_greeting_is_history_the_model_sees_and_the_summary_reads(store, graph, monkeypatch):
    seen = scripted_agent(monkeypatch, ["It's sunny."])
    cid = str(uuid4())

    async def scenario():
        await agent.record_greeting(cid, "Hi, this is VALR. How can I help?", "completed")
        return await agent.load_history(cid)
    assert [m["role"] for m in asyncio.run(scenario())] == ["model"]
    agent_turn(cid, "Weather in Mysore?")
    assert [(m["role"], m["content"]) for m in seen[0][1:] if m["role"] != "system"] == [
        ("assistant", "Hi, this is VALR. How can I help?"), ("user", "Weather in Mysore?")]
    history = asyncio.run(agent.load_history(cid))
    assert memory.dialogue_text(history).startswith("Assistant: Hi, this is VALR.")



# Names: only a plausible one that the caller actually said.

@pytest.mark.parametrize("bad", ["name", "Name", "caller", "the caller", "User", "unknown", "n/a",
                                 "", "  ", None, "null", "Not provided", "A", "Asha 2", "asha@x.com",
                                 "call me at 98123", "Hi there", "One Two Three Four", "you",
                                 "VALR", 42])
def test_placeholders_and_non_names_are_not_names(bad):
    assert memory.plausible_name(bad) is None


def test_real_names_are_kept_only_if_the_caller_said_them():
    for good in ("Asha", "Mary-Jane O'Neil", "José", "Priya Raman", "D'Souza"):
        assert memory.plausible_name(good) == good
    assert memory.plausible_name(" Asha. ") == "Asha"
    assert memory.plausible_name("Asha", "hi i'm asha, weather please") == "Asha"
    assert memory.plausible_name("Asha", "what's the weather in Mysore") is None
    assert memory.plausible_name("Asha Rao", "I'm Asha") is None  # Every word must be heard.


def test_the_summarizer_name_must_be_in_what_the_caller_said(store, monkeypatch):
    requests = fake_llm(monkeypatch, json.dumps({**NOTES, "name": "name"}),
                        json.dumps({**NOTES, "name": "Asha"}), json.dumps({**NOTES, "name": "Asha"}))

    async def scenario():
        await one_call(PHONE, said("I'd like to give my name.", "Sure, go ahead."))
        assert store.callers[PHONE].display_name is None  # The placeholder is dropped.
        await one_call(PHONE, said("What's the weather?", "Sunny, Asha."))  # Only the assistant said it.
        assert store.callers[PHONE].display_name is None
        await one_call(PHONE, said("This is Asha again.", "Hello."))
    asyncio.run(scenario())
    assert store.callers[PHONE].display_name == "Asha"
    assert "null if they did not clearly say" in requests[0][0]["content"]


def test_a_stored_placeholder_is_never_spoken_or_prompted(store):
    store.callers[PHONE] = remembered(display_name="name")
    asyncio.run(memory.begin_call("c", PHONE))
    assert memory.greeting("c") == memory.GREETING == "Hi, this is VALR. How can I help?"
    assert "Name:" not in memory.prompt_block("c")
