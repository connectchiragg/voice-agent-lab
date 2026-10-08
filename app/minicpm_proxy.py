"""Deploy separately beside the official gateway, behind verified TLS.

Expose this service only. The official gateway/registry/worker stay on loopback.
No inference, model loading, recordings, browser token or content logging here.
"""

import asyncio
import os
import secrets
from contextlib import suppress

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.minicpm import MAX_FRAME_BYTES, MiniCPMError, validate_endpoint

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
session_lock = asyncio.Lock()  # One process / one GPU session in this learning setup.


@app.websocket("/v1/realtime")
async def proxy(socket: WebSocket) -> None:
    token = os.getenv("MINICPM_TOKEN", "")
    authorization = socket.headers.get("authorization", "")
    authorized = (len(token) >= 32 and not any(char.isspace() for char in token)
                  and secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()))
    # Server-to-server only: reject browser-origin calls and every other mode/query.
    if (not authorized or socket.headers.get("origin") is not None
            or str(socket.scope.get("query_string", b""), "ascii", errors="replace") != "mode=audio"):
        await socket.close(code=1008)
        return
    upstream = os.getenv("MINICPM_UPSTREAM_URL", "ws://127.0.0.1:8006/v1/realtime?mode=audio")
    try:
        validate_endpoint(upstream, private=True)
    except MiniCPMError:
        await socket.close(code=1011)
        return
    if session_lock.locked():
        await socket.close(code=1013)
        return
    async with session_lock:
        await socket.accept()
        jobs: list[asyncio.Task[None]] = []
        try:
            async with asyncio.timeout(600), connect(
                upstream, open_timeout=5, close_timeout=2, max_size=MAX_FRAME_BYTES,
                max_queue=4, compression=None,
            ) as official:
                async def forward_input() -> None:
                    while True:
                        frame = await socket.receive()
                        if frame["type"] == "websocket.disconnect":
                            return
                        text = frame.get("text")
                        if text is None or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                            await socket.close(code=1009)
                            return
                        await official.send(text)

                async def forward_output() -> None:
                    async for frame in official:
                        if not isinstance(frame, str):
                            await socket.close(code=1011)
                            return
                        await socket.send_text(frame)

                jobs = [asyncio.create_task(forward_input()), asyncio.create_task(forward_output())]
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
        except (TimeoutError, WebSocketDisconnect, WebSocketException, OSError):
            # Do not expose upstream URLs, exception bodies or session contents.
            pass
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            with suppress(RuntimeError, WebSocketDisconnect, OSError):
                await socket.close()
