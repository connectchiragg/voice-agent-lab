"""The LangGraph agent: streaming, tool loop, checkpoints and interruption records."""
import asyncio
import json
from contextlib import aclosing
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import agent
from app.llm import ToolCallRequest


@pytest.fixture
def graph(monkeypatch):
    saver = InMemorySaver()
    monkeypatch.setattr(agent, "graph", agent.build_graph(saver))
    return saver


def scripted(monkeypatch, *replies):
    """Fake model: each call plays the next reply's events and records its input."""
    seen = []
    queue = list(replies)

    async def stream_model(messages, tools=None, tool_choice=None):
        seen.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        for event in queue.pop(0):
            if isinstance(event, Exception):
                raise event
            yield event

    monkeypatch.setattr(agent, "stream_model", stream_model)
    return seen


def run_turn(conversation_id, text):
    async def collect():
        return [event["text"] async for event in agent.stream_conversation(conversation_id, text)]
    return asyncio.run(collect())


def history(conversation_id):
    return [(m["role"], m["parts"][0]["text"], m["status"])
            for m in asyncio.run(agent.load_history(conversation_id))]


def test_reply_streams_and_is_checkpointed(graph, monkeypatch):
    scripted(monkeypatch, ["First", " second"])
    cid = str(uuid4())
    assert run_turn(cid, "hello") == ["First", " second"]
    assert history(cid) == [("user", "hello", "completed"), ("model", "First second", "completed")]


def test_history_reaches_the_next_turn_and_survives_a_rebuild(graph, monkeypatch):
    seen = scripted(monkeypatch, ["reply one"], ["reply two"])
    cid = str(uuid4())
    run_turn(cid, "first")
    monkeypatch.setattr(agent, "graph", agent.build_graph(graph))  # State only from checkpoints.
    run_turn(cid, "second")
    assert [m["content"] for m in seen[1]["messages"][1:]] == ["first", "reply one", "second"]
    assert len(history(cid)) == 4


def test_tool_call_runs_tool_and_answers_without_filler_when_fast(graph, monkeypatch):
    seen = scripted(monkeypatch,
                    [[ToolCallRequest("call-1", "get_time", '{"timezone": "Asia/Kolkata"}')]],
                    ["It's ", "evening."])
    cid = str(uuid4())
    assert run_turn(cid, "what time is it?") == ["It's ", "evening."]
    turns = asyncio.run(agent.load_history(cid))
    assert [m["role"] for m in turns] == ["user", "model", "tool", "model"]
    assert turns[1]["tool_calls"][0]["name"] == "get_time"
    assert turns[1]["parts"][0]["text"] == ""
    assert "time" in json.loads(turns[2]["parts"][0]["text"])
    # The second model call sees the call and its result, in API format.
    second = seen[1]["messages"]
    assert second[2]["tool_calls"][0]["id"] == "call-1"
    assert second[3] == {"role": "tool", "tool_call_id": "call-1", "content": turns[2]["parts"][0]["text"]}


def slow_tool(monkeypatch, seconds=0.05):
    async def run_tool(name, arguments):
        await asyncio.sleep(seconds)
        return '{"ok": true}'
    monkeypatch.setattr(agent, "run_tool", run_tool)
    monkeypatch.setattr(agent, "FILLER_AFTER_S", 0.01)


def test_slow_tool_gets_a_spoken_filler_that_is_stored(graph, monkeypatch):
    slow_tool(monkeypatch)
    scripted(monkeypatch, [[ToolCallRequest("c", "get_weather", "{}")]], ["Sunny."])
    cid = str(uuid4())
    assert run_turn(cid, "weather?") == [agent.TOOL_FILLER + " ", "Sunny."]
    assert history(cid)[1] == ("model", agent.TOOL_FILLER, "completed")


def test_text_before_a_slow_tool_replaces_the_filler(graph, monkeypatch):
    slow_tool(monkeypatch)
    scripted(monkeypatch,
             ["Sure,", [ToolCallRequest("c", "get_weather", "{}")]],
             ["done."])
    cid = str(uuid4())
    assert run_turn(cid, "weather?") == ["Sure,", " ", "done."]


def test_tool_rounds_are_capped_then_the_model_must_answer(graph, monkeypatch):
    call = [ToolCallRequest("c", "get_time", "{}")]
    seen = scripted(monkeypatch, [call], [call], [call], ["Final answer."])
    cid = str(uuid4())
    run_turn(cid, "loop forever")
    assert [entry["tools"] is not None for entry in seen] == [True, True, True, False]
    assert history(cid)[-1] == ("model", "Final answer.", "completed")


def test_provider_failure_keeps_partial_reply_once(graph, monkeypatch):
    scripted(monkeypatch, ["partial", RuntimeError("provider dropped")])
    cid = str(uuid4())
    with pytest.raises(RuntimeError):
        run_turn(cid, "hello")
    assert history(cid) == [("user", "hello", "completed"), ("model", "partial", "interrupted")]


def test_closing_the_stream_cancels_the_model(graph, monkeypatch):
    closed = asyncio.Event()

    async def stream_model(messages, tools=None, tool_choice=None):
        try:
            yield "partial"
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(agent, "stream_model", stream_model)

    async def scenario():
        async with aclosing(agent.stream_conversation(str(uuid4()), "hello")) as stream:
            assert (await anext(stream))["text"] == "partial"
        await asyncio.wait_for(closed.wait(), timeout=1)

    asyncio.run(scenario())


def test_interrupted_reply_keeps_only_heard_text(graph, monkeypatch):
    scripted(monkeypatch, ["Full reply nobody finished hearing."])
    cid = str(uuid4())
    run_turn(cid, "hello")
    # The interrupted turn's input was never checkpointed: append both messages.
    asyncio.run(agent.record_interruption(cid, "tell me more", "Well, it..."))
    assert history(cid)[2:] == [("user", "tell me more", "completed"),
                                ("model", "Well, it...", "interrupted")]
    # Recording again for the same turn replaces the reply rather than appending.
    asyncio.run(agent.record_interruption(cid, "tell me more", "Well, it is..."))
    assert history(cid)[2:] == [("user", "tell me more", "completed"),
                                ("model", "Well, it is...", "interrupted")]


def test_interruption_after_a_tool_keeps_the_tool_exchange(graph, monkeypatch):
    slow_tool(monkeypatch)
    scripted(monkeypatch, [[ToolCallRequest("c", "get_weather", "{}")]], ["It is sunny and warm."])
    cid = str(uuid4())
    run_turn(cid, "weather?")
    heard = agent.TOOL_FILLER + " It is sunny..."
    asyncio.run(agent.record_interruption(cid, "weather?", heard))
    assert [(role, status) for role, _, status in history(cid)] == [
        ("user", "completed"), ("model", "completed"), ("tool", "completed"), ("model", "interrupted")]
    assert history(cid)[-1][1] == "It is sunny..."  # The filler is not stored twice.


def test_promise_without_a_tool_call_is_retried_with_a_tool_required(graph, monkeypatch):
    seen = scripted(monkeypatch,
                    ["Let me look up the forecast."],
                    [[ToolCallRequest("c", "get_time", "{}")]],
                    ["It is sunny."])
    cid = str(uuid4())
    assert run_turn(cid, "weather?") == ["Let me look up the forecast.", " ", "It is sunny."]
    assert [entry["tool_choice"] for entry in seen] == [None, "required", None]
    assert [role for role, _, _ in history(cid)] == ["user", "model", "tool", "model"]
    assert history(cid)[1][1] == "Let me look up the forecast."


def test_ordinary_answers_are_not_treated_as_promises(graph, monkeypatch):
    seen = scripted(monkeypatch, ["Coorg is lovely in winter, and the drive is about five hours."])
    run_turn(str(uuid4()), "trip ideas?")
    assert len(seen) == 1
    assert not agent.promises_lookup("Coorg is lovely in winter, and the drive is about five hours.")
    assert agent.promises_lookup("One moment, I'll check the weather there.")
    assert agent.promises_lookup("Let me look up the forecast for Madikeri.")
    assert not agent.promises_lookup("It's eleven AM in New York. Anything else you'd like to check?")


def speculate_turn(conversation_id, text, committed, cancel_after=None):
    """Collect a speculative turn's deltas; cancel it after `cancel_after` deltas."""
    async def collect():
        deltas = []
        async with aclosing(agent.stream_conversation(conversation_id, text, committed)) as stream:
            async for event in stream:
                deltas.append(event["text"])
                if len(deltas) == cancel_after:
                    break
        return deltas
    return collect()


def test_cancelled_speculation_leaves_no_history(graph, monkeypatch):
    scripted(monkeypatch, [[ToolCallRequest("c", "get_time", '{"timezone": "Asia/Kolkata"}')]],
             ["It's ", "evening."])
    cid = str(uuid4())

    async def scenario():
        committed = asyncio.Event()
        task = asyncio.create_task(speculate_turn(cid, "what time is it", committed))
        await asyncio.sleep(0.05)  # Tool ran and the reply finished; it waits for a commit.
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert history(cid) == []


def test_failed_speculation_leaves_no_history(graph, monkeypatch):
    scripted(monkeypatch, ["partial", RuntimeError("provider dropped")])
    cid = str(uuid4())
    with pytest.raises(RuntimeError):
        asyncio.run(speculate_turn(cid, "hello", asyncio.Event()))
    assert history(cid) == []


def test_committed_speculation_is_stored_like_a_normal_turn(graph, monkeypatch):
    scripted(monkeypatch, ["reply one"], ["First", " second"])
    cid = str(uuid4())
    run_turn(cid, "first")

    async def scenario():
        committed = asyncio.Event()
        task = asyncio.create_task(speculate_turn(cid, "second", committed))
        await asyncio.sleep(0.01)
        # Finished, but not stored before the commit.
        assert len(await agent.load_history(cid)) == 2
        committed.set()
        return await task

    assert asyncio.run(scenario()) == ["First", " second"]
    assert history(cid)[2:] == [("user", "second", "completed"),
                                ("model", "First second", "completed")]


def test_interrupted_committed_speculation_keeps_its_completed_steps(graph, monkeypatch):
    slow_tool(monkeypatch, seconds=0)

    async def stream_model(messages, tools=None, tool_choice=None):
        if tools is not None and len(messages) == 2:
            yield [ToolCallRequest("c", "get_weather", "{}")]
            return
        yield "It is sunny"
        await asyncio.Event().wait()  # Still speaking when the user interrupts.

    monkeypatch.setattr(agent, "stream_model", stream_model)
    cid = str(uuid4())

    async def scenario():
        committed = asyncio.Event()
        committed.set()
        task = asyncio.create_task(speculate_turn(cid, "weather?", committed))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.record_interruption(cid, "weather?", "It is...")

    asyncio.run(scenario())
    assert [(role, status) for role, _, status in history(cid)] == [
        ("user", "completed"), ("model", "completed"), ("tool", "completed"), ("model", "interrupted")]


def test_side_effecting_tool_waits_for_the_commit(graph, monkeypatch):
    ran = []

    async def run_tool(name, arguments):
        ran.append(name)
        return '{"ok": true}'

    monkeypatch.setattr(agent, "run_tool", run_tool)
    monkeypatch.setattr(agent, "has_side_effects", lambda name: name == "book_table")
    scripted(monkeypatch, [[ToolCallRequest("c", "book_table", "{}")]], ["Booked."])
    cid = str(uuid4())

    async def scenario():
        committed = asyncio.Event()
        task = asyncio.create_task(speculate_turn(cid, "book it", committed))
        await asyncio.sleep(0.05)
        assert ran == []  # Not run while speculative.
        committed.set()
        return await task

    assert asyncio.run(scenario()) == ["Booked."]
    assert ran == ["book_table"]
