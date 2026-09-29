import asyncio
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from app import database


def test_checkpoint_recovery_and_isolation(monkeypatch):
    async def scenario():
        saver = InMemorySaver()
        monkeypatch.setattr(database, "graph", database.build_graph(saver))
        cid = str(uuid4())
        reply_id = await database.start_turn(cid, "hello")
        await database.update_reply(cid, reply_id, "partial")
        # Rebuild the graph: all conversation data must come from checkpoints.
        monkeypatch.setattr(database, "graph", database.build_graph(saver))
        history = await database.load_history(cid)
        assert history[0]["parts"][0]["text"] == "hello"
        assert history[1]["status"] == "interrupted"
        assert history[1]["parts"][0]["text"] == "partial"
        before = (await database.graph.aget_state(database.config(cid))).values["updated_at"]
        await database.update_reply(cid, reply_id, "complete", "completed")
        state = await database.graph.aget_state(database.config(cid))
        assert state.values["updated_at"] >= before
        assert state.values["messages"][1]["status"] == "completed"
        assert await database.load_history(str(uuid4())) == []

    asyncio.run(scenario())
