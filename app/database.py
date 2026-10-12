"""A one-node conversation workflow with PostgreSQL (or in-memory) checkpoints."""
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from dotenv import load_dotenv
from fastapi import FastAPI
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg.conninfo import make_conninfo

from app.llm import stream_reply

load_dotenv(Path(__file__).resolve().parent.parent / ".env.docker")
logger = logging.getLogger("uvicorn.error")

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
    """Stream immediately; persist the accumulated reply only when generation ends."""
    messages = list(state["messages"])
    message = messages[-2]["parts"][0]["text"]
    conversation_id = config["configurable"]["thread_id"]
    writer = get_stream_writer()
    # The input checkpoint holds the user and empty assistant, not streamed deltas.
    writer({"type": "start"})
    reply = ""
    try:
        async with aclosing(stream_reply(message, messages[:-2])) as stream:
            async for chunk in stream:
                reply += chunk
                writer({"type": "delta", "text": chunk})
    except Exception:
        # Retain a failed reply once, rather than writing every token to storage.
        messages[-1] = {"role": "model", "parts": [{"text": reply}], "status": "interrupted"}
        await save_history(conversation_id, messages)
        raise
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
    """Use local Postgres when it is running; otherwise keep history in memory."""
    global graph
    async with AsyncExitStack() as stack:
        try:
            connection_string = make_conninfo(
                host="127.0.0.1", port=5432, dbname="voice_agent", user="voice_agent",
                password=os.environ["POSTGRES_PASSWORD"], connect_timeout=5,
                options="-c statement_timeout=5000",
            )
            saver: BaseCheckpointSaver[str] = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(connection_string))
            await saver.setup()
        except Exception as error:
            logger.warning("Postgres unavailable (%s); conversation history is in memory "
                           "and lost on restart.", type(error).__name__)
            saver = InMemorySaver()
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
        state, config(conversation_id), stream_mode="custom", durability="async",
    )) as stream:
        async for event in stream:
            yield cast(ChatEvent, event)


async def record_interruption(conversation_id: str, user_text: str, heard: str) -> None:
    """Store the interrupted reply as only the text the user actually heard.

    The cancelled graph run may or may not have checkpointed its input (the user
    turn plus an empty reply), so match on the user text instead of assuming.
    """
    messages = await load_history(conversation_id)
    reply: Message = {"role": "model", "parts": [{"text": heard}], "status": "interrupted"}
    user: Message = {"role": "user", "parts": [{"text": user_text}], "status": "completed"}

    def is_user_turn(message: Message) -> bool:
        return message["role"] == "user" and message["parts"][0]["text"] == user_text

    if len(messages) >= 2 and messages[-1]["role"] == "model" and is_user_turn(messages[-2]):
        messages[-1] = reply
    elif messages and is_user_turn(messages[-1]):
        messages.append(reply)
    else:
        messages.extend([user, reply])
    await save_history(conversation_id, messages)
