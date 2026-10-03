"""One recorded input, a completed text reply, then progressively delivered speech."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing

from app import speech
from app.database import stream_conversation
from app.events import AudioChunk, AudioFormat, EventType, VoiceEvent

logger = logging.getLogger("uvicorn.error")


async def voice_turn(conversation_id: str, audio: bytes, media_type: str) -> AsyncIterator[VoiceEvent | AudioChunk]:
    yield VoiceEvent(EventType.TRANSCRIBING)
    try:
        transcript = await speech.transcribe(audio, media_type)
    except speech.SpeechError as error:
        yield VoiceEvent(EventType.ERROR, error=str(error))
        yield VoiceEvent(EventType.DONE)
        return
    yield VoiceEvent(EventType.TRANSCRIPT, text=transcript)
    yield VoiceEvent(EventType.GENERATING)

    # Gemini still checkpoints its deltas, but the browser receives text only once.
    reply = ""
    try:
        async with asyncio.timeout(90), aclosing(stream_conversation(conversation_id, transcript)) as stream:
            async for event in stream:
                if event["type"] == "delta":
                    reply += event["text"]
    except Exception as error:
        logger.warning("Gemini stream or persistence failed: %s", type(error).__name__)
        yield VoiceEvent(EventType.INTERRUPTED, text=reply, error=
            "Reply interrupted. Any saved transcript/partial text is retained; storage failures may leave unsaved text.")
        yield VoiceEvent(EventType.DONE)
        return
    yield VoiceEvent(EventType.REPLY_COMPLETE, text=reply)

    if not reply.strip() or len(reply) > speech.MAX_SPEECH_CHARACTERS:
        yield VoiceEvent(EventType.NOTICE, text=
            "Speech supports nonempty replies up to 200 characters. Full text is saved; audio was skipped.")
    else:
        yield VoiceEvent(EventType.SYNTHESIZING)
        audio_started = False
        partial = False
        try:
            async with asyncio.timeout(45), aclosing(speech.stream_speech(reply)) as stream:
                async for part in stream:
                    if isinstance(part, AudioFormat):
                        audio_started = True
                        yield VoiceEvent(EventType.AUDIO_START, format=part)
                    else:
                        yield part
        except (speech.SpeechError, TimeoutError) as error:
            partial = True
            message = str(error) if isinstance(error, speech.SpeechError) else "Speech timed out. Please try again."
            yield VoiceEvent(EventType.ERROR, error=message)
        if audio_started:
            yield VoiceEvent(EventType.AUDIO_END, partial=partial)
    yield VoiceEvent(EventType.DONE)
