import asyncio
from contextlib import aclosing
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_config
from app import database


def test_checkpoint_recovery_and_isolation(monkeypatch):
    async def provider(message, history):
        assert get_config()["metadata"]["langgraph_node"] == "generate_reply"
        if message == "hello":
            yield "partial"
            raise RuntimeError("provider disconnected")
        assert history[-1]["status"] == "interrupted"
        yield "complete"

    monkeypatch.setattr(database, "stream_reply", provider)

    async def scenario():
        saver = InMemorySaver()
        monkeypatch.setattr(database, "graph", database.build_graph(saver))
        cid = str(uuid4())
        try:
            async for event in database.stream_conversation(cid, "hello"):
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError("Provider failure should reach the caller")
        # Rebuild the graph: all conversation data must come from checkpoints.
        monkeypatch.setattr(database, "graph", database.build_graph(saver))
        history = await database.load_history(cid)
        assert history[0]["parts"][0]["text"] == "hello"
        assert history[1]["status"] == "interrupted"
        assert history[1]["parts"][0]["text"] == "partial"
        before = (await database.graph.aget_state(database.config(cid))).values["updated_at"]
        async for event in database.stream_conversation(cid, "continue"):
            pass
        state = await database.graph.aget_state(database.config(cid))
        assert state.values["updated_at"] >= before
        assert state.values["messages"][-1]["status"] == "completed"
        assert state.next == ()
        assert await database.load_history(str(uuid4())) == []

    asyncio.run(scenario())


def test_chunks_stream_without_saving_and_close_cancels_provider(monkeypatch):
    async def scenario():
        closed = asyncio.Event()

        async def provider(message, history):
            try:
                yield "partial"
                await asyncio.Event().wait()
            finally:
                closed.set()

        monkeypatch.setattr(database, "stream_reply", provider)
        monkeypatch.setattr(database, "graph", database.build_graph(InMemorySaver()))
        cid = str(uuid4())
        async with aclosing(database.stream_conversation(cid, "hello")) as stream:
            assert (await anext(stream))["type"] == "start"
            assert (await anext(stream))["text"] == "partial"
            history = await database.load_history(cid)
            assert history[-1]["parts"][0]["text"] == ""
            assert history[-1]["status"] == "interrupted"
        await asyncio.wait_for(closed.wait(), timeout=1)

    asyncio.run(scenario())


def test_failed_recovery_save_does_not_block_earlier_chunks(monkeypatch):
    async def provider(message, history):
        yield "reaches UI before saving"
        raise RuntimeError("provider disconnected")

    async def fail_save(conversation_id, messages):
        raise RuntimeError("storage unavailable")

    async def scenario():
        monkeypatch.setattr(database, "graph", database.build_graph(InMemorySaver()))
        monkeypatch.setattr(database, "stream_reply", provider)
        monkeypatch.setattr(database, "save_history", fail_save)
        cid = str(uuid4())
        events = []
        try:
            async for event in database.stream_conversation(cid, "hello"):
                events.append(event)
        except RuntimeError as error:
            assert str(error) == "storage unavailable"
        else:
            raise AssertionError("Storage failure should stop generation")
        assert events == [{"type": "start"}, {"type": "delta", "text": "reaches UI before saving"}]
        history = await database.load_history(cid)
        assert history[0]["parts"][0]["text"] == "hello"
        assert history[1]["parts"][0]["text"] == ""

    asyncio.run(scenario())


def test_successful_reply_uses_only_graph_checkpoints(monkeypatch):
    async def provider(message, history):
        yield "First"
        yield " second"
        yield " third"

    async def unexpected_manual_save(conversation_id, messages):
        raise AssertionError("Successful generation must not manually save chunks")

    async def scenario():
        monkeypatch.setattr(database, "graph", database.build_graph(InMemorySaver()))
        monkeypatch.setattr(database, "stream_reply", provider)
        monkeypatch.setattr(database, "save_history", unexpected_manual_save)
        cid = str(uuid4())
        events = [event async for event in database.stream_conversation(cid, "hello")]
        assert [event["text"] for event in events if event["type"] == "delta"] == [
            "First", " second", " third",
        ]
        history = await database.load_history(cid)
        assert history[-1]["parts"][0]["text"] == "First second third"
        assert history[-1]["status"] == "completed"

    asyncio.run(scenario())


def test_completed_turns_reach_next_node_without_duplicate_messages(monkeypatch):
    calls = []

    async def provider(message, history):
        calls.append((message, history))
        yield "reply to " + message

    async def scenario():
        monkeypatch.setattr(database, "stream_reply", provider)
        monkeypatch.setattr(database, "graph", database.build_graph(InMemorySaver()))
        cid = str(uuid4())
        for message in ("first", "second"):
            async for event in database.stream_conversation(cid, message):
                pass
        assert calls[0] == ("first", [])
        assert calls[1][0] == "second"
        assert len(calls[1][1]) == 2
        assert calls[1][1][-1]["parts"][0]["text"] == "reply to first"
        assert calls[1][1][-1]["status"] == "completed"
        assert len(await database.load_history(cid)) == 4

    asyncio.run(scenario())
