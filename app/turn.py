"""Smart Turn v3 (ONNX, BSD-2): has the speaker finished their turn?

The model reads Whisper log-mel features of the last 8 seconds of the turn.
The features are computed here in numpy, matching transformers'
WhisperFeatureExtractor(chunk_length=8), so no torch/transformers dependency.
"""
import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

SAMPLE_RATE = 16000
WINDOW = 8 * SAMPLE_RATE
N_FFT = 400
HOP = 160
N_MELS = 80


def _hz_to_mel(hz: NDArray[np.float64]) -> NDArray[np.float64]:
    # Slaney scale: linear below 1 kHz, logarithmic above.
    mel = hz / (200 / 3)
    log_region = hz >= 1000
    mel[log_region] = 15 + np.log(hz[log_region] / 1000) / (np.log(6.4) / 27)
    return mel


def _mel_to_hz(mel: NDArray[np.float64]) -> NDArray[np.float64]:
    hz = mel * (200 / 3)
    log_region = mel >= 15
    hz[log_region] = 1000 * np.exp((np.log(6.4) / 27) * (mel[log_region] - 15))
    return hz


def mel_filters() -> NDArray[np.float64]:
    """Slaney-normalised triangular filters, shape (N_MELS, N_FFT // 2 + 1)."""
    fft_hz = np.linspace(0, SAMPLE_RATE / 2, N_FFT // 2 + 1)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(np.array([0.0]))[0],
                                   _hz_to_mel(np.array([8000.0]))[0], N_MELS + 2))
    widths = np.diff(edges)
    ramps = edges[:, None] - fft_hz[None, :]
    rising = -ramps[:-2] / widths[:-1, None]
    falling = ramps[2:] / widths[1:, None]
    filters = np.maximum(0, np.minimum(rising, falling))
    return filters * (2 / (edges[2:] - edges[:-2]))[:, None]


MEL_FILTERS = mel_filters()
HANN = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)  # periodic


def log_mel(samples: NDArray[np.float32]) -> NDArray[np.float32]:
    """Last 8 s (left-padded with silence) -> normalised log-mel, shape (80, 800)."""
    audio = samples[-WINDOW:].astype(np.float64)
    audio = np.pad(audio, (WINDOW - len(audio), 0))
    audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
    padded = np.pad(audio, N_FFT // 2, mode="reflect")
    starts = np.arange(0, len(padded) - N_FFT + 1, HOP)
    frames = padded[starts[:, None] + np.arange(N_FFT)] * HANN
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    mel = np.log10(np.maximum(MEL_FILTERS @ power.T, 1e-10))[:, :-1]
    mel = np.maximum(mel, mel.max() - 8)
    return ((mel + 4) / 4).astype(np.float32)


class SmartTurn:
    def __init__(self, session: ort.InferenceSession) -> None:
        self.session = session

    def completion_probability(self, samples: NDArray[np.float32]) -> float:
        features = log_mel(samples)[None]
        (output,) = self.session.run(None, {"input_features": features})
        return float(output.reshape(-1)[0])
