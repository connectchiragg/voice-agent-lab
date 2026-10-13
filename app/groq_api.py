"""Shared Groq client, short rate-limit waits, and safe provider errors (used by the LLM)."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from groq import APIError, APIStatusError, APITimeoutError, AsyncGroq, RateLimitError

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
MAX_RATE_LIMIT_WAIT_S = 3.0


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


def rate_limit_wait_s(error: RateLimitError) -> float | None:
    """Seconds Groq asks us to wait, if short enough to wait out on a live call."""
    try:
        wait = float(error.response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return wait if 0 <= wait <= MAX_RATE_LIMIT_WAIT_S else None


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
