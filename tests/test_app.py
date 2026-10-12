"""Call WebSocket contract, with fake engines in place of the real models."""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from tests.test_pipeline import fake_engines, speech_then_pause

START = {"type": "start", "sample_rate": 16000, "encoding": "pcm_f32le", "echo_cancelled": True}


@pytest.fixture
def engines(monkeypatch):
    engines, seen = fake_engines()
    monkeypatch.setattr(main.app.state, "engines", engines, raising=False)
    return seen


def test_call_hears_a_turn_and_replies_with_audio(engines):
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json() == {"type": "ready", "sample_rate": 24000, "encoding": "pcm_f32le"}
        for packet in speech_then_pause():
            socket.send_bytes(packet)
        assert socket.receive_json() == {"type": "transcript", "text": "what's up"}
        kinds, audio = [], 0
        while "reply_end" not in kinds:
            frame = socket.receive()
            if frame.get("bytes"):
                audio += len(frame["bytes"])
            else:
                kinds.append(__import__("json").loads(frame["text"])["type"])
        assert "metrics" in kinds and "text" in kinds
        assert audio == 2 * 6000 * 4
        socket.send_json({"type": "playback", "active": False})
    assert len(engines["replies"]) == 1


def test_invalid_start_and_audio_are_rejected(engines):
    client = TestClient(main.app)
    with client.websocket_connect("/ws/voice") as socket:
        socket.send_json({**START, "sample_rate": 48000})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008
    with client.websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        socket.receive_json()
        socket.send_bytes(np.array([np.inf], "<f4").tobytes())
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008


def test_missing_models_fail_clearly(monkeypatch):
    monkeypatch.delattr(main.app.state, "engines", raising=False)
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json() == {"type": "error", "error": "Voice models are not loaded."}


def test_call_ui_assets_are_served():
    client = TestClient(main.app)
    assert "Start call" in client.get("/").text
    source = client.get("/static/voice.js").text
    assert "AudioWorkletNode" in source and '"transcript"' in source
    assert client.get("/static/microphone-worklet.js").status_code == 200
    assert client.get("/static/audio-player.js").status_code == 200
