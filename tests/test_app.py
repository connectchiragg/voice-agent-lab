"""Call WebSocket contract, with fake engines in place of Deepgram."""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from tests.test_pipeline import FakeListener, make_engines


class TalkingListener(FakeListener):
    """Hears one turn: a partial after the first packet, the end of turn after the third."""
    async def send(self, pcm, arrived=None):
        await super().send(pcm, arrived)
        if len(self.audio) == 1:
            await self.emit("StartOfTurn", "what's")
        elif len(self.audio) == 3:
            await self.emit("EndOfTurn", "what's up", speech_ended=arrived)


def speech_packets():
    return [np.full(1600, 0.1, "<f4").tobytes() for _ in range(3)]

START = {"type": "start", "sample_rate": 16000, "encoding": "pcm_f32le", "echo_cancelled": True}


@pytest.fixture
def engines(monkeypatch):
    engines, seen = make_engines()
    engines.new_listener = TalkingListener
    monkeypatch.setattr(main.app.state, "engines", engines, raising=False)
    return seen


def test_call_hears_a_turn_and_replies_with_audio(engines):
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json() == {"type": "ready", "sample_rate": 24000, "encoding": "pcm_f32le"}
        for packet in speech_packets():
            socket.send_bytes(packet)
        assert socket.receive_json() == {"type": "partial", "text": "what's"}
        assert socket.receive_json() == {"type": "transcript", "text": "what's up"}
        kinds, audio = [], 0
        while "reply_end" not in kinds:
            frame = socket.receive()
            if frame.get("bytes"):
                audio += len(frame["bytes"])
            else:
                kinds.append(__import__("json").loads(frame["text"])["type"])
        assert "metrics" in kinds and kinds.count("caption") == 2
        assert audio == 2 * 6000 * 4  # Two segments of fake Aura audio, as float32.
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


def test_missing_deepgram_key_fails_clearly(monkeypatch):
    monkeypatch.delattr(main.app.state, "engines", raising=False)
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json() == {"type": "error", "error": "Speech services are not configured."}


def test_refused_deepgram_connection_fails_clearly(engines, monkeypatch):
    class RefusedListener(FakeListener):
        async def start(self):
            raise OSError("402")

    main.app.state.engines.new_listener = RefusedListener
    with TestClient(main.app).websocket_connect("/ws/voice") as socket:
        socket.send_json(START)
        assert socket.receive_json() == {"type": "error",
                                         "error": "The speech service is unavailable. Try again later."}


def test_engines_need_a_deepgram_key(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    assert main.load_engines() is None
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    engines = main.load_engines()
    assert engines is not None and engines.output_sample_rate == 24000


def test_call_ui_assets_are_served():
    client = TestClient(main.app)
    assert "Start call" in client.get("/").text
    source = client.get("/static/voice.js").text
    assert "AudioWorkletNode" in source and '"transcript"' in source
    assert client.get("/static/microphone-worklet.js").status_code == 200
    assert client.get("/static/audio-player.js").status_code == 200
    # WebRTC is the default client; ?transport=ws keeps the WebSocket one.
    assert 'transport === "ws"' in client.get("/").text
    webrtc_source = client.get("/static/voice-webrtc.js").text
    assert "RTCPeerConnection" in webrtc_source and '"/webrtc/offer"' in webrtc_source
    assert client.get("/static/playout-delay.js").status_code == 200


def test_health_reports_whether_speech_is_configured(engines):
    assert TestClient(main.app).get("/healthz").json() == {"ok": True, "speech": True}
