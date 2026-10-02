"""One complete recording -> transcript -> saved Gemini reply -> optional WAV."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Literal, NotRequired, TypedDict

from app import speech
from app.database import ChatEvent, stream_conversation

logger = logging.getLogger("uvicorn.error")


class VoiceEvent(TypedDict):
    type: Literal["transcribing", "transcript", "reply_complete", "synthesizing",
                  "audio", "notice", "error", "interrupted", "done"]
    text: NotRequired[str]
    error: NotRequired[str]
    media_type: NotRequired[str]


async def voice_turn(conversation_id: str, audio: bytes, media_type: str) -> AsyncIterator[VoiceEvent | ChatEvent | bytes]:
    yield {"type": "transcribing"}
    try:
        transcript = await speech.transcribe(audio, media_type)
    except speech.SpeechError as error:
        yield {"type": "error", "error": str(error)}
        yield {"type": "done"}
        return
    yield {"type": "transcript", "text": transcript}

    reply = ""
    try:
        async with asyncio.timeout(90), aclosing(stream_conversation(conversation_id, transcript)) as stream:
            async for event in stream:
                if event["type"] == "delta":
                    reply += event["text"]
                yield event
    except Exception as error:
        logger.warning("Gemini stream or persistence failed: %s", type(error).__name__)
        yield {"type": "interrupted", "error":
            "Reply interrupted. Any saved transcript/partial text is retained. Storage failures may leave unsaved text."}
        yield {"type": "done"}
        return
    # Generation is already completed and saved; TTS failure must not undo it.
    yield {"type": "reply_complete"}
    if not reply.strip() or len(reply) > speech.MAX_SPEECH_CHARACTERS:
        yield {"type": "notice", "text":
            "Reply saved as text, but speech requires 1–200 characters. Nothing was truncated."}
    else:
        yield {"type": "synthesizing"}
        try:
            wav = await speech.synthesize(reply)
        except speech.SpeechError as error:
            yield {"type": "error", "error": str(error)}
        else:
            yield {"type": "audio", "media_type": "audio/wav"}
            yield wav
    yield {"type": "done"}
