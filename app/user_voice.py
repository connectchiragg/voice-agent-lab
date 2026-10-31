"""Browser calls in a guest's workspace: its own Deepgram key and its own LLM key, per call.

The engines (app/main.py load_engines) are built once at start-up on the server's keys; they
stay the owner's, unchanged and with no extra lookup on his hot path (one ContextVar read).
For any other workspace a call starts only with both of its keys (`prepare`, once per call:
KeyMissing otherwise, whose message the call page shows) and runs on `for_guest(engines, keys)`:
the same engines with Flux and Aura on the workspace's Deepgram key (api.deepgram.com, the
global host) and no Groq rate-limit gate on speculation.

The reply stream (app/llm.stream_model) asks `route()` first: None for the owner (Groq, as
before), else the workspace's LLM key. The keys are pinned while a call is open (`opened` /
`closed`), so a long call never re-reads them; the caller-memory summary after a call reads
them through app/user_keys.py's cache. `stream` speaks OpenAI-compatible streaming chat
completions (groq, openai, gemini, openrouter) or Anthropic's streaming Messages API, over
httpx (the app image has neither SDK), and yields what app/llm.stream_model yields: the model
(ModelUsed), text deltas, then the turn's tool calls. A guest's turns are never counted against
the owner's Groq limits, and the owner's fallback model is never tried.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx

from app import key_providers, tenant, user_keys
from app.groq_api import ProviderError

logger = logging.getLogger("uvicorn.error")
GUEST_DEEPGRAM_HOST = "api.deepgram.com"
_pinned: dict[str, tuple[int, dict[str, user_keys.UserKey]]] = {}
_http: httpx.AsyncClient | None = None


def guest() -> str | None:
    """The workspace in scope when it is not the owner's (None: the owner, or no one)."""
    user = tenant.get()
    return None if user is None or user == tenant.OWNER else user


async def prepare() -> dict[str, user_keys.UserKey] | None:
    """Before a call opens: None for the owner; a guest's keys, or KeyMissing (speech first)."""
    if guest() is None:
        return None
    keys = await user_keys.load_current()
    for purpose in ("speech", "llm"):
        if purpose not in keys:
            raise user_keys.KeyMissing(purpose)
    return keys


def for_guest(engines: Any, keys: dict[str, user_keys.UserKey]) -> Any:
    """The start-up engines with Flux and Aura on the workspace's Deepgram key."""
    from app.deepgram import AuraSpeaker, DeepgramSettings, FluxListener
    speech = keys["speech"]
    sample_rate = engines.output_sample_rate
    eager = os.getenv("FLUX_EAGER_EOT_THRESHOLD", "0.4").strip()  # As DeepgramSettings.from_env.
    settings = DeepgramSettings(
        api_key=speech.key, host=GUEST_DEEPGRAM_HOST, voice=os.getenv("DEEPGRAM_VOICE", DeepgramSettings.voice),
        eot_threshold=float(os.getenv("FLUX_EOT_THRESHOLD", DeepgramSettings.eot_threshold)),
        eager_eot_threshold=float(eager) if eager else None,
        eot_timeout_ms=int(os.getenv("FLUX_EOT_TIMEOUT_MS", DeepgramSettings.eot_timeout_ms)),
        output_sample_rate=sample_rate)
    return replace(engines, new_listener=lambda on_event: FluxListener(settings, on_event),
                   new_speaker=lambda: AuraSpeaker(settings), may_speculate=lambda: True)


def opened(keys: dict[str, user_keys.UserKey]) -> str | None:
    """A guest's call opened: pin its keys; returns the workspace to pass to `closed`."""
    user = guest()
    if user is not None:
        count, _ = _pinned.get(user, (0, keys))
        _pinned[user] = (count + 1, keys)
    return user


def closed(user: str | None) -> None:
    if user is not None and user in _pinned:
        count, keys = _pinned[user]
        if count <= 1:
            _pinned.pop(user)
        else:
            _pinned[user] = (count - 1, keys)


async def route() -> user_keys.UserKey | None:
    """The LLM key a reply in this context must use: None for the owner (Groq, as before)."""
    user = guest()
    if user is None:
        return None
    pinned = _pinned.get(user)
    keys = pinned[1] if pinned else await user_keys.load_current()
    found = keys.get("llm")
    if found is None:
        raise ProviderError(503, user_keys.ADD_KEY)
    return found


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    return _http


# Request shaping.

def merge_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Every system message joined into one (the prompt, then the local-time note), and the rest."""
    system = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "system" and m.get("content"))
    return system, [m for m in messages if m["role"] != "system"]


def openai_body(key: user_keys.UserKey, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                tool_choice: str | None, max_tokens: int) -> dict[str, Any]:
    if key.provider == "gemini":  # Its compatible endpoint is not known to take a mid-chat system turn.
        system, rest = merge_system(messages)
        messages = [{"role": "system", "content": system}] + rest
    body: dict[str, Any] = {"model": key.model, "messages": messages, "stream": True,
                            "max_completion_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
    return key_providers.chat_body(key.provider, body, voice=True)


def anthropic_body(key: user_keys.UserKey, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                   tool_choice: str | None, max_tokens: int) -> dict[str, Any]:
    """Chat messages -> Messages API: one system prompt, tool calls as tool_use blocks, tool
    results as tool_result blocks in the next user turn, same-role turns merged."""
    system, rest = merge_system(messages)
    turns: list[dict[str, Any]] = []
    for message in rest:
        role = "user" if message["role"] in ("user", "tool") else "assistant"
        if message["role"] == "tool":
            blocks: list[dict[str, Any]] = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                                             "content": str(message.get("content") or "")}]
        else:
            text = str(message.get("content") or "").strip()
            blocks = [{"type": "text", "text": text}] if text else []
            for call in message.get("tool_calls") or []:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                               "input": arguments if isinstance(arguments, dict) else {}})
        if not blocks:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += blocks
        else:
            turns.append({"role": role, "content": blocks})
    if turns and turns[0]["role"] == "assistant":  # The Messages API starts with the user.
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "(The call starts.)"}]})
    body: dict[str, Any] = {"model": key.model, "max_tokens": max_tokens, "stream": True, "messages": turns,
                            "output_config": {"effort": "low"}}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": t["function"].get("parameters") or {"type": "object"}} for t in tools]
        # Forced tool use is refused by the newest Opus and Sonnet; Haiku takes it.
        forced = tool_choice == "required" and "haiku" in key.model
        body["tool_choice"] = {"type": "any" if forced else "auto"}
    return body


# Streaming.

def failure(provider: key_providers.Provider, model: str, status: int) -> ProviderError:
    """A provider's refusal, for the caller; never its body (it may echo private data)."""
    logger.warning("Guest LLM %s request failed: HTTP %d", provider.name, status)
    if status in (401, 403):
        return ProviderError(503, f"{provider.label} rejected your API key. Check it in Settings → API keys.")
    if status == 429:
        return ProviderError(429, f"{provider.label}'s rate limit was reached. Wait before trying again.")
    if status in (400, 404, 422):
        return ProviderError(503, f"{provider.label} couldn't use the model {model}. Check it in Settings → API keys.")
    if status == 402:
        return ProviderError(503, f"{provider.label} says the account is out of credit.")
    return ProviderError(502, f"{provider.label} is unavailable. Please try again.")


async def sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


async def stream(key: user_keys.UserKey, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                 tool_choice: str | None, max_tokens: int, client: httpx.AsyncClient | None = None
                 ) -> AsyncIterator[Any]:
    """ModelUsed, text deltas, then [ToolCallRequest, ...] if the model called tools."""
    from app.llm import ModelUsed, ToolCallRequest
    provider = key_providers.PROVIDERS[key.provider]
    anthropic = provider.kind == "anthropic"
    path = "/messages" if anthropic else "/chat/completions"
    body = (anthropic_body if anthropic else openai_body)(key, messages, tools, tool_choice, max_tokens)
    calls: dict[int, dict[str, str]] = {}
    try:
        async with (client or http()).stream("POST", provider.base_url + path, json=body,
                                             headers=key_providers.headers(provider, key.key)) as response:
            if response.status_code != 200:
                await response.aread()
                raise failure(provider, key.model, response.status_code)
            yield ModelUsed(f"{provider.name}:{key.model}")
            async for event in sse(response):
                if anthropic:
                    kind = event.get("type")
                    if kind == "error":
                        raise failure(provider, key.model, 529 if "overloaded" in json.dumps(event) else 500)
                    index = int(event.get("index", 0) or 0)
                    if kind == "content_block_start" and (event.get("content_block") or {}).get("type") == "tool_use":
                        block = event["content_block"]
                        calls[index] = {"id": block.get("id", ""), "name": block.get("name", ""), "arguments": ""}
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield delta["text"]
                        elif delta.get("type") == "input_json_delta" and index in calls:
                            calls[index]["arguments"] += delta.get("partial_json", "")
                    continue
                for choice in (event.get("choices") or [])[:1]:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield delta["content"]
                    for call in delta.get("tool_calls") or []:
                        entry = calls.setdefault(int(call.get("index", 0) or 0), {"id": "", "name": "", "arguments": ""})
                        entry["id"] = call.get("id") or entry["id"]
                        function = call.get("function") or {}
                        entry["name"] += function.get("name") or ""
                        entry["arguments"] += function.get("arguments") or ""
    except httpx.TimeoutException:
        raise ProviderError(504, f"{provider.label} did not respond in time.") from None
    except httpx.HTTPError as error:
        logger.warning("Guest LLM %s request failed: %s", provider.name, type(error).__name__)
        raise ProviderError(502, f"{provider.label} is unavailable. Please try again.") from None
    if calls:
        yield [ToolCallRequest(calls[i]["id"], calls[i]["name"], calls[i]["arguments"] or "{}") for i in sorted(calls)]
