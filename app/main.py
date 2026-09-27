from pathlib import Path

import asyncio
import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.llm import generate_reply
from app.database import load_history, save_exchange

STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"
logger = logging.getLogger("uvicorn.error")
conversation_locks: dict[str, asyncio.Lock] = {}

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
            history = await load_history(conversation_id)
        except Exception:
            logger.exception("Could not load conversation")
            await socket.send_json({"error": "Database unavailable. Reconnect after PostgreSQL is running."})
            await socket.close(code=1011)
            return
        if requested_id:
            await socket.send_json({"history": history})
        while True:
            message = (await socket.receive_text()).strip()

            if not message or len(message) > 8000:
                await socket.send_json({"error": "Send between 1 and 8,000 characters."})
                continue
            # Two connections resuming the same ID must not interleave turns.
            await lock.acquire()
            try:
                history = await load_history(conversation_id)
                reply = await asyncio.wait_for(generate_reply(message, history), timeout=35)
                await save_exchange(conversation_id, message, reply)
            except RuntimeError as error:
                logger.exception("Chat request failed (history messages: %d)", len(history))
                await socket.send_json({"error": str(error)})
            except Exception:
                logger.exception("Chat generation or persistence failed (history messages: %d)", len(history))
                await socket.send_json({"error": "Could not generate or save the reply. Check the server logs, then retry."})
            else:
                await socket.send_json({"reply": reply})
            finally:
                lock.release()
    except WebSocketDisconnect:
        pass
