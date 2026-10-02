"""WebSocket protocol, full voice turns, and checkpoint recovery without real APIs."""

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi.routing import APIWebSocketRoute
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from app import database, main, speech

client = TestClient(main.app)
WAV = b"RIFF" + b"\x00" * 4 + b"WAVE-test"


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "graph", database.build_graph(InMemorySaver()))
    monkeypatch.setattr(main, "conversation_locks", {})

    async def transcribe(audio: bytes, media_type: str) -> str:
        assert media_type in speech.AUDIO_FILENAMES
        return audio.decode()

    async def synthesize(text: str) -> bytes:
        assert 0 < len(text) <= 200
        return WAV

    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        yield "Hello"

    monkeypatch.setattr(speech, "transcribe", transcribe)
    monkeypatch.setattr(speech, "synthesize", synthesize)
    monkeypatch.setattr(database, "stream_reply", stream)


def send_recording(socket: WebSocketTestSession, audio: bytes, media_type: str = "audio/webm;codecs=opus") -> None:
    socket.send_json({"type": "recording", "media_type": media_type})
    socket.send_bytes(audio)


def receive_turn(socket: WebSocketTestSession) -> list[dict | bytes]:
    events: list[dict | bytes] = []
    while True:
        frame = socket.receive()
        if frame.get("bytes") is not None:
            events.append(frame["bytes"])
        else:
            event = json.loads(frame["text"])
            events.append(event)
            if event["type"] == "done":
                return events


def test_one_api_and_voice_only_ui() -> None:
    paths = [route.path for route in main.app.routes if isinstance(route, APIWebSocketRoute)]
    assert paths == ["/ws/voice"]
    assert {route.path for route in main.app.routes} == {"/", "/static", "/ws/voice"}
    html = client.get("/").text
    assert "Voice Agent Lab" in html
    assert "<input" not in html and "<form" not in html
    assert client.get("/static/voice.js").status_code == 200
    for path in ("/health", "/docs", "/openapi.json", "/static/chat.js", "/static/speech.js"):
        assert client.get(path).status_code == 404
    for path in ("/api/transcribe", "/api/speak"):
        assert client.post(path).status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat"):
            pass


@pytest.mark.parametrize("fail", [False, True])
def test_voice_turn_persistence_and_reconnect(monkeypatch: pytest.MonkeyPatch, storage: None, fail: bool) -> None:
    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        assert message == "hi"
        yield "Hello"
        if fail:
            raise ValueError("private provider error")
        yield " there"

    monkeypatch.setattr(database, "stream_reply", stream)
    cid = str(uuid4())
    url = f"/ws/voice?conversation_id={cid}"
    with client.websocket_connect(url) as socket:
        assert socket.receive_json() == {"type": "history", "messages": [], "conversation_id": cid}
        send_recording(socket, b"hi")
        events = receive_turn(socket)
        assert events[:4] == [{"type": "transcribing"}, {"type": "transcript", "text": "hi"},
                              {"type": "start"}, {"type": "delta", "text": "Hello"}]
        assert "private" not in str(events)
        if fail:
            assert events[-2]["type"] == "interrupted"
            assert WAV not in events
        else:
            assert events[-4:] == [{"type": "synthesizing"}, {"type": "audio", "media_type": "audio/wav"},
                                   WAV, {"type": "done"}]
    with client.websocket_connect(url) as socket:
        history = socket.receive_json()["messages"]
        assert history[0]["parts"][0]["text"] == "hi"
        assert history[1]["parts"][0]["text"] == ("Hello" if fail else "Hello there")
        assert history[1]["status"] == ("interrupted" if fail else "completed")
    with client.websocket_connect(f"/ws/voice?conversation_id={uuid4()}") as socket:
        assert socket.receive_json()["messages"] == []


def test_continue_after_interruption(monkeypatch: pytest.MonkeyPatch, storage: None) -> None:
    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        if message == "first":
            yield "Partial"
            raise TimeoutError()
        assert history[-1]["status"] == "interrupted"
        yield "Continued"

    monkeypatch.setattr(database, "stream_reply", stream)
    with client.websocket_connect("/ws/voice") as socket:
        socket.receive_json()
        for recording in (b"first", b"continue"):
            send_recording(socket, recording)
            events = receive_turn(socket)
            assert any(isinstance(event, dict) and event["type"] ==
                       ("interrupted" if recording == b"first" else "reply_complete") for event in events)


def test_failure_before_first_token_keeps_transcript(monkeypatch: pytest.MonkeyPatch, storage: None) -> None:
    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        if message == "first":
            raise RuntimeError("private provider failure")
        assert history[0]["parts"][0]["text"] == "first"
        assert history[1]["status"] == "interrupted"
        yield "Recovered"

    monkeypatch.setattr(database, "stream_reply", stream)
    cid = str(uuid4())
    with client.websocket_connect(f"/ws/voice?conversation_id={cid}") as socket:
        socket.receive_json()
        send_recording(socket, b"first")
        events = receive_turn(socket)
        assert events[-2]["type"] == "interrupted"
        assert "private" not in str(events)
        history = asyncio.run(database.load_history(cid))
        assert history[0]["parts"][0]["text"] == "first"
        assert history[1]["parts"][0]["text"] == ""
        assert history[1]["status"] == "interrupted"
        send_recording(socket, b"retry")
        assert WAV in receive_turn(socket)


def test_transcription_failure_does_not_save_turn_and_can_retry(monkeypatch: pytest.MonkeyPatch, storage: None) -> None:
    async def transcribe(audio: bytes, media_type: str) -> str:
        if audio == b"bad":
            raise speech.SpeechError(429, "Speech quota reached")
        return "hi"

    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        yield "Hello"

    monkeypatch.setattr(speech, "transcribe", transcribe)
    monkeypatch.setattr(database, "stream_reply", stream)
    cid = str(uuid4())
    with client.websocket_connect(f"/ws/voice?conversation_id={cid}") as socket:
        socket.receive_json()
        send_recording(socket, b"bad")
        assert receive_turn(socket) == [{"type": "transcribing"}, {"type": "error", "error": "Speech quota reached"},
                                        {"type": "done"}]
        assert asyncio.run(database.load_history(cid)) == []
        send_recording(socket, b"good")
        assert WAV in receive_turn(socket)


def test_tts_failure_preserves_completed_text(monkeypatch: pytest.MonkeyPatch, storage: None) -> None:
    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        yield "Completed reply"

    async def fail_tts(text: str) -> bytes:
        raise speech.SpeechError(503, "Speech unavailable")

    monkeypatch.setattr(database, "stream_reply", stream)
    monkeypatch.setattr(speech, "synthesize", fail_tts)
    cid = str(uuid4())
    with client.websocket_connect(f"/ws/voice?conversation_id={cid}") as socket:
        socket.receive_json()
        send_recording(socket, b"hi")
        events = receive_turn(socket)
        assert events[-2] == {"type": "error", "error": "Speech unavailable"}
        assert not any(isinstance(event, dict) and event["type"] == "interrupted" for event in events)
    assert asyncio.run(database.load_history(cid))[-1]["status"] == "completed"


def test_long_reply_is_saved_without_truncation_or_tts(monkeypatch: pytest.MonkeyPatch, storage: None) -> None:
    async def stream(message: str, history: list[dict]) -> AsyncIterator[str]:
        yield "x" * 201

    async def unexpected_tts(text: str) -> bytes:
        pytest.fail("Long replies must not reach TTS")

    monkeypatch.setattr(database, "stream_reply", stream)
    monkeypatch.setattr(speech, "synthesize", unexpected_tts)
    cid = str(uuid4())
    with client.websocket_connect(f"/ws/voice?conversation_id={cid}") as socket:
        socket.receive_json()
        send_recording(socket, b"hi")
        events = receive_turn(socket)
        assert events[-2]["type"] == "notice"
    assert asyncio.run(database.load_history(cid))[-1]["parts"][0]["text"] == "x" * 201


@pytest.mark.parametrize("header", ["not json", "hello", "[]", '{"type":"text","media_type":"audio/wav"}',
                                    '{"type":"recording","media_type":"text/plain"}',
                                    '{"type":"recording","media_type":null}'])
def test_invalid_headers_can_recover(storage: None, header: str) -> None:
    with client.websocket_connect("/ws/voice") as socket:
        socket.receive_json()
        socket.send_text(header)
        assert receive_turn(socket)[0]["type"] == "error"
        send_recording(socket, b"hi")
        assert WAV in receive_turn(socket)


@pytest.mark.parametrize("audio", [b"", b"12345"])
def test_empty_and_oversized_clips(monkeypatch: pytest.MonkeyPatch, storage: None, audio: bytes) -> None:
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 4)
    with client.websocket_connect("/ws/voice") as socket:
        socket.receive_json()
        send_recording(socket, audio)
        assert receive_turn(socket)[0]["type"] == "error"


def test_binary_requires_header_and_duplicate_header_resets(storage: None) -> None:
    with client.websocket_connect("/ws/voice") as socket:
        socket.receive_json()
        socket.send_bytes(b"audio")
        assert receive_turn(socket)[0]["type"] == "error"
        for _ in range(2):
            socket.send_json({"type": "recording", "media_type": "audio/wav"})
        assert receive_turn(socket)[0]["type"] == "error"
        socket.send_bytes(b"audio")
        assert receive_turn(socket)[0]["type"] == "error"


def test_invalid_uuid_and_unavailable_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/voice?conversation_id=invalid"):
            pass
    assert error.value.code == 1008
    monkeypatch.setattr(database, "graph", None)
    with client.websocket_connect("/ws/voice") as socket:
        assert socket.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1011
