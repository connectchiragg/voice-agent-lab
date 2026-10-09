"""CPU-only, observation-only speech gate. No conversation or playback controls.

Silero state/context contract follows snakers4/silero-vad utils_vad.py at
1e261b036686cd0017d500ee96acd1c4ba572a9d (MIT licensed).
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

Samples = NDArray[np.float32]
SAMPLE_RATE = 16000
FRAME = 512
WINDOW = 32000
HOP = 1600


class SpeechGate(Protocol):
    def score(self, samples: Samples) -> float: ...
    def reset(self) -> None: ...


class SileroGate:
    def __init__(self, model_path: str) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(model_path, sess_options=options,
                                            providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, 64), dtype=np.float32)

    def score(self, samples: Samples) -> float:
        if samples.shape != (FRAME,):
            raise ValueError("Silero requires 512 samples at 16 kHz")
        audio = np.concatenate((self.context, samples.reshape(1, -1)), axis=1)
        output, self.state = self.session.run(None, {
            "input": audio, "state": self.state,
            "sr": np.array(SAMPLE_RATE, dtype=np.int64),
        })
        self.context = audio[:, -64:].copy()
        return float(output[0, 0])


@dataclass(frozen=True)
class SpeechDiagnostic:
    type: str
    audio_ms: float
    speech_probability: float
    speech_detected: bool
    assistant_speaking: bool
    echo_cancelled: bool
    inference_ms: float
    window_ready: bool
    classifier_status: str = "disabled_missing_preprocessing"

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


class SpeechDiagnostics:
    """Own one instance per connection; retain audio in memory only.

    0.5 is a provisional Silero gate, not an interruption threshold.
    Echo cancellation is reported by the client, not performed here.
    """

    def __init__(self, gate: SpeechGate, *, echo_cancelled: bool,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.gate = gate
        self.echo_cancelled = echo_cancelled
        self.clock = clock
        self.pending = np.empty(0, dtype=np.float32)
        self.window = np.empty(0, dtype=np.float32)
        self.samples_seen = 0
        self.next_report = HOP
        self.scores: list[float] = []
        self.elapsed_ms = 0.0
        self.closed = False

    def feed(self, audio: bytes, *, assistant_speaking: bool) -> list[SpeechDiagnostic]:
        if self.closed:
            raise ValueError("Diagnostics session is closed")
        if not audio or len(audio) % 4 or len(audio) > SAMPLE_RATE * 4:
            raise ValueError("Send up to one second of mono float32 PCM at 16 kHz")
        samples = np.frombuffer(audio, dtype="<f4")
        if not np.isfinite(samples).all() or np.any(np.abs(samples) > 1):
            raise ValueError("PCM samples must be finite and within [-1, 1]")
        self.pending = np.concatenate((self.pending, samples))
        reports: list[SpeechDiagnostic] = []
        while len(self.pending) >= FRAME:
            frame, self.pending = self.pending[:FRAME], self.pending[FRAME:]
            start = self.clock()
            probability = self.gate.score(frame)
            self.elapsed_ms += (self.clock() - start) * 1000
            self.scores.append(probability)
            self.window = np.concatenate((self.window, frame))[-WINDOW:]
            self.samples_seen += FRAME
            if self.samples_seen >= self.next_report:
                score = max(self.scores)
                reports.append(SpeechDiagnostic(
                    "speech_diagnostic", self.samples_seen / 16, score, score >= 0.5,
                    assistant_speaking, self.echo_cancelled, self.elapsed_ms,
                    len(self.window) == WINDOW,
                ))
                self.scores.clear()
                self.elapsed_ms = 0
                self.next_report += HOP
        return reports

    def close(self) -> None:
        self.pending = np.empty(0, dtype=np.float32)
        self.window = np.empty(0, dtype=np.float32)
        self.scores.clear()
        self.gate.reset()
        self.closed = True
