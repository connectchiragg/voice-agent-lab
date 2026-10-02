"""Exercise the real Groq SDK against a fake HTTP provider, independent of transport."""

import asyncio
import json
from collections.abc import Callable
from io import BytesIO
import wave

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


def speech_call(operation: str) -> str | bytes:
    if operation == "tts":
        return asyncio.run(speech.synthesize("Hello."))
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


@pytest.mark.parametrize("operation", ["stt", "tts"])
def test_missing_groq_key(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(speech.SpeechError, match="GROQ_API_KEY") as error:
        speech_call(operation)
    assert error.value.status_code == 503


@pytest.mark.parametrize("provider_status, expected", [
    (400, 422), (401, 503), (403, 503), (404, 503),
    (422, 422), (429, 429), (500, 502),
])
@pytest.mark.parametrize("operation", ["stt", "tts"])
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


@pytest.mark.parametrize("operation", ["stt", "tts"])
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
    assert asyncio.run(speech.synthesize(text)) == wav


@pytest.mark.parametrize("text", ["", " ", "x" * 201])
def test_invalid_speech_does_not_call_provider(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    def unexpected_call() -> AsyncGroq:
        pytest.fail("Invalid text must not reach Groq.")
    monkeypatch.setattr(speech, "create_client", unexpected_call)
    with pytest.raises(speech.SpeechError) as error:
        asyncio.run(speech.synthesize(text))
    assert error.value.status_code == 422


def test_speech_rejects_invalid_audio(groq_stub: Callable[[ProviderHandler], None]) -> None:
    groq_stub(lambda request: httpx.Response(200, content=b"not-a-wav"))
    with pytest.raises(speech.SpeechError) as error:
        speech_call("tts")
    assert error.value.status_code == 502


def test_speech_explains_missing_terms(groq_stub: Callable[[ProviderHandler], None]) -> None:
    groq_stub(lambda request: httpx.Response(400, json={"error": {
        "code": "model_terms_required", "message": "private provider details",
    }}))
    with pytest.raises(speech.SpeechError, match="Accept Orpheus model terms") as error:
        speech_call("tts")
    assert error.value.status_code == 503
    assert "private" not in str(error.value)
