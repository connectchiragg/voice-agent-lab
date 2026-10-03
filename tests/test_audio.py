"""WAV headers and network chunks are allowed to end at different boundaries."""

import struct

import pytest

from app.audio import AudioDecodeError, STREAMING_SIZE, WavStreamDecoder
from app.events import AudioFormat, EventType, VoiceEvent


def wav(pcm: bytes, channels: int = 1, streaming: bool = False, metadata: bytes = b"") -> bytes:
    fmt = struct.pack("<HHIIHH", 1, channels, 24000, 24000 * channels * 2, channels * 2, 16)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt + metadata
    body += b"data" + struct.pack("<I", STREAMING_SIZE if streaming else len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 3, 47, 4096])
def test_arbitrary_network_boundaries(channels: int, streaming: bool, chunk_size: int) -> None:
    pcm = b"\x00\x80\xff\x7f" * 20
    metadata = b"JUNK" + struct.pack("<I", 3) + b"abc\x00"
    data = wav(pcm, channels, streaming, metadata)
    decoder = WavStreamDecoder()
    chunks = [decoder.feed(data[i:i + chunk_size]) for i in range(0, len(data), chunk_size)]
    decoder.finish()
    assert decoder.format == AudioFormat(sample_rate=24000, channels=channels)
    assert b"".join(chunks) == pcm
    assert all(len(chunk) % (channels * 2) == 0 for chunk in chunks)


def test_ignores_metadata_after_finite_audio() -> None:
    decoder = WavStreamDecoder()
    assert decoder.feed(wav(b"\x01\x00") + b"LISTextra") == b"\x01\x00"
    assert decoder.feed(b"more metadata") == b""
    decoder.finish()


def test_groq_style_streaming_header_with_extended_format() -> None:
    data = bytearray(wav(b"\x01\x00", streaming=True))
    struct.pack_into("<I", data, 4, STREAMING_SIZE)
    struct.pack_into("<I", data, 16, 18)
    data[36:36] = b"\x00\x00"  # PCM format extension size.
    decoder = WavStreamDecoder()
    assert decoder.feed(bytes(data[:45])) == b""
    assert decoder.feed(bytes(data[45:])) == b"\x01\x00"
    decoder.finish()


@pytest.mark.parametrize("data", [b"", b"RIFF", wav(b""), wav(b"\x01\x00")[:-1],
                                       wav(b"\x01\x00\x02\x00")[:-2]])
def test_empty_or_truncated_stream(data: bytes) -> None:
    decoder = WavStreamDecoder()
    decoder.feed(data)
    with pytest.raises(AudioDecodeError):
        decoder.finish()


@pytest.mark.parametrize("offset, value", [(20, 3), (22, 3), (32, 8), (34, 32)])
def test_unsupported_format(offset: int, value: int) -> None:
    data = bytearray(wav(b"\x01\x00"))
    struct.pack_into("<H", data, offset, value)
    with pytest.raises(AudioDecodeError):
        WavStreamDecoder().feed(bytes(data))


def test_invalid_headers() -> None:
    for data in (b"not a WAV file", b"RIFF\x00\x00\x00\x00WAVEdata\x02\x00\x00\x00",
                 b"RIFF\x00\x00\x00\x00WAVEJUNK" + struct.pack("<I", 100000)):
        with pytest.raises(AudioDecodeError):
            WavStreamDecoder().feed(data)


def test_event_payload_contains_format_without_empty_fields() -> None:
    assert VoiceEvent(EventType.AUDIO_START, format=AudioFormat(24000, 1)).to_payload() == {
        "type": "audio_start", "format": {"sample_rate": 24000, "channels": 1, "encoding": "pcm_s16le"},
    }
