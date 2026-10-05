"""Groq transcription of complete clips, separate from speech synthesis."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import APIError, APIStatusError, APITimeoutError, AsyncGroq

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


async def transcribe(audio: bytes, media_type: str) -> str:
    """Return a transcript of one complete clip; do not save the recording."""
    try:
        async with create_client() as client:
            result = await client.audio.transcriptions.create(
                file=(AUDIO_FILENAMES[media_type], audio, media_type),
                model=os.getenv("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo"),
                response_format="json",
                temperature=0,
            )
    except APIError as error:
        raise provider_error(error) from error
    transcript = result.text.strip()
    if not transcript:
        raise SpeechError(422, "No speech was detected. Please record again.")
    if len(transcript) > 8000:
        raise SpeechError(422, "The transcript exceeds the chat limit. Record a shorter message.")
    return transcript
