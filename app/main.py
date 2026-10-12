"""Browser page and the call WebSocket; the cascaded voice pipeline plugs in here."""
import asyncio
import logging
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Voice Agent Lab", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class CallSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["start"]
    sample_rate: Literal[16000]
    encoding: Literal["pcm_f32le"]
    echo_cancelled: bool


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(ROOT / "static/index.html")


@app.websocket("/ws/voice")
async def voice(socket: WebSocket) -> None:
    await socket.accept()
    try:
        async with asyncio.timeout(15):
            CallSettings.model_validate_json(await socket.receive_text())
        # STT -> LLM -> TTS is being rebuilt; refuse clearly instead of faking a call.
        await socket.send_json({"type": "error", "error": "Voice pipeline not built yet."})
    except WebSocketDisconnect:
        return
    except (ValidationError, TimeoutError):
        await socket.close(code=1008)
        return
    await socket.close()
