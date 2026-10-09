"""One voice WebSocket, plus static serving for its browser client."""

import asyncio
import logging
from contextlib import aclosing
from pathlib import Path
from typing import Literal, NotRequired, TypeAlias, TypedDict, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from app import speech
from app.diagnostics import observe_speech
from app.database import lifespan, load_history
from app.events import AudioChunk, EventType, VoiceEvent
from app.voice import voice_turn

STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"
logger = logging.getLogger("uvicorn.error")
conversation_locks: dict[str, asyncio.Lock] = {}

app = FastAPI(title="Voice Agent Lab", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")
app.add_api_websocket_route("/ws/diagnostics", observe_speech)


class RecordingHeader(BaseModel):
    type: Literal["recording"]
    media_type: str


class TextFrame(TypedDict):
    type: Literal["websocket.receive"]
    text: str
    bytes: NotRequired[None]


class AudioFrame(TypedDict):
    type: Literal["websocket.receive"]
    bytes: bytes
    text: NotRequired[None]


class DisconnectFrame(TypedDict):
    type: Literal["websocket.disconnect"]
    code: int
    reason: NotRequired[str | None]


Frame: TypeAlias = TextFrame | AudioFrame | DisconnectFrame


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "index.html")


async def protocol_error(socket: WebSocket, message: str) -> None:
    await socket.send_json(VoiceEvent(EventType.ERROR, error=message).to_payload())
    await socket.send_json(VoiceEvent(EventType.DONE).to_payload())


@app.websocket("/ws/voice")
async def voice(socket: WebSocket) -> None:
    requested_id = socket.query_params.get("conversation_id")
    try:
        conversation_id = str(UUID(requested_id)) if requested_id else str(uuid4())
    except ValueError:
        await socket.close(code=1008)
        return
    await socket.accept()
    lock = conversation_locks.setdefault(conversation_id, asyncio.Lock())
    try:
        try:
            async with lock:
                history = await load_history(conversation_id)
            await socket.send_json({"type": "history", "messages": history,
                                    "conversation_id": conversation_id})
        except Exception as error:
            logger.warning("Could not load conversation: %s", type(error).__name__)
            await socket.send_json({"type": "error", "error":
                "Database unavailable. Reconnect after PostgreSQL is running."})
            await socket.close(code=1011)
            return

        media_type: str | None = None
        while True:
            # Describe the framework's dictionary without converting it at runtime.
            frame = cast(Frame, await socket.receive())
            if frame["type"] == "websocket.disconnect":
                return
            text = frame.get("text")
            if text is not None:
                # Each turn is a JSON recording header followed by one binary clip.
                try:
                    header = RecordingHeader.model_validate_json(text)
                    candidate = header.media_type.split(";")[0].strip().lower()
                    if candidate not in speech.AUDIO_FILENAMES:
                        raise ValueError("Unsupported audio format")
                    if media_type is not None:
                        raise ValueError("Another recording header is already pending")
                except (ValidationError, ValueError):
                    media_type = None
                    await protocol_error(socket, "Send a recording header with a supported audio media_type.")
                else:
                    media_type = candidate
                continue

            audio = frame.get("bytes")
            recording_type, media_type = media_type, None
            if recording_type is None or not audio or len(audio) > speech.MAX_AUDIO_BYTES:
                await protocol_error(socket, "Send a recording header, then a nonempty audio clip up to 5 MB.")
                continue
            # Serialize the entire turn, even across connections using the same ID.
            async with lock:
                async with aclosing(voice_turn(conversation_id, audio, recording_type)) as stream:
                    async for event in stream:
                        if isinstance(event, AudioChunk):
                            await socket.send_bytes(event.data)
                        else:
                            await socket.send_json(event.to_payload())
    except WebSocketDisconnect:
        pass
