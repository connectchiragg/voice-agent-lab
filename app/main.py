from pathlib import Path

import asyncio
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.llm import generate_reply

STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"
logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Voice Agent Lab")
app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIRECTORY / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws/chat")
async def chat(socket: WebSocket):
    await socket.accept()
    history = []
    try:
        while True:
            message = (await socket.receive_text()).strip()
            if not message or len(message) > 8000:
                await socket.send_json({"error": "Send between 1 and 8,000 characters."})
                continue
            try:
                reply = await asyncio.wait_for(generate_reply(message, history), timeout=35)
            except RuntimeError as error:
                logger.exception("Chat request failed (history messages: %d)", len(history))
                await socket.send_json({"error": str(error)})
            except Exception:
                logger.exception("Gemini request failed (history messages: %d)", len(history))
                await socket.send_json({"error": "Gemini request failed. Check your key, model and quota, then retry."})
            else:
                # Keep complete turns only; failed requests do not change history.
                history.append({"role": "user", "parts": [{"text": message}]})
                history.append({"role": "model", "parts": [{"text": reply}]})
                await socket.send_json({"reply": reply})
    except WebSocketDisconnect:
        pass
