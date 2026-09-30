from pathlib import Path
from contextlib import aclosing

import asyncio
import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database import lifespan, load_history, stream_conversation

STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"
logger = logging.getLogger("uvicorn.error")
conversation_locks: dict[str, asyncio.Lock] = {}

app = FastAPI(title="Voice Agent Lab", lifespan=lifespan)
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
            async with lock:
                history = await load_history(conversation_id)
                if requested_id:
                    await socket.send_json({"history": history})
        except Exception:
            logger.exception("Could not load conversation")
            await socket.send_json({"error": "Database unavailable. Reconnect after PostgreSQL is running."})
            await socket.close(code=1011)
            return
        while True:
            message = (await socket.receive_text()).strip()
            if not message or len(message) > 8000:
                await socket.send_json({"error": "Send between 1 and 8,000 characters."})
                continue
            # Two connections resuming the same ID must not interleave turns.
            await lock.acquire()
            try:
                async with asyncio.timeout(90), aclosing(stream_conversation(conversation_id, message)) as stream:
                    async for event in stream:
                        await socket.send_json(event)
            except WebSocketDisconnect:
                return
            except TimeoutError:
                logger.exception("Chat stream exceeded its time limit")
                await socket.send_json({"type": "interrupted", "error":
                    "Response timed out. Your message and saved partial reply are retained."})
            except Exception:
                logger.exception("Chat stream or persistence failed")
                await socket.send_json({"type": "interrupted", "error":
                    "Response interrupted. You can send another message. If storage failed, the last chunk may not be saved."})
            else:
                await socket.send_json({"type": "done"})
            finally:
                lock.release()
    except WebSocketDisconnect:
        pass
