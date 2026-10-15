"""G.711 µ-law (PCMU), the telephone network's 8 kHz codec, to and from float32 PCM.

Decoding is exact (every code maps to one 14-bit value); encoding is the
standard ITU-T G.711 quantiser, the same step the carrier applies anyway.
"""
import numpy as np
from numpy.typing import NDArray

Samples = NDArray[np.float32]
DECODE_BIAS = 0x84
# Encoding works on 14-bit magnitudes, as in the ITU reference (and CPython's audioop).
BIAS = 0x21
CLIP = 8159
SEGMENT_ENDS = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])


def _decode_table() -> NDArray[np.int16]:
    codes = ~np.arange(256, dtype=np.int32) & 0xFF
    exponent = (codes >> 4) & 0x07
    magnitude = (((codes & 0x0F) << 3) + DECODE_BIAS) << exponent
    values = np.where(codes & 0x80, DECODE_BIAS - magnitude, magnitude - DECODE_BIAS)
    return values.astype(np.int16)


DECODE = _decode_table()


def decode(payload: bytes) -> Samples:
    """µ-law bytes to float32 in [-1, 1)."""
    return DECODE[np.frombuffer(payload, dtype=np.uint8)].astype(np.float32) / 32768


def encode(samples: Samples) -> bytes:
    """float32 in [-1, 1] to µ-law bytes, one per sample (14-bit G.711 reference quantiser)."""
    pcm = np.clip(np.round(np.asarray(samples, dtype=np.float32) * 32768), -32768, 32767)
    value = pcm.astype(np.int32) >> 2
    mask = np.where(value < 0, 0x7F, 0xFF)
    value = np.minimum(np.abs(value), CLIP) + BIAS
    segment = np.searchsorted(SEGMENT_ENDS, value)  # 8: beyond the last segment.
    code = np.where(segment > 7, 0x7F, (segment << 4) | ((value >> (segment + 1)) & 0x0F))
    return (code ^ mask).astype(np.uint8).tobytes()
