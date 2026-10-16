"""Where background Claude calls go: OpenRouter (BYOK) or Anthropic direct.

Research worker and profile scripts only; the live voice LLM stays on Groq direct.

- `LLM_GATEWAY=openrouter|anthropic` (default `openrouter` when OPENROUTER_API_KEY is set).
  OpenRouter is reached with the Anthropic SDK pointed at https://openrouter.ai/api
  (POST /api/v1/messages, the Anthropic Messages format). Each request is pinned to the
  first-party Anthropic endpoint (`provider.only=["anthropic"]`, no fallbacks), which keeps
  native web_search and the prompt cache. Model ids map `claude-haiku-5-5` ->
  `anthropic/claude-haiku-5.5`; callers keep the canonical ids (pricing, CrewAI).
  OpenRouter returns thinking summaries by default, Anthropic omits them: the gateway
  sends `display: "omitted"` unless the caller set `thinking`.
- `LLM_GATEWAY_FALLBACK=1` (default): an OpenRouter error (HTTP status or connection)
  re-runs the request on Anthropic direct, logged as "VALR gateway fallback".
- `LLM_BYOK_GUARD=refuse|warn|off` (default refuse): every OpenRouter response must carry
  `usage.is_byok=true` (it ran on our Anthropic key, not OpenRouter credits). On false,
  "VALR BYOK ALERT" is logged at ERROR; `refuse` also trips the gateway for this process:
  later calls go to Anthropic direct (fallback on) or raise ByokRequired (fallback off).
  At most one request per process can run on credits.
"""
import logging
import os
import re
import threading
from collections import Counter
from typing import Any

import anthropic

logger = logging.getLogger("valr")
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
PROVIDER_PIN = {"only": ["anthropic"], "allow_fallbacks": False}
_MODEL = re.compile(r"^(claude-[a-z]+)-(\d+)-(\d+)$")


class ByokRequired(RuntimeError):
    """OpenRouter served a request on its own credits and the guard refuses more."""


def openrouter_model(model: str) -> str:
    """`claude-haiku-5-5` -> `anthropic/claude-haiku-5.5`; slugs pass through."""
    if "/" in model:
        return model
    match = _MODEL.match(model)
    return f"anthropic/{match[1]}-{match[2]}.{match[3]}" if match else f"anthropic/{model}"


def _resolve(client: Any, path: str) -> Any:
    for name in path.split("."):
        client = getattr(client, name)
    return client


class Gateway:
    """Routes Messages calls; thread-safe counters in `stats`."""

    def __init__(self, mode: str | None = None, openrouter_key: str | None = None,
                 fallback: bool | None = None, guard: str | None = None,
                 openrouter: Any = None, direct: Any = None, timeout: float | None = None,
                 max_retries: int = 2) -> None:
        self.openrouter_key = openrouter_key if openrouter_key is not None else os.getenv("OPENROUTER_API_KEY", "")
        self.mode = (mode or os.getenv("LLM_GATEWAY") or ("openrouter" if self.openrouter_key else "anthropic")).lower()
        if self.mode not in ("openrouter", "anthropic"):
            raise ValueError(f"LLM_GATEWAY must be openrouter or anthropic, not {self.mode!r}")
        if self.mode == "openrouter" and not self.openrouter_key and openrouter is None:
            raise ValueError("LLM_GATEWAY=openrouter needs OPENROUTER_API_KEY")
        self.fallback = fallback if fallback is not None else os.getenv("LLM_GATEWAY_FALLBACK", "1") == "1"
        self.guard = (guard or os.getenv("LLM_BYOK_GUARD", "refuse")).lower()
        if self.guard not in ("refuse", "warn", "off"):
            raise ValueError(f"LLM_BYOK_GUARD must be refuse, warn or off, not {self.guard!r}")
        # Per request; the Sonnet writer at effort high can take most of a minute.
        self.timeout = timeout if timeout is not None else float(os.getenv("LLM_GATEWAY_TIMEOUT_S", "120"))
        self.max_retries = max_retries
        self._openrouter, self._direct = openrouter, direct
        self.tripped = False
        self.stats: Counter[str] = Counter()
        self.lock = threading.Lock()

    def describe(self) -> str:
        return (f"LLM gateway {self.mode} (fallback to Anthropic direct "
                f"{'on' if self.fallback else 'off'}, BYOK guard {self.guard})")

    # Clients, built on first use.

    def openrouter_client(self) -> Any:
        if self._openrouter is None:
            # The SDK sends the key as x-api-key, which OpenRouter accepts (checked 2026-10-16).
            self._openrouter = anthropic.Anthropic(api_key=self.openrouter_key, base_url=OPENROUTER_BASE_URL,
                                                   timeout=self.timeout, max_retries=self.max_retries)
        return self._openrouter

    def direct_client(self) -> Any:
        if self._direct is None:
            self._direct = anthropic.Anthropic(timeout=self.timeout, max_retries=self.max_retries)
        return self._direct

    def client(self) -> "GatewayClient":
        """An object with the Anthropic client's `messages` and `beta.messages` surface."""
        return GatewayClient(self)

    # Routing.

    def openrouter_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        out = dict(kwargs)
        out["model"] = openrouter_model(kwargs["model"])
        if "thinking" not in out:
            out["thinking"] = {"type": "adaptive", "display": "omitted"}
        body = dict(kwargs.get("extra_body") or {})
        # OpenRouter silently ignores the deprecated `output_format` (CrewAI sends it for
        # Sonnet 5.5): move it to output_config.format, which it forwards (checked 2026-10-16).
        if "output_format" in body:
            config = {**(out.pop("output_config", None) or {}), **(body.get("output_config") or {})}
            body["output_config"] = {**config, "format": body.pop("output_format")}
        # OpenRouter rejects Anthropic's `fallbacks: "default"` (server-side refusal fallback,
        # Claude API only): drop it there; a refusal then surfaces as stop_reason "refusal".
        for source in (out, body):
            if isinstance(source.get("fallbacks"), str):
                source.pop("fallbacks")
                self._count("openrouter_fallbacks_dropped")
        body["provider"] = PROVIDER_PIN
        out["extra_body"] = body
        return out

    def call(self, path: str, method: str, args: tuple, kwargs: dict[str, Any]) -> Any:
        if self.mode == "openrouter":
            if self.tripped:
                if not self.fallback:
                    raise ByokRequired("OpenRouter served a request on credits; LLM_BYOK_GUARD=refuse")
            else:
                try:
                    response = getattr(_resolve(self.openrouter_client(), path), method)(
                        *args, **self.openrouter_kwargs(kwargs))
                except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
                    if not self.fallback:
                        raise
                    self._count("fallback")
                    logger.warning("VALR gateway fallback: OpenRouter failed (%s %s) on %s; "
                                   "retrying on Anthropic direct", type(error).__name__,
                                   getattr(error, "status_code", ""), kwargs.get("model"))
                else:
                    if method == "create":
                        self.check_byok(response, kwargs.get("model", ""))
                    else:
                        self._count("openrouter_stream_unchecked")
                    return response
        self._count("direct")
        return getattr(_resolve(self.direct_client(), path), method)(*args, **kwargs)

    def check_byok(self, response: Any, model: str) -> None:
        usage = getattr(response, "usage", None)
        byok = getattr(usage, "is_byok", None)
        if byok is None and isinstance(getattr(usage, "model_extra", None), dict):
            byok = usage.model_extra.get("is_byok")
        logger.info("VALR gateway openrouter %s is_byok=%s cost=%s", model, byok,
                    getattr(usage, "cost", None))
        if byok is True:
            self._count("openrouter_byok")
            return
        self._count("openrouter_not_byok")
        if self.guard == "off":
            return
        logger.error("VALR BYOK ALERT: OpenRouter served %s with is_byok=%s (OpenRouter credits, "
                     "not our Anthropic key). Check the Anthropic key under openrouter.ai "
                     "Integrations (BYOK) with 'Never use shared capacity'.%s", model, byok,
                     " Further calls in this process skip OpenRouter." if self.guard == "refuse" else "")
        if self.guard == "refuse":
            self.tripped = True

    def _count(self, name: str) -> None:
        with self.lock:
            self.stats[name] += 1


class _Messages:
    def __init__(self, gateway: Gateway, path: str) -> None:
        self._gateway, self._path = gateway, path

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return self._gateway.call(self._path, "create", args, kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        return self._gateway.call(self._path, "stream", args, kwargs)


class _Beta:
    def __init__(self, gateway: Gateway) -> None:
        self.messages = _Messages(gateway, "beta.messages")


class GatewayClient:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.messages = _Messages(gateway, "messages")
        self.beta = _Beta(gateway)


_shared: Gateway | None = None
_shared_lock = threading.Lock()


def shared() -> Gateway:
    """The process-wide gateway (one BYOK trip state for all callers)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Gateway()
            logger.info("VALR %s", _shared.describe())
        return _shared


def required_key_missing() -> str:
    """'' when the configured gateway has a key, else a message saying what is missing."""
    mode = (os.getenv("LLM_GATEWAY") or ("openrouter" if os.getenv("OPENROUTER_API_KEY") else "anthropic")).lower()
    if mode == "openrouter" and not os.getenv("OPENROUTER_API_KEY"):
        return "LLM_GATEWAY=openrouter but OPENROUTER_API_KEY is not set"
    if mode == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY is not set"
    return ""
