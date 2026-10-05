"""One recorded input; Gemini generation and Cartesia speech run concurrently."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing

from app import speech, tts
from app.database import stream_conversation
from app.events import AudioChunk, EventType, VoiceEvent

logger = logging.getLogger("uvicorn.error")


async def voice_turn(conversation_id: str, audio: bytes, media_type: str) -> AsyncIterator[VoiceEvent | AudioChunk]:
    try:
        transcript = await speech.transcribe(audio, media_type)
    except speech.SpeechError as error:
        yield VoiceEvent(EventType.ERROR, error=str(error))
        yield VoiceEvent(EventType.DONE)
        return
    yield VoiceEvent(EventType.TRANSCRIPT, text=transcript)

    # None marks completion. Queues connect the two jobs without coupling failures.
    texts: asyncio.Queue[str | None] = asyncio.Queue()
    events: asyncio.Queue[VoiceEvent | AudioChunk | None] = asyncio.Queue()

    async def generate_text() -> None:
        reply = ""
        try:
            async with asyncio.timeout(90), aclosing(stream_conversation(conversation_id, transcript)) as stream:
                async for event in stream:
                    if event["type"] == "delta":
                        reply += event["text"]
                        texts.put_nowait(event["text"])
            events.put_nowait(VoiceEvent(EventType.REPLY_COMPLETE, text=reply))
        except Exception as error:
            logger.warning("Gemini stream or persistence failed: %s", type(error).__name__)
            events.put_nowait(VoiceEvent(EventType.INTERRUPTED, text=reply, error=
                "Reply interrupted. Any saved transcript/partial text is retained; storage failures may leave unsaved text."))
        finally:
            texts.put_nowait(None)
            events.put_nowait(None)

    async def text_chunks() -> AsyncIterator[str]:
        while (text := await texts.get()) is not None:
            yield text

    async def generate_audio() -> None:
        started = False
        partial = False
        try:
            async with asyncio.timeout(90), aclosing(tts.stream_speech(text_chunks())) as stream:
                async for chunk in stream:
                    if not started:
                        started = True
                        events.put_nowait(VoiceEvent(EventType.AUDIO_START,
                            sample_rate=tts.SAMPLE_RATE, encoding=tts.ENCODING))
                    events.put_nowait(chunk)
        except Exception as error:
            partial = True
            logger.warning("Speech stream failed: %s", type(error).__name__)
            message = str(error) if isinstance(error, speech.SpeechError) else "Speech generation failed. Please try again."
            events.put_nowait(VoiceEvent(EventType.ERROR, error=message))
        finally:
            if started:
                events.put_nowait(VoiceEvent(EventType.AUDIO_END, partial=partial))
            events.put_nowait(None)

    jobs = [asyncio.create_task(generate_text()), asyncio.create_task(generate_audio())]
    try:
        finished = 0
        while finished < len(jobs):
            event = await events.get()
            if event is None:
                finished += 1
            else:
                yield event
        yield VoiceEvent(EventType.DONE)
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
