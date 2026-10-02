"""Exercise the real Groq SDK against a fake HTTP provider, never real recordings."""

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient
from groq import AsyncGroq

from app import speech
from app.main import app

client = TestClient(app)
ProviderHandler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def groq_stub(monkeypatch: pytest.MonkeyPatch) -> Callable[[ProviderHandler], None]:
    monkeypatch.setenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo")

    def configure(handler: ProviderHandler) -> None:
        def create_client() -> AsyncGroq:
            return AsyncGroq(
                api_key="test-key", max_retries=0, timeout=30,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )

        monkeypatch.setattr(speech, "create_client", create_client)

    return configure


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
    response = client.post("/api/transcribe", content=b"fake-audio",
                           headers={"Content-Type": "audio/webm;codecs=opus"})
    assert response.status_code == 200
    assert response.json() == {"text": "Hello from the microphone."}


@pytest.mark.parametrize("body, media_type, expected", [
    (b"", "audio/webm", 400),
    (b"audio", "text/plain", 415),
    (b"audio", "", 415),
])
def test_invalid_uploads_do_not_call_provider(
    monkeypatch: pytest.MonkeyPatch, body: bytes, media_type: str, expected: int,
) -> None:
    def unexpected_call() -> AsyncGroq:
        pytest.fail("Invalid uploads must not reach Groq.")

    monkeypatch.setattr(speech, "create_client", unexpected_call)
    response = client.post("/api/transcribe", content=body,
                           headers={"Content-Type": media_type})
    assert response.status_code == expected


def test_upload_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 4)
    response = client.post("/api/transcribe", content=b"12345",
                           headers={"Content-Type": "audio/wav"})
    assert response.status_code == 413


def test_missing_groq_key_preserves_text_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    response = client.post("/api/transcribe", content=b"audio",
                           headers={"Content-Type": "audio/wav"})
    assert response.status_code == 503
    assert "GROQ_API_KEY" in response.json()["detail"]
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("provider_status, expected", [
    (400, 422), (401, 503), (403, 503), (404, 503),
    (422, 422), (429, 429), (500, 502),
])
def test_provider_errors_are_safe_and_not_retried(
    groq_stub: Callable[[ProviderHandler], None], provider_status: int, expected: int,
) -> None:
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(provider_status, json={"error": "private provider details"})

    groq_stub(provider)
    response = client.post("/api/transcribe", content=b"audio",
                           headers={"Content-Type": "audio/wav"})
    assert response.status_code == expected
    assert "private" not in response.text
    assert attempts == 1


def test_provider_timeout(groq_stub: Callable[[ProviderHandler], None]) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    groq_stub(provider)
    response = client.post("/api/transcribe", content=b"audio",
                           headers={"Content-Type": "audio/wav"})
    assert response.status_code == 504
    assert "private" not in response.text


@pytest.mark.parametrize("text", ["  ", "x" * 8001])
def test_unusable_transcripts(groq_stub: Callable[[ProviderHandler], None], text: str) -> None:
    groq_stub(lambda request: httpx.Response(200, json={"text": text}))
    response = client.post("/api/transcribe", content=b"audio",
                           headers={"Content-Type": "audio/wav"})
    assert response.status_code == 422
