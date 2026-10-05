"""Exercise the real Groq SDK against a fake HTTP provider, independent of transport."""

import asyncio
from collections.abc import Callable

import httpx
import pytest
from groq import AsyncGroq

from app import speech

ProviderHandler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def groq_stub(monkeypatch: pytest.MonkeyPatch) -> Callable[[ProviderHandler], None]:
    monkeypatch.setenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo")
    monkeypatch.setenv("GROQ_SPEECH_MODEL", "canopylabs/orpheus-v1-english")
    monkeypatch.setenv("GROQ_SPEECH_VOICE", "troy")

    def configure(handler: ProviderHandler) -> None:
        def create_client() -> AsyncGroq:
            return AsyncGroq(
                api_key="test-key", max_retries=0, timeout=30,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
        monkeypatch.setattr(speech, "create_client", create_client)

    return configure


def speech_call(operation: str) -> str:
    return asyncio.run(speech.transcribe(b"audio", "audio/wav"))


def test_transcription_contract(groq_stub: Callable[[ProviderHandler], None]) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/audio/transcriptions"
        assert request.headers["content-type"].startswith("multipart/form-data")
        body = request.read()
        assert b"whisper-large-v3-turbo" in body
        assert b'filename="recording.webm"' in body
        assert b"fake-audio" in body
        return httpx.Response(200, json={"text": "  Hello from the microphone.  "})

    groq_stub(provider)
    assert asyncio.run(speech.transcribe(b"fake-audio", "audio/webm")) == "Hello from the microphone."


@pytest.mark.parametrize("operation", ["stt"])
def test_missing_groq_key(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(speech.SpeechError, match="GROQ_API_KEY") as error:
        speech_call(operation)
    assert error.value.status_code == 503


@pytest.mark.parametrize("provider_status, expected", [
    (400, 422), (401, 503), (403, 503), (404, 503),
    (422, 422), (429, 429), (500, 502),
])
@pytest.mark.parametrize("operation", ["stt"])
def test_provider_errors_are_safe_and_not_retried(
    groq_stub: Callable[[ProviderHandler], None], provider_status: int, expected: int, operation: str,
) -> None:
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(provider_status, json={"error": "private provider details"})

    groq_stub(provider)
    with pytest.raises(speech.SpeechError) as error:
        speech_call(operation)
    assert error.value.status_code == expected
    assert "private" not in str(error.value)
    assert attempts == 1


@pytest.mark.parametrize("operation", ["stt"])
def test_provider_timeout(groq_stub: Callable[[ProviderHandler], None], operation: str) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    groq_stub(provider)
    with pytest.raises(speech.SpeechError) as error:
        speech_call(operation)
    assert error.value.status_code == 504
    assert "private" not in str(error.value)


@pytest.mark.parametrize("text", ["  ", "x" * 8001])
def test_unusable_transcripts(groq_stub: Callable[[ProviderHandler], None], text: str) -> None:
    groq_stub(lambda request: httpx.Response(200, json={"text": text}))
    with pytest.raises(speech.SpeechError) as error:
        speech_call("stt")
    assert error.value.status_code == 422
