"""Conversation state persisted by LangGraph's PostgreSQL checkpointer."""
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast

from dotenv import load_dotenv
from fastapi import FastAPI
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg.conninfo import make_conninfo

load_dotenv(Path(__file__).resolve().parent.parent / ".env.docker")

MessageStatus = Literal["completed", "interrupted"]


class TextPart(TypedDict):
    text: str


class Message(TypedDict):
    role: Literal["user", "model"]
    parts: list[TextPart]
    status: MessageStatus


class Conversation(TypedDict):
    messages: list[Message]
    updated_at: str


ConversationGraph = CompiledStateGraph[Conversation, None, Conversation, Conversation]
graph: ConversationGraph | None = None


def save_node(state: Conversation) -> dict[str, object]:
    """State updates are supplied directly by save_history; no extra work here."""
    return {}


def build_graph(checkpointer: BaseCheckpointSaver[str]) -> ConversationGraph:
    # Only offload state here; Gemini streaming stays in llm.py.
    builder = StateGraph(Conversation)
    builder.add_node("save", save_node)
    builder.add_edge(START, "save")
    builder.add_edge("save", END)
    return builder.compile(checkpointer=checkpointer)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global graph
    connection_string = make_conninfo(
        host="127.0.0.1", port=5432, dbname="voice_agent", user="voice_agent",
        password=os.environ["POSTGRES_PASSWORD"], connect_timeout=5,
        options="-c statement_timeout=5000",
    )
    async with AsyncPostgresSaver.from_conn_string(connection_string) as saver:
        await saver.setup()
        graph = build_graph(saver)
        try:
            yield
        finally:
            graph = None


def config(conversation_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": conversation_id}}


async def load_history(conversation_id: str) -> list[Message]:
    if graph is None:
        raise RuntimeError("Conversation storage has not started.")
    state = await graph.aget_state(config(conversation_id))
    return cast(list[Message], state.values.get("messages", []))


async def save_history(conversation_id: str, messages: list[Message]) -> None:
    if graph is None:
        raise RuntimeError("Conversation storage has not started.")
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
                       status: MessageStatus = "interrupted") -> None:
    messages = await load_history(conversation_id)
    messages[message_id] = {"role": "model", "parts": [{"text": text}], "status": status}
    await save_history(conversation_id, messages)
