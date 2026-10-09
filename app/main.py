"""Continuous audio transport, with observation-only VAD and no database writes."""
import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from app import minicpm
from app.diagnostics import observe_speech

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Voice Agent Lab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.add_api_websocket_route("/ws/diagnostics", observe_speech)


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


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(ROOT / "static/index.html")


@app.websocket("/ws/voice")
async def voice(socket: WebSocket) -> None:
    await socket.accept()
    observer = None
    jobs: list[asyncio.Task[None]] = []
    send_lock = asyncio.Lock()

    async def send(data: dict[str, object] | bytes) -> None:
        async with send_lock:
            if isinstance(data, bytes):
                await socket.send_bytes(data)
            else:
                await socket.send_json(data)

    try:
        async with asyncio.timeout(15):
            settings = CallSettings.model_validate_json(await socket.receive_text())
        from app.vad import SileroGate, SpeechDiagnostics
        gate = await asyncio.to_thread(SileroGate, os.getenv("SILERO_MODEL_PATH",
                          str(ROOT / ".cache/speech-diagnostics/silero_vad.onnx")))
        observer = SpeechDiagnostics(gate, echo_cancelled=settings.echo_cancelled)
        async with minicpm.open_session(minicpm.MiniCPMSettings.from_env(),
                                       "You are a helpful voice assistant.") as session:
            await send({"type": "ready", "sample_rate": minicpm.OUTPUT_SAMPLE_RATE,
                        "encoding": minicpm.ENCODING})
            playing = False
            diagnostic_audio: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(maxsize=8)

            async def uplink() -> None:
                nonlocal playing
                while True:
                    frame = await socket.receive()
                    if frame["type"] == "websocket.disconnect":
                        return
                    if frame.get("text") is not None:
                        playing = PlaybackState.model_validate_json(frame["text"]).active
                        continue
                    audio = frame.get("bytes")
                    if audio is None:
                        raise ValueError("Expected PCM")
                    await session.send_audio(audio)
                    if not diagnostic_audio.full():
                        diagnostic_audio.put_nowait((audio, playing))
                    else:
                        logger.warning("Speech diagnostics overloaded; observation gap")
                        while not diagnostic_audio.empty():
                            diagnostic_audio.get_nowait()
                        diagnostic_audio.put_nowait((b"", playing))

            async def downlink() -> None:
                async for output in session.outputs():
                    if output.kind == minicpm.OutputKind.AUDIO:
                        await send(output.audio)
                    elif output.kind == minicpm.OutputKind.TEXT:
                        await send({"type": "text", "text": output.text})
                    elif output.kind == minicpm.OutputKind.CLOSED:
                        return
                    # LISTEN is not an instruction to stop queued browser audio.

            async def diagnostics() -> None:
                nonlocal observer
                while True:
                    audio, active = await diagnostic_audio.get()
                    if not audio:
                        observer.close()
                        observer = SpeechDiagnostics(gate, echo_cancelled=settings.echo_cancelled)
                        continue
                    inference = asyncio.create_task(asyncio.to_thread(
                        observer.feed, audio, assistant_speaking=active))
                    try:
                        reports = await asyncio.shield(inference)
                    except asyncio.CancelledError:
                        # A CPU thread cannot be cancelled; finish before clearing state.
                        await inference
                        raise
                    for report in reports:
                        logger.info("Speech diagnostic: %s", report.to_payload())
                        await send(report.to_payload())

            jobs = [asyncio.create_task(uplink()), asyncio.create_task(downlink()),
                    asyncio.create_task(diagnostics())]
            try:
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except ValueError:
        await socket.close(code=1008)
    except Exception as error:
        logger.warning("Voice call failed: %s", type(error).__name__)
        try:
            await send({"type": "error", "error": "Call unavailable. Check MiniCPM connectivity and the local VAD model."})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        if observer is not None:
            observer.close()
        try:
            await socket.close()
        except RuntimeError:
            pass
