"""Application-owned events; raw framework frames remain TypedDicts in main.py."""

from dataclasses import asdict, dataclass
from enum import StrEnum


class EventType(StrEnum):
    TRANSCRIPT = "transcript"
    REPLY_COMPLETE = "reply_complete"
    AUDIO_START = "audio_start"
    AUDIO_END = "audio_end"
    NOTICE = "notice"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DONE = "done"


@dataclass(frozen=True)
class AudioChunk:
    data: bytes


@dataclass(frozen=True)
class VoiceEvent:
    type: EventType
    text: str | None = None
    error: str | None = None
    partial: bool | None = None

    def to_payload(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}
