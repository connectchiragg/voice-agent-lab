"""Small structured-JSON wrapper over the Anthropic Messages API, with a token ledger.

The classifier (Haiku) and evaluator (Sonnet) only need "prompt in, JSON out",
so they depend on the `JsonLLM` protocol; tests pass a fake.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)

# USD per 1M tokens (input, output); Anthropic list prices, Oct 2026.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}


class LLMError(RuntimeError):
    pass


@dataclass
class Ledger:
    calls: dict = field(default_factory=dict)

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        row = self.calls.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        row["calls"] += 1
        row["input_tokens"] += input_tokens
        row["output_tokens"] += output_tokens

    def cost_usd(self) -> float:
        total = 0.0
        for model, row in self.calls.items():
            price_in, price_out = PRICES.get(model, (0.0, 0.0))
            total += row["input_tokens"] / 1e6 * price_in + row["output_tokens"] / 1e6 * price_out
        return round(total, 4)

    def summary(self) -> str:
        parts = [f"{m}: {r['calls']} calls, {r['input_tokens']} in / {r['output_tokens']} out"
                 for m, r in self.calls.items()]
        return "; ".join(parts or ["no LLM calls"]) + f"; est. ${self.cost_usd():.4f}"


class JsonLLM(Protocol):
    ledger: Ledger

    def json(self, *, model: str, system: str, user: str, schema: dict, max_tokens: int,
             effort: str) -> dict: ...


class AnthropicJsonLLM:
    """Structured outputs (output_config.format) so every reply is schema-valid JSON."""

    def __init__(self, client=None, ledger: Ledger | None = None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.ledger = ledger or Ledger()

    def json(self, *, model: str, system: str, user: str, schema: dict, max_tokens: int,
             effort: str) -> dict:
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}],
                      output_config={"effort": effort,
                                     "format": {"type": "json_schema", "schema": schema}})
        if model.startswith("claude-sonnet-5-5"):
            # Server-side fallback on a safety-classifier refusal (Claude API only).
            response = self.client.beta.messages.create(
                **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        else:
            response = self.client.messages.create(**kwargs)
        usage = response.usage
        self.ledger.add(model, usage.input_tokens, usage.output_tokens)
        log.info("llm %s: %d in / %d out tokens", model, usage.input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            raise LLMError(f"{model} declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{model} hit max_tokens={max_tokens}")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{model} returned invalid JSON") from exc
