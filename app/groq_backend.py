"""Groq's free tier for VALR's background LLM work (research worker only; see app/llm_gateway.py).

The live voice agent calls Groq directly (app/llm.py: openai/gpt-oss-120b, fallback
openai/gpt-oss-20b). Groq's limits are per model per organisation, so background work must
never use those models: `groq_model()` refuses them.

- Models: Groq's free tier on 2026-10-28 (GET /openai/v1/models on the VM) offers, besides the
  voice models, only `qwen/qwen3.8-27b` (131k context, tools, JSON mode and strict JSON
  schema) as a general chat model. `openai/gpt-oss-safeguard-20b` is a policy classifier with
  3 requests and 2,000 tokens a minute, and `meta-models/muse-spark-open` needs the org admin
  to accept its terms. So every canonical Claude name maps to qwen3.8-27b: Haiku-class tasks
  with reasoning off, Sonnet-class with reasoning on (hidden). VALR_GROQ_FAST_MODEL and
  VALR_GROQ_STRONG_MODEL override the table.
- Limits (free tier, from the docs and the x-ratelimit-* headers): qwen3.8-27b allows 30
  requests and 8,000 tokens a minute, 1,000 requests and 200,000 tokens a day, and at most
  7,000 input tokens in one request (a larger one is answered 413). `GroqTransport` enforces
  them on the client: a per-model sliding window plus day counters, the server's own
  remaining/reset headers, a reserve that background work cannot use (so the Gmail
  classifier still gets through while research runs), waiting with backoff on 429, and
  shrinking the longest message of a request that would not fit (413).
- Web search: Groq Compound was retired on 2026-09-21 and browser_search exists only on the
  gpt-oss models (the voice agent's). `web_search()` reads DuckDuckGo's keyless HTML results
  page (Wikipedia search when that fails) and the model writes the cited answer.
"""
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger("valr")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# The live voice agent's models (app/llm.py defaults); GROQ_LLM_MODEL / LLM_FALLBACK_MODEL add theirs.
VOICE_MODELS = frozenset({"openai/gpt-oss-120b", "openai/gpt-oss-20b"})
DEFAULT_MODEL = "qwen/qwen3.8-27b"
CHARS_PER_TOKEN = 3.5  # Deliberately low (English is about 4): estimates err on the large side.


@dataclass(frozen=True)
class Limits:
    rpm: int
    rpd: int
    tpm: int   # Input plus output tokens a minute (x-ratelimit-limit-tokens).
    tpd: int
    itpm: int  # Input tokens a minute; also the most one request may carry (larger -> 413).
    otpm: int  # Output tokens a minute; also the most one request may ask for (max_tokens).
    context: int

    @property
    def max_input(self) -> int:
        return self.itpm

    @property
    def max_output(self) -> int:
        return self.otpm


# Free tier, measured 2026-10-28 on the VM (docs, /models, response headers and 413/429 bodies):
# Groq books a request's max_completion_tokens against the output budget while it runs, so a
# request asking for more than 1,000 output tokens is refused outright ("OTPM: Limit 1000").
FREE_LIMITS = {
    "qwen/qwen3.8-27b": Limits(30, 1000, 8000, 200_000, 7000, 1000, 131_072),
    # Not measured beyond the docs (3 RPM, 2K TPM); the input/output split is a guess.
    "openai/gpt-oss-safeguard-20b": Limits(3, 1000, 2000, 200_000, 1500, 500, 131_072),
}
UNKNOWN_LIMITS = Limits(30, 1000, 6000, 100_000, 5000, 1000, 32_768)


def limits_for(model: str) -> Limits:
    return FREE_LIMITS.get(model, UNKNOWN_LIMITS)


def billed_model(response: Any, requested: str) -> str:
    """The model to price a response at: the Groq model that served it (free), else the
    caller's canonical name (an Anthropic response's own `model` may carry a date suffix)."""
    meta = getattr(response, "valr", None)
    if isinstance(meta, dict) and meta.get("backend") == "groq" and meta.get("model"):
        return str(meta["model"])
    return requested


def voice_models() -> set[str]:
    extra = {os.getenv("GROQ_LLM_MODEL", "").strip(), os.getenv("LLM_FALLBACK_MODEL", "").strip()}
    return set(VOICE_MODELS) | {m for m in extra if m}


def groq_model(model: str) -> str:
    """Canonical name -> the Groq model. Haiku-class -> VALR_GROQ_FAST_MODEL, the rest ->
    VALR_GROQ_STRONG_MODEL (both default to qwen/qwen3.8-27b). A Groq id ("org/model") is used
    as given. Never one of the voice agent's models."""
    if "/" in model and not model.startswith("anthropic/"):
        chosen = model
    elif "haiku" in model:
        chosen = os.getenv("VALR_GROQ_FAST_MODEL", DEFAULT_MODEL)
    else:
        chosen = os.getenv("VALR_GROQ_STRONG_MODEL", DEFAULT_MODEL)
    if chosen in voice_models():
        raise ValueError(f"{chosen} serves live calls; background work must use another Groq model")
    return chosen


def reasoning_for(model: str, thinking: Any = None, effort: str | None = None) -> str:
    """'none' (answer directly) or 'default' (think, hidden). Off unless VALR_GROQ_REASONING=1:
    on the free tier hidden reasoning spends the same 1,000 output tokens a minute as the answer.
    With it on: off when the caller turned thinking off or asked for low effort, and for
    Haiku-class names unless they asked."""
    if os.getenv("VALR_GROQ_REASONING", "0") != "1":
        return "none"
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return "none"
    if effort:
        return "none" if effort == "low" else "default"
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
        return "default"
    return "none" if "haiku" in model else "default"


# Token estimates and fitting a request into the per-request input limit.

def estimate_tokens(body: dict[str, Any]) -> int:
    chars = 0
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(str(part.get("text", ""))) for part in content if isinstance(part, dict))
        chars += len(json.dumps(message.get("tool_calls"))) if message.get("tool_calls") else 0
        chars += 16
    if body.get("tools"):
        chars += len(json.dumps(body["tools"]))
    if isinstance(body.get("response_format"), dict):
        chars += len(json.dumps(body["response_format"]))
    return math.ceil(chars / CHARS_PER_TOKEN) + 8


CUT = "\n...[shortened to fit the model's rate limit]...\n"


def fit(body: dict[str, Any], max_tokens: int) -> int:
    """Shrink the longest text messages (cutting their middle, so the start and the
    instructions at the end survive) until the estimate fits. Returns the characters cut."""
    cut_total = 0
    for _ in range(20):
        over = estimate_tokens(body) - max_tokens
        if over <= 0:
            break
        texts = [m for m in body.get("messages") or [] if isinstance(m.get("content"), str)]
        if not texts:
            break
        longest = max(texts, key=lambda m: len(m["content"]))
        text = longest["content"]
        remove = int(over * CHARS_PER_TOKEN) + len(CUT) + 64
        if len(text) - remove < 400:
            remove = max(len(text) - 400, 0)
            if remove == 0:
                break
        keep = len(text) - remove
        head = keep * 2 // 3
        longest["content"] = text[:head] + CUT + text[len(text) - (keep - head):]
        cut_total += remove
    return cut_total


# The rate limiter.

def parse_duration(text: str | None) -> float:
    """Groq's reset values: '1m26.4s', '232ms', '9.78s', '2h3m'. 0 when unreadable."""
    if not text:
        return 0.0
    total, found = 0.0, False
    for value, unit in re.findall(r"([\d.]+)(ms|h|m|s)", text):
        found = True
        total += float(value) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    if not found:
        try:
            return float(text)
        except ValueError:
            return 0.0
    return total


class RateLimited(Exception):
    def __init__(self, model: str, wait_s: float, why: str) -> None:
        super().__init__(f"{model}: {why} (about {wait_s:.0f} s until there is room)")
        self.model, self.wait_s, self.why = model, wait_s, why


@dataclass
class ModelLimiter:
    """Client-side limits for one model (thread-safe): requests, input, output and total tokens
    over a sliding minute, and leaky-bucket day counters. A request books its input estimate
    and its max output (as Groq does while it runs) and is settled to the usage it reports.
    Interactive work (the Gmail classifier, notification judge, company resolver) may use the
    whole budget; background work (research, coach, profile) waits while the minute's use is
    above 1 - `reserve` of a budget, and its max output is capped at that share."""
    model: str
    limits: Limits
    reserve: float = 0.25
    clock: Callable[[], float] = time.monotonic
    minute: deque = field(default_factory=deque)  # [t, input, output, ticket]
    requests: deque = field(default_factory=deque)
    # Groq's daily limits refill continuously (1,000 requests a day: one every 86.4 s), so the
    # day counters are leaky buckets that drain at limit/86,400 per second.
    day_requests: float = 0.0
    day_tokens: float = 0.0
    drained_at: float = -1.0
    blocked_until: float = 0.0
    server_requests: tuple[int, float] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    next_ticket: int = 0
    interactive_waiting: int = 0  # Background work does not start while this is above 0.

    def _roll(self, now: float) -> None:
        while self.minute and self.minute[0][0] <= now - 60:
            self.minute.popleft()
        while self.requests and self.requests[0] <= now - 60:
            self.requests.popleft()
        if self.drained_at < 0:
            self.drained_at = now
        elapsed, self.drained_at = now - self.drained_at, now
        self.day_requests = max(self.day_requests - elapsed * self.limits.rpd / 86400, 0.0)
        self.day_tokens = max(self.day_tokens - elapsed * self.limits.tpd / 86400, 0.0)

    def _share(self, priority: str) -> float:
        return 1.0 if priority == "interactive" else 1.0 - self.reserve

    def _window_wait(self, now: float, need: int, budget: float, index: int | None) -> float:
        """Seconds until the minute has room for `need` (index: 1 input, 2 output, None both)."""
        def size(entry: list) -> int:
            return entry[1] + entry[2] if index is None else entry[index]
        used = sum(size(e) for e in self.minute)
        if used + need <= budget or not self.minute:
            return 0.0
        freed = 0
        for entry in self.minute:
            freed += size(entry)
            if used - freed + need <= budget:
                return entry[0] + 60 - now
        return self.minute[-1][0] + 60 - now

    def wait_for(self, tokens: int, priority: str, output: int = 0) -> tuple[float, str]:
        """(seconds to wait, why); (0, "") when the request may go now. Caller holds the lock."""
        now = self.clock()
        self._roll(now)
        share, lim = self._share(priority), self.limits
        waits: list[tuple[float, str]] = []
        if self.blocked_until > now:
            waits.append((self.blocked_until - now, "429 backoff"))
        if self.day_requests + 1 > lim.rpd * share:
            waits.append(((self.day_requests + 1 - lim.rpd * share) * 86400 / lim.rpd, "requests per day"))
        if self.day_tokens + tokens + output > lim.tpd * share:
            waits.append(((self.day_tokens + tokens + output - lim.tpd * share) * 86400 / lim.tpd, "tokens per day"))
        if len(self.requests) >= max(int(lim.rpm * share), 1):
            waits.append((self.requests[0] + 60 - now, "requests per minute"))
        for need, limit, index, why in ((tokens + output, lim.tpm, None, "tokens per minute"),
                                        (tokens, lim.itpm, 1, "input tokens per minute"),
                                        (output, lim.otpm, 2, "output tokens per minute")):
            # The request must fit the minute; background work also waits while the minute's
            # use is above its share, so the classifier waits at most for requests in flight.
            wait = max(self._window_wait(now, need, limit, index),
                       self._window_wait(now, 0, limit * share, index) if share < 1 else 0.0)
            if wait > 0:
                waits.append((wait, why))
        if priority != "interactive" and self.interactive_waiting:
            waits.append((1.0, "interactive work first"))
        if self.server_requests and self.server_requests[1] > now and \
                self.server_requests[0] <= lim.rpd * (1 - share):
            waits.append((self.server_requests[1] - now, "server daily requests"))
        if not waits:
            return 0.0, ""
        return max(waits)

    def acquire(self, tokens: int, priority: str, max_wait: float,
                sleep: Callable[[float], None] = time.sleep, output: int = 0) -> tuple[int, float]:
        """Wait for room, then book `tokens` input and `output` output tokens. -> (ticket,
        seconds waited). Raises RateLimited when the wait would pass `max_wait`. While an
        interactive request waits, background requests do not start."""
        started, waiting = self.clock(), False
        try:
            while True:
                with self.lock:
                    wait, why = self.wait_for(tokens, priority, output)
                    if wait <= 0:
                        now = self.clock()
                        self.next_ticket += 1
                        self.minute.append([now, tokens, output, self.next_ticket])
                        self.requests.append(now)
                        self.day_requests += 1
                        self.day_tokens += tokens + output
                        return self.next_ticket, now - started
                    waited = self.clock() - started
                    if waited + wait > max_wait:
                        raise RateLimited(self.model, wait, why)
                    if priority == "interactive" and not waiting:
                        waiting = True
                        self.interactive_waiting += 1
                logger.info("VALR groq queue: %s waits %.1f s (%s, %s)", self.model, wait, why, priority)
                sleep(min(max(wait, 0.05), 5.0 if priority != "interactive" else 1.0))
        finally:
            if waiting:
                with self.lock:
                    self.interactive_waiting -= 1

    def settle(self, ticket: int, tokens: int, output: int = 0) -> None:
        """Replace a booking by the input and output tokens the response reports."""
        with self.lock:
            for entry in self.minute:
                if entry[3] == ticket:
                    self.day_tokens += tokens + output - entry[1] - entry[2]
                    entry[1], entry[2] = tokens, output
                    return

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + max(seconds, 1.0))

    def update(self, headers: Any) -> None:
        """Learn from x-ratelimit-* (tokens: per minute; requests: per day on Groq)."""
        now = self.clock()
        try:
            # Not x-ratelimit-remaining-tokens: it counts the max_tokens of requests in flight,
            # and its reset is the time to a full bucket, so waiting on it starves everyone.
            requests = headers.get("x-ratelimit-remaining-requests")
            if requests is not None:
                self.server_requests = (int(float(requests)),
                                        now + parse_duration(headers.get("x-ratelimit-reset-requests")))
        except (TypeError, ValueError):
            pass

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._roll(self.clock())
            return {"model": self.model, "minute_input": sum(e[1] for e in self.minute),
                    "minute_output": sum(e[2] for e in self.minute),
                    "minute_requests": len(self.requests), "day_requests": round(self.day_requests, 1),
                    "day_tokens": round(self.day_tokens)}


class Limiters:
    def __init__(self, clock: Callable[[], float] = time.monotonic, reserve: float | None = None) -> None:
        self.clock = clock
        self.reserve = reserve if reserve is not None else float(os.getenv("VALR_GROQ_RESERVE", "0.25"))
        self.by_model: dict[str, ModelLimiter] = {}
        self.lock = threading.Lock()

    def get(self, model: str) -> ModelLimiter:
        with self.lock:
            if model not in self.by_model:
                self.by_model[model] = ModelLimiter(model, limits_for(model), self.reserve, self.clock)
            return self.by_model[model]


# The HTTP transport: every Groq request (the gateway's and CrewAI's) passes through it.

PRIORITY_HEADER = "x-valr-priority"


class GroqTransport(httpx.BaseTransport):
    """Rate limits, 429 waits and 413 shrinking around a real transport. Adds response
    headers x-valr-queued-s, x-valr-retries and x-valr-shrunk-chars for callers that want to
    say they were slowed or shortened."""

    def __init__(self, limiters: Limiters, inner: httpx.BaseTransport | None = None,
                 max_wait: float | None = None, sleep: Callable[[float], None] = time.sleep,
                 attempts: int = 5) -> None:
        self.limiters, self.inner = limiters, inner or httpx.HTTPTransport()
        self.max_wait = max_wait if max_wait is not None else float(os.getenv("VALR_GROQ_MAX_WAIT_S", "150"))
        self.sleep, self.attempts = sleep, attempts
        self.stats: dict[str, int] = {"requests": 0, "queued": 0, "retries_429": 0, "shrunk": 0}
        self.lock = threading.Lock()

    def _count(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.stats[name] = self.stats.get(name, 0) + n

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        priority = request.headers.get(PRIORITY_HEADER, "background")
        if PRIORITY_HEADER in request.headers:
            del request.headers[PRIORITY_HEADER]
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return self.inner.handle_request(request)
        request.read()
        body = json.loads(request.content or b"{}")
        model = str(body.get("model", ""))
        if model in voice_models():
            raise ValueError(f"{model} serves live calls; background work must use another Groq model")
        limiter = self.limiters.get(model)
        target = int(limiter.limits.max_input * 0.9)
        shrunk = fit(body, target)
        # Groq books max_completion_tokens against the output budget: never ask for more than
        # it allows (more is refused outright), nor, for background work, more than its share.
        cap = limiter.limits.max_output if priority == "interactive" else \
            max(int(limiter.limits.max_output * (1 - limiter.reserve)), 1)
        asked = int(body.get("max_completion_tokens") or body.get("max_tokens") or cap)
        output = min(asked, cap)
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = output
        capped = asked if asked > output else 0
        queued, retries = 0.0, 0
        response: httpx.Response | None = None
        for _ in range(self.attempts):
            estimate = estimate_tokens(body)
            try:
                ticket, waited = limiter.acquire(estimate, priority, self.max_wait - queued, self.sleep, output)
            except RateLimited as error:
                logger.warning("VALR groq rate limit: %s", error)
                return self._synthetic_429(request, error, queued, retries, shrunk)
            queued += waited
            self._count("requests")
            outgoing = httpx.Request(request.method, request.url, headers=self._headers(request, body),
                                     content=json.dumps(body).encode(), extensions=request.extensions)
            response = self.inner.handle_request(outgoing)
            limiter.update(response.headers)
            if response.status_code == 429 and b"Request too large" in response.read():
                limiter.settle(ticket, 0, 0)  # Can never pass as asked: give back the error.
                break
            if response.status_code == 429:
                retries += 1
                self._count("retries_429")
                after = parse_duration(response.headers.get("retry-after")) or min(2 ** retries, 30)
                limiter.settle(ticket, 0, 0)
                limiter.penalize(after)
                logger.info("VALR groq 429 on %s (%s): waiting %.1f s", model, priority, after)
                continue
            if response.status_code == 413 and target > 1000:
                response.read()
                limiter.settle(ticket, 0, 0)
                target = int(target * 0.7)
                shrunk += fit(body, target)
                self._count("shrunk")
                continue
            if response.status_code == 200:
                content = response.read()
                try:
                    usage = json.loads(content).get("usage") or {}
                    limiter.settle(ticket, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
                except (ValueError, AttributeError):
                    pass
            break
        assert response is not None
        if queued > 0.5:
            self._count("queued")
        if shrunk:
            self._count("shrunk")
            logger.info("VALR groq shrank a %s request by %d chars to fit %d input tokens", model, shrunk, target)
        response.headers["x-valr-queued-s"] = f"{queued:.2f}"
        response.headers["x-valr-retries"] = str(retries)
        response.headers["x-valr-shrunk-chars"] = str(shrunk)
        response.headers["x-valr-output-capped"] = str(capped)
        if capped:
            self._count("output_capped")
            logger.info("VALR groq capped a %s request's max output from %d to %d tokens", model, capped, output)
        return response

    @staticmethod
    def _headers(request: httpx.Request, body: dict[str, Any]) -> httpx.Headers:
        headers = httpx.Headers(request.headers)
        headers.pop("content-length", None)
        return headers

    @staticmethod
    def _synthetic_429(request: httpx.Request, error: RateLimited, queued: float, retries: int,
                       shrunk: int) -> httpx.Response:
        body = {"error": {"message": f"VALR client-side limit: {error}", "type": "rate_limit_exceeded",
                          "code": "valr_queue_limit"}}
        return httpx.Response(429, json=body, request=request, headers={
            "retry-after": str(int(error.wait_s) + 1), "x-valr-queued-s": f"{queued:.2f}",
            "x-valr-retries": str(retries), "x-valr-shrunk-chars": str(shrunk)})

    def close(self) -> None:
        self.inner.close()


# Free web search.

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) voice-agent-lab VALR research (lab project)"
_RESULT = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def _target(href: str) -> str:
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        return unquote(parse_qs(parsed.query).get("uddg", [""])[0])
    return href


def parse_ddg(html: str, limit: int) -> list[dict[str, str]]:
    snippets = [_clean(s) for s in _SNIPPET.findall(html)]
    results = []
    for index, (href, title) in enumerate(_RESULT.findall(html)):
        url = _target(href)
        if not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue
        results.append({"title": _clean(title), "url": url,
                        "snippet": snippets[index] if index < len(snippets) else ""})
        if len(results) >= limit:
            break
    return results


def wikipedia_search(query: str, limit: int, http: httpx.Client) -> list[dict[str, str]]:
    data = http.get("https://en.wikipedia.org/w/api.php", params=dict(
        action="query", list="search", srsearch=query, srlimit=limit, format="json")).json()
    return [{"title": hit["title"], "url": "https://en.wikipedia.org/wiki/" + hit["title"].replace(" ", "_"),
             "snippet": _clean(hit.get("snippet", ""))} for hit in data.get("query", {}).get("search", [])]


def web_search(query: str, limit: int = 5, http: httpx.Client | None = None) -> tuple[list[dict[str, str]], str]:
    """Keyless web results: DuckDuckGo's HTML page, else Wikipedia search. -> (results, engine)."""
    own = http is None
    http = http or httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        try:
            response = http.post("https://html.duckduckgo.com/html/", data={"q": query})
            response.raise_for_status()
            results = parse_ddg(response.text, limit)
            if results:
                return results, "duckduckgo"
            logger.warning("VALR web search: DuckDuckGo returned no results (blocked?); using Wikipedia")
        except httpx.HTTPError as error:
            logger.warning("VALR web search: DuckDuckGo failed (%s); using Wikipedia", type(error).__name__)
        try:
            return wikipedia_search(query, limit, http), "wikipedia"
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("VALR web search: Wikipedia failed too (%s)", type(error).__name__)
            return [], "none"
    finally:
        if own:
            http.close()
