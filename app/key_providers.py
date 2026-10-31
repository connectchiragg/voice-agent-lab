"""The providers a workspace's own API key may be for (app/user_keys.py): endpoints, default
models, the one cheap call that checks a key, and the request shape each one accepts.

Purposes: `llm` (groq, openai, anthropic, gemini, openrouter) and `speech` (deepgram).
Every LLM provider but Anthropic speaks OpenAI-compatible chat completions; Anthropic gets its
own Messages API. Default models (checked 2026-10-31):

- groq        openai/gpt-oss-120b         VALR's own voice model (app/llm.py)
- openai      gpt-6-luna                  developers.openai.com/api/docs/models: the cheapest,
                                          "most efficient", $0.10 / $0.50 per MTok
- anthropic   claude-haiku-5-5            the current Haiku, $0.10 / $0.50 per MTok
- gemini      gemini-3.1-flash-lite       Gemini's OpenAI-compatible endpoint (ai.google.dev
                                          /gemini-api/docs/openai); live on OpenRouter's list
- openrouter  google/gemini-3.1-flash-lite  from openrouter.ai/api/v1/models (tools supported)

`check` makes one free call per key: the provider's models list (OpenRouter: GET /key, since
its models list needs no key; Deepgram: GET /projects). 401 (403 but for Deepgram, whose
narrow-scope keys may not list projects; Gemini's 400 "valid API key") means "rejected"; anything else that is not a 200
(timeout, 5xx, 429) is "unchecked": the key is kept and tried for real later.

httpx only: the app image has neither the anthropic nor the openai SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

Kind = Literal["openai", "anthropic", "deepgram"]
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    purpose: Literal["llm", "speech"]
    kind: Kind
    base_url: str
    default_model: str
    check_path: str
    console: str  # Where to make a key.


PROVIDERS: dict[str, Provider] = {p.name: p for p in (
    Provider("groq", "Groq", "llm", "openai", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b",
             "/models", "https://console.groq.com/keys"),
    Provider("openai", "OpenAI", "llm", "openai", "https://api.openai.com/v1", "gpt-6-luna",
             "/models", "https://platform.openai.com/api-keys"),
    Provider("anthropic", "Anthropic", "llm", "anthropic", "https://api.anthropic.com/v1", "claude-haiku-5-5",
             "/models", "https://console.anthropic.com/settings/keys"),
    Provider("gemini", "Google Gemini", "llm", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
             "gemini-3.1-flash-lite", "/models", "https://aistudio.google.com/apikey"),
    Provider("openrouter", "OpenRouter", "llm", "openai", "https://openrouter.ai/api/v1",
             "google/gemini-3.1-flash-lite", "/key", "https://openrouter.ai/settings/keys"),
    Provider("deepgram", "Deepgram", "speech", "deepgram", "https://api.deepgram.com/v1", "",
             "/projects", "https://console.deepgram.com"),
)}
PURPOSES = ("llm", "speech")
MODEL_CHARS = 120


def for_purpose(purpose: str) -> list[Provider]:
    return [p for p in PROVIDERS.values() if p.purpose == purpose]


def headers(provider: Provider, key: str) -> dict[str, str]:
    if provider.kind == "anthropic":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if provider.kind == "deepgram":
        return {"Authorization": f"Token {key}"}
    return {"Authorization": f"Bearer {key}"}


async def check(provider: Provider, key: str, http: httpx.AsyncClient) -> Literal["ok", "rejected", "unchecked"]:
    """One cheap authenticated GET. Never logs or returns the key or the provider's body."""
    try:
        response = await http.get(provider.base_url + provider.check_path, headers=headers(provider, key),
                                  timeout=10)
    except httpx.HTTPError:
        return "unchecked"
    if response.status_code == 200:
        return "ok"
    if response.status_code == 401 or (response.status_code == 403 and provider.kind != "deepgram"):
        return "rejected"
    if response.status_code == 403:  # Deepgram: authenticated, but the key may not list projects.
        return "ok"
    if response.status_code == 400 and provider.name == "gemini" and "api key" in response.text.lower():
        return "rejected"  # Gemini answers a bad key with 400 "Please pass a valid API key" (2026-10-31).
    return "unchecked"


def chat_body(provider: str, body: dict[str, Any], voice: bool = False) -> dict[str, Any]:
    """An OpenAI-compatible request body as `provider` takes it. Groq's own fields
    (reasoning_format, its "none"/"default" reasoning_effort) go; Gemini and OpenRouter get
    max_tokens for max_completion_tokens. `voice`: a live reply, asked to reason little."""
    out = dict(body)
    out.pop("reasoning_format", None)
    effort = out.pop("reasoning_effort", None)
    if provider == "groq" and (voice or effort is not None):
        # gpt-oss takes low/medium/high; Qwen takes none/default (VALR's background models).
        gpt_oss = str(out.get("model", "")).startswith("openai/gpt-oss")
        if voice:
            out["reasoning_effort"] = "low" if gpt_oss else "none"
        else:
            out["reasoning_effort"] = {"none": "low", "default": "medium"}.get(effort, effort) if gpt_oss else effort
    elif provider in ("openai", "gemini") and voice:
        out["reasoning_effort"] = "low"
    elif provider == "openrouter" and voice:
        out["reasoning"] = {"effort": "low"}
    if provider in ("gemini", "openrouter") and "max_completion_tokens" in out:
        out["max_tokens"] = out.pop("max_completion_tokens")
    return out
