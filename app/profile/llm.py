"""Small structured-JSON wrapper over the Anthropic Messages API, with a token ledger.

The classifier (Haiku) and evaluator (Sonnet) only need "prompt in, JSON out",
so they depend on the `JsonLLM` protocol; tests pass a fake.
"""
import json
import logging
import time
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
             effort: str, constrained: bool = True) -> dict: ...


class AnthropicJsonLLM:
    """JSON replies from the Messages API: grammar-constrained (output_config.format) by
    default, or schema-in-prompt for schemas too large to compile."""

    def __init__(self, client=None, ledger: Ledger | None = None, attempts: int = 4,
                 sleep=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(timeout=90.0, max_retries=2)
        self.client = client
        self.ledger = ledger or Ledger()
        self.attempts = attempts
        self.sleep = sleep or time.sleep

    def _stream(self, kwargs: dict):
        # Streamed, so a stalled request fails on the read timeout instead of hanging. The
        # server-side-fallback beta was dropped: it hung 10+ min on long rubric requests.
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def json(self, *, model: str, system: str, user: str, schema: dict, max_tokens: int,
             effort: str, constrained: bool = True) -> dict:
        """constrained=False skips grammar-constrained decoding (large schemas compile too
        slowly server-side): the schema goes in the prompt and the reply is parsed."""
        import anthropic
        output_config = {"effort": effort}
        if constrained:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        else:
            system = (f"{system}\n\nReply with only one JSON object (no prose, no code fence) "
                      f"that matches this JSON schema:\n{json.dumps(schema)}")
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": user}],
                      output_config=output_config)
        for attempt in range(self.attempts):
            try:
                response = self._stream(kwargs)
                break
            except anthropic.APIStatusError as exc:
                raise LLMError(f"{model} request failed with HTTP {exc.status_code}") from exc
            except anthropic.APIConnectionError as exc:
                if attempt == self.attempts - 1:
                    raise LLMError(f"{model} connection failed: {type(exc).__name__}") from exc
                log.info("llm %s: connection dropped, retry %d", model, attempt + 1)
                self.sleep(5 * 2 ** attempt)
        usage = response.usage
        self.ledger.add(model, usage.input_tokens, usage.output_tokens)
        log.info("llm %s: %d in / %d out tokens", model, usage.input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            raise LLMError(f"{model} declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{model} hit max_tokens={max_tokens}")
        text = "".join(b.text for b in response.content if b.type == "text")
        return parse_json_object(text, model)


def parse_json_object(text: str, model: str = "model") -> dict:
    """The JSON object in a reply, tolerating a code fence or stray prose around it."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"{model} returned invalid JSON")
