"""CPU VAP observations; never controls playback or conversation history."""
import os
import queue
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
import numpy as np
from numpy.typing import NDArray

Samples = NDArray[np.float32]
FRAME = 1600

@dataclass(frozen=True)
class SpeechPrediction:
    vad: float
    p_now: float
    p_future: float

class SpeechModel(Protocol):
    def predict(self, samples: Samples) -> SpeechPrediction: ...
    def reset(self) -> None: ...

class VapModel:
    def __init__(self) -> None:
        from maai import Maai, MaaiInput
        import torch
        checkpoint = Path(os.environ["VAP_MODEL_PATH"]).expanduser()
        encoder = Path(os.environ["VAP_CPC_PATH"]).expanduser()
        if not checkpoint.is_file() or not encoder.is_file():
            raise ValueError("Download VAP and CPC checkpoints first")
        torch.set_num_threads(2)
        self.model = Maai(mode="vap", lang="en", audio_ch1=MaaiInput.Chunk(),
                          frame_rate=10, model_type="normal", mc=False,
                          device="cpu", local_model=str(checkpoint), cpc_model=str(encoder))
        if not self.model.is_mono:
            raise ValueError("Dedicated single-channel VAP required")

    def predict(self, samples: Samples) -> SpeechPrediction:
        self.model.process(samples, np.zeros_like(samples))
        result = self.model.result_dict_queue.get_nowait()
        return SpeechPrediction(*(float(np.asarray(result[key]).squeeze())
                                  for key in ("vad", "p_now", "p_future")))

    def reset(self) -> None:
        self.model.reset_runtime_state()
        while True:
            try:
                self.model.result_dict_queue.get_nowait()
            except queue.Empty:
                break

@dataclass(frozen=True)
class SpeechDiagnostic:
    type: str
    audio_ms: float
    speech_probability: float
    speech_detected: bool
    assistant_speaking: bool
    echo_cancelled: bool
    inference_ms: float
    p_now: float
    p_future: float
    model: str = "vap_mono"
    classifier_status: str = "prediction_only"

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

class SpeechDiagnostics:
    """Continuous input including silence; 0.5 is an uncalibrated VAD threshold."""
    def __init__(self, model: SpeechModel, *, echo_cancelled: bool) -> None:
        self.model = model
        self.echo_cancelled = echo_cancelled
        self.pending = np.empty(0, dtype=np.float32)
        self.samples_seen = 0
        self.closed = False

    def feed(self, audio: bytes, *, assistant_speaking: bool) -> list[SpeechDiagnostic]:
        if self.closed:
            raise ValueError("Diagnostics session is closed")
        if not audio or len(audio) % 4 or len(audio) > 16000 * 4:
            raise ValueError("Send up to one second of 16 kHz float32 PCM")
        samples = np.frombuffer(audio, dtype="<f4")
        if not np.isfinite(samples).all() or np.any(np.abs(samples) > 1):
            raise ValueError("PCM must be finite and within [-1, 1]")
        self.pending = np.concatenate((self.pending, samples))
        reports = []
        while len(self.pending) >= FRAME:
            frame, self.pending = self.pending[:FRAME], self.pending[FRAME:]
            start = time.perf_counter()
            prediction = self.model.predict(frame)
            elapsed = (time.perf_counter() - start) * 1000
            self.samples_seen += FRAME
            reports.append(SpeechDiagnostic("speech_diagnostic", self.samples_seen / 16,
                prediction.vad, prediction.vad >= 0.5, assistant_speaking,
                self.echo_cancelled, elapsed, prediction.p_now, prediction.p_future))
        return reports

    def close(self) -> None:
        self.pending = np.empty(0, dtype=np.float32)
        self.model.reset()
        self.closed = True
