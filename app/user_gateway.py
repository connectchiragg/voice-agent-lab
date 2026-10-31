"""The LLM gateway of a workspace that is not the owner's: its own key, its own provider.

`llm_gateway.for_current()` hands every background caller (research crew and inner calls,
interview briefings, company resolver, lead classifier, notification judge, coach and post-call
analysis) a `UserGateway` when a guest's workspace is in scope. It keeps the Gateway surface
(`client()`, `openai_client()`, `limits()`, ...) so callers do not change, and it can never
reach a server key:

- no fallback and no BYOK guard; `backend=` overrides (LLM_GATEWAY_RESEARCH, ...) are ignored;
  every call is served by this provider, at the workspace's one model (the canonical
  claude-haiku/sonnet/opus names all map to it);
- groq, openai, gemini, openrouter: the gateway's chat-completions path (the same translation,
  JSON retry and keyless web search, DuckDuckGo then Wikipedia, as the owner's Groq mode) on
  the provider's endpoint, with the body shaped for it (app/key_providers.chat_body). No Groq
  free-tier limiter: the quota is the workspace's own;
- anthropic: the Anthropic SDK with the workspace's key, so Anthropic's own web_search tool and
  citations work as for the owner's Anthropic mode.

Gateways are cached per workspace and key fingerprint (CACHE_MAX, least recently used out), so
a changed key gets a new gateway. Worker image only (it imports anthropic/openai lazily).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any

import httpx

from app import groq_backend, key_providers, user_keys
from app.llm_gateway import OWN_KWARGS, Gateway, GatewayClient, _resolve

logger = logging.getLogger("valr")
CACHE_MAX = 64


class StripTransport(httpx.BaseTransport):
    """Drops VALR's internal x-valr-* request headers before they leave for a provider."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self.inner = inner or httpx.HTTPTransport(retries=1)
        self.limiters = groq_backend.Limiters()  # Unused: the Gateway surface reads it.

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for name in [n for n in request.headers if n.lower().startswith("x-valr-")]:
            del request.headers[name]
        return self.inner.handle_request(request)


class NoServerKey(RuntimeError):
    """A guest's gateway was asked for a client on a server key."""


class UserGateway(Gateway):
    def __init__(self, provider: str, key: str, model: str = "", transport: httpx.BaseTransport | None = None,
                 direct: Any = None, timeout: float | None = None) -> None:
        self.provider = key_providers.PROVIDERS[provider]
        if self.provider.purpose != "llm":
            raise ValueError(f"{provider} is not an LLM provider")
        self.model_id = model or self.provider.default_model
        self.chat_provider = provider
        self._key = key
        chat = self.provider.kind == "openai"
        if not chat and direct is None:
            import anthropic
            direct = anthropic.Anthropic(api_key=key, timeout=timeout or 600.0, max_retries=2,
                                         **({"http_client": httpx.Client(transport=transport)} if transport else {}))
        super().__init__(mode="groq" if chat else "anthropic", openrouter_key="", groq_key=key if chat else "",
                         fallback=False, guard="off", direct=direct if not chat else _Refused(),
                         openrouter=_Refused(), timeout=timeout,
                         groq_transport=StripTransport(transport) if chat else None)

    def __repr__(self) -> str:
        return f"UserGateway({self.chat_provider!r}, {self.model_id!r})"

    def describe(self) -> str:
        return f"LLM gateway {self.chat_provider} (a workspace's own key, model {self.model_id}; no fallback)"

    # Clients: the provider's, never the server's.

    def groq_http(self) -> httpx.Client:
        if self._groq_http is None:
            self._groq_http = httpx.Client(base_url=self.provider.base_url, transport=self.transport,
                                           timeout=self.timeout, headers=key_providers.headers(self.provider, self._key))
        return self._groq_http

    def openai_client(self, priority: str = "background", timeout: float | None = None) -> Any:
        import openai
        if self.provider.kind != "openai":
            raise NoServerKey("an Anthropic key has no OpenAI-compatible client")
        http = httpx.Client(transport=self.transport, timeout=timeout or self.timeout)
        return openai.OpenAI(api_key=self._key, base_url=self.provider.base_url, http_client=http, max_retries=1)

    def openrouter_client(self) -> Any:
        raise NoServerKey("a workspace's gateway has no OpenRouter BYOK client")

    # Routing: one provider, one model, whatever the caller asked for.

    def chat_model(self, model: str) -> str:
        return self.model_id

    def served_model(self, model: str, backend: str | None = None) -> str:
        return self.model_id

    def shape_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        return key_providers.chat_body(self.chat_provider, body)

    def crew_mode(self, feature: str) -> str:
        return "groq" if self.provider.kind == "openai" else "anthropic"

    def limits(self, model: str, backend: str | None = None) -> dict[str, Any]:
        if self.chat_provider == "groq":
            lim = groq_backend.limits_for(self.model_id)
            return {"backend": "groq", "model": self.model_id, "context": lim.context,
                    "max_input": int(lim.max_input * 0.9), "max_output": lim.max_output,
                    "max_output_background": lim.max_output, "rpm": lim.rpm, "rpd": lim.rpd,
                    "tpm": lim.tpm, "itpm": lim.itpm, "otpm": lim.otpm, "tpd": lim.tpd}
        return {"backend": self.chat_provider, "model": self.model_id, "context": 200_000, "max_input": 200_000,
                "max_output": 32_000, "max_output_background": 32_000, "rpm": None, "rpd": None, "tpm": None,
                "itpm": None, "otpm": None, "tpd": None}

    def client(self, timeout: float | None = None, priority: str = "background",
               backend: str | None = None) -> GatewayClient:
        return GatewayClient(self, timeout, priority, None)

    def call(self, path: str, method: str, args: tuple, kwargs: dict[str, Any],
             priority: str = "background") -> Any:
        own = {key: kwargs.pop(key) for key in OWN_KWARGS if key in kwargs}
        kwargs["model"] = self.model_id
        if self.provider.kind == "openai":
            self._count(self.chat_provider)
            return self.call_groq(method, kwargs, own.get("json_output"), priority)
        self._count("anthropic")
        return getattr(_resolve(self._direct, path), method)(*args, **kwargs)


class _Refused:
    """Stands where the base gateway keeps a server-key client: any use raises."""

    def __getattr__(self, name: str) -> Any:
        raise NoServerKey("a workspace's gateway never uses the server's keys")


_gateways: OrderedDict[tuple[str, str, str, str], UserGateway] = OrderedDict()
_lock = threading.Lock()


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def for_workspace(user: str) -> UserGateway:
    """The workspace's gateway on its own `llm` key; user_keys.KeyMissing without one."""
    found = user_keys.keys_sync(user).get("llm")
    if found is None:
        raise user_keys.KeyMissing("llm")
    cache_key = (user, found.provider, found.model, fingerprint(found.key))
    with _lock:
        gateway = _gateways.get(cache_key)
        if gateway is None:
            for stale in [k for k in _gateways if k[0] == user]:
                _gateways.pop(stale)
            gateway = _gateways[cache_key] = UserGateway(found.provider, found.key, found.model)
            logger.info("VALR %s", gateway.describe())
            while len(_gateways) > CACHE_MAX:
                _gateways.popitem(last=False)
        _gateways.move_to_end(cache_key)
        return gateway


def forget() -> None:
    with _lock:
        _gateways.clear()
