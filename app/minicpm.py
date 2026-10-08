"""Audio-only client for the pinned MiniCPM-o Realtime API; no model loading.

Source: OpenBMB/MiniCPM-o-Demo@47709a9210dfd71afa76c058e017fc8c4db5c8d2
docs-app/content/docs/en/realtime-api/audio.md and gateway.py / worker.py.
"""

import asyncio
import base64
import json
import math
import os
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
ENCODING = "pcm_f32le"
MAX_FRAME_BYTES = 1024 * 1024
MAX_INPUT_BYTES = INPUT_SAMPLE_RATE * 4  # At most one second per input message.


class MiniCPMError(Exception):
    """Safe error text: never include URLs, credentials or provider payloads."""


def validate_endpoint(url: str, *, private: bool = False) -> None:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise MiniCPMError("Invalid MiniCPM endpoint.") from None
    loopback = parts.hostname in {"127.0.0.1", "::1"}
    transport_ok = parts.scheme == "ws" and loopback if private else (
        parts.scheme == "wss" or (parts.scheme == "ws" and loopback)
    )
    if (not transport_ok or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment
            or parts.path != "/v1/realtime" or parts.query != "mode=audio"):
        raise MiniCPMError("Use /v1/realtime?mode=audio over verified WSS, or WS on numeric loopback only.")
    try:
        parts.port
    except ValueError:
        raise MiniCPMError("Invalid MiniCPM endpoint port.") from None


@dataclass(frozen=True)
class MiniCPMSettings:
    url: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        validate_endpoint(self.url)
        if len(self.token) < 32 or any(char.isspace() for char in self.token):
            raise MiniCPMError("Set a random MINICPM_TOKEN of at least 32 characters without whitespace.")

    @classmethod
    def from_env(cls) -> "MiniCPMSettings":
        return cls(os.getenv("MINICPM_URL", ""), os.getenv("MINICPM_TOKEN", ""))


class OutputKind(StrEnum):
    TEXT = "text"
    AUDIO = "audio"
    LISTEN = "listen"
    CLOSED = "closed"


@dataclass(frozen=True)
class MiniCPMOutput:
    kind: OutputKind
    text: str = ""
    audio: bytes = b""
    response_id: str | None = None


def validate_pcm(audio: bytes, limit: int) -> None:
    if not audio or len(audio) > limit or len(audio) % 4:
        raise MiniCPMError("Audio must contain bounded, complete float32 PCM samples.")
    if any(not math.isfinite(sample) for (sample,) in struct.iter_unpack("<f", audio)):
        raise MiniCPMError("Audio contains non-finite PCM samples.")


async def receive_message(socket: ClientConnection) -> dict[str, object]:
    raw = await socket.recv()
    if not isinstance(raw, str):
        raise MiniCPMError("MiniCPM must return JSON text frames.")
    try:
        message = json.loads(raw)
    except ValueError:
        raise MiniCPMError("MiniCPM returned malformed JSON.") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise MiniCPMError("MiniCPM returned an invalid event.")
    if message["type"] == "error":
        raise MiniCPMError("MiniCPM rejected the session or input.")
    return message


class MiniCPMSession:
    def __init__(self, socket: ClientConnection) -> None:
        self.socket = socket
        self.closed = False

    async def send_audio(self, audio: bytes) -> None:
        """Send already-resampled 16 kHz mono PCM; never accept WAV/WebM here."""
        if self.closed:
            raise MiniCPMError("MiniCPM session has ended.")
        validate_pcm(audio, MAX_INPUT_BYTES)
        await self.socket.send(json.dumps({
            "type": "input.append",
            "input": {"audio": base64.b64encode(audio).decode("ascii"), "force_listen": False},
        }))

    async def outputs(self) -> AsyncIterator[MiniCPMOutput]:
        """One consumer; input can be sent concurrently with receiving output."""
        while not self.closed:
            message = await receive_message(self.socket)
            if message["type"] == "session.closed":
                self.closed = True
                yield MiniCPMOutput(OutputKind.CLOSED)
                return
            if message["type"] != "response.output.delta":
                raise MiniCPMError("Unexpected MiniCPM session event.")
            try:
                kind = OutputKind(message.get("kind"))
            except ValueError:
                raise MiniCPMError("Unknown MiniCPM output kind.") from None
            response_id = message.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise MiniCPMError("Invalid MiniCPM response identifier.")
            if kind == OutputKind.TEXT:
                text = message.get("text")
                if not isinstance(text, str):
                    raise MiniCPMError("Invalid MiniCPM text output.")
                yield MiniCPMOutput(kind, text=text, response_id=response_id)
            elif kind == OutputKind.AUDIO:
                encoded = message.get("audio")
                try:
                    if not isinstance(encoded, str):
                        raise ValueError
                    audio = base64.b64decode(encoded, validate=True)
                except ValueError:
                    raise MiniCPMError("Invalid MiniCPM audio output.") from None
                validate_pcm(audio, MAX_FRAME_BYTES)
                yield MiniCPMOutput(kind, audio=audio, response_id=response_id)
            elif kind == OutputKind.LISTEN:
                yield MiniCPMOutput(kind, response_id=response_id)
            else:
                raise MiniCPMError("Invalid MiniCPM delta kind.")


@asynccontextmanager
async def open_session(settings: MiniCPMSettings, system_prompt: str) -> AsyncIterator[MiniCPMSession]:
    """Own one socket/session; no retries or insecure TLS bypass."""
    try:
        async with connect(
            settings.url, additional_headers={"Authorization": f"Bearer {settings.token}"},
            open_timeout=15, close_timeout=5, max_size=MAX_FRAME_BYTES,
            max_queue=4, compression=None,
        ) as socket:
            async with asyncio.timeout(60):
                while True:
                    message = await receive_message(socket)
                    if message["type"] == "session.queue_done":
                        break
                    if message["type"] not in {"session.queued", "session.queue_update"}:
                        raise MiniCPMError("Unexpected MiniCPM queue event.")
                await socket.send(json.dumps({"type": "session.init", "payload": {"system_prompt": system_prompt}}))
                if (await receive_message(socket))["type"] != "session.created":
                    raise MiniCPMError("MiniCPM did not initialize the session.")
            session = MiniCPMSession(socket)
            try:
                yield session
            finally:
                if not session.closed:
                    try:
                        async with asyncio.timeout(2):
                            await socket.send(json.dumps({"type": "session.close", "reason": "user_stop"}))
                    except (TimeoutError, WebSocketException, OSError):
                        pass
                session.closed = True
    except (TimeoutError, WebSocketException, OSError):
        raise MiniCPMError("MiniCPM connection failed or timed out.") from None
