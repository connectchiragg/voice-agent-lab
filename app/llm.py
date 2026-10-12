"""Streamed replies from an open-weight model on Groq (default: Qwen, reasoning off)."""
import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from groq import APIConnectionError, APIError, APITimeoutError, AsyncGroq

from app.speech import SpeechError, create_client, provider_error

logger = logging.getLogger("uvicorn.error")

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
            text += "\n[Your reply was cut off here; the user heard only this much.]"
        role = "assistant" if turn["role"] == "model" else "user"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": message})
    return messages


async def text_deltas(stream: AsyncIterator[Any]) -> AsyncIterator[str]:
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


async def open_reply(client: AsyncGroq, messages: list[dict[str, str]]) -> tuple[Any, AsyncIterator[str], str]:
    """Start a stream and wait for its first text; returns (stream, rest, first)."""
    stream = await client.with_options(timeout=10).chat.completions.create(
        model=os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b"),
        messages=messages,  # type: ignore[arg-type]
        reasoning_effort=os.getenv("GROQ_LLM_REASONING", "none"),  # type: ignore[arg-type]
        max_completion_tokens=400,
        stream=True,
    )
    deltas = text_deltas(stream)
    try:
        return stream, deltas, await anext(deltas)
    except BaseException:
        await stream.close()
        raise


async def stream_reply(message: str, history: Sequence[Any], client: AsyncGroq | None = None,
                       first_token_s: float = 3.5) -> AsyncIterator[str]:
    """Yield text deltas as soon as Groq produces them.

    Groq's first token normally arrives in well under a second, but a request
    occasionally stalls. A caller is waiting in silence, so if nothing arrives
    within `first_token_s` we abandon that request and retry once. Nothing has
    been yielded yet at that point, so a retry cannot duplicate text.
    """
    owned = client is None
    client = client or create_client()
    messages = to_chat_messages(message, history)
    try:
        for attempt in (1, 2):
            try:
                stream, deltas, first = await asyncio.wait_for(open_reply(client, messages), first_token_s)
                break
            except (TimeoutError, APITimeoutError, APIConnectionError) as error:
                if attempt == 2:
                    raise
                logger.warning("No first token from Groq (%s); retrying once", type(error).__name__)
        try:
            yield first
            async for delta in deltas:
                yield delta
        finally:
            await stream.close()
    except TimeoutError:
        raise SpeechError(504, "The language model did not respond in time.") from None
    except APIError as error:
        raise provider_error(error) from error
    finally:
        if owned:
            await client.close()
