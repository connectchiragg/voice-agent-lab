"""Local streaming speech-to-text: NVIDIA Nemotron Speech Streaming EN 0.6B via sherpa-onnx.

Chosen by benchmark (see README): lowest word error on Indian-accented English
(Svarah), 8 kHz phone audio and noise among the open streaming models tried.

One recognizer (the model weights) is shared by every call; each utterance
gets its own stream. Audio is only fed while the turn detector hears speech,
so the model costs nothing during silence or while the assistant talks.
"""
import threading
import time

import numpy as np
import sherpa_onnx
from numpy.typing import NDArray

SAMPLE_RATE = 16000
# Measured: the model drops first words without lead-in, and needs a short tail
# to emit the last ones. 0.32 s lead / 0.5 s tail cut word errors by about a third.
LEAD_SECONDS = 0.32
TAIL_SECONDS = 0.5
MODEL_DIR = "sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25"


def load_recognizer(model_dir: str, threads: int = 2) -> sherpa_onnx.OnlineRecognizer:
    path = model_dir.rstrip("/") + "/"
    # NeMo transducers only support greedy search in sherpa-onnx (no hotwords).
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        path + "tokens.txt", path + "encoder.int8.onnx", path + "decoder.int8.onnx",
        path + "joiner.int8.onnx", num_threads=threads, model_type="nemo_transducer")


class StreamingTranscriber:
    """Turns one utterance at a time into live partial text and a final transcript.

    Implements the TurnDetector listener hooks; all methods run on the
    detector's worker thread, never on the event loop.
    """

    def __init__(self, recognizer: sherpa_onnx.OnlineRecognizer, lock: threading.Lock) -> None:
        self.recognizer = recognizer
        self.lock = lock  # Shared with every call that uses this recognizer.
        self.stream: sherpa_onnx.OnlineStream | None = None
        self.text = ""
        self.partial: str | None = None  # Newest unseen partial text, if any.
        self.final_ms = 0.0

    def _decode(self, samples: NDArray[np.float32]) -> None:
        assert self.stream is not None
        with self.lock:
            self.stream.accept_waveform(SAMPLE_RATE, samples)
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            text = self.recognizer.get_result(self.stream).strip()
        if text != self.text:
            self.text = self.partial = text

    def on_speech_start(self, preroll: NDArray[np.float32]) -> None:
        with self.lock:
            self.stream = self.recognizer.create_stream()
        self.text, self.partial = "", None
        self._decode(np.concatenate((np.zeros(int(SAMPLE_RATE * LEAD_SECONDS), np.float32), preroll)))

    def on_speech(self, samples: NDArray[np.float32]) -> None:
        if self.stream is not None:
            self._decode(samples)

    def on_speech_end(self) -> str:
        """Flush the stream and return the final transcript."""
        if self.stream is None:
            return ""
        started = time.perf_counter()
        self._decode(np.zeros(int(SAMPLE_RATE * TAIL_SECONDS), np.float32))
        with self.lock:
            self.stream.input_finished()
            while self.recognizer.is_ready(self.stream):
                self.recognizer.decode_stream(self.stream)
            text = self.recognizer.get_result(self.stream).strip()
        self.final_ms = (time.perf_counter() - started) * 1000
        self.stream, self.text, self.partial = None, text, None
        return text

    def on_discard(self) -> None:
        self.stream, self.text, self.partial = None, "", None

    def take_partial(self) -> str | None:
        partial, self.partial = self.partial, None
        return partial
