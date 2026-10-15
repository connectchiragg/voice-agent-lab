"""Browser page and the call WebSocket, wired to the cascaded voice pipeline."""
import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app import agent, groq_api
from app.deepgram import OUTPUT_SAMPLE_RATE, AuraSpeaker, DeepgramSettings, FluxListener
from app.pipeline import Call, Engines, ReplyEvent, ReplyModel

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("uvicorn.error")
# Start the LLM from Flux's EagerEndOfTurn transcript (see Call); 0 disables it.
SPECULATIVE_LLM = os.getenv("SPECULATIVE_LLM", "1") == "1"


def load_engines() -> Engines | None:
    """Deepgram Flux (STT) and Aura-2 (TTS) per call; None without an API key."""
    if not os.getenv("DEEPGRAM_API_KEY"):
        logger.warning("DEEPGRAM_API_KEY is not set; calls are refused.")
        return None
    settings = DeepgramSettings.from_env()
    logger.info("Deepgram %s: Flux eot %.2f, eager %s, timeout %d ms; voice %s", settings.host,
                settings.eot_threshold, settings.eager_eot_threshold, settings.eot_timeout_ms,
                settings.voice)

    async def reply(conversation_id: str, text: str,
                    committed: asyncio.Event | None = None) -> AsyncIterator[ReplyEvent]:
        async for event in agent.stream_conversation(conversation_id, text, committed):
            if event["type"] == "delta":
                yield event["text"]
            elif event["type"] == "model":
                yield ReplyModel(event["model"])

    speculate = SPECULATIVE_LLM and settings.eager_eot_threshold is not None
    return Engines(new_listener=lambda on_event: FluxListener(settings, on_event),
                   new_speaker=lambda: AuraSpeaker(settings),
                   reply=reply, record_interruption=agent.record_interruption,
                   output_sample_rate=OUTPUT_SAMPLE_RATE,
                   speculate=reply if speculate else None,
                   may_speculate=lambda: not groq_api.near_rate_limit())


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
    app.state.engines = load_engines()
    watcher = asyncio.create_task(watch_event_loop())
    try:
        async with groq_api.shared_groq_client(), agent.lifespan(app):
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


@app.get("/healthz")
async def health() -> dict[str, bool]:
    """Liveness for container hosts; `speech` is false without a Deepgram key."""
    return {"ok": True, "speech": getattr(app.state, "engines", None) is not None}


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
            await send({"type": "error", "error": "Speech services are not configured."})
            return
        if not settings.echo_cancelled:
            logger.warning("No browser echo cancellation; barge-in disabled for this call.")
        # Without echo cancellation the assistant's own voice would interrupt it.
        call = Call(engines, send, str(uuid4()), barge_in=settings.echo_cancelled)
        try:
            await call.start()  # Opens the Flux and Aura sockets for this call.
        except Exception as error:
            # A refused handshake carries Deepgram's HTTP status (401 key, 402 credit).
            status = getattr(getattr(error, "response", None), "status_code", None)
            logger.warning("Deepgram connection failed: %s %s", type(error).__name__, status or "")
            await send({"type": "error", "error": "The speech service is unavailable. Try again later."})
            return
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
