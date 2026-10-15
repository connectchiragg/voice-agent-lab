"""Phone calls over Telnyx: Call Control answers, a bidirectional media stream carries the audio.

Per inbound call to the Telnyx number:

1. Telnyx POSTs `call.initiated` to /telnyx/webhook, signed with ed25519; unsigned
   or stale requests are refused. We answer with a media stream to
   `TELNYX_STREAM_URL?token=<one-time token>`, in RTP mode (bidirectional), in
   the TELNYX_CODEC both ways: L16 at 16 kHz (wideband, the default) or PCMU at
   8 kHz. Or we reject the call when the server is full.
2. Telnyx opens /ws/telnyx: `connected`, `start` (its `media_format` says what
   Telnyx actually sends; logged), then 20 ms `media` frames. These are decoded,
   resampled only if Telnyx sent another rate than the engines', and handed to
   the pipeline in `INPUT_BATCH_S` packets. Flux and Aura run at the codec's
   rate, so wideband audio is never resampled.
3. Reply audio (Aura, linear16) is encoded in the TELNYX_CODEC and sent in
   `CHUNK_S` media messages, each followed by a `mark`. Telnyx returns a mark
   when the audio before it has played, so marks say what the caller has heard.
   Sending runs at most `LEAD_S` ahead of that.
4. Barge-in (Flux StartOfTurn while the assistant speaks): the pipeline's
   interrupt becomes `clear`, which drops the audio Telnyx has queued.
5. At `max_call_s` the reply is cut, a goodbye is spoken, and we hang up.

Callbacks (app/callbacks.py): the callback worker dials out with a client_state
naming the callback. On that call's `call.answered` we report "answered" to its
workflow and start the media stream (`streaming_start`, same settings and
one-time token as an answer); from there it is an inbound call that opens with
the callback greeting. A `call.hangup` before that reports no answer or busy.

Limits from Telnyx's documentation (checked 2026-10-15), and what we derive:

- Trial accounts cap every call at 10 minutes; paid accounts have no documented
  limit on an answered inbound call (dial's own `time_limit_secs` is at most 4 h).
  TELNYX_MAX_CALL_S defaults to 540 s so the goodbye always beats the trial cap.
- One bidirectional RTP stream per call. Outbound media chunks must be 20 ms to
  30 s of audio; CHUNK_S is 100 ms.
- Codecs (`stream_codec`, `stream_bidirectional_codec`): PCMU, PCMA, G722 (all
  8 kHz), OPUS, AMR-WB, and L16 at 16 kHz, with `stream_bidirectional_sampling_rate`
  (default 8000). Payloads are base64 RTP payloads without headers. Telnyx does
  not state L16's byte order. RTP L16 is network order (RFC 3551), but a real
  call proved Telnyx's L16 media little-endian (big-endian decoding gave loud
  static both ways), so TELNYX_L16_BYTE_ORDER defaults to `little`. The caller's own leg may still be 8 kHz (PSTN): then
  Telnyx transcodes, and only our side of the path is wideband. Telnyx may answer floods with
  `rate_limit_reached`, so we pace rather than dump a whole reply at once.
- Webhooks: 2xx is success; 408, 429 and 5xx are retried (the same `data.id`).
  We answer from a background task and return 200 at once, and skip repeats.
- Signatures: `telnyx-signature-ed25519` over "{telnyx-timestamp}|{raw body}",
  checked against the account's public key; timestamps older than 5 minutes are
  refused (Telnyx SDK tolerance).
- Concurrent calls start at 2 on a new account; TELNYX_MAX_CALLS defaults to 2.
- Recording (TELNYX_RECORD=1, see app/recordings.py): `record_start` once the
  stream has started, WAV, dual channel. `call.recording.saved` has download
  URLs valid for 10 minutes, so only the recording's id is kept. Telnyx charges
  $0.002 per recorded minute; storage is free (pricing page, 2026-10-15).
"""
import asyncio
import base64
import binascii
import json
import logging
import math
import os
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal
from uuid import uuid4

import httpx
import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, ValidationError

from app import callbacks, g711, memory, recordings
from app.callbacks import CallbackCall
from app.pipeline import Call, Engines, Samples
from app.transport import SpeechUnavailable, open_call

logger = logging.getLogger("uvicorn.error")
# Overridable only so the local fake-Telnyx harness can stand in for Call Control.
API_URL = os.getenv("TELNYX_API_URL", "https://api.telnyx.com/v2")
INPUT_BATCH_S = 0.08  # Deepgram recommends 80 ms chunks for Flux.
CHUNK_S = 0.1  # Reply audio per media message.
LEAD_S = 1.0  # Most reply audio sent but not yet heard; `clear` drops it on barge-in.
TICK_S = 0.02
SIGNATURE_TOLERANCE_S = 300
CONNECT_TIMEOUT_S = 10  # From answering to the media stream's `start`.
GOODBYE = "We've reached the time limit for this call. Thanks for calling, goodbye!"
GOODBYE_TIMEOUT_S = 15  # Synthesis plus playback of the goodbye, at most.
MAX_PAYLOAD_CHARS = 16000  # 0.37 s of base64 L16 (1.5 s of µ-law): far above 20 ms frames.
# Undecodable caller audio (wrong codec or byte order) reaches Flux as noise it
# never transcribes. After the greeting has played, if this long passes with no
# words from Flux while the caller's audio is loud, warn.
DECODE_CHECK_S = 8.0
LOUD_RMS = 0.03  # About -30 dBFS per 20 ms frame: speech, or noise from a misdecoded line.
LOUD_SHARE = 0.2  # Of the frames heard during the check.


L16_DTYPES = {"little": "<i2", "big": ">i2"}  # TELNYX_L16_BYTE_ORDER.


def l16_decode(payload: bytes, byte_order: str = "little") -> Samples:
    """L16 (16-bit signed) to float32 in [-1, 1)."""
    if len(payload) % 2:
        raise ValueError("L16 payload of an odd length")
    return np.frombuffer(payload, dtype=L16_DTYPES[byte_order]).astype(np.float32) / 32768


def l16_encode(samples: Samples, byte_order: str = "little") -> bytes:
    pcm = np.clip(np.round(np.asarray(samples, dtype=np.float32) * 32768), -32768, 32767)
    return pcm.astype(L16_DTYPES[byte_order]).tobytes()


@dataclass(frozen=True)
class PhoneCodec:
    name: str  # As Telnyx names it, in `media_format.encoding` and on answer.
    sample_rate: int
    decode: Callable[[bytes], Samples]
    encode: Callable[[Samples], bytes]
    byte_order: str = ""  # L16 only; G.711 is bytewise.

    @property
    def label(self) -> str:
        return f"{self.name} {self.byte_order}-endian" if self.byte_order else self.name


def l16_codec(byte_order: str) -> PhoneCodec:
    return PhoneCodec("L16", 16000, partial(l16_decode, byte_order=byte_order),
                      partial(l16_encode, byte_order=byte_order), byte_order)


# Telnyx sends and expects L16 little-endian, although RTP L16 is network order
# (RFC 3551): big-endian was heard as loud static on a real call and Flux heard
# nothing. TELNYX_L16_BYTE_ORDER=big returns to network order.
L16 = l16_codec("little")
L16_BE = l16_codec("big")
PCMU = PhoneCodec("PCMU", 8000, g711.decode, g711.encode)  # Fallback: TELNYX_CODEC=PCMU.
CODECS = {codec.name: codec for codec in (L16, PCMU)}


def l16_byte_order() -> str:
    order = os.getenv("TELNYX_L16_BYTE_ORDER", "little").strip().lower()
    if order not in L16_DTYPES:
        raise ValueError("TELNYX_L16_BYTE_ORDER must be little or big")
    return order


def codec_named(name: str, byte_order: str = "little") -> PhoneCodec | None:
    """A codec by its Telnyx name; L16 in the configured byte order."""
    name = name.strip().upper()
    if name == L16.name:
        return L16 if byte_order == "little" else L16_BE
    return CODECS.get(name)


def phone_codec() -> PhoneCodec:
    """TELNYX_CODEC: L16 (16 kHz, default) or PCMU (8 kHz); L16 in TELNYX_L16_BYTE_ORDER."""
    codec = codec_named(os.getenv("TELNYX_CODEC", L16.name), l16_byte_order())
    if codec is None:
        raise ValueError(f"TELNYX_CODEC must be one of {', '.join(CODECS)}")
    return codec


def resample(samples: Samples, source_rate: int, target_rate: int) -> Samples:
    """Linear interpolation; only for a stream Telnyx sent at another rate than asked."""
    if source_rate == target_rate or not len(samples):
        return samples
    count = round(len(samples) * target_rate / source_rate)
    positions = np.arange(count) * (source_rate / target_rate)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


@dataclass(frozen=True)
class TelnyxSettings:
    api_key: str
    public_key: Ed25519PublicKey
    stream_url: str  # wss://<public host>/ws/telnyx
    max_call_s: int = 540
    max_calls: int = 2
    codec: PhoneCodec = L16  # Asked for both ways; the engines run at its rate.
    record: bool = False  # TELNYX_RECORD=1: record each call (dual-channel WAV).

    @classmethod
    def from_env(cls) -> "TelnyxSettings | None":
        """None (phone calls refused) unless the key, public key and stream URL are set."""
        names = ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "TELNYX_STREAM_URL")
        missing = [name for name in names if not os.getenv(name)]
        if missing:
            logger.warning("Phone calls disabled; not set: %s", ", ".join(missing))
            return None
        return cls(api_key=os.environ["TELNYX_API_KEY"],
                   public_key=load_public_key(os.environ["TELNYX_PUBLIC_KEY"]),
                   stream_url=os.environ["TELNYX_STREAM_URL"],
                   max_call_s=int(os.getenv("TELNYX_MAX_CALL_S", cls.max_call_s)),
                   max_calls=int(os.getenv("TELNYX_MAX_CALLS", cls.max_calls)),
                   codec=phone_codec(), record=recordings.enabled())


def load_public_key(encoded: str) -> Ed25519PublicKey:
    """The portal's public key: 32 raw bytes, base64."""
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded.strip(), validate=True))


def signature_valid(key: Ed25519PublicKey, body: bytes, signature: str | None,
                    timestamp: str | None, now: float | None = None) -> bool:
    """Telnyx's webhook signature: ed25519 over "{timestamp}|{raw body}", recent timestamp."""
    if not signature or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
        key.verify(base64.b64decode(signature, validate=True), f"{sent_at}|".encode() + body)
    except (ValueError, binascii.Error, InvalidSignature):
        return False
    now = time.time() if now is None else now
    return abs(now - sent_at) <= SIGNATURE_TOLERANCE_S


class CallControl:
    """The few Call Control commands we use. Errors are logged by status, never by body."""

    def __init__(self, client: httpx.AsyncClient, api_key: str, codec: PhoneCodec = L16) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.codec = codec

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        try:
            response = await self.client.get(f"{API_URL}/{path}", params=params,
                                             headers=self.headers, timeout=5)
        except httpx.HTTPError as error:
            logger.warning("Telnyx GET %s failed: %s", path.split("/")[0], type(error).__name__)
            return None
        if not response.is_success:
            logger.warning("Telnyx GET %s refused: HTTP %d", path.split("/")[0], response.status_code)
            return None
        return response.json()

    async def command(self, call_control_id: str, action: str, body: dict[str, Any]) -> bool:
        url = f"{API_URL}/calls/{call_control_id}/actions/{action}"
        try:
            response = await self.client.post(url, json=body, headers=self.headers, timeout=5)
        except httpx.HTTPError as error:
            logger.warning("Telnyx %s failed: %s", action, type(error).__name__)
            return False
        if response.is_success:
            return True
        logger.warning("Telnyx %s refused: HTTP %d", action, response.status_code)
        return False

    def stream(self, stream_url: str) -> dict[str, Any]:
        return {
            "stream_url": stream_url,
            "stream_track": "inbound_track",  # The caller only; our own audio is not echoed back.
            "stream_codec": self.codec.name,
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": self.codec.name,
            "stream_bidirectional_sampling_rate": self.codec.sample_rate,
        }

    async def answer(self, call_control_id: str, stream_url: str, command_id: str) -> bool:
        # command_id: Telnyx ignores a repeat (webhook retry).
        return await self.command(call_control_id, "answer",
                                  {**self.stream(stream_url), "command_id": command_id})

    async def start_stream(self, call_control_id: str, stream_url: str, command_id: str) -> bool:
        """For an outbound call once answered: the same stream an answer would start."""
        return await self.command(call_control_id, "streaming_start",
                                  {**self.stream(stream_url), "command_id": command_id})

    async def reject(self, call_control_id: str, command_id: str) -> bool:
        return await self.command(call_control_id, "reject",
                                  {"cause": "USER_BUSY", "command_id": command_id})

    async def hangup(self, call_control_id: str) -> bool:
        return await self.command(call_control_id, "hangup", {})

    async def record_start(self, call_control_id: str) -> bool:
        return await self.command(call_control_id, "record_start", {
            "format": "wav", "channels": "dual",  # The caller and the assistant apart.
            "command_id": f"record-{call_control_id}"})

    async def recording(self, recording_id: str | None, call_control_id: str) -> dict[str, Any] | None:
        """The recording object (`id`, `duration_millis`, `created_at`, fresh `download_urls`)."""
        if recording_id:
            found = await self.get(f"recordings/{recording_id}")
            return found.get("data") if found else None
        found = await self.get("recordings", {"filter[call_control_id]": call_control_id})
        listed = found.get("data") if found else None
        return max(listed, key=lambda item: str(item.get("created_at", ""))) if listed else None


class Playout:
    """Reply audio queued for Telnyx, and an estimate of what the caller has heard.

    Positions count reply samples only. `written` is the end of the queue and
    `sent` how much has gone to Telnyx. `heard` is anchored at the last mark
    Telnyx returned (or where sending resumed after silence) and advances with
    the clock from there, never past what was sent.

    Marks carry an epoch: those Telnyx returns because of a `clear` belong to
    the old epoch and are ignored.
    """

    def __init__(self, codec: PhoneCodec = L16) -> None:
        self.encode = codec.encode
        self.rate = codec.sample_rate
        self.chunk = round(self.rate * CHUNK_S)
        self.chunks: deque[Samples] = deque()
        self.written = self.sent = self.heard = 0
        self.heard_at = -math.inf
        self.epoch = 0
        self.marks = 0
        self.reply_start = 0

    def push(self, samples: Samples) -> None:
        if len(samples):
            self.chunks.append(samples)
            self.written += len(samples)

    def heard_by(self, now: float) -> int:
        played = (now - self.heard_at) * self.rate  # Since the anchor; -inf: never anchored.
        return self.sent if played >= self.sent - self.heard else self.heard + max(0, round(played))

    def playing(self, now: float) -> bool:
        return self.heard_by(now) < self.written

    def next_chunk(self, now: float) -> tuple[bytes, str] | None:
        """µ-law for the next media message and the mark to send after it, or None to wait."""
        if self.sent == self.written:
            return None
        heard = self.heard_by(now)
        if heard >= self.sent:  # Nothing in flight: playback (re)starts with this chunk.
            self.heard, self.heard_at = self.sent, now
        elif self.sent - heard >= LEAD_S * self.rate:
            return None
        samples = self.take(self.chunk)
        self.sent += len(samples)
        self.marks += 1
        return self.encode(samples), f"{self.epoch}:{self.marks}:{self.sent}"

    def take(self, count: int) -> Samples:
        parts, needed = [], count
        while needed and self.chunks:
            head = self.chunks[0]
            parts.append(head[:needed])
            if len(head) <= needed:
                self.chunks.popleft()
            else:
                self.chunks[0] = head[needed:]
            needed -= len(parts[-1])
        return np.concatenate(parts)

    def marked(self, name: str, now: float) -> None:
        """Telnyx returned a mark: the audio before it has played."""
        try:
            epoch, _, position = (int(part) for part in name.split(":"))
        except ValueError:
            return
        if epoch == self.epoch and position >= self.heard:
            self.heard, self.heard_at = position, now

    def clear(self, now: float) -> float:
        """Barge-in: drop everything unplayed; seconds of the current reply heard."""
        heard = self.heard_by(now)
        played_s = max(0, heard - self.reply_start) / self.rate
        self.chunks.clear()
        self.epoch += 1
        self.written = self.sent = self.heard = self.reply_start = heard
        self.heard_at = now
        return played_s

    def start_reply(self) -> None:
        self.reply_start = self.written


class MediaFormat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    encoding: str
    sample_rate: int
    channels: int = 1


class StartInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    call_control_id: str
    media_format: MediaFormat


class StartMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event: Literal["start"]
    stream_id: str
    start: StartInfo


Sender = Callable[[dict[str, Any]], Awaitable[None]]


class PhoneSession:
    """One Telnyx media stream and its pipeline call.

    `send` is the pipeline's Send: audio is queued for pacing, an interrupt
    becomes `clear` and reports the heard seconds back to the call. Other
    events (partials, captions, metrics) have no screen to go to.
    """

    def __init__(self, to_telnyx: Sender, codec: PhoneCodec = L16,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.to_telnyx = to_telnyx
        self.clock = clock
        self.codec = codec  # Reply audio, at the engines' rate.
        self.inbound = codec  # What Telnyx says it sends (`start`), possibly another codec.
        self.playout = Playout(codec)
        self.call: Call | None = None
        self.batch = round(codec.sample_rate * INPUT_BATCH_S)
        self.pending: list[Samples] = []
        self.pending_count = 0
        self.listening = True
        self.playing = False
        self.wake = asyncio.Event()
        self.greeted = asyncio.Event()  # The greeting finished or was interrupted.
        self.words_heard = False  # Flux transcribed something.
        self.frames = self.loud_frames = 0  # Caller frames during the decode check.

    # Pipeline -> Telnyx.

    async def send(self, data: dict[str, object] | bytes) -> None:
        if isinstance(data, bytes):
            self.playout.push(np.frombuffer(data, dtype="<f4"))
            self.wake.set()
            return
        kind = data.get("type")
        if kind in ("partial", "transcript") and data.get("text"):
            self.words_heard = True
        if kind == "transcript":
            self.playout.start_reply()
        elif kind == "interrupt":
            self.greeted.set()
            await self.interrupt()
        elif kind == "reply_end":
            self.greeted.set()

    async def interrupt(self) -> None:
        played_s = self.playout.clear(self.clock())
        await self.to_telnyx({"event": "clear"})
        self.set_playing(False)
        if self.call is not None:
            self.call.report_played(played_s)

    def set_playing(self, playing: bool) -> None:
        if playing != self.playing:
            self.playing = playing
            if self.call is not None:
                self.call.set_playing(playing)

    async def pump(self) -> None:
        """Send queued reply audio, paced; keep the call's playing state current."""
        while True:
            message = self.playout.next_chunk(self.clock())
            if message is not None:
                payload, mark = message
                await self.to_telnyx({"event": "media",
                                      "media": {"payload": base64.b64encode(payload).decode()}})
                await self.to_telnyx({"event": "mark", "mark": {"name": mark}})
            self.set_playing(self.playout.playing(self.clock()))
            if message is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), TICK_S)
                except TimeoutError:
                    pass

    async def drained(self) -> None:
        while self.playout.playing(self.clock()):
            await asyncio.sleep(TICK_S)

    # Telnyx -> pipeline.

    async def hear(self, payload: str) -> None:
        if len(payload) > MAX_PAYLOAD_CHARS:
            raise ValueError("Media frame too large")
        samples = self.inbound.decode(base64.b64decode(payload, validate=True))
        if not self.listening or self.call is None:
            return
        if len(samples):
            self.frames += 1
            self.loud_frames += float(np.sqrt(np.mean(np.square(samples)))) > LOUD_RMS
        samples = resample(samples, self.inbound.sample_rate, self.codec.sample_rate)
        self.pending.append(samples)
        self.pending_count += len(samples)
        if self.pending_count >= self.batch:
            packet = np.concatenate(self.pending).astype("<f4")
            self.pending, self.pending_count = [], 0
            await self.call.hear(packet.tobytes())

    async def handle(self, message: dict[str, Any]) -> bool:
        """One message from Telnyx; False once the stream has stopped."""
        event = message.get("event")
        if event == "media":
            media = message.get("media") or {}
            if media.get("track", "inbound") == "inbound":
                await self.hear(str(media.get("payload", "")))
        elif event == "mark":
            self.playout.marked(str((message.get("mark") or {}).get("name", "")), self.clock())
        elif event == "dtmf":
            logger.info("Phone call: DTMF received (not used yet)")
        elif event == "error":
            error = message.get("payload") or {}
            logger.warning("Telnyx stream error %s: %s", error.get("code"), error.get("title"))
        elif event == "stop":
            return False
        return True

    async def check_decoding(self, wait_s: float = DECODE_CHECK_S) -> None:
        """Warn when the caller's line is loud after the greeting but Flux hears no words."""
        await self.greeted.wait()
        await self.drained()
        self.frames = self.loud_frames = 0
        await asyncio.sleep(wait_s)
        if not self.words_heard and self.frames and self.loud_frames >= LOUD_SHARE * self.frames:
            logger.warning("Phone call: caller audio may be undecodable: codec %s, byte order %s "
                           "(no words from Flux %.0f s after the greeting; %d of %d frames loud)",
                           self.inbound.name, self.inbound.byte_order or "n/a", wait_s,
                           self.loud_frames, self.frames)

    async def say_goodbye(self) -> None:
        """Cut any reply, stop listening, speak the goodbye and wait until it has played."""
        assert self.call is not None
        self.listening = False
        if self.call.assistant_busy:
            await self.call.interrupt()
        self.playout.start_reply()
        async with aclosing(self.call.speaker.synthesize(GOODBYE)) as stream:
            async for audio in stream:
                await self.send(audio.astype("<f4").tobytes())
        await self.drained()


@dataclass(frozen=True)
class PendingCall:
    """An answered call whose media stream has not connected yet."""
    call_control_id: str
    deadline: float  # monotonic
    caller_key: str | None = None  # memory.caller_key_from_phone of `from`; never logged.
    callback: CallbackCall | None = None  # Set for a callback's outbound call.


@dataclass
class Switchboard:
    """Phone calls on this server: answered calls awaiting their stream, and live ones."""
    settings: TelnyxSettings
    control: CallControl
    pending: dict[str, PendingCall] = field(default_factory=dict)  # By one-time stream token.
    live: set[str] = field(default_factory=set)
    seen: deque[str] = field(default_factory=lambda: deque(maxlen=1000))  # Webhook ids handled.
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, work: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def first_time(self, event_id: str) -> bool:
        if event_id in self.seen:
            return False
        self.seen.append(event_id)
        return True

    @property
    def calls(self) -> int:
        now = time.monotonic()
        return len(self.live) + sum(1 for call in self.pending.values() if call.deadline > now)

    async def incoming(self, call_control_id: str, event_id: str,
                       caller_key: str | None = None) -> None:
        if self.calls >= self.settings.max_calls:
            logger.info("Phone call rejected: %d calls in progress", self.calls)
            await self.control.reject(call_control_id, event_id)
            return
        await self.connect(PendingCall(call_control_id, time.monotonic() + CONNECT_TIMEOUT_S,
                                       caller_key),
                           lambda url: self.control.answer(call_control_id, url, event_id))

    async def callback_answered(self, call_control_id: str, event_id: str,
                                caller_key: str | None, callback: CallbackCall) -> None:
        """A callback's outbound call was answered: stream it like an inbound call."""
        if self.calls >= self.settings.max_calls:
            logger.warning("Callback answered with %d calls in progress; hanging up", self.calls)
            await self.control.hangup(call_control_id)
            return
        await self.connect(PendingCall(call_control_id, time.monotonic() + CONNECT_TIMEOUT_S,
                                       caller_key, callback),
                           lambda url: self.control.start_stream(call_control_id, url, event_id))

    async def connect(self, call: PendingCall, start: Callable[[str], Awaitable[bool]]) -> None:
        """Start the call's media stream with a one-time token; hang up if it never connects."""
        token = secrets.token_urlsafe(24)
        self.pending[token] = call
        if not await start(f"{self.settings.stream_url}?token={token}"):
            self.pending.pop(token, None)
            return
        call_control_id = call.call_control_id
        logger.info("Phone call answered; waiting for its media stream")
        await asyncio.sleep(CONNECT_TIMEOUT_S)
        if self.pending.pop(token, None) is not None:
            logger.warning("Phone call media stream never connected; hanging up")
            await self.control.hangup(call_control_id)

    def claim(self, token: str) -> PendingCall | None:
        """The call this one-time stream token was issued for, if still valid."""
        call = self.pending.pop(token, None)
        if call is None or call.deadline < time.monotonic():
            return None
        self.live.add(call.call_control_id)
        return call


router = APIRouter()


def board_of(app: FastAPI) -> Switchboard | None:
    return getattr(app.state, "switchboard", None)


@asynccontextmanager
async def lifespan(app: FastAPI, settings: TelnyxSettings | None = None) -> AsyncIterator[None]:
    """App lifespan: one HTTP client for Call Control; background work stops on shutdown."""
    settings = settings or TelnyxSettings.from_env()
    if settings is None:
        app.state.switchboard = None
        yield
        return
    async with httpx.AsyncClient() as client:
        board = Switchboard(settings, CallControl(client, settings.api_key, settings.codec))
        app.state.switchboard = board
        try:
            yield
        finally:
            for task in list(board.tasks):
                task.cancel()
            await asyncio.gather(*board.tasks, return_exceptions=True)


@router.post("/telnyx/webhook")
async def webhook(request: Request) -> dict[str, bool]:
    board = board_of(request.app)
    if board is None:
        raise HTTPException(503, "Phone calls are not configured.")
    body = await request.body()
    if not signature_valid(board.settings.public_key, body,
                           request.headers.get("telnyx-signature-ed25519"),
                           request.headers.get("telnyx-timestamp")):
        raise HTTPException(401, "Invalid signature.")
    try:
        data = json.loads(body)["data"]
        kind, event_id, payload = data["event_type"], str(data["id"]), data.get("payload") or {}
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, "Unexpected webhook.") from error
    if not board.first_time(event_id):
        return {"ok": True}  # A retry of something already handled.
    call_control_id = str(payload.get("call_control_id", ""))
    callback = callbacks.callback_of(payload.get("client_state"))  # Our own outbound calls.
    if kind == "call.initiated" and payload.get("direction") == "incoming" and call_control_id:
        # The caller's number becomes their memory key here and is never logged.
        caller_key = memory.caller_key_from_phone(payload.get("from"))
        board.spawn(board.incoming(call_control_id, event_id, caller_key))
    elif kind == "streaming.failed" and call_control_id:
        logger.warning("Telnyx media stream failed: %s", payload.get("failure_reason"))
        board.spawn(hang_up(board, call_control_id))
    elif kind == "call.answered" and callback is not None and call_control_id:
        # The callee is the caller who asked for the callback.
        caller_key = memory.caller_key_from_phone(payload.get("to"))
        board.spawn(callbacks.report(callback, call_control_id, "answered"))
        board.spawn(board.callback_answered(call_control_id, event_id, caller_key, callback))
    elif kind == "call.hangup":
        logger.info("Phone call ended: %s", payload.get("hangup_cause"))
        if callback is not None and call_control_id:  # Ignored by the workflow once answered.
            result = callbacks.hangup_result(payload.get("hangup_cause"))
            board.spawn(callbacks.report(callback, call_control_id, result))
    elif kind == "call.recording.saved" and call_control_id:
        board.spawn(recording_saved(board, call_control_id, payload))
    elif kind == "call.recording.error":
        logger.warning("Telnyx recording failed: %s", payload.get("reason") or payload.get("error"))
    return {"ok": True}


async def start_recording(board: Switchboard, call_control_id: str) -> None:
    if await board.control.record_start(call_control_id):
        logger.info("Phone call recording started (wav, dual channel)")


def iso_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def recording_saved(board: Switchboard, call_control_id: str, payload: dict[str, Any]) -> None:
    """Keep the recording's id and metadata; its URLs expire, so they are never stored."""
    found = await board.control.recording(payload.get("recording_id"), call_control_id)
    recording_id = str((found or {}).get("id") or payload.get("recording_id") or "")
    if not recording_id:
        logger.warning("Phone call recording saved, but its id was not found")
        return
    started, ended = (iso_time(payload.get(key)) for key in ("recording_started_at",
                                                             "recording_ended_at"))
    duration_ms = (found or {}).get("duration_millis")
    if duration_ms is None and started and ended:
        duration_ms = round((ended - started).total_seconds() * 1000)
    created_at = (iso_time((found or {}).get("created_at")) or ended or started
                  or datetime.now(timezone.utc))
    try:
        stored = await recordings.save(recordings.Recording(recording_id, call_control_id,
                                                            created_at, duration_ms))
    except Exception as error:
        logger.warning("Recording metadata not stored: %s", type(error).__name__)
        stored = False
    logger.info("Phone call recording saved: id %s, %s s%s", recording_id,
                "?" if duration_ms is None else f"{duration_ms / 1000:.1f}",
                "" if stored else " (not stored: no Postgres)")


async def hang_up(board: Switchboard, call_control_id: str) -> None:
    await board.control.hangup(call_control_id)


async def receive_json(socket: WebSocket) -> dict[str, Any]:
    message = json.loads(await socket.receive_text())
    if not isinstance(message, dict):
        raise ValueError("Expected a JSON object")
    return message


async def wait_for_start(socket: WebSocket) -> StartMessage:
    """Skip `connected`; the first other message must be `start`."""
    while (message := await receive_json(socket)).get("event") == "connected":
        pass
    return StartMessage.model_validate(message)


@router.websocket("/ws/telnyx")
async def media_stream(socket: WebSocket, token: str = "") -> None:
    board = board_of(socket.app)
    pending = board.claim(token) if board is not None else None
    if board is None or pending is None:
        await socket.close(code=1008)  # Refused before accepting: HTTP 403.
        return
    call_control_id = pending.call_control_id
    await socket.accept()
    engines: Engines | None = getattr(socket.app.state, "phone_engines", None)
    send_lock = asyncio.Lock()

    async def to_telnyx(message: dict[str, Any]) -> None:
        async with send_lock:  # The pump, the reply task and this loop all send.
            await socket.send_text(json.dumps(message))

    session = PhoneSession(to_telnyx, board.settings.codec)
    tasks: list[asyncio.Task[None]] = []
    hang_up_after = True
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            start = await wait_for_start(socket)
        media = start.start.media_format
        if start.start.call_control_id != call_control_id:
            raise ValueError("The stream belongs to another call")
        inbound = codec_named(media.encoding, board.settings.codec.byte_order or l16_byte_order())
        if inbound is None or media.sample_rate != inbound.sample_rate or media.channels != 1:
            logger.warning("Unexpected Telnyx media format %s/%d", media.encoding, media.sample_rate)
            raise ValueError("Unexpected media format")
        session.inbound = inbound
        if board.settings.record:  # Once the stream has started, so the greeting is on it.
            board.spawn(start_recording(board, call_control_id))
        logger.info("Phone media: caller audio %s %d Hz, replies %s %d Hz", inbound.label,
                    inbound.sample_rate, session.codec.label, session.codec.sample_rate)
        if engines is None:
            logger.warning("Phone call refused: speech services are not configured")
            return
        # Echo: the carrier cancels line echo, and only the caller's track is
        # streamed to us, so barge-in stays on (unlike a browser without AEC).
        conversation_id = str(uuid4())
        if pending.callback is not None:
            callbacks.calls[conversation_id] = pending.callback  # For the greeting.
        session.call = await open_call(engines, session.send, conversation_id, barge_in=True,
                                       caller_id=pending.caller_key)
        logger.info("Phone call connected (%s caller)",
                    "identified" if pending.caller_key else "anonymous")
        session.call.greet()  # The assistant speaks first; barge-in can cut it.
        time_limit = asyncio.create_task(end_at_time_limit(session, board.settings.max_call_s))
        reader = asyncio.create_task(read_stream(socket, session))
        tasks += [asyncio.create_task(session.pump()), asyncio.create_task(session.check_decoding()),
                  time_limit, reader]
        await asyncio.wait((reader, time_limit), return_when=asyncio.FIRST_COMPLETED)
        if reader.done():
            reader.result()  # Raises what ended the stream, if anything did.
            hang_up_after = False  # `stop`: the call has already ended.
    except WebSocketDisconnect:
        logger.info("Telnyx media stream disconnected")
    except SpeechUnavailable:
        pass  # Logged by open_call; the caller is hung up.
    except (ValidationError, ValueError, binascii.Error, TimeoutError) as error:
        logger.warning("Telnyx media stream refused: %s", type(error).__name__)
        await socket.close(code=1008)
    except Exception as error:
        logger.warning("Phone call failed: %s", type(error).__name__)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        board.live.discard(call_control_id)
        if session.call is not None:
            await session.call.close()
            callbacks.calls.pop(session.call.conversation_id, None)
        if hang_up_after:
            # Hanging up an already ended call is refused harmlessly (HTTP 422).
            await board.control.hangup(call_control_id)
        try:
            await socket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


async def read_stream(socket: WebSocket, session: PhoneSession) -> None:
    """Telnyx's messages until `stop`."""
    while await session.handle(await receive_json(socket)):
        pass


async def end_at_time_limit(session: PhoneSession, max_call_s: float) -> None:
    await asyncio.sleep(max_call_s)
    logger.info("Phone call reached its %d s limit; saying goodbye", max_call_s)
    try:
        async with asyncio.timeout(GOODBYE_TIMEOUT_S):
            await session.say_goodbye()
    except Exception as error:  # Hang up regardless.
        logger.warning("Goodbye failed: %s", type(error).__name__)
