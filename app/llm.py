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

from groq import (APIConnectionError, APIError, APITimeoutError, AsyncGroq, InternalServerError,
                  RateLimitError)

from app import groq_api
from app.groq_api import ProviderError, groq_client, provider_error

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
DEFAULT_MODEL = "openai/gpt-oss-120b"
# Hedge for stalled requests: a separate Groq rate-limit bucket, same prompt and tool format.
DEFAULT_FALLBACK_MODEL = "openai/gpt-oss-20b"
# Measured on the AWS VM (30 requests each, this prompt and tools): gpt-oss-120b's first
# token p50 466 ms, p75 479, p90 680, max 2.6 s; gpt-oss-20b p50 404, p90 505, max 722.
# 600 ms is past the 120b's usual spread, so about one request in eight is hedged, and
# a hedged reply typically starts about 1 s after the request instead of stalling.
FIRST_TOKEN_DEADLINE_MS = 600
GIVE_UP_S = 5.0  # Neither model has started: report an error instead of waiting on.


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # JSON text, exactly as the model produced it.


@dataclass(frozen=True)
class ModelUsed:
    """First event of every stream: the model that produced the rest."""
    name: str


# The answering model (first), then text deltas, then the turn's tool calls (last).
ModelEvent = str | list[ToolCallRequest] | ModelUsed


def system_prompt(now: datetime | None = None) -> str:
    """The prompt plus the current local time, so the model never invents it."""
    zone = os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")
    now = now or datetime.now(ZoneInfo(zone))
    hour = now.strftime("%I").lstrip("0")
    return (f"{SYSTEM_PROMPT} The user's local time is {now:%A, %d %B %Y}, "
            f"{hour}:{now:%M %p} ({zone}).")


def to_chat_messages(history: Sequence[Any], caller_memory: str = "") -> list[dict[str, Any]]:
    """Stored conversation -> Groq chat messages, kept small for the token budget.

    `caller_memory` (see app/memory.py) ends the system prompt for a known caller.

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
    system = system_prompt() + (f"\n\n{caller_memory}" if caller_memory else "")
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
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


async def model_events(chunks: AsyncIterator[Any]) -> AsyncIterator[ModelEvent]:
    """Text deltas as they arrive; tool-call fragments are assembled and yielded last."""
    calls: dict[int, dict[str, str]] = {}
    async for chunk in chunks:
        usage = getattr(getattr(chunk, "x_groq", None), "usage", None)
        if usage is not None:
            # queue_time is time spent waiting inside Groq before generation began.
            logger.info("Groq LLM usage: %d input tokens, %d output, queue %.0f ms",
                        usage.prompt_tokens, usage.completion_tokens, (usage.queue_time or 0) * 1000)
            groq_api.note_usage(usage.prompt_tokens)
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


def has_output(chunk: Any) -> bool:
    """A text or tool-call delta: the model has started answering."""
    if not chunk.choices:
        return False
    delta = chunk.choices[0].delta
    return bool(delta.content or delta.tool_calls)


async def replay(head: list[Any], rest: AsyncIterator[Any]) -> AsyncIterator[Any]:
    for chunk in head:
        yield chunk
    async for chunk in rest:
        yield chunk


@dataclass
class Reply:
    """An open stream whose first text or tool-call delta has arrived."""
    model: str
    stream: Any
    events: AsyncIterator[ModelEvent]


async def open_reply(client: AsyncGroq, model: str, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None, tool_choice: str | None = None) -> Reply:
    """Start a stream and wait for its first text or tool-call delta (or its end)."""
    options: dict[str, Any] = {"tools": tools} if tools else {}
    if tools and tool_choice:
        options["tool_choice"] = tool_choice
    stream = await client.with_options(timeout=10).chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        reasoning_effort=os.getenv("GROQ_LLM_REASONING", "low"),  # type: ignore[arg-type]
        max_completion_tokens=400,
        stream=True,
        **options,
    )
    chunks = aiter(stream)
    head: list[Any] = []
    try:
        # Tool calls are only yielded once assembled, so watch the raw chunks: a
        # tool-call fragment counts as the model having started.
        while not (head and has_output(head[-1])):
            head.append(await anext(chunks))
    except StopAsyncIteration:
        pass  # The model produced nothing at all.
    except BaseException:
        await stream.close()  # Lost the race, cancelled, or failed.
        raise
    return Reply(model, stream, model_events(replay(head, chunks)))


@dataclass
class HedgeStats:
    """Counts since the process started, logged with every request."""
    requests: int = 0
    hedged: int = 0  # A fallback request was started.
    fallback_won: int = 0


hedge_stats = HedgeStats()


def first_token_deadline_s() -> float:
    return float(os.getenv("FIRST_TOKEN_DEADLINE_MS", FIRST_TOKEN_DEADLINE_MS)) / 1000


def worth_falling_back(error: BaseException) -> bool:
    """Failures another model may not share: its own rate-limit bucket, a timeout, an outage."""
    return isinstance(error, (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError))


async def first_reply(client: AsyncGroq, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]] | None, tool_choice: str | None) -> Reply:
    """Race the primary model against at most one fallback request; return the first to start.

    The fallback starts when the primary has produced nothing after the
    first-token deadline, or as soon as the primary fails with a rate limit,
    timeout or server error. The same request is never retried.
    """
    primary = os.getenv("GROQ_LLM_MODEL", DEFAULT_MODEL)
    fallback = os.getenv("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip()
    deadline_s = first_token_deadline_s()
    started = time.perf_counter()
    attempts: dict[asyncio.Task[Reply], str] = {}
    errors: dict[str, BaseException] = {}

    def start(model: str) -> None:
        attempts[asyncio.create_task(open_reply(client, model, messages, tools, tool_choice))] = model

    def may_hedge() -> bool:
        return bool(fallback) and fallback != primary and fallback not in attempts.values() \
            and fallback not in errors

    def hedge(reason: str) -> None:
        logger.warning("Groq LLM %s %s; falling back to %s", primary, reason, fallback)
        hedge_stats.hedged += 1
        start(fallback)

    logger.info("Groq LLM request (%d in the last minute)", groq_api.note_request())
    hedge_stats.requests += 1
    start(primary)
    try:
        while attempts:
            limit_s = deadline_s if may_hedge() else GIVE_UP_S
            done, _ = await asyncio.wait(attempts, return_when=asyncio.FIRST_COMPLETED,
                                         timeout=max(0.0, limit_s - (time.perf_counter() - started)))
            if not done:
                if not may_hedge():
                    raise TimeoutError
                hedge(f"sent nothing for {deadline_s * 1000:.0f} ms")
                continue
            for task in done:
                model = attempts.pop(task)
                error = task.exception()
                if error is None:
                    reply = task.result()
                    log_winner(reply.model, primary, (time.perf_counter() - started) * 1000)
                    return reply  # `finally` closes the other stream.
                errors[model] = error
                if model == primary and isinstance(error, RateLimitError):
                    groq_api.note_rate_limited()
                if model == primary and may_hedge() and worth_falling_back(error):
                    hedge(f"failed ({type(error).__name__})")
        raise errors.get(primary) or next(iter(errors.values()))
    finally:
        for task in attempts:
            task.cancel()
        for result in await asyncio.gather(*attempts, return_exceptions=True):
            if isinstance(result, Reply):  # Finished in the same instant as the winner.
                await result.stream.close()


def log_winner(model: str, primary: str, elapsed_ms: float) -> None:
    if model != primary:
        hedge_stats.fallback_won += 1
    stats = hedge_stats
    logger.info("Groq LLM first token from %s after %.0f ms; hedged %d of %d requests (%.0f%%), "
                "fallback won %d", model, elapsed_ms, stats.hedged, stats.requests,
                100 * stats.hedged / stats.requests, stats.fallback_won)


async def stream_model(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                       client: AsyncGroq | None = None,
                       tool_choice: str | None = None) -> AsyncIterator[ModelEvent]:
    """Yield the answering model's name, then text deltas as Groq produces them, then any tool calls.

    Groq's first token normally arrives in about half a second, but a request
    occasionally stalls for seconds while a caller waits in silence. So the
    request is hedged (see `first_reply`): nothing is yielded until one stream
    has started, so text is never duplicated.
    """
    async with (nullcontext(client) if client is not None else groq_client()) as groq:
        try:
            reply = await first_reply(groq, messages, tools, tool_choice)
            try:
                yield ModelUsed(reply.model)
                async for event in reply.events:
                    yield event
            finally:
                await reply.stream.close()
        except TimeoutError:
            raise ProviderError(504, "The language model did not respond in time.") from None
        except APIError as error:
            raise provider_error(error) from error
