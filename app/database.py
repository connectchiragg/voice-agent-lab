"""A one-node conversation workflow with PostgreSQL checkpoints."""
import os
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from dotenv import load_dotenv
from fastapi import FastAPI
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg.conninfo import make_conninfo

from app.llm import stream_reply

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


class ChatEvent(TypedDict):
    type: Literal["start", "delta"]
    text: NotRequired[str]


ConversationGraph = CompiledStateGraph[Conversation, None, Conversation, Conversation]
graph: ConversationGraph | None = None


async def generate_reply(state: Conversation, config: RunnableConfig) -> Conversation:
    """Run Gemini inside the graph; checkpoint text before publishing each chunk."""
    messages = state["messages"]
    message = messages[-2]["parts"][0]["text"]
    conversation_id = config["configurable"]["thread_id"]
    writer = get_stream_writer()
    # The graph's input checkpoint already holds the user and empty assistant.
    writer({"type": "start"})
    reply = ""
    async with aclosing(stream_reply(message, messages[:-2])) as stream:
        async for chunk in stream:
            reply += chunk
            messages[-1] = {"role": "model", "parts": [{"text": reply}], "status": "interrupted"}
            # Node-boundary checkpoints alone would lose an unfinished stream.
            await save_history(conversation_id, messages)
            writer({"type": "delta", "text": chunk})
    messages[-1] = {"role": "model", "parts": [{"text": reply}], "status": "completed"}
    # LangGraph persists this returned state before the workflow finishes.
    return {"messages": messages, "updated_at": datetime.now(timezone.utc).isoformat()}


def build_graph(checkpointer: BaseCheckpointSaver[str]) -> ConversationGraph:
    builder = StateGraph(Conversation)
    builder.add_node("generate_reply", generate_reply)
    builder.add_edge(START, "generate_reply")
    builder.add_edge("generate_reply", END)
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
    }, as_node="generate_reply")


async def stream_conversation(conversation_id: str, message: str) -> AsyncIterator[ChatEvent]:
    """Supply a new turn to the graph and forward its custom streaming events."""
    if graph is None:
        raise RuntimeError("Conversation storage has not started.")
    messages = await load_history(conversation_id)
    messages.extend([
        {"role": "user", "parts": [{"text": message}], "status": "completed"},
        {"role": "model", "parts": [{"text": ""}], "status": "interrupted"},
    ])
    state: Conversation = {"messages": messages, "updated_at": datetime.now(timezone.utc).isoformat()}
    async with aclosing(graph.astream(
        state, config(conversation_id), stream_mode="custom", durability="sync",
    )) as stream:
        async for event in stream:
            yield cast(ChatEvent, event)
