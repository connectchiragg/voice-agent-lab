"""Groq transcription of complete clips, separate from speech synthesis."""

import asyncio
import io
import logging
import os
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from numpy.typing import NDArray
from groq import APIError, APIStatusError, APITimeoutError, AsyncGroq, RateLimitError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger("uvicorn.error")

MAX_AUDIO_BYTES = 5 * 1024 * 1024
AUDIO_FILENAMES = {
    "audio/webm": "recording.webm",
    "audio/mp4": "recording.m4a",
    "audio/ogg": "recording.ogg",
    "audio/wav": "recording.wav",
    "audio/mpeg": "recording.mp3",
}


class SpeechError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def create_client() -> AsyncGroq:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise SpeechError(503, "Set GROQ_API_KEY in your backend .env file, then restart the app.")
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


def provider_error(error: APIError) -> SpeechError:
    # Log error categories, not provider bodies that may contain private data.
    logger.warning("Groq speech request failed: %s", type(error).__name__)
    if isinstance(error, APITimeoutError):
        return SpeechError(504, "Groq timed out. Please try again.")
    if isinstance(error, APIStatusError):
        if error.status_code == 429:
            return SpeechError(429, "Groq's rate limit was reached. Wait before trying again.")
        if error.status_code in (401, 403):
            return SpeechError(503, "Groq rejected the key or model access. Check your Groq account.")
        if error.status_code in (400, 422):
            return SpeechError(422, "Groq could not process this input. Check the model, voice and input limits.")
        if error.status_code == 404:
            return SpeechError(503, "The configured Groq model is unavailable.")
    return SpeechError(502, "Groq is unavailable. Please try again.")


def pcm_to_wav(samples: NDArray[np.float32], sample_rate: int = 16000) -> bytes:
    """Wrap float32 PCM as 16-bit mono WAV, a format Groq accepts directly."""
    pcm16 = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16.tobytes())
    return output.getvalue()


async def transcribe(audio: bytes, media_type: str) -> str:
    """Return a transcript of one complete clip; do not save the recording."""
    for attempt in (1, 2):
        try:
            async with groq_client() as client:
                result = await client.audio.transcriptions.create(
                    file=(AUDIO_FILENAMES[media_type], audio, media_type),
                    model=os.getenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo"),
                    response_format="json",
                    temperature=0,
                )
            break
        except RateLimitError as error:
            wait = rate_limit_wait_s(error)
            if attempt == 2 or wait is None:
                raise provider_error(error) from error
            logger.warning("Groq transcription rate limited; retrying in %.1f s", wait)
            await asyncio.sleep(wait)
        except APIError as error:
            raise provider_error(error) from error
    transcript = result.text.strip()
    if not transcript:
        raise SpeechError(422, "No speech was detected. Please record again.")
    if len(transcript) > 8000:
        raise SpeechError(422, "The transcript exceeds the chat limit. Record a shorter message.")
    return transcript
