"""Call handshake contract while the voice pipeline is rebuilt."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main

START = {"type": "start", "sample_rate": 16000, "encoding": "pcm_f32le", "echo_cancelled": True}


def test_valid_start_reports_pipeline_unavailable():
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        message = socket.receive_json()
        assert message == {"type": "error", "error": "Voice pipeline not built yet."}


def test_invalid_start_is_rejected():
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json({**START, "sample_rate": 48000})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008


def test_call_ui_assets_are_served():
    client = TestClient(main.app)
    assert "Start call" in client.get("/").text
    source = client.get("/static/voice.js").text
    assert "AudioWorkletNode" in source
    assert "speech_diagnostic" not in source
    assert client.get("/static/microphone-worklet.js").status_code == 200
    assert client.get("/static/audio-player.js").status_code == 200
