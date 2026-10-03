"""Decode a streamed PCM WAV without waiting for the complete audio file."""

import struct
from dataclasses import dataclass, field

from app.events import AudioFormat

MAX_WAV_HEADER_BYTES = 64 * 1024
STREAMING_SIZE = 0xFFFFFFFF


class AudioDecodeError(ValueError):
    pass


@dataclass
class WavStreamDecoder:
    format: AudioFormat | None = field(default=None, init=False)
    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _header_offset: int = field(default=12, init=False)
    _data_started: bool = field(default=False, init=False)
    _remaining_bytes: int | None = field(default=None, init=False)
    _received_bytes: int = field(default=0, init=False)

    def feed(self, chunk: bytes) -> bytes:
        self._buffer.extend(chunk)
        if not self._data_started:
            self._read_header()
            if not self._data_started:
                return b""
        assert self.format is not None
        if self._remaining_bytes is not None:
            # Ignore any WAV metadata after the declared audio data.
            del self._buffer[self._remaining_bytes:]
        count = len(self._buffer) // self.format.frame_bytes * self.format.frame_bytes
        pcm = bytes(self._buffer[:count])
        del self._buffer[:count]
        self._received_bytes += count
        if self._remaining_bytes is not None:
            self._remaining_bytes -= count
        return pcm

    def _read_header(self) -> None:
        if len(self._buffer) < 12:
            return
        if self._buffer[:4] != b"RIFF" or self._buffer[8:12] != b"WAVE":
            raise AudioDecodeError("Speech provider did not return WAV audio.")
        while self._header_offset + 8 <= len(self._buffer):
            offset = self._header_offset
            name = bytes(self._buffer[offset:offset + 4])
            size = struct.unpack_from("<I", self._buffer, offset + 4)[0]
            if name == b"data":
                if self.format is None:
                    raise AudioDecodeError("WAV audio is missing its format header.")
                if size not in (0, STREAMING_SIZE) and size % self.format.frame_bytes:
                    raise AudioDecodeError("WAV audio contains an incomplete sample frame.")
                self._remaining_bytes = None if size in (0, STREAMING_SIZE) else size
                del self._buffer[:offset + 8]
                self._data_started = True
                return
            end = offset + 8 + size + size % 2
            if end > MAX_WAV_HEADER_BYTES:
                raise AudioDecodeError("Speech provider returned an oversized WAV header.")
            if end > len(self._buffer):
                return
            if name == b"fmt ":
                if size < 16:
                    raise AudioDecodeError("Speech provider returned an invalid WAV format.")
                tag, channels, sample_rate, byte_rate, alignment, bits = struct.unpack_from(
                    "<HHIIHH", self._buffer, offset + 8,
                )
                if tag != 1 or bits != 16 or channels not in (1, 2):
                    raise AudioDecodeError("Speech requires mono/stereo signed 16-bit PCM audio.")
                if not 8000 <= sample_rate <= 96000 or alignment != channels * 2 or byte_rate != sample_rate * alignment:
                    raise AudioDecodeError("Speech provider returned inconsistent WAV format values.")
                self.format = AudioFormat(sample_rate=sample_rate, channels=channels)
            self._header_offset = end

    def finish(self) -> None:
        if not self._data_started or not self._received_bytes:
            raise AudioDecodeError("Speech provider returned no usable audio.")
        if self._buffer or self._remaining_bytes not in (None, 0):
            raise AudioDecodeError("Speech audio was truncated. Please try again.")
