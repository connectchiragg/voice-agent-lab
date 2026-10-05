"""Cartesia wire contract and cleanup, without spending provider credits."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import aclosing

import pytest

from app import tts
from app.speech import SpeechError

PCM = b"\x00\x00\x00\x3f"


class SocketStub:
    def __init__(self, response: dict | None = None, fail_send: bool = False) -> None:
        self.requests: list[dict] = []
        self.frames: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.response = response
        self.fail_send = fail_send

    async def __aenter__(self) -> "SocketStub":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def send(self, frame: str) -> None:
        if self.fail_send:
            raise OSError("private transport details")
        request = json.loads(frame)
        self.requests.append(request)
        if self.response is not None:
            self.frames.put_nowait(json.dumps({**self.response, "context_id": request["context_id"]}))
        elif request["transcript"]:
            self.frames.put_nowait(json.dumps({"type": "chunk", "context_id": request["context_id"],
                                              "data": base64.b64encode(PCM).decode()}))
        else:
            self.frames.put_nowait(json.dumps({"type": "done", "context_id": request["context_id"]}))

    def __aiter__(self) -> "SocketStub":
        return self

    async def __anext__(self) -> str:
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def close(self) -> None:
        self.closed = True
        self.frames.put_nowait(None)


def configure(monkeypatch: pytest.MonkeyPatch, socket: SocketStub) -> None:
    monkeypatch.setenv("CARTESIA_API_KEY", "fake-key")
    monkeypatch.setenv("CARTESIA_MODEL", "sonic-3.6-2026-08-27")
    monkeypatch.setenv("CARTESIA_VOICE", "test-voice")

    def connect(url: str, **options: object) -> SocketStub:
        assert url == "wss://api.cartesia.ai/tts/websocket?cartesia_version=2026-08-14"
        assert options["additional_headers"] == {"X-API-Key": "fake-key"}
        return socket

    monkeypatch.setattr(tts, "connect", connect)


async def parts() -> AsyncIterator[str]:
    yield "First "
    yield "part."


def test_one_context_incremental_input_and_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = SocketStub()
    configure(monkeypatch, socket)

    async def exercise() -> None:
        audio = [chunk async for chunk in tts.stream_speech(parts())]
        assert [chunk.data for chunk in audio] == [PCM, PCM]

    asyncio.run(exercise())
    assert [request["transcript"] for request in socket.requests] == ["First ", "part.", ""]
    assert [request["continue"] for request in socket.requests] == [True, True, False]
    assert len({request["context_id"] for request in socket.requests}) == 1
    assert socket.requests[0]["voice"] == "test-voice"
    assert socket.requests[0]["output_format"] == {
        "container": "raw", "encoding": "pcm_f32le", "sample_rate": 24000,
    }
    assert socket.closed


def test_audio_before_final_text_and_cancellation_closes_input(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = SocketStub()
    configure(monkeypatch, socket)
    input_closed = False

    async def texts() -> AsyncIterator[str]:
        nonlocal input_closed
        try:
            yield "First part."
            await asyncio.sleep(60)
        finally:
            input_closed = True

    async def exercise() -> None:
        async with aclosing(tts.stream_speech(texts())) as stream:
            assert (await asyncio.wait_for(anext(stream), timeout=1)).data == PCM
            assert len(socket.requests) == 1  # No final input required for audio.
        assert socket.closed
        assert input_closed

    asyncio.run(exercise())


@pytest.mark.parametrize("response, expected", [
    ({"type": "error", "status_code": 429, "message": "private details"}, 429),
    ({"type": "error", "status_code": 403, "message": "private details"}, 502),
    ({"type": "done"}, 502),
    ({"type": "chunk", "data": "not base64!"}, 502),
])
def test_provider_errors_are_safe(monkeypatch: pytest.MonkeyPatch, response: dict, expected: int) -> None:
    socket = SocketStub(response=response)
    configure(monkeypatch, socket)

    async def exercise() -> None:
        with pytest.raises(SpeechError) as error:
            async for chunk in tts.stream_speech(parts()):
                pass
        assert error.value.status_code == expected
        assert "private" not in str(error.value)
        assert socket.closed

    asyncio.run(exercise())


def test_send_failure_wakes_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    socket = SocketStub(fail_send=True)
    configure(monkeypatch, socket)

    async def exercise() -> None:
        with pytest.raises(SpeechError):
            async with asyncio.timeout(1):
                async for chunk in tts.stream_speech(parts()):
                    pass
        assert socket.closed

    asyncio.run(exercise())


def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)

    async def exercise() -> None:
        with pytest.raises(SpeechError, match="CARTESIA_API_KEY"):
            async for chunk in tts.stream_speech(parts()):
                pass

    asyncio.run(exercise())
