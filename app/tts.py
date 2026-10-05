"""Incremental text in, raw mono Float32 PCM out over Cartesia's WebSocket."""

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator
from contextlib import aclosing
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from app.events import AudioChunk
from app.speech import SpeechError

SAMPLE_RATE = 24000
ENCODING = "pcm_f32le"
API_VERSION = "2026-08-14"


async def send_text(socket: ClientConnection, texts: AsyncIterator[str], context_id: str) -> None:
    """All deltas belong to one speech context, not separate synthesis requests."""
    request = {
        "model_id": os.getenv("CARTESIA_MODEL", "sonic-3.6-2026-08-27"),
        "voice": os.getenv("CARTESIA_VOICE", "47c38ca4-5f35-497b-b1a3-415245fb35e1"),
        "language": "en", "context_id": context_id,
        "output_format": {"container": "raw", "encoding": ENCODING, "sample_rate": SAMPLE_RATE},
    }
    try:
        async with aclosing(texts):
            async for text in texts:
                if text:
                    await socket.send(json.dumps({**request, "transcript": text, "continue": True}))
        await socket.send(json.dumps({**request, "transcript": "", "continue": False}))
    except Exception:
        # Wake a receiver that would otherwise wait forever after sending fails.
        await socket.close()
        raise


async def stream_speech(texts: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise SpeechError(503, "Set CARTESIA_API_KEY in your backend .env file, then restart the app.")
    try:
        async with connect(
            f"wss://api.cartesia.ai/tts/websocket?cartesia_version={API_VERSION}",
            additional_headers={"X-API-Key": api_key}, open_timeout=15,
            max_size=4 * 1024 * 1024,
        ) as socket:
            context_id = str(uuid4())
            sender = asyncio.create_task(send_text(socket, texts, context_id))
            received_audio = False
            try:
                async for frame in socket:
                    event = json.loads(frame)
                    if not isinstance(event, dict):
                        raise ValueError("Expected a speech event object")
                    if event.get("type") == "error":
                        status = event.get("status_code")
                        if status == 429:
                            message = "Cartesia quota reached. Wait or check your plan."
                        else:
                            message = "Cartesia rejected speech generation. Check your key, model and voice."
                        raise SpeechError(429 if status == 429 else 502, message)
                    if event.get("context_id") != context_id:
                        continue
                    if event.get("type") == "chunk":
                        audio = base64.b64decode(event["data"], validate=True)
                        if audio:
                            received_audio = True
                            yield AudioChunk(audio)
                    elif event.get("type") == "done":
                        await sender
                        if not received_audio:
                            raise SpeechError(502, "Cartesia returned no speech audio.")
                        return
                raise SpeechError(502, "Cartesia's speech connection closed before completion.")
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
    except SpeechError:
        raise
    except TimeoutError as error:
        raise SpeechError(504, "Cartesia speech timed out. Please try again.") from error
    except (WebSocketException, OSError, ValueError, KeyError) as error:
        raise SpeechError(502, "Cartesia's speech stream failed. Please try again.") from error
