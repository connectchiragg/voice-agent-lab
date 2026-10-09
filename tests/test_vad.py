import numpy as np
import pytest

from app.vad import SpeechDiagnostics


class FakeGate:
    def __init__(self, score: float = 0.8) -> None:
        self.probability = score
        self.calls = 0
        self.resets = 0

    def score(self, samples: np.ndarray) -> float:
        assert samples.shape == (512,)
        self.calls += 1
        return self.probability

    def reset(self) -> None:
        self.resets += 1


def pcm(count: int) -> bytes:
    return np.zeros(count, dtype="<f4").tobytes()


def test_fragmented_buffer_and_gate() -> None:
    gate = FakeGate()
    observer = SpeechDiagnostics(gate, echo_cancelled=True)
    assert observer.feed(pcm(100), assistant_speaking=True) == []
    reports = observer.feed(pcm(1948), assistant_speaking=True)
    assert gate.calls == 4
    assert len(reports) == 1
    assert reports[0].speech_detected
    assert reports[0].assistant_speaking
    assert reports[0].classifier_status == "disabled_missing_preprocessing"
    assert reports[0].inference_ms >= 0


def test_silence_and_bounded_window() -> None:
    observer = SpeechDiagnostics(FakeGate(0.1), echo_cancelled=False)
    reports = []
    for _ in range(4):
        reports.extend(observer.feed(pcm(16000), assistant_speaking=False))
    assert len(observer.window) == 32000
    assert len(observer.pending) < 512
    assert not any(report.speech_detected for report in reports)
    assert reports[-1].window_ready
    assert not reports[-1].echo_cancelled
    assert len(reports) == 40


def test_cleanup_and_separate_sessions() -> None:
    gate = FakeGate()
    first = SpeechDiagnostics(gate, echo_cancelled=True)
    second = SpeechDiagnostics(FakeGate(), echo_cancelled=True)
    first.feed(pcm(2000), assistant_speaking=True)
    assert second.samples_seen == 0
    first.close()
    assert len(first.window) == len(first.pending) == 0
    assert gate.resets == 1
    with pytest.raises(ValueError):
        first.feed(pcm(512), assistant_speaking=False)


@pytest.mark.parametrize("audio", [b"", b"x", pcm(16001), np.array([np.nan], dtype="<f4").tobytes(), np.array([2], dtype="<f4").tobytes()])
def test_invalid_pcm(audio: bytes) -> None:
    with pytest.raises(ValueError):
        SpeechDiagnostics(FakeGate(), echo_cancelled=True).feed(audio, assistant_speaking=False)


def test_diagnostic_socket_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    from app import main, vad
    gate = FakeGate()
    monkeypatch.setattr(vad, "SileroGate", lambda path: gate)
    with TestClient(main.app).websocket_connect("/ws/diagnostics") as socket:
        socket.send_json({"type": "settings", "sample_rate": 16000,
                          "encoding": "pcm_f32le", "echo_cancelled": True,
                          "assistant_speaking": True})
        assert socket.receive_json()["type"] == "diagnostics_ready"
        socket.send_bytes(pcm(2048))
        report = socket.receive_json()
        assert report["speech_detected"]
        assert report["assistant_speaking"]
        assert report["classifier_status"] == "disabled_missing_preprocessing"
    assert gate.resets == 1
