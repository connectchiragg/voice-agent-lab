"""The conversation agent: a LangGraph tool loop with PostgreSQL (or in-memory) checkpoints."""
import asyncio
import logging
import os
import re
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

from app.llm import ToolCallRequest, stream_model, to_chat_messages
from app.tools import SCHEMAS, run_tool

load_dotenv(Path(__file__).resolve().parent.parent / ".env.docker")
logger = logging.getLogger("uvicorn.error")

MessageStatus = Literal["completed", "interrupted"]
MAX_TOOL_ROUNDS = 3
# Spoken if a tool is still running after FILLER_AFTER_S and the model has said
# nothing yet, so a slow lookup is not dead air. Instant tools get no filler.
TOOL_FILLER = "Let me check that."
FILLER_AFTER_S = 0.3
# A short reply like "Let me look up the forecast." with no tool call is a broken
# promise; the agent then asks again with a tool call required.
PROMISE = re.compile(r"\b(let me|let's|i'll|i will|i'm going to) (just )?"
                     r"(check|look|find|see|get|pull|fetch)\b|\bone moment\b|"
                     r"\bgive me a (second|moment)\b", re.IGNORECASE)


def promises_lookup(text: str) -> bool:
    return len(text.split()) <= 20 and PROMISE.search(text) is not None


class TextPart(TypedDict):
    text: str


class ToolCall(TypedDict):
    id: str
    name: str
    arguments: str


class Message(TypedDict):
    role: Literal["user", "model", "tool"]
    parts: list[TextPart]
    status: MessageStatus
    tool_calls: NotRequired[list[ToolCall]]  # Model messages that request tools.
    tool_call_id: NotRequired[str]  # Tool results, matched to their call.
    name: NotRequired[str]


class Conversation(TypedDict):
    messages: list[Message]
    updated_at: str


class ChatEvent(TypedDict):
    type: Literal["delta"]
    text: str


ConversationGraph = CompiledStateGraph[Conversation, None, Conversation, Conversation]
graph: ConversationGraph | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_message(text: str, status: MessageStatus = "completed") -> Message:
    return {"role": "model", "parts": [{"text": text}], "status": status}


def tool_rounds(messages: list[Message]) -> int:
    """Tool-calling model messages since the latest user message."""
    rounds = 0
    for message in reversed(messages):
        if message["role"] == "user":
            break
        rounds += bool(message.get("tool_calls"))
    return rounds


async def generate(messages: list[Message], allow_tools: bool, tool_choice: str | None = None,
                   ) -> AsyncIterator[str | list[ToolCallRequest]]:
    async with aclosing(stream_model(to_chat_messages(messages), SCHEMAS if allow_tools else None,
                                     tool_choice=tool_choice)) as stream:
        async for event in stream:
            yield event


async def agent(state: Conversation, config: RunnableConfig) -> Conversation:
    """Stream the model's text; end the turn, or hand its tool calls to `tools`."""
    messages = list(state["messages"])
    writer = get_stream_writer()
    allow_tools = tool_rounds(messages) < MAX_TOOL_ROUNDS  # Then the model must answer.
    text, calls = "", []
    try:
        for tool_choice in (None, "required"):
            async for event in generate(messages, allow_tools, tool_choice):
                if isinstance(event, str):
                    text += event
                    writer({"type": "delta", "text": event})
                else:
                    calls = event
            if calls or not allow_tools or not promises_lookup(text):
                break
            logger.warning("Model promised a lookup without calling a tool; requiring one")
            if not text.endswith((" ", "\n")):
                text += " "
                writer({"type": "delta", "text": " "})
    except Exception:
        # Keep a failed reply once, rather than writing every token to storage.
        if text:
            messages.append(model_message(text, "interrupted"))
            await save_history(config["configurable"]["thread_id"], messages)
        raise
    if not calls:
        messages.append(model_message(text))
        return {"messages": messages, "updated_at": now()}
    if text.strip() and not text.endswith((" ", "\n")):
        writer({"type": "delta", "text": " "})  # Separate it from the answer that follows.
    message = model_message(text.strip())
    message["tool_calls"] = [{"id": call.id, "name": call.name, "arguments": call.arguments}
                             for call in calls]
    messages.append(message)
    return {"messages": messages, "updated_at": now()}


async def tools(state: Conversation) -> Conversation:
    """Run the requested tools concurrently and append their results."""
    messages = list(state["messages"])
    request = messages[-1]
    calls = request.get("tool_calls", [])
    running = asyncio.ensure_future(
        asyncio.gather(*(run_tool(call["name"], call["arguments"]) for call in calls)))
    done, _ = await asyncio.wait({running}, timeout=FILLER_AFTER_S)
    if not done and not request["parts"][0]["text"].strip():
        get_stream_writer()({"type": "delta", "text": TOOL_FILLER + " "})
        # Stored with the tool call: the user heard it as part of this turn.
        messages[-1] = {**request, "parts": [{"text": TOOL_FILLER}]}
    try:
        results = await running
    finally:
        running.cancel()
    for call, result in zip(calls, results):
        messages.append({"role": "tool", "parts": [{"text": result}], "status": "completed",
                         "tool_call_id": call["id"], "name": call["name"]})
    return {"messages": messages, "updated_at": now()}


def after_agent(state: Conversation) -> str:
    return "tools" if state["messages"][-1].get("tool_calls") else END


def build_graph(checkpointer: BaseCheckpointSaver[str]) -> ConversationGraph:
    builder = StateGraph(Conversation)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", after_agent, ["tools", END])
    builder.add_edge("tools", "agent")
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
    }, as_node="agent")


async def stream_conversation(conversation_id: str, message: str) -> AsyncIterator[ChatEvent]:
    """Supply a new turn to the graph and forward its custom streaming events."""
    if graph is None:
        raise RuntimeError("Conversation storage has not started.")
    messages = await load_history(conversation_id)
    messages.append({"role": "user", "parts": [{"text": message}], "status": "completed"})
    state: Conversation = {"messages": messages, "updated_at": now()}
    async with aclosing(graph.astream(
        state, config(conversation_id), stream_mode="custom", durability="async",
    )) as stream:
        async for event in stream:
            yield cast(ChatEvent, event)


async def record_interruption(conversation_id: str, user_text: str, heard: str) -> None:
    """Store the interrupted reply as only the text the user actually heard.

    The cancelled run may have checkpointed any prefix of the turn: nothing,
    the user message, or tool calls and results. Tool exchanges that happened
    are kept; any spoken reply after them is replaced by what was heard.
    """
    messages = await load_history(conversation_id)
    start = next((index for index in range(len(messages) - 1, -1, -1)
                  if messages[index]["role"] == "user"
                  and messages[index]["parts"][0]["text"] == user_text), None)
    if start is None:
        messages.append({"role": "user", "parts": [{"text": user_text}], "status": "completed"})
        tail: list[Message] = []
    else:
        tail = [message for message in messages[start + 1:]
                if message["role"] == "tool" or message.get("tool_calls")]
        messages = messages[:start + 1]
    for message in tail:
        # Words spoken before a tool call (such as the filler) are already stored there.
        said = message["parts"][0]["text"] if message.get("tool_calls") else ""
        if said and heard.startswith(said):
            heard = heard[len(said):].strip()
    messages.extend(tail)
    messages.append(model_message(heard, "interrupted"))
    await save_history(conversation_id, messages)
