"""Application-owned events; raw framework frames remain TypedDicts in main.py."""

from dataclasses import asdict, dataclass
from enum import StrEnum


class EventType(StrEnum):
    TRANSCRIBING = "transcribing"
    TRANSCRIPT = "transcript"
    GENERATING = "generating"
    SYNTHESIZING = "synthesizing"
    REPLY_COMPLETE = "reply_complete"
    AUDIO_START = "audio_start"
    AUDIO_END = "audio_end"
    NOTICE = "notice"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DONE = "done"


class AudioEncoding(StrEnum):
    PCM_S16LE = "pcm_s16le"


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    encoding: AudioEncoding = AudioEncoding.PCM_S16LE

    @property
    def frame_bytes(self) -> int:
        return self.channels * 2  # One signed 16-bit sample per channel.


@dataclass(frozen=True)
class AudioChunk:
    data: bytes


@dataclass(frozen=True)
class VoiceEvent:
    type: EventType
    text: str | None = None
    error: str | None = None
    format: AudioFormat | None = None
    partial: bool | None = None

    def to_payload(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}
