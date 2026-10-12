"""Silero VAD (ONNX, MIT): speech probability for each 32 ms of 16 kHz audio."""
import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

CHUNK = 512  # Silero's fixed window at 16 kHz.
CONTEXT = 64  # Trailing samples of the previous chunk, prepended to the next.
SAMPLE_RATE = np.array(16000, dtype=np.int64)


def load_session(path: str) -> ort.InferenceSession:
    """Single-threaded CPU session; these models are tiny and run per call."""
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(path, options, providers=["CPUExecutionProvider"])


class SileroVad:
    """Stateful per call: the session is shared, the recurrent state is not."""

    def __init__(self, session: ort.InferenceSession) -> None:
        self.session = session
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(CONTEXT, dtype=np.float32)

    def probability(self, chunk: NDArray[np.float32]) -> float:
        if len(chunk) != CHUNK:
            raise ValueError(f"Silero expects {CHUNK} samples")
        model_input = np.concatenate((self.context, chunk))[None, :]
        output, self.state = self.session.run(None, {
            "input": model_input, "state": self.state, "sr": SAMPLE_RATE})
        self.context = chunk[-CONTEXT:]
        return float(output[0, 0])
