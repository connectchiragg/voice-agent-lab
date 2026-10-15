"""Shared Groq client, local rate-limit tracking, and safe provider errors (used by the LLM)."""

import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from groq import APIError, APIStatusError, APITimeoutError, AsyncGroq

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger("uvicorn.error")


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def create_client() -> AsyncGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ProviderError(503, "Set GROQ_API_KEY in your backend .env file, then restart the app.")
    # No invisible retries: let the user decide whether to try again.
    return AsyncGroq(api_key=api_key, timeout=30, max_retries=0)


shared_client: AsyncGroq | None = None


@asynccontextmanager
async def shared_groq_client() -> AsyncIterator[None]:
    """App lifespan: one client and connection pool for every Groq request.

    Reusing connections skips a TCP and TLS handshake per request.
    """
    global shared_client
    async with create_client() as client:
        shared_client = client
        try:
            yield
        finally:
            shared_client = None


@asynccontextmanager
async def groq_client() -> AsyncIterator[AsyncGroq]:
    """The shared client while the app runs; a short-lived one otherwise (scripts, tests)."""
    if shared_client is not None:
        yield shared_client
    else:
        async with create_client() as client:
            yield client


# Free-tier limits for the LLM, tracked locally so optional extra calls (speculative
# replies) can back off before Groq refuses a call the user is waiting for. Other
# processes sharing the key are not counted.
REQUESTS_PER_MINUTE = 30
INPUT_TOKENS_PER_MINUTE = 7000
NEAR_LIMIT = 0.7  # Above this share of either limit, skip optional calls.
recent_requests: deque[tuple[float, int]] = deque()  # (monotonic time, estimated input tokens)
input_tokens_estimate = 500  # Updated from reported usage; cancelled streams never report it.
last_rate_limited: float | None = None


def note_request() -> int:
    """Count one LLM request; returns requests in the last minute."""
    now = time.monotonic()
    recent_requests.append((now, input_tokens_estimate))
    while recent_requests and recent_requests[0][0] < now - 60:
        recent_requests.popleft()
    return len(recent_requests)


def note_usage(input_tokens: int) -> None:
    global input_tokens_estimate
    input_tokens_estimate = input_tokens


def note_rate_limited() -> None:
    global last_rate_limited
    last_rate_limited = time.monotonic()


def near_rate_limit() -> bool:
    """True after a 429 in the last minute, or near either per-minute limit."""
    now = time.monotonic()
    if last_rate_limited is not None and now - last_rate_limited < 60:
        return True
    minute = [tokens for at, tokens in recent_requests if at >= now - 60]
    # Count the optional call itself: it would spend a request and its input tokens.
    return (len(minute) + 1 > NEAR_LIMIT * REQUESTS_PER_MINUTE
            or sum(minute) + input_tokens_estimate > NEAR_LIMIT * INPUT_TOKENS_PER_MINUTE)


def provider_error(error: APIError) -> ProviderError:
    # Log error categories, not provider bodies that may contain private data.
    logger.warning("Groq request failed: %s", type(error).__name__)
    if isinstance(error, APITimeoutError):
        return ProviderError(504, "Groq timed out. Please try again.")
    if isinstance(error, APIStatusError):
        if error.status_code == 429:
            return ProviderError(429, "Groq's rate limit was reached. Wait before trying again.")
        if error.status_code in (401, 403):
            return ProviderError(503, "Groq rejected the key or model access. Check your Groq account.")
        if error.status_code in (400, 422):
            return ProviderError(422, "Groq could not process this request.")
        if error.status_code == 404:
            return ProviderError(503, "The configured Groq model is unavailable.")
    return ProviderError(502, "Groq is unavailable. Please try again.")
