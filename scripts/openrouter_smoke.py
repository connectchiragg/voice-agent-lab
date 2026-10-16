"""Smoke test and latency comparison: background Claude calls via OpenRouter (BYOK) vs direct.

    .venv-worker/bin/python scripts/openrouter_smoke.py [--runs 5] [--only openrouter]
    docker exec -i voice-research python - --runs 1 --only openrouter < scripts/openrouter_smoke.py

Runs the request shapes the research worker and profile scripts send, through
app/llm_gateway.py with fallback off (so an OpenRouter failure shows as a failure):
plain, web_search (max_uses=1, citations), Sonnet effort high with thinking,
cache_control (cache read on repeats), structured output (CrewAI's three shapes plus the
profile's server-side fallback), and is_byok on every OpenRouter response. Prints a table
with median latency, TTFT (streamed plain message) and usage. Spends a few cents (each web
search is $0.01, billed by Anthropic). Keys come from .env and are never printed.
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent if __file__ != "<stdin>" else Path.cwd()
sys.path.insert(0, str(ROOT))  # Piped into the worker container: `python - < this file` from /app.
load_dotenv(os.getenv("VALR_ENV_FILE") or ROOT / ".env")
load_dotenv(Path("~/projects/voice-agent-lab/.env").expanduser())

from app.llm_gateway import Gateway  # noqa: E402

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
LOW = {"output_config": {"effort": "low"}}
# A stable prefix of roughly 2,500 tokens (above the 5.5 models' cacheable minimum).
CACHE_SYSTEM = ("You are a careful assistant for a phone researcher. " + " ".join(
    f"Rule {n}: answer briefly, cite sources when you have them, and never invent facts about "
    f"places, prices, ratings or opening hours (reference {n})." for n in range(110)))
SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
          "required": ["city", "country"], "additionalProperties": False}


def usage_of(response):
    u = response.usage
    extra = getattr(u, "model_extra", None) or {}
    server = getattr(u, "server_tool_use", None)
    details = getattr(u, "output_tokens_details", None) or extra.get("output_tokens_details") or {}
    thinking = details.get("thinking_tokens") if isinstance(details, dict) else getattr(details, "thinking_tokens", None)
    return {"in": u.input_tokens, "out": u.output_tokens, "cache_read": u.cache_read_input_tokens or 0,
            "cache_write": u.cache_creation_input_tokens or 0,
            "searches": (getattr(server, "web_search_requests", 0) or 0) if server else 0,
            "thinking_tokens": thinking, "is_byok": getattr(u, "is_byok", extra.get("is_byok")),
            "or_cost": getattr(u, "cost", extra.get("cost"))}


def text_of(response):
    return "".join(b.text for b in response.content if b.type == "text")


def plain(c):
    r = c.messages.create(model=HAIKU, max_tokens=60, messages=[{"role": "user", "content": "Say hello in five words."}],
                          extra_body=LOW)
    return r, bool(text_of(r).strip())


def web_search(c):
    r = c.messages.create(
        model=HAIKU, max_tokens=1200, system="Run exactly one web search, then state two facts, each grounded in a result.",
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        messages=[{"role": "user", "content": "Search: Coorg India best time to visit"}], extra_body=LOW)
    results = [b for b in r.content if b.type == "web_search_tool_result" and isinstance(b.content, list)]
    cited = [b for b in r.content if b.type == "text" and getattr(b, "citations", None)]
    ok = bool(results and results[0].content and cited and cited[0].citations[0].url)
    return r, ok


def sonnet_thinking(c):
    r = c.messages.create(
        model=SONNET, max_tokens=3000, thinking={"type": "adaptive", "display": "summarized"},
        messages=[{"role": "user", "content": "Find all integer solutions (x, y) with 0 < x < y < 60 to "
                                              "x^2 + y^2 = 2525. List them."}],
        extra_body={"output_config": {"effort": "high"}})
    thought = [b for b in r.content if b.type == "thinking" and getattr(b, "thinking", "")]
    return r, bool(thought) and all(n in text_of(r) for n in ("26", "43", "34", "37"))


def cached(c):
    r = c.messages.create(model=HAIKU, max_tokens=20,
                          system=[{"type": "text", "text": CACHE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                          messages=[{"role": "user", "content": "Reply OK."}], extra_body=LOW)
    return r, (r.usage.cache_read_input_tokens or 0) > 0 or (r.usage.cache_creation_input_tokens or 0) > 0


def structured_beta(c):
    """CrewAI on Sonnet 5.5: beta structured-outputs header + output_format."""
    r = c.beta.messages.create(model=SONNET, max_tokens=400, betas=["structured-outputs-2025-11-13"],
                               messages=[{"role": "user", "content": "Where is the Eiffel Tower?"}],
                               extra_body={"output_format": {"type": "json_schema", "schema": SCHEMA},
                                           "output_config": {"effort": "medium"}})
    return r, set(json.loads(text_of(r))) == {"city", "country"}


def structured_tool(c):
    """CrewAI on Haiku 5.5: a structured_output tool, forced."""
    r = c.messages.create(model=HAIKU, max_tokens=400,
                          tools=[{"name": "structured_output", "description": "Output the structured response",
                                  "input_schema": SCHEMA}],
                          tool_choice={"type": "tool", "name": "structured_output"},
                          messages=[{"role": "user", "content": "Where is the Eiffel Tower?"}], extra_body=LOW)
    calls = [b for b in r.content if b.type == "tool_use"]
    return r, bool(calls) and set(calls[0].input) == {"city", "country"}


def structured_profile(c):
    """app/profile/llm.py on Sonnet: output_config.format plus the server-side refusal fallback."""
    r = c.beta.messages.create(model=SONNET, max_tokens=400, betas=["server-side-fallback-2026-07-01"],
                               messages=[{"role": "user", "content": "Where is the Eiffel Tower?"}],
                               extra_body={"fallbacks": "default", "output_config": {
                                   "effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}})
    return r, set(json.loads(text_of(r))) == {"city", "country"}


def ttft(c):
    t0 = time.perf_counter()
    first = None
    with c.messages.stream(model=HAIKU, max_tokens=60, messages=[{"role": "user", "content": "Say hello in five words."}],
                           extra_body=LOW) as stream:
        for event in stream:
            if event.type == "content_block_delta" and first is None:
                first = time.perf_counter() - t0
        r = stream.get_final_message()
    return r, first is not None, first


TESTS = [("plain", plain), ("web_search", web_search), ("sonnet_thinking", sonnet_thinking),
         ("cache_control", cached), ("structured_crewai_sonnet", structured_beta),
         ("structured_crewai_haiku_tool", structured_tool), ("structured_profile_fallbacks", structured_profile)]


def run(mode, runs):
    gw = Gateway(mode=mode, fallback=False, guard="warn")
    c = gw.client()
    out = {}
    for name, fn in TESTS + [("ttft_plain_stream", ttft)]:
        rows = []
        for _ in range(runs):
            t0 = time.perf_counter()
            try:
                result = fn(c)
                elapsed = time.perf_counter() - t0
                r, ok = result[0], result[1]
                row = {"ok": ok, "s": elapsed, **usage_of(r)}
                if len(result) == 3:
                    row["ttft"] = result[2]
            except Exception as error:  # noqa: BLE001
                row = {"ok": False, "s": time.perf_counter() - t0, "error": f"{type(error).__name__}: {str(error)[:300]}"}
            rows.append(row)
        out[name] = rows
        oks = sum(r["ok"] for r in rows)
        med = statistics.median(r["s"] for r in rows)
        extra = ""
        if name == "ttft_plain_stream":
            t = [r["ttft"] for r in rows if r.get("ttft")]
            extra = f" ttft_med={statistics.median(t):.2f}s" if t else ""
        last = rows[-1]
        print(f"{mode:10} {name:30} ok {oks}/{runs} med {med:.2f}s{extra} last={ {k: v for k, v in last.items() if k not in ('s', 'ok')} }",
              flush=True)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--only", choices=["openrouter", "anthropic"])
    parser.add_argument("--json", help="write raw rows here")
    args = parser.parse_args()
    results = {}
    for mode in ([args.only] if args.only else ["openrouter", "anthropic"]):
        results[mode] = run(mode, args.runs)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    main()
