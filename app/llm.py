"""Streamed replies and tool calls from an open-weight model on Groq (default: gpt-oss-120b)."""
import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from groq import APIConnectionError, APIError, APITimeoutError, AsyncGroq, RateLimitError

from app.speech import SpeechError, groq_client, provider_error, rate_limit_wait_s

logger = logging.getLogger("uvicorn.error")

SYSTEM_PROMPT = (
    "You are a friendly voice assistant on a live call. Reply conversationally in English, "
    "usually in one or two short sentences. Never use Markdown, lists, emoji, symbols or URLs: "
    "everything you write is spoken aloud, so write units and numbers as words, for example "
    "twenty-eight degrees. Ask a brief follow-up question when it helps. "
    "Use your tools for weather, and for the time in other places, instead of guessing: "
    "call the tool in this same response, and never say you will check something without "
    "calling the tool. Never work out another place's time from time differences yourself, "
    "even if you think you know them: always call get_time."
)
HISTORY_MESSAGES = 12  # Earlier turns cost input tokens on every call (Groq: 7,000/min).
CUT_OFF_NOTE = "\n[Your reply was cut off here; the user heard only this much.]"


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # JSON text, exactly as the model produced it.


ModelEvent = str | list[ToolCallRequest]  # A text delta, or the turn's tool calls (last).


def system_prompt(now: datetime | None = None) -> str:
    """The prompt plus the current local time, so the model never invents it."""
    zone = os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")
    now = now or datetime.now(ZoneInfo(zone))
    hour = now.strftime("%I").lstrip("0")
    return (f"{SYSTEM_PROMPT} The user's local time is {now:%A, %d %B %Y}, "
            f"{hour}:{now:%M %p} ({zone}).")


def to_chat_messages(history: Sequence[Any]) -> list[dict[str, Any]]:
    """Stored conversation -> Groq chat messages, kept small for the token budget.

    - Tool calls and results from earlier turns are left out: the spoken answer
      that followed them already carries the facts.
    - Only the last HISTORY_MESSAGES dialogue messages before this turn are sent.
    - A tool call is only sent with its result; calls whose results were never
      stored (interrupted mid-tool) are dropped, as the API would reject them.
    """
    current = max((index for index, turn in enumerate(history) if turn["role"] == "user"), default=0)
    # Drop earlier tool exchanges whole, including words said before a call such as
    # "Let me check that.": kept alone, they teach the model to promise a lookup
    # and answer without calling a tool.
    earlier = [turn for turn in history[:current]
               if turn["role"] != "tool" and not turn.get("tool_calls")]
    turns = earlier[-HISTORY_MESSAGES:] + list(history[current:])
    answered = {turn["tool_call_id"] for turn in turns if turn["role"] == "tool"}
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt()}]
    for index, turn in enumerate(turns):
        text = turn["parts"][0]["text"]
        if turn["role"] == "tool":
            messages.append({"role": "tool", "tool_call_id": turn["tool_call_id"], "content": text})
            continue
        if turn["role"] == "user":
            messages.append({"role": "user", "content": text})
            continue
        if turn.get("status") == "interrupted":
            text += CUT_OFF_NOTE
        message: dict[str, Any] = {"role": "assistant", "content": text}
        in_current_turn = index >= len(turns) - (len(history) - current)
        calls = [call for call in turn.get("tool_calls", []) if call["id"] in answered] if in_current_turn else []
        if calls:
            message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": call["arguments"]}} for call in calls]
        messages.append(message)
    return messages


async def model_events(stream: AsyncIterator[Any]) -> AsyncIterator[ModelEvent]:
    """Text deltas as they arrive; tool-call fragments are assembled and yielded last."""
    calls: dict[int, dict[str, str]] = {}
    async for chunk in stream:
        usage = getattr(getattr(chunk, "x_groq", None), "usage", None)
        if usage is not None:
            # queue_time is time spent waiting inside Groq before generation began.
            logger.info("Groq LLM usage: %d input tokens, %d output, queue %.0f ms",
                        usage.prompt_tokens, usage.completion_tokens, (usage.queue_time or 0) * 1000)
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content
        for call in delta.tool_calls or []:
            entry = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
            entry["id"] = call.id or entry["id"]
            if call.function is not None:
                entry["name"] += call.function.name or ""
                entry["arguments"] += call.function.arguments or ""
    if calls:
        yield [ToolCallRequest(**calls[index]) for index in sorted(calls)]


async def open_reply(client: AsyncGroq, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None, tool_choice: str | None = None,
                     ) -> tuple[Any, AsyncIterator[ModelEvent], ModelEvent | None]:
    """Start a stream and wait for its first event; returns (stream, rest, first)."""
    options: dict[str, Any] = {"tools": tools} if tools else {}
    if tools and tool_choice:
        options["tool_choice"] = tool_choice
    stream = await client.with_options(timeout=10).chat.completions.create(
        model=os.getenv("GROQ_LLM_MODEL", "openai/gpt-oss-120b"),
        messages=messages,  # type: ignore[arg-type]
        reasoning_effort=os.getenv("GROQ_LLM_REASONING", "low"),  # type: ignore[arg-type]
        max_completion_tokens=400,
        stream=True,
        **options,
    )
    events = model_events(stream)
    try:
        return stream, events, await anext(events)
    except StopAsyncIteration:
        return stream, events, None  # The model produced nothing at all.
    except BaseException:
        await stream.close()
        raise


async def stream_model(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                       client: AsyncGroq | None = None, first_token_s: float = 3.5,
                       tool_choice: str | None = None) -> AsyncIterator[ModelEvent]:
    """Yield text deltas as soon as Groq produces them, then any tool calls.

    Groq's first token normally arrives in well under a second, but a request
    occasionally stalls. A caller is waiting in silence, so if nothing arrives
    within `first_token_s` we abandon that request and retry once. A short
    rate-limit wait (429 with retry-after <= 3 s) is also retried once. Nothing
    has been yielded before a retry, so text is never duplicated.
    """
    async with (nullcontext(client) if client is not None else groq_client()) as groq:
        try:
            for attempt in (1, 2):
                started = time.perf_counter()
                try:
                    stream, events, first = await asyncio.wait_for(
                        open_reply(groq, messages, tools, tool_choice), first_token_s)
                except (TimeoutError, APITimeoutError, APIConnectionError) as error:
                    logger.warning("No first token from Groq after %.0f ms (%s), attempt %d",
                                   (time.perf_counter() - started) * 1000, type(error).__name__, attempt)
                    if attempt == 2:
                        raise
                    continue
                except RateLimitError as error:
                    wait = rate_limit_wait_s(error)
                    if attempt == 2 or wait is None:
                        raise
                    logger.warning("Groq LLM rate limited; retrying in %.1f s", wait)
                    await asyncio.sleep(wait)
                    continue
                elapsed_ms = (time.perf_counter() - started) * 1000
                if elapsed_ms > 1000:
                    logger.warning("Slow Groq first token: %.0f ms, attempt %d", elapsed_ms, attempt)
                break
            try:
                if first is not None:
                    yield first
                async for event in events:
                    yield event
            finally:
                await stream.close()
        except TimeoutError:
            raise SpeechError(504, "The language model did not respond in time.") from None
        except APIError as error:
            raise provider_error(error) from error
