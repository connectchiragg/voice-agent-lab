"""The seam between a call transport (WebSocket or WebRTC) and the voice pipeline.

Every transport drives a `CallSession` the same way:

- audio in: `hear` with mono float32 PCM at the engines' input rate, 16 kHz
  (8 kHz for phone calls), sent on to Flux as linear16;
- audio and events out: the pipeline's `Send` callback, given event dicts
  (partial, transcript, caption, metrics, interrupt, reply_end, notice) and
  raw float32 PCM bytes at the engines' output sample rate;
- events in: `set_playing` and `report_played`, from whatever knows what the
  listener has heard (the browser player over WebSocket, the sender's frame
  count over WebRTC).
"""
import logging
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.pipeline import Call, Engines, Send

logger = logging.getLogger("uvicorn.error")


class CallSession(Protocol):
    async def hear(self, packet: bytes) -> None: ...
    def set_playing(self, active: bool) -> None: ...
    def report_played(self, seconds: float) -> None: ...
    async def close(self) -> None: ...


class SpeechUnavailable(Exception):
    """The call could not connect to the speech service; the message is for the user."""


async def open_call(engines: Engines, send: Send, conversation_id: str, *,
                    barge_in: bool, caller_id: str | None = None) -> Call:
    """Create a call and open its Flux and Aura sockets, whatever the transport.

    `caller_id` is the caller's normalized key for caller memory, from
    `app.memory.caller_key_from_phone` or `web_caller_key`; None for an unknown
    caller, who gets no memory.
    """
    call = Call(engines, send, conversation_id, barge_in=barge_in, caller_id=caller_id)
    try:
        await call.start()
    except Exception as error:
        # A refused handshake carries Deepgram's HTTP status (401 key, 402 credit).
        status = getattr(getattr(error, "response", None), "status_code", None)
        logger.warning("Deepgram connection failed: %s %s", type(error).__name__, status or "")
        await call.close()
        raise SpeechUnavailable("The speech service is unavailable. Try again later.") from error
    return call


class PlaybackState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["playback"]
    active: bool


class PlayedReport(BaseModel):
    """Sent after an interrupt: seconds of the interrupted reply's audio played."""
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["interrupted"]
    played_s: float = Field(ge=0, le=3600)


PlayerEvent = TypeAdapter(Annotated[PlaybackState | PlayedReport, Field(discriminator="type")])


def apply_player_event(call: CallSession, text: str) -> None:
    """A browser player's report (WebSocket transport); raises ValidationError."""
    event = PlayerEvent.validate_json(text)
    if isinstance(event, PlaybackState):
        call.set_playing(event.active)
    else:
        call.report_played(event.played_s)
