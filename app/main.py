"""Browser page and the call WebSocket, wired to the cascaded voice pipeline."""
import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app import database, groq_api
from app.pipeline import Call, Engines, Samples, TurnDetector

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("uvicorn.error")
MODEL_FILES = ("silero_vad.onnx", "smart-turn-v3.2-cpu.onnx", "kokoro-v1.0.onnx", "voices-v1.0.bin",
               "sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25/encoder.int8.onnx")


def load_engines() -> Engines:
    """Load local models once per process; each call gets its own VAD and STT stream."""
    import threading

    from app.stt import MODEL_DIR, StreamingTranscriber, load_recognizer
    from app.tts import OUTPUT_SAMPLE_RATE, KokoroVoice
    from app.turn import SmartTurn
    from app.vad import SileroVad, load_session

    models = Path(os.getenv("MODELS_DIR", ROOT / ".cache/models"))
    missing = [name for name in MODEL_FILES if not (models / name).is_file()]
    if missing:
        raise RuntimeError(f"Missing models in {models}: {', '.join(missing)}. "
                           "Run scripts/download-models.sh first.")
    vad_session = load_session(str(models / "silero_vad.onnx"))
    turn = SmartTurn(load_session(str(models / "smart-turn-v3.2-cpu.onnx")))
    voice = KokoroVoice(str(models / "kokoro-v1.0.onnx"), str(models / "voices-v1.0.bin"),
                        os.getenv("KOKORO_VOICE", "af_heart"))
    recognizer, recognizer_lock = load_recognizer(str(models / MODEL_DIR)), threading.Lock()
    # Warm up so the first real turn does not pay one-off initialisation costs.
    turn.completion_probability(np.zeros(16000, dtype=np.float32))
    voice.synthesize("Hello.")
    warm = StreamingTranscriber(recognizer, recognizer_lock)
    warm.on_speech_start(np.zeros(16000, dtype=np.float32))
    warm.on_speech_end()

    async def reply(conversation_id: str, text: str) -> AsyncIterator[str]:
        async for event in database.stream_conversation(conversation_id, text):
            if event["type"] == "delta":
                yield event["text"]

    async def synthesize(text: str) -> Samples:
        return await asyncio.to_thread(voice.synthesize, text)

    return Engines(new_transcriber=lambda: StreamingTranscriber(recognizer, recognizer_lock),
                   new_detector=lambda listener: TurnDetector(SileroVad(vad_session), turn,
                                                             listener=listener),
                   reply=reply, synthesize=synthesize,
                   record_interruption=database.record_interruption,
                   output_sample_rate=OUTPUT_SAMPLE_RATE)


async def watch_event_loop(interval_s: float = 0.1, threshold_s: float = 0.25) -> None:
    """Warn when something blocks the event loop: audio and network both stall then."""
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(interval_s)
        lag = loop.time() - started - interval_s
        if lag > threshold_s:
            logger.warning("Event loop blocked for %.0f ms", lag * 1000)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.engines = await asyncio.to_thread(load_engines)
    watcher = asyncio.create_task(watch_event_loop())
    try:
        async with groq_api.shared_groq_client(), database.lifespan(app):
            yield
    finally:
        watcher.cancel()


app = FastAPI(title="Voice Agent Lab", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class CallSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["start"]
    sample_rate: Literal[16000]
    encoding: Literal["pcm_f32le"]
    echo_cancelled: bool


class PlaybackState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["playback"]
    active: bool


class PlayedReport(BaseModel):
    """Sent after an interrupt: seconds of the interrupted reply's audio played."""
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["interrupted"]
    played_s: float = Field(ge=0, le=3600)


ClientEvent = TypeAdapter(Annotated[PlaybackState | PlayedReport, Field(discriminator="type")])


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(ROOT / "static/index.html")


@app.websocket("/ws/voice")
async def voice(socket: WebSocket) -> None:
    await socket.accept()
    engines: Engines | None = getattr(socket.app.state, "engines", None)
    send_lock = asyncio.Lock()
    call: Call | None = None

    async def send(data: dict[str, object] | bytes) -> None:
        async with send_lock:  # The reply task and this loop both send.
            if isinstance(data, bytes):
                await socket.send_bytes(data)
            else:
                await socket.send_json(data)

    try:
        async with asyncio.timeout(15):
            settings = CallSettings.model_validate_json(await socket.receive_text())
        if engines is None:
            await send({"type": "error", "error": "Voice models are not loaded."})
            return
        if not settings.echo_cancelled:
            logger.warning("No browser echo cancellation; barge-in disabled for this call.")
        # Without echo cancellation the assistant's own voice would interrupt it.
        call = Call(engines, send, str(uuid4()), barge_in=settings.echo_cancelled)
        await send({"type": "ready", "sample_rate": engines.output_sample_rate,
                    "encoding": "pcm_f32le"})
        while True:
            frame = await socket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            if frame.get("text") is not None:
                event = ClientEvent.validate_json(frame["text"])
                if isinstance(event, PlaybackState):
                    call.set_playing(event.active)
                else:
                    call.report_played(event.played_s)
                continue
            packet = frame.get("bytes")
            if packet is None:
                raise ValueError("Expected PCM bytes")
            await call.hear(packet)
    except WebSocketDisconnect:
        pass
    except (ValidationError, ValueError, TimeoutError):
        await socket.close(code=1008)
    except Exception as error:
        logger.warning("Voice call failed: %s", type(error).__name__)
        try:
            await send({"type": "error", "error": "The call failed. Please start a new one."})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if call is not None:
            await call.close()
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass  # Already closed by the browser.
