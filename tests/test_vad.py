import numpy as np
import pytest
from app.vad import SpeechDiagnostics, SpeechPrediction

class FakeModel:
    def __init__(self, score=0.8):
        self.score = score
        self.calls = self.resets = 0
    def predict(self, samples):
        assert samples.shape == (1600,)
        self.calls += 1
        return SpeechPrediction(self.score, 0.6, 0.4)
    def reset(self):
        self.resets += 1

def pcm(count):
    return np.zeros(count, dtype='<f4').tobytes()

def test_fragments_and_predictions():
    model = FakeModel()
    observer = SpeechDiagnostics(model, echo_cancelled=True)
    assert observer.feed(pcm(100), assistant_speaking=True) == []
    report, = observer.feed(pcm(1500), assistant_speaking=True)
    assert model.calls == 1
    assert report.p_now == 0.6 and report.p_future == 0.4
    assert report.speech_detected and report.assistant_speaking
    assert report.inference_ms >= 0

def test_silence_is_processed_continuously():
    model = FakeModel(0.1)
    observer = SpeechDiagnostics(model, echo_cancelled=False)
    reports = observer.feed(pcm(16000), assistant_speaking=False)
    assert len(reports) == model.calls == 10
    assert not any(r.speech_detected for r in reports)
    assert len(observer.pending) == 0

def test_cleanup_and_session_isolation():
    model = FakeModel()
    first = SpeechDiagnostics(model, echo_cancelled=True)
    second = SpeechDiagnostics(FakeModel(), echo_cancelled=True)
    first.feed(pcm(1700), assistant_speaking=True)
    assert second.samples_seen == 0
    first.close()
    assert len(first.pending) == 0 and model.resets == 1
    with pytest.raises(ValueError):
        first.feed(pcm(1600), assistant_speaking=False)

@pytest.mark.parametrize('audio', [b'', b'x', pcm(16001), np.array([np.nan], dtype='<f4').tobytes(), np.array([2], dtype='<f4').tobytes()])
def test_invalid_pcm(audio):
    with pytest.raises(ValueError):
        SpeechDiagnostics(FakeModel(), echo_cancelled=True).feed(audio, assistant_speaking=False)

def test_diagnostic_socket(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, vad
    model = FakeModel()
    monkeypatch.setattr(vad, 'VapModel', lambda: model)
    with TestClient(main.app).websocket_connect('/ws/diagnostics') as socket:
        socket.send_json({'type':'settings','sample_rate':16000,'encoding':'pcm_f32le',
                          'echo_cancelled':True,'assistant_speaking':True})
        assert socket.receive_json()['model'] == 'vap_mono'
        socket.send_bytes(pcm(1600))
        assert socket.receive_json()['p_now'] == 0.6
    assert model.resets == 1
