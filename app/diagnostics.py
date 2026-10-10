"""Opt-in PCM diagnostics socket, isolated from the working voice endpoint."""

import asyncio
import logging
from typing import Literal

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger("uvicorn.error")


class DiagnosticSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["settings"]
    sample_rate: Literal[16000]
    encoding: Literal["pcm_f32le"]
    echo_cancelled: bool
    assistant_speaking: bool


async def observe_speech(socket: WebSocket) -> None:
    await socket.accept()
    observer = None
    try:
        settings = DiagnosticSettings.model_validate_json(await socket.receive_text())
        # Optional dependencies/model are loaded only for this opt-in endpoint.
        from app.vad import VapModel, SpeechDiagnostics

        gate = await asyncio.to_thread(VapModel)
        observer = SpeechDiagnostics(gate, echo_cancelled=settings.echo_cancelled)
        await socket.send_json({"type": "diagnostics_ready",
                                "classifier_status": "prediction_only", "model": "vap_mono"})
        while True:
            frame = await socket.receive()
            if frame["type"] == "websocket.disconnect":
                break
            if frame.get("text") is not None:
                settings = DiagnosticSettings.model_validate_json(frame["text"])
                observer.echo_cancelled = settings.echo_cancelled
                continue
            audio = frame.get("bytes")
            if audio is None:
                raise ValueError("Expected PCM bytes or settings")
            reports = await asyncio.to_thread(observer.feed, audio,
                                               assistant_speaking=settings.assistant_speaking)
            for report in reports:
                logger.info("Speech diagnostic: %s", report.to_payload())
                await socket.send_json(report.to_payload())
    except WebSocketDisconnect:
        pass
    except (ValidationError, ValueError):
        await socket.close(code=1008)
    except Exception as error:
        logger.warning("Speech diagnostics unavailable: %s", type(error).__name__)
        await socket.close(code=1011)
    finally:
        if observer is not None:
            observer.close()
