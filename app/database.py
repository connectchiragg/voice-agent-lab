"""Conversation state persisted by LangGraph's PostgreSQL checkpointer."""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.conninfo import make_conninfo

load_dotenv(Path(__file__).resolve().parent.parent / ".env.docker")
graph = None


class Conversation(TypedDict):
    messages: list[dict]
    updated_at: str


def build_graph(checkpointer):
    # Only offload state here; Gemini streaming stays in llm.py.
    builder = StateGraph(Conversation)
    builder.add_node("save", lambda state: {})
    builder.add_edge(START, "save")
    builder.add_edge("save", END)
    return builder.compile(checkpointer=checkpointer)


@asynccontextmanager
async def lifespan(app):
    global graph
    connection_string = make_conninfo(
        host="127.0.0.1", port=5432, dbname="voice_agent", user="voice_agent",
        password=os.environ["POSTGRES_PASSWORD"], connect_timeout=5,
        options="-c statement_timeout=5000",
    )
    async with AsyncPostgresSaver.from_conn_string(connection_string) as saver:
        await saver.setup()
        graph = build_graph(saver)
        # Import existing history once; retain the old tables for rollback.
        cursor = await saver.conn.execute(
            "SELECT c.id::text, c.updated_at, m.role, m.content, m.status "
            "FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
            "ORDER BY c.id, m.id"
        )
        histories = {}
        for row in await cursor.fetchall():
            cid = row["id"]
            state = histories.setdefault(cid, {"messages": [], "updated_at": row["updated_at"].isoformat()})
            if row["role"] is not None:
                state["messages"].append({"role": row["role"], "parts": [{"text": row["content"]}], "status": row["status"]})
        for cid, state in histories.items():
            if not (await graph.aget_state(config(cid))).values:
                await graph.aupdate_state(config(cid), state, as_node="save")
        try:
            yield
        finally:
            graph = None


def config(conversation_id):
    return {"configurable": {"thread_id": conversation_id}}


async def load_history(conversation_id: str) -> list[dict]:
    state = await graph.aget_state(config(conversation_id))
    return state.values.get("messages", [])


async def save_history(conversation_id, messages):
    await graph.aupdate_state(config(conversation_id), {
        "messages": messages, "updated_at": datetime.now(timezone.utc).isoformat(),
    }, as_node="save")


async def start_turn(conversation_id: str, message: str) -> int:
    messages = await load_history(conversation_id)
    messages.extend([
        {"role": "user", "parts": [{"text": message}], "status": "completed"},
        {"role": "model", "parts": [{"text": ""}], "status": "interrupted"},
    ])
    await save_history(conversation_id, messages)
    return len(messages) - 1


async def update_reply(conversation_id: str, message_id: int, text: str,
                       status: str = "interrupted") -> None:
    messages = await load_history(conversation_id)
    messages[message_id] = {"role": "model", "parts": [{"text": text}], "status": status}
    await save_history(conversation_id, messages)
