"""Continuous call contract, concurrent uplink/downlink and connection cleanup."""
import asyncio
import struct
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main, minicpm, vad

START = {"type": "start", "sample_rate": 16000, "encoding": "pcm_f32le", "echo_cancelled": True}
PCM = struct.pack("<f", 0.1) * 1600


class FakeGate:
    def predict(self, samples):
        return vad.SpeechPrediction(0.9, 0.8, 0.7)
    def reset(self):
        pass


@pytest.fixture
def provider(monkeypatch):
    class Session:
        def __init__(self):
            self.inputs = asyncio.Queue()
            self.closed = False
            self.received = []
        async def send_audio(self, audio):
            minicpm.validate_pcm(audio, minicpm.MAX_INPUT_BYTES)
            self.received.append(audio)
            await self.inputs.put(audio)
        async def outputs(self):
            # Output begins without waiting for microphone input or a turn.
            yield minicpm.MiniCPMOutput(minicpm.OutputKind.AUDIO, audio=PCM)
            while True:
                await self.inputs.get()
                yield minicpm.MiniCPMOutput(minicpm.OutputKind.TEXT, text="heard")
                yield minicpm.MiniCPMOutput(minicpm.OutputKind.AUDIO, audio=PCM)
    session = Session()
    @asynccontextmanager
    async def open_session(settings, prompt):
        try:
            yield session
        finally:
            session.closed = True
    monkeypatch.setattr(minicpm, "open_session", open_session)
    monkeypatch.setattr(minicpm.MiniCPMSettings, "from_env", lambda: object())
    monkeypatch.setattr(vad, "VapModel", lambda: FakeGate())
    return session


def test_continuous_bidirectional_call(provider):
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json()["type"] == "ready"
        assert socket.receive_bytes() == PCM
        socket.send_json({"type": "playback", "active": True})
        # User audio continues while assistant playback is active.
        for _ in range(2):
            socket.send_bytes(PCM)
            seen = set()
            while "audio" not in seen or "text" not in seen:
                frame = socket.receive()
                if frame.get("bytes"):
                    assert frame["bytes"] == PCM
                    seen.add("audio")
                else:
                    import json
                    message = json.loads(frame["text"])
                    seen.add(message["type"])
                    assert message["type"] in {"text", "speech_diagnostic"}
    assert provider.received == [PCM, PCM]
    assert provider.closed


def test_invalid_start_and_audio(provider):
    client = TestClient(main.app)
    with client.websocket_connect("/ws/voice") as socket:
        socket.send_json({**START, "sample_rate": 48000})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008
    with client.websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        socket.receive_json()
        socket.receive_bytes()
        socket.send_bytes(b"x")
        assert socket.receive_json()["type"] == "error"
    assert provider.closed


def test_call_ui_and_no_database_requirement():
    client = TestClient(main.app)
    assert "Start call" in client.get("/").text
    source = client.get("/static/voice.js").text
    assert "MediaRecorder" not in source
    assert "AudioWorkletNode" in source
    assert "busy" not in source
    assert client.get("/static/microphone-worklet.js").status_code == 200
    assert client.post("/api/transcribe").status_code == 404


def test_unavailable_provider_is_safe(provider, monkeypatch):
    def fail_settings():
        raise minicpm.MiniCPMError("private token or URL")
    monkeypatch.setattr(minicpm.MiniCPMSettings, "from_env", fail_settings)
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        message = socket.receive_json()
        assert message["type"] == "error"
        assert "private" not in str(message)


def test_vap_unavailable_does_not_break_audio(provider, monkeypatch):
    def unavailable():
        raise RuntimeError("model missing")
    monkeypatch.setattr(vad, "VapModel", unavailable)
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json()["type"] == "ready"
        assert socket.receive_bytes() == PCM
        socket.send_bytes(PCM)
        assert socket.receive_json()["type"] == "text"
        assert socket.receive_bytes() == PCM
    assert provider.closed


def test_vap_inference_failure_does_not_break_call(provider, monkeypatch):
    class BrokenModel(FakeGate):
        def predict(self, samples):
            raise RuntimeError("inference failed")
    monkeypatch.setattr(vad, "VapModel", BrokenModel)
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        socket.receive_json()
        socket.receive_bytes()
        for _ in range(2):
            socket.send_bytes(PCM)
            assert socket.receive_json()["type"] == "text"
            assert socket.receive_bytes() == PCM
    assert provider.received == [PCM, PCM]
    assert provider.closed
