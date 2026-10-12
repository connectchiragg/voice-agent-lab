"""Kokoro-82M (ONNX, Apache-2.0) speech synthesis, 24 kHz mono float32."""
import re
import threading

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro
from numpy.typing import NDArray
from phonemizer.backend import EspeakBackend

OUTPUT_SAMPLE_RATE = 24000
# Markdown and symbols that a voice should not read aloud.
UNSPOKEN = re.compile(r"[*_#`~>|\[\]]+")


class KokoroVoice:
    def __init__(self, model_path: str, voices_path: str, voice: str) -> None:
        # Default threading: unlike the tiny VAD/turn models, Kokoro benefits from it.
        session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, voices_path)
        self.voice = voice
        # kokoro-onnx builds a new espeak backend per call (~600 ms on an M2).
        # One reused backend yields identical phonemes in about a millisecond.
        self.phonemizer = EspeakBackend("en-us", preserve_punctuation=True, with_stress=True)
        self.lock = threading.Lock()  # espeak is not safe to share across threads.

    def phonemes(self, text: str) -> str:
        raw = self.phonemizer.phonemize([text.strip()])[0]
        return "".join(char for char in raw if char in self.kokoro.tokenizer.vocab).strip()

    def synthesize(self, text: str) -> NDArray[np.float32]:
        text = UNSPOKEN.sub("", text).strip()
        if not text:
            return np.empty(0, dtype=np.float32)
        with self.lock:
            phonemes = self.phonemes(text)
            if not phonemes:
                return np.empty(0, dtype=np.float32)
            audio, sample_rate = self.kokoro.create(phonemes, voice=self.voice, is_phonemes=True)
        if sample_rate != OUTPUT_SAMPLE_RATE:
            raise ValueError("Unexpected Kokoro sample rate")
        return audio.astype(np.float32)
