"""The real Groq SDK against a fake HTTP provider: safe, non-leaking errors for the LLM path."""
import asyncio

import httpx
import pytest
from groq import AsyncGroq

from app import groq_api, llm

MESSAGES = [{"role": "user", "content": "hi"}]


def fake_client(handler) -> AsyncGroq:
    return AsyncGroq(api_key="test-key", max_retries=0, timeout=30,
                     http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def complete(client: AsyncGroq) -> list[object]:
    async def run():
        return [event async for event in llm.stream_model(MESSAGES, client=client)]
    return asyncio.run(run())


def test_missing_groq_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(groq_api.ProviderError, match="GROQ_API_KEY") as error:
        groq_api.create_client()
    assert error.value.status_code == 503


@pytest.mark.parametrize("provider_status, expected", [
    (400, 422), (401, 503), (403, 503), (404, 503), (422, 422), (429, 429), (500, 502),
])
def test_provider_errors_are_safe_and_not_retried(provider_status: int, expected: int) -> None:
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path == "/openai/v1/chat/completions"
        return httpx.Response(provider_status, json={"error": "private provider details"})

    with pytest.raises(groq_api.ProviderError) as error:
        complete(fake_client(provider))
    assert error.value.status_code == expected
    assert "private" not in str(error.value)
    assert attempts == 1  # A 429 without a short retry-after is not waited out.


def test_provider_timeout_is_retried_once_then_safe() -> None:
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("private timeout detail", request=request)

    with pytest.raises(groq_api.ProviderError) as error:
        complete(fake_client(provider))
    assert error.value.status_code == 504
    assert "private" not in str(error.value)
    assert attempts == 2
