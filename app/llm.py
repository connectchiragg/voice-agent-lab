"""Streamed replies from an open-weight model on Groq (default: Qwen, reasoning off)."""
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from groq import APIError, AsyncGroq

from app.speech import create_client, provider_error

SYSTEM_PROMPT = (
    "You are a friendly voice assistant on a live call. Reply conversationally in English, "
    "usually in one or two short sentences. Never use Markdown, lists, emoji or URLs: "
    "everything you write is spoken aloud. Ask a brief follow-up question when it helps."
)


def to_chat_messages(message: str, history: Sequence[Any]) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history:
        text = turn["parts"][0]["text"]
        if turn.get("status") == "interrupted":
            text += "\n[This reply was interrupted before it finished.]"
        role = "assistant" if turn["role"] == "model" else "user"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": message})
    return messages


async def stream_reply(message: str, history: Sequence[Any],
                       client: AsyncGroq | None = None) -> AsyncIterator[str]:
    """Yield text deltas as soon as Groq produces them."""
    owned = client is None
    client = client or create_client()
    try:
        # A caller is waiting in silence: fail in seconds rather than the default 30.
        stream = await client.with_options(timeout=10).chat.completions.create(
            model=os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b"),
            messages=to_chat_messages(message, history),  # type: ignore[arg-type]
            reasoning_effort=os.getenv("GROQ_LLM_REASONING", "none"),  # type: ignore[arg-type]
            max_completion_tokens=400,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta
    except APIError as error:
        raise provider_error(error) from error
    finally:
        if owned:
            await client.close()
