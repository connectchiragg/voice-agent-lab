"""Deepgram over WebSocket: Flux (speech-to-text with turn detection) and Aura-2 (text-to-speech).

One Flux socket and one Aura socket per call, opened when the call starts and
kept open. These adapters only speak Deepgram's protocol; what a turn event
means for the conversation is decided in `app/pipeline.py`.

Limits from Deepgram's documentation (checked 2026-10-15), and what we derive:

- A streaming socket that receives no audio for about 10 s is closed (NET-0001),
  and Flux has no KeepAlive message. The browser streams the microphone
  continuously; if it stops, we send silence after SILENCE_KEEPALIVE_S.
- Aura (/v1/speak) accepts at most 20 Flush messages per 60 s. We flush once
  per segment of whole sentences, and wait for a slot rather than be refused.
- Neither /v2/listen nor /v1/speak documents a maximum session length, so any
  close is treated as possible: Flux reconnects at once, Aura on next use.
- Audio may be streamed at most 1.25x real time, so after a reconnect only the
  last RECONNECT_BUFFER_S of held-back audio is sent.
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import numpy as np
import websockets
from numpy.typing import NDArray
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("uvicorn.error")
Samples = NDArray[np.float32]
INPUT_SAMPLE_RATE = 16000  # linear16 from the browser's 16 kHz float32.
OUTPUT_SAMPLE_RATE = 24000  # Aura's default; the browser plays any rate it is told.
SILENCE_KEEPALIVE_S = 5.0  # Half of the ~10 s no-audio window.
KEEPALIVE_CHECK_S = 1.0
RECONNECT_BUFFER_S = 1.0
# Flux word timings are coarse: on `say` clips the last word's end was up to
# 0.5 s before or after the audio's. The end of speech is refined to the last
# packet this loud (peak, linear16) within SPEECH_END_SEARCH_S after it.
LOUD_PEAK = 655  # 2% of full scale; browser noise suppression keeps silence far below.
SPEECH_END_SEARCH_S = 1.0
RECONNECT_DELAYS_S = (0.0, 0.5, 1.0, 2.0)
FLUSH_LIMIT, FLUSH_WINDOW_S = 20, 60.0
AUDIO_TIMEOUT_S = 5.0  # Aura sent nothing for this long mid-segment: give up on it.
CLEAR_TIMEOUT_S = 2.0
# Markdown and symbols that a voice should not read aloud.
UNSPOKEN = re.compile(r"[*_#`~>|\[\]]+")


class Socket(Protocol):
    """The part of a websockets client connection the adapters use (tests fake it)."""
    async def send(self, message: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connect = Callable[[str, dict[str, str]], Awaitable[Socket]]


async def open_socket(url: str, headers: dict[str, str]) -> Socket:
    # websockets pings every 20 s, which also keeps an idle Aura socket open.
    return await websockets.connect(url, additional_headers=headers, max_size=None,
                                    open_timeout=5, close_timeout=1)


@dataclass(frozen=True)
class DeepgramSettings:
    api_key: str
    # Deepgram's India region. From the Mumbai VM (2026-10-15): Aura first byte
    # 71 ms vs 290 ms and Flux EndOfTurn ~200 ms sooner than api.deepgram.com.
    host: str = "api.in.deepgram.com"
    voice: str = "aura-2-thalia-en"
    eot_threshold: float = 0.7
    eager_eot_threshold: float | None = 0.4  # None: no EagerEndOfTurn, no speculation.
    eot_timeout_ms: int = 3000
    # linear16 both ways. Browser: 16 kHz in, 24 kHz out. Phone (Telnyx PCMU):
    # 8 kHz both ways, so phone audio is never resampled.
    input_sample_rate: int = INPUT_SAMPLE_RATE
    output_sample_rate: int = OUTPUT_SAMPLE_RATE

    @classmethod
    def from_env(cls) -> "DeepgramSettings":
        eager = os.getenv("FLUX_EAGER_EOT_THRESHOLD", "0.4").strip()
        return cls(api_key=os.environ["DEEPGRAM_API_KEY"],
                   host=os.getenv("DEEPGRAM_HOST", cls.host),
                   voice=os.getenv("DEEPGRAM_VOICE", cls.voice),
                   eot_threshold=float(os.getenv("FLUX_EOT_THRESHOLD", cls.eot_threshold)),
                   eager_eot_threshold=float(eager) if eager else None,
                   eot_timeout_ms=int(os.getenv("FLUX_EOT_TIMEOUT_MS", cls.eot_timeout_ms)))

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self.api_key}"}

    def flux_url(self) -> str:
        query: dict[str, Any] = {"model": "flux-general-en", "encoding": "linear16",
                                 "sample_rate": self.input_sample_rate, "eot_threshold": self.eot_threshold,
                                 "eot_timeout_ms": self.eot_timeout_ms}
        if self.eager_eot_threshold is not None:
            query["eager_eot_threshold"] = self.eager_eot_threshold
        return f"wss://{self.host}/v2/listen?{urlencode(query)}"

    def aura_url(self) -> str:
        query = {"model": self.voice, "encoding": "linear16", "sample_rate": self.output_sample_rate}
        return f"wss://{self.host}/v1/speak?{urlencode(query)}"


def to_linear16(samples: Samples) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


@dataclass(frozen=True)
class TurnEvent:
    """A Flux TurnInfo event, or "Reset" when the socket was reconnected mid-call."""
    event: str  # StartOfTurn, Update, EagerEndOfTurn, TurnResumed, EndOfTurn, Reset
    transcript: str = ""
    received: float = 0.0  # perf_counter when the message arrived.
    # perf_counter when the audio holding the user's last speech reached us, if
    # Flux gave word timings: the end of speech, by packet arrival.
    speech_ended: float | None = None


class FluxListener:
    """One Flux socket per call: audio in, turn events out (to `on_event`, in order).

    Each packet's arrival time and loudness are remembered, so the end of the
    last word (seconds into the stream) maps back to when that audio reached
    the server: latency counts from packet arrival, not from Deepgram's clock
    or our own processing.

    If the socket drops, it reconnects and sends the audio held back meanwhile
    (at most RECONNECT_BUFFER_S), and reports a "Reset" event: Flux starts a
    new turn, so the turn in progress is lost. If every attempt fails, the
    next `send` raises and the call ends.
    """

    def __init__(self, settings: DeepgramSettings, on_event: Callable[[TurnEvent], Awaitable[None]],
                 connect: Connect = open_socket) -> None:
        self.settings = settings
        self.on_event = on_event
        self.connect = connect
        self.socket: Socket | None = None
        self.reader: asyncio.Task[None] | None = None
        self.keepalive: asyncio.Task[None] | None = None
        self.sent_samples = 0  # On the current socket; Flux timestamps restart with it.
        # (samples sent including this packet, its arrival time, loud?)
        self.arrivals: deque[tuple[int, float, bool]] = deque(maxlen=3000)
        self.held: deque[tuple[bytes, float]] = deque()  # Audio received while reconnecting.
        self.last_sent = 0.0
        self.failed: BaseException | None = None
        self.closing = False

    async def start(self) -> None:
        await self.open()
        self.keepalive = asyncio.create_task(self.keep_alive())

    async def open(self) -> None:
        socket = await self.connect(self.settings.flux_url(), self.settings.headers)
        self.socket, self.sent_samples = socket, 0
        self.arrivals.clear()
        self.reader = asyncio.create_task(self.read(socket))

    async def send(self, pcm: bytes, arrived: float | None = None) -> None:
        """Stream linear16 audio; `arrived` is when its packet reached us (perf_counter)."""
        if self.failed is not None:
            raise ConnectionError("Speech recognition is unavailable.") from self.failed
        arrived = time.perf_counter() if arrived is None else arrived
        if self.socket is None:  # Reconnecting.
            self.hold(pcm, arrived)
            return
        try:
            await self.socket.send(pcm)
        except ConnectionClosed:
            self.hold(pcm, arrived)  # The reader notices the close and reconnects.
            return
        self.sent_samples += len(pcm) // 2
        loud = bool(len(pcm) > 1 and np.abs(np.frombuffer(pcm, "<i2")).max() >= LOUD_PEAK)
        self.arrivals.append((self.sent_samples, arrived, loud))
        self.last_sent = time.perf_counter()

    def hold(self, pcm: bytes, arrived: float) -> None:
        self.held.append((pcm, arrived))
        limit = RECONNECT_BUFFER_S * self.settings.input_sample_rate * 2
        while sum(len(packet) for packet, _ in self.held) > limit:
            self.held.popleft()

    def speech_end_arrival(self, word_end_s: float) -> float | None:
        """When the last loud packet from the last word's end on arrived (else the word end's)."""
        rate = self.settings.input_sample_rate
        word_end = word_end_s * rate
        limit = word_end + SPEECH_END_SEARCH_S * rate
        found = None
        for sent, arrived, loud in self.arrivals:
            if sent < word_end:
                continue
            if found is None or (loud and sent - rate // 10 < limit):
                found = arrived
            if sent >= limit:
                break
        return found

    def turn_event(self, message: dict[str, Any], received: float) -> TurnEvent:
        words = message.get("words") or []
        end = words[-1].get("end") if words else None
        return TurnEvent(message["event"], str(message.get("transcript", "")).strip(), received,
                         self.speech_end_arrival(float(end)) if end is not None else None)

    async def read(self, socket: Socket) -> None:
        try:
            async for raw in socket:
                received = time.perf_counter()
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "TurnInfo":
                    try:
                        await self.on_event(self.turn_event(message, received))
                    except Exception:  # A bug in one handler must not stop listening.
                        logger.exception("Turn event handler failed")
                elif kind in ("Error", "Warning", "ConfigureFailure"):
                    logger.warning("Flux %s %s: %s", kind, message.get("code"), message.get("description"))
        except ConnectionClosed as error:
            if not self.closing:
                logger.warning("Flux socket closed (%s)", error.rcvd.code if error.rcvd else "no close frame")
        if not self.closing:
            await self.reconnect()

    async def reconnect(self) -> None:
        self.socket = None
        await self.on_event(TurnEvent("Reset", received=time.perf_counter()))
        for delay in RECONNECT_DELAYS_S:
            await asyncio.sleep(delay)
            try:
                await self.open()
            except Exception as error:  # Network, DNS, or a refused handshake.
                logger.warning("Flux reconnect failed: %s", type(error).__name__)
                self.failed = error
                continue
            self.failed = None
            logger.info("Flux reconnected; resending %d held packets", len(self.held))
            held, self.held = self.held, deque()
            for pcm, arrived in held:
                await self.send(pcm, arrived)
            return

    async def keep_alive(self) -> None:
        silence = bytes(self.settings.input_sample_rate // 10 * 2)  # 100 ms, like a browser packet.
        while True:
            await asyncio.sleep(KEEPALIVE_CHECK_S)
            if self.socket is not None and time.perf_counter() - self.last_sent > SILENCE_KEEPALIVE_S:
                await self.send(silence)

    async def close(self) -> None:
        self.closing = True
        for task in (self.keepalive, self.reader):
            if task is not None:
                task.cancel()
        if self.socket is not None:
            try:
                await self.socket.send(json.dumps({"type": "CloseStream"}))
                await self.socket.close()
            except (ConnectionClosed, OSError):
                pass
        await asyncio.gather(*(task for task in (self.keepalive, self.reader) if task is not None),
                             return_exceptions=True)


def speakable(text: str) -> str:
    return re.sub(r"\s+", " ", UNSPOKEN.sub("", text)).strip()


CLOSED = object()  # Queued when the Aura socket stops delivering messages.


class AuraSpeaker:
    """One Aura-2 socket per call: text in, float32 audio out (24 kHz unless configured).

    `synthesize` sends one segment as Speak + Flush and yields its audio until
    Deepgram confirms the flush, so segments are spoken one at a time, in
    order, and each segment's audio length is exact. Aura generates several
    times faster than real time, so the next segment's audio arrives long
    before the browser has played the previous one.

    A segment abandoned midway (barge-in) is followed by Clear: Deepgram stops
    generating, and everything up to Cleared is dropped before the next one.
    """

    def __init__(self, settings: DeepgramSettings, connect: Connect = open_socket,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.connect = connect
        self.clock = clock
        self.socket: Socket | None = None
        self.messages: asyncio.Queue[Any] = asyncio.Queue()
        self.reader: asyncio.Task[None] | None = None
        self.clearing: asyncio.Task[None] | None = None
        self.flushes: deque[float] = deque()
        self.carry = b""  # An odd trailing byte, never expected from Aura.

    async def start(self) -> None:
        await self.open()

    async def open(self) -> None:
        socket = await self.connect(self.settings.aura_url(), self.settings.headers)
        self.socket, self.messages, self.carry = socket, asyncio.Queue(), b""
        self.reader = asyncio.create_task(self.read(socket, self.messages))

    @staticmethod
    async def read(socket: Socket, messages: "asyncio.Queue[Any]") -> None:
        try:
            async for message in socket:
                messages.put_nowait(message)
        except ConnectionClosed:
            pass
        finally:
            messages.put_nowait(CLOSED)

    async def drop_socket(self) -> None:
        """Forget a closed or confused socket; the next segment opens a new one."""
        socket, self.socket = self.socket, None
        if self.reader is not None:
            self.reader.cancel()
        if socket is not None:
            try:
                await socket.close()
            except (ConnectionClosed, OSError):
                pass

    async def flush_slot(self) -> None:
        """Wait until a Flush stays within Deepgram's 20 per 60 s."""
        while self.flushes and self.clock() - self.flushes[0] >= FLUSH_WINDOW_S:
            self.flushes.popleft()
        if len(self.flushes) >= FLUSH_LIMIT:
            wait_s = self.flushes[0] + FLUSH_WINDOW_S - self.clock()
            logger.warning("Aura flush limit reached; waiting %.1f s", wait_s)
            await asyncio.sleep(wait_s)
            self.flushes.popleft()
        self.flushes.append(self.clock())

    def pcm(self, data: bytes) -> Samples:
        data, self.carry = self.carry + data, b""
        if len(data) % 2:
            data, self.carry = data[:-1], data[-1:]
        return np.frombuffer(data, "<i2").astype(np.float32) / 32768

    async def synthesize(self, text: str) -> AsyncIterator[Samples]:
        """Yield the audio of `text` as Aura streams it. Close the iterator when abandoning it."""
        text = speakable(text)
        if not text:
            return
        if self.clearing is not None:
            await asyncio.gather(self.clearing, return_exceptions=True)
            self.clearing = None
        if self.socket is None:
            await self.open()
        assert self.socket is not None
        await self.flush_slot()
        flushed = False
        try:
            await self.socket.send(json.dumps({"type": "Speak", "text": text}))
            await self.socket.send(json.dumps({"type": "Flush"}))
            while True:
                async with asyncio.timeout(AUDIO_TIMEOUT_S):
                    message = await self.messages.get()
                if message is CLOSED:
                    flushed = True  # Nothing left to clear.
                    await self.drop_socket()
                    raise ConnectionError("Aura closed the socket mid-segment.")
                if isinstance(message, bytes):
                    yield self.pcm(message)
                    continue
                event = json.loads(message)
                if event.get("type") == "Flushed":
                    flushed = True
                    return
                if event.get("type") == "Warning":
                    logger.warning("Aura warning %s: %s", event.get("code"), event.get("description"))
        except ConnectionClosed:
            flushed = True
            await self.drop_socket()
            raise
        finally:
            if not flushed:
                self.clearing = asyncio.ensure_future(self.clear())

    async def clear(self) -> None:
        """Stop the abandoned segment and drop its remaining audio, up to Cleared."""
        if self.socket is None:
            return
        try:
            await self.socket.send(json.dumps({"type": "Clear"}))
            async with asyncio.timeout(CLEAR_TIMEOUT_S):
                while (message := await self.messages.get()) is not CLOSED:
                    if isinstance(message, str) and json.loads(message).get("type") == "Cleared":
                        return
        except (TimeoutError, ConnectionClosed, OSError) as error:
            logger.warning("Aura clear failed (%s); reopening the socket", type(error).__name__)
        await self.drop_socket()

    async def close(self) -> None:
        if self.clearing is not None:
            self.clearing.cancel()
        await self.drop_socket()
