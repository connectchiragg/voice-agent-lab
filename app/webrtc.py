"""WebRTC call transport: aiortc on this server, peer-to-peer with the browser.

Signalling is one HTTPS round trip: the browser fetches ICE servers (short-lived
Cloudflare TURN credentials, see app/ice.py), gathers its candidates, then
POSTs its offer to /webrtc/offer and gets the answer back. Media is Opus over
UDP, relayed by TURN when needed, so the server needs no inbound UDP ports.

Per call:

- inbound: the microphone track is decoded by aiortc, downsampled to 16 kHz
  mono float32 and handed to the pipeline in `INPUT_BATCH_S` packets;
- outbound: `SpeechTrack` paces queued TTS audio out in 20 ms frames (silence
  when idle) and drops its queue at once on an interrupt;
- control: a data channel named "control" carries the same events as the
  WebSocket transport. The browser sends only `playout` reports (its
  getStats-derived delay from a frame leaving here to it being heard), from
  which this side derives what the listener has heard: the playing state,
  the played seconds after an interrupt, and when each caption starts.
"""
import asyncio
import fractions
import json
import logging
import math
import os
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

import av
import httpx
import numpy as np
from aiortc import (MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCSessionDescription)
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import MediaStreamError
from aiortc.rtp import RtpPacket
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import ice, memory
from app.pipeline import Engines, Samples
from app.transport import CallSession, SpeechUnavailable, open_call

logger = logging.getLogger("uvicorn.error")
# Per-call limit; TURN credentials expire shortly after it.
MAX_CALL_S = int(os.getenv("WEBRTC_MAX_CALL_S", "900"))
MAX_CALLS = int(os.getenv("WEBRTC_MAX_CALLS", "4"))  # Concurrent peers on this server.
CONNECT_TIMEOUT_S = 20  # From the answer to the data channel opening.
FRAME_S = 0.020
INPUT_RATE = 16000
INPUT_BATCH_S = 0.04  # Microphone audio per pipeline packet.
DEFAULT_PLAYOUT_DELAY_S = 0.1  # Until the browser's first report.
MAX_QUEUED_S = 120  # Sanity bound on reply audio waiting to be sent.
STALL_RESYNC_S = 0.2  # Late by more than this: restart the frame clock, do not burst.


class Downsampler:
    """Decoded WebRTC audio (any rate and layout) to 16 kHz mono float32.

    One per stream: libswresample keeps its filter state across frames, so
    frame edges add no clicks. aiortc decodes Opus to stereo with both
    channels equal; the first channel is taken rather than mixed (+3 dB).
    """

    def __init__(self, rate: int = INPUT_RATE) -> None:
        self.resampler = av.AudioResampler(format="fltp", layout="stereo", rate=rate)

    def __call__(self, frame: av.AudioFrame) -> Samples:
        planes = [out.to_ndarray()[0] for out in self.resampler.resample(frame)]
        if not planes:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(planes).astype(np.float32, copy=False)


class LowDelayAudioBuffer:
    """In place of aiortc's audio jitter buffer, which holds 4 packets (80 ms)
    and stalls on a lost packet until 16 newer ones arrive (320 ms).

    Each Opus packet is a whole frame, and the pipeline needs no smooth
    playout: pass packets on at once, in order, and drop late ones. aiortc
    1.15 has no public hook for this; see `Peer.tune_receivers`.
    """

    def __init__(self) -> None:
        self.last: int | None = None

    def add(self, packet: RtpPacket) -> tuple[bool, JitterFrame | None]:
        sequence = packet.sequence_number
        if self.last is not None and not 0 < (sequence - self.last) % 65536 < 32768:
            return False, None  # A duplicate, or later than one already passed on.
        self.last = sequence
        return False, JitterFrame(data=packet._data, timestamp=packet.timestamp)  # type: ignore[attr-defined]


class SpeechTrack(MediaStreamTrack):
    """Reply audio paced out in 20 ms frames; silence while there is nothing to say.

    Positions count reply-audio samples only (never the silence between
    replies): `written` is the end of the queue and `sent` how much of it has
    gone out. The playout delay is the browser's latest estimate of the time
    from a frame leaving here to it being heard.
    """
    kind = "audio"

    def __init__(self, sample_rate: int, on_playing: Callable[[bool], None] | None = None) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.frame_samples = round(sample_rate * FRAME_S)
        self.time_base = fractions.Fraction(1, sample_rate)
        self.on_playing = on_playing
        self.chunks: deque[Samples] = deque()
        self.queued = 0
        self.written = self.sent = 0
        self.frames = 0
        self.clock_start: float | None = None
        self.last_audio_at = -math.inf  # Loop time the last reply frame was sent.
        self.playout_delay_s = DEFAULT_PLAYOUT_DELAY_S
        self.playing = False

    def push(self, samples: Samples) -> None:
        if self.queued + len(samples) > MAX_QUEUED_S * self.sample_rate:
            logger.warning("Reply audio queue full; dropping %d samples", len(samples))
            return
        if len(samples):
            self.chunks.append(samples)
            self.queued += len(samples)
            self.written += len(samples)
            self.set_playing(True)

    def flush(self) -> None:
        """Interrupt: nothing queued is sent. The browser mutes what is in flight."""
        self.chunks.clear()
        self.queued = 0
        self.written = self.sent
        self.last_audio_at = -math.inf
        self.set_playing(False)

    def set_playing(self, playing: bool) -> None:
        if playing != self.playing:
            self.playing = playing
            if self.on_playing is not None:
                self.on_playing(playing)

    def seconds_until_heard(self, position: int) -> float:
        """From now until the sample at `position` is heard (negative: already was)."""
        return (position - self.sent) / self.sample_rate + self.playout_delay_s

    def heard_since(self, position: int, now: float) -> float:
        """Seconds of audio from `position` on that the listener has heard by `now`.

        Sent audio counts as heard except what went out within the playout
        delay, which is still on its way or in the jitter buffer.
        """
        sent_s = max(0, self.sent - position) / self.sample_rate
        in_flight_s = max(0.0, self.last_audio_at + FRAME_S - (now - self.playout_delay_s))
        return max(0.0, sent_s - in_flight_s)

    def take(self, count: int) -> tuple[Samples, int]:
        """The next frame of audio, padded with silence; also how much was audio."""
        frame = np.zeros(count, dtype=np.float32)
        filled = 0
        while filled < count and self.chunks:
            chunk = self.chunks[0]
            used = min(count - filled, len(chunk))
            frame[filled:filled + used] = chunk[:used]
            filled += used
            if used == len(chunk):
                self.chunks.popleft()
            else:
                self.chunks[0] = chunk[used:]
        self.queued -= filled
        self.sent += filled
        return frame, filled

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self.clock_start is None:
            self.clock_start = now
        due = self.clock_start + self.frames * FRAME_S
        if due > now:
            await asyncio.sleep(due - now)
        elif now - due > STALL_RESYNC_S:
            logger.info("Speech track %.0f ms late; resyncing its clock", (now - due) * 1000)
            self.clock_start = now - self.frames * FRAME_S
        samples, audio = self.take(self.frame_samples)
        now = loop.time()
        if audio:
            self.last_audio_at = now
        elif self.playing and now >= self.last_audio_at + FRAME_S + self.playout_delay_s:
            self.set_playing(False)  # The last reply audio has been heard.
        pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm[np.newaxis], format="s16", layout="mono")
        frame.sample_rate = self.sample_rate
        frame.pts = self.frames * self.frame_samples
        frame.time_base = self.time_base
        self.frames += 1
        return frame


class PlayoutReport(BaseModel):
    """Browser estimate: one-way network delay + jitter buffer + output delay."""
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["playout"]
    delay_s: float = Field(ge=0, le=5)


class Peer:
    """One browser peer: connection, tracks, data channel and its pipeline call.

    `send` is the pipeline's Send: reply audio goes to the speech track, and
    events to the data channel, except that an interrupt first flushes the
    track and reports the heard seconds to the call itself.
    """

    def __init__(self, pc: RTCPeerConnection, output_sample_rate: int,
                 on_closed: Callable[["Peer"], None] | None = None) -> None:
        self.pc = pc
        self.speech = SpeechTrack(output_sample_rate, self.playing_changed)
        self.on_closed = on_closed
        self.call: CallSession | None = None
        self.channel = None
        self.connected = asyncio.Event()  # The data channel is open.
        self.reply_start = 0  # Speech position where the current reply began.
        # A caption follows its segment's audio, which began at the first
        # audio since the previous caption.
        self.segment_start: int | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.closed = False
        self.close_reason = ""
        pc.on("connectionstatechange", self.connection_changed)
        pc.on("datachannel", self.channel_opened)
        pc.on("track", self.track_received)

    def spawn(self, work: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def start(self) -> None:
        self.spawn(self.supervise())

    def tune_receivers(self) -> None:
        """After the remote description: low-delay buffering for the microphone."""
        for transceiver in self.pc.getTransceivers():
            if transceiver.kind == "audio":
                transceiver.receiver._RTCRtpReceiver__jitter_buffer = LowDelayAudioBuffer()

    # Pipeline -> browser.

    async def send(self, data: dict[str, object] | bytes) -> None:
        if isinstance(data, bytes):
            if self.segment_start is None:
                self.segment_start = self.speech.written
            self.speech.push(np.frombuffer(data, dtype="<f4"))
            return
        kind = data.get("type")
        if kind == "transcript":  # A new reply: heard seconds count from here.
            self.reply_start = self.speech.written
            self.segment_start = None
        elif kind == "caption":
            data = {**data, "starts_in_s": round(self.speech.seconds_until_heard(self.caption_start()), 3)}
        elif kind == "interrupt":
            played_s = self.speech.heard_since(self.reply_start, asyncio.get_running_loop().time())
            self.speech.flush()
            self.segment_start = None
            self.send_event(data)  # The browser mutes in-flight audio and cancels captions.
            if self.call is not None:
                self.call.report_played(played_s)
            return
        self.send_event(data)

    def caption_start(self) -> int:
        """Speech position where the captioned audio started."""
        start, self.segment_start = self.segment_start, None
        return self.speech.written if start is None else start

    def send_event(self, event: dict[str, object]) -> None:
        if self.channel is not None and self.channel.readyState == "open":
            self.channel.send(json.dumps(event))

    def playing_changed(self, playing: bool) -> None:
        if self.call is not None:
            self.call.set_playing(playing)

    # Browser -> pipeline.

    def channel_opened(self, channel) -> None:  # type: ignore[no-untyped-def]
        if channel.label != "control" or self.channel is not None:
            channel.close()
            return
        self.channel = channel
        channel.on("message", self.control_received)
        channel.on("close", lambda: self.spawn(self.close("data channel closed")))
        self.connected.set()
        self.send_event({"type": "ready"})

    def control_received(self, message: str | bytes) -> None:
        try:
            report = PlayoutReport.model_validate_json(message)
        except ValidationError:
            self.spawn(self.close("invalid control message"))
            return
        self.speech.playout_delay_s = report.delay_s

    def track_received(self, track: MediaStreamTrack) -> None:
        if track.kind == "audio":
            self.spawn(self.listen(track))

    async def listen(self, track: MediaStreamTrack) -> None:
        """Microphone frames to the pipeline, in `INPUT_BATCH_S` packets."""
        downsample = Downsampler()
        batch = round(INPUT_RATE * INPUT_BATCH_S)
        pending: list[Samples] = []
        count = 0
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            samples = downsample(frame)
            if self.call is None or not self.connected.is_set():
                continue  # Nothing can reach the browser yet.
            pending.append(samples)
            count += len(samples)
            if count >= batch:
                packet = np.concatenate(pending).astype("<f4")
                pending, count = [], 0
                try:
                    await self.call.hear(packet.tobytes())
                except ValueError:
                    await self.close("invalid microphone audio")
                    return

    # Lifecycle.

    async def connection_changed(self) -> None:
        if self.pc.connectionState in ("failed", "closed"):
            await self.close(f"connection {self.pc.connectionState}")

    async def supervise(self) -> None:
        """Close a peer that never connects, or that reaches the call time limit."""
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                await self.connected.wait()
        except TimeoutError:
            await self.close("never connected")
            return
        await asyncio.sleep(MAX_CALL_S)
        self.send_event({"type": "error", "error": "This call reached its time limit. Start a new one."})
        await asyncio.sleep(0.5)  # Let the message leave before closing.
        await self.close("time limit")

    async def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed, self.close_reason = True, reason
        logger.info("WebRTC call closed: %s", reason)
        current = asyncio.current_task()
        others = [task for task in self.tasks if task is not current]
        for task in others:
            task.cancel()
        await asyncio.gather(*others, return_exceptions=True)
        if self.call is not None:
            await self.call.close()
        self.speech.stop()
        await self.pc.close()
        if self.on_closed is not None:
            self.on_closed(self)


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["offer"]
    sdp: str = Field(min_length=1, max_length=20000)
    echo_cancelled: bool


router = APIRouter(prefix="/webrtc")


def peers_of(app: FastAPI) -> set[Peer]:
    if not hasattr(app.state, "webrtc_peers"):
        app.state.webrtc_peers = set()
    return app.state.webrtc_peers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: close every peer on shutdown."""
    peers = peers_of(app)
    try:
        yield
    finally:
        await asyncio.gather(*(peer.close("server shutdown") for peer in list(peers)),
                             return_exceptions=True)


class RateLimit:
    """At most `limit` requests per client IP in any `window_s`; in memory, per process."""

    def __init__(self, limit: int, window_s: float = 60) -> None:
        self.limit, self.window_s = limit, window_s
        self.seen: dict[str, deque[float]] = {}

    def allow(self, client: str, now: float) -> bool:
        times = self.seen.setdefault(client, deque())
        while times and now - times[0] >= self.window_s:
            times.popleft()
        if len(times) >= self.limit:
            return False
        times.append(now)
        if len(self.seen) > 10000:  # Forget idle clients so memory stays bounded.
            self.seen = {key: value for key, value in self.seen.items()
                         if value and now - value[-1] < self.window_s}
        return True


# Each request mints TURN credentials; behind the tunnel uvicorn's proxy
# headers make request.client the browser's address.
ice_limit = RateLimit(int(os.getenv("WEBRTC_ICE_PER_MINUTE", "10")))


async def call_ice_servers() -> list[ice.IceServer]:
    try:
        return await ice.mint_ice_servers(MAX_CALL_S + 60)
    except (httpx.HTTPError, KeyError, ValueError) as error:
        logger.warning("TURN credentials failed: %s", type(error).__name__)
        raise HTTPException(503, "The media relay is unavailable. Try again later.") from error


@router.get("/ice-servers")
async def ice_servers(request: Request) -> dict[str, object]:
    """For the browser, before it creates its offer."""
    client = request.client.host if request.client else "unknown"
    if not ice_limit.allow(client, asyncio.get_running_loop().time()):
        raise HTTPException(429, "Too many calls started. Wait a minute and try again.")
    return {"ice_servers": await call_ice_servers()}


@router.post("/offer")
async def offer(offer: Offer, request: Request) -> dict[str, str]:
    engines: Engines | None = getattr(request.app.state, "engines", None)
    if engines is None:
        raise HTTPException(503, "Voice models are not loaded.")
    peers = peers_of(request.app)
    if len(peers) >= MAX_CALLS:
        raise HTTPException(503, "Too many calls right now. Try again shortly.")
    servers = await call_ice_servers()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**server) for server in servers]))
    peer = Peer(pc, engines.output_sample_rate, peers.discard)
    peers.add(peer)
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type="offer"))
        peer.tune_receivers()
        pc.addTrack(peer.speech)
        await pc.setLocalDescription(await pc.createAnswer())
    except Exception as error:
        logger.warning("Invalid WebRTC offer: %s", type(error).__name__)
        await peer.close("invalid offer")
        raise HTTPException(400, "Invalid offer.") from error
    if not offer.echo_cancelled:
        logger.warning("No browser echo cancellation; barge-in disabled for this call.")
    try:
        peer.call = await open_call(engines, peer.send, str(uuid4()), barge_in=offer.echo_cancelled,
                                    caller_id=await memory.web_caller_key(request.headers))
    except SpeechUnavailable as error:
        await peer.close("the speech service is unavailable")
        raise HTTPException(503, str(error)) from error
    peer.start()
    return {"type": "answer", "sdp": pc.localDescription.sdp}
