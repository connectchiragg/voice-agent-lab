"""Exercise the real Groq SDK against a fake HTTP provider, never real recordings."""

from collections.abc import Callable
from io import BytesIO
import json
import wave

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


@pytest.mark.parametrize("path", ["/api/transcribe", "/api/speak"])
def test_missing_groq_key_preserves_text_app(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    response = speech_request(path)
    assert response.status_code == 503
    assert "GROQ_API_KEY" in response.json()["detail"]
    assert client.get("/").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("provider_status, expected", [
    (400, 422), (401, 503), (403, 503), (404, 503),
    (422, 422), (429, 429), (500, 502),
])
@pytest.mark.parametrize("path", ["/api/transcribe", "/api/speak"])
def test_provider_errors_are_safe_and_not_retried(
    groq_stub: Callable[[ProviderHandler], None], provider_status: int, expected: int,
    path: str,
) -> None:
    attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(provider_status, json={"error": "private provider details"})

    groq_stub(provider)
    response = speech_request(path)
    assert response.status_code == expected
    assert "private" not in response.text
    assert attempts == 1


@pytest.mark.parametrize("path", ["/api/transcribe", "/api/speak"])
def test_provider_timeout(groq_stub: Callable[[ProviderHandler], None], path: str) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    groq_stub(provider)
    response = speech_request(path)
    assert response.status_code == 504
    assert "private" not in response.text


@pytest.mark.parametrize("text", ["  ", "x" * 8001])
def test_unusable_transcripts(groq_stub: Callable[[ProviderHandler], None], text: str) -> None:
    groq_stub(lambda request: httpx.Response(200, json={"text": text}))
    response = client.post("/api/transcribe", content=b"audio",
                           headers={"Content-Type": "audio/wav"})
    assert response.status_code == 422


def speech_request(path: str) -> httpx.Response:
    if path == "/api/speak":
        return client.post(path, json={"text": "Hello."})
    return client.post(path, content=b"audio", headers={"Content-Type": "audio/wav"})


@pytest.mark.parametrize("text", ["Hello.", "x" * 200])
def test_speech_contract(groq_stub: Callable[[ProviderHandler], None], text: str) -> None:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 240)
    wav = buffer.getvalue()

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openai/v1/audio/speech"
        assert json.loads(request.read()) == {
            "model": "canopylabs/orpheus-v1-english", "voice": "troy",
            "input": text, "response_format": "wav",
        }
        return httpx.Response(200, content=wav, headers={"Content-Type": "audio/wav"})

    groq_stub(provider)
    response = client.post("/api/speak", json={"text": text})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == wav


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": " "},
                                     {"text": "x" * 201}, {"text": 123}])
def test_invalid_speech_does_not_call_provider(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, str | int],
) -> None:
    def unexpected_call() -> AsyncGroq:
        pytest.fail("Invalid text must not reach Groq.")

    monkeypatch.setattr(speech, "create_client", unexpected_call)
    assert client.post("/api/speak", json=payload).status_code == 422


def test_speech_rejects_invalid_audio(groq_stub: Callable[[ProviderHandler], None]) -> None:
    groq_stub(lambda request: httpx.Response(200, content=b"not-a-wav"))
    assert speech_request("/api/speak").status_code == 502


def test_speech_explains_missing_terms(groq_stub: Callable[[ProviderHandler], None]) -> None:
    groq_stub(lambda request: httpx.Response(400, json={"error": {
        "code": "model_terms_required", "message": "private provider details",
    }}))
    response = speech_request("/api/speak")
    assert response.status_code == 503
    assert "Accept Orpheus model terms" in response.json()["detail"]
    assert "private" not in response.text
