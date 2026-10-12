"""Hand-built cascaded voice pipeline: VAD + turn detection -> STT -> LLM -> TTS.

Audio timing lives here as plain asyncio tasks and queues. The agent (LangGraph)
is called once per finished user turn and only streams text back.
"""
import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from app.vad import CHUNK

logger = logging.getLogger("uvicorn.error")
Samples = NDArray[np.float32]
CHUNK_MS = CHUNK / 16  # 32 ms per VAD chunk at 16 kHz.
MAX_PACKET_BYTES = 16000 * 4  # At most one second of float32 PCM per message.
OUTPUT_PACKET_SAMPLES = 24000 // 5  # Send TTS audio in 200 ms messages.


class SpeechProbability(Protocol):
    def probability(self, chunk: Samples) -> float: ...
    def reset(self) -> None: ...


class TurnCompletion(Protocol):
    def completion_probability(self, samples: Samples) -> float: ...


@dataclass(frozen=True)
class Utterance:
    audio: Samples
    pause_ms: float  # Silence waited after the last speech before ending the turn.
    completion: float | None  # Smart Turn probability, None if the pause timed out.


class TurnDetector:
    """Silero finds speech; Smart Turn decides whether a pause ends the turn.

    Speech starts after `start_ms` of voiced audio (with pre-roll so the first
    syllable is kept). After `pause_ms` of silence Smart Turn scores the whole
    utterance; a confident "complete" ends the turn immediately, otherwise we
    keep listening until speech resumes or `max_pause_ms` forces the end.
    """

    def __init__(self, vad: SpeechProbability, turn: TurnCompletion, *,
                 speech_threshold: float = 0.5, completion_threshold: float = 0.5,
                 start_ms: float = 96, pause_ms: float = 200, max_pause_ms: float = 1500,
                 preroll_ms: float = 320, max_turn_s: float = 30) -> None:
        self.vad = vad
        self.turn = turn
        self.speech_threshold = speech_threshold
        self.completion_threshold = completion_threshold
        self.start_chunks = max(1, round(start_ms / CHUNK_MS))
        self.pause_chunks = max(1, round(pause_ms / CHUNK_MS))
        self.max_pause_chunks = max(self.pause_chunks, round(max_pause_ms / CHUNK_MS))
        self.max_turn_chunks = round(max_turn_s * 1000 / CHUNK_MS)
        self.preroll: deque[Samples] = deque(maxlen=max(1, round(preroll_ms / CHUNK_MS)))
        self.pending = np.empty(0, dtype=np.float32)
        self.discard()

    @property
    def speaking(self) -> bool:
        return self.utterance is not None

    def discard(self) -> None:
        """Drop any partial turn (for example, echo of the assistant's own voice)."""
        self.utterance: list[Samples] | None = None
        self.voiced = 0
        self.silent = 0
        self.preroll.clear()

    def feed(self, samples: Samples) -> list[Utterance]:
        self.pending = np.concatenate((self.pending, samples))
        finished = []
        while len(self.pending) >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            utterance = self.step(chunk)
            if utterance is not None:
                finished.append(utterance)
        return finished

    def step(self, chunk: Samples) -> Utterance | None:
        voiced = self.vad.probability(chunk) >= self.speech_threshold
        if self.utterance is None:
            self.preroll.append(chunk)
            self.voiced = self.voiced + 1 if voiced else 0
            if self.voiced >= self.start_chunks:
                self.utterance = list(self.preroll)
                self.silent = 0
            return None
        self.utterance.append(chunk)
        if voiced:
            self.silent = 0
            return None
        self.silent += 1
        if len(self.utterance) >= self.max_turn_chunks:
            return self.finish(None)
        if self.silent == self.pause_chunks:
            completion = self.turn.completion_probability(np.concatenate(self.utterance))
            if completion >= self.completion_threshold:
                return self.finish(completion)
        elif self.silent >= self.max_pause_chunks:
            return self.finish(None)
        return None

    def finish(self, completion: float | None) -> Utterance:
        assert self.utterance is not None
        audio = np.concatenate(self.utterance)
        pause_ms = self.silent * CHUNK_MS
        self.discard()
        return Utterance(audio, pause_ms, completion)


SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")
CLAUSE_END = re.compile(r"(?<=[,;:—])\s+")


class SentenceBuffer:
    """Cut streamed text into speakable pieces as early as is natural.

    The first piece may end at a clause boundary (a comma) so speech starts
    sooner; later pieces end at sentences, which sound smoother.
    """

    def __init__(self, first_clause_chars: int = 24) -> None:
        self.text = ""
        self.started = False
        self.first_clause_chars = first_clause_chars

    def push(self, delta: str) -> list[str]:
        self.text += delta
        pieces = []
        while True:
            match = SENTENCE_END.search(self.text)
            if match is None and not self.started:
                clause = CLAUSE_END.search(self.text)
                if clause and clause.start() >= self.first_clause_chars:
                    match = clause
            if match is None:
                return pieces
            piece, self.text = self.text[:match.start()].strip(), self.text[match.end():]
            if piece:
                pieces.append(piece)
                self.started = True

    def flush(self) -> str | None:
        piece, self.text = self.text.strip(), ""
        return piece or None


@dataclass
class Engines:
    """Everything a call needs, injected so tests can replace any stage."""
    new_detector: Callable[[], TurnDetector]
    transcribe: Callable[[Samples], Awaitable[str]]
    reply: Callable[[str, str], AsyncIterator[str]]  # (conversation_id, user text)
    synthesize: Callable[[str], Awaitable[Samples]]
    output_sample_rate: int = 24000


@dataclass
class TurnMetrics:
    """Milliseconds for one turn; together they approximate perceived latency."""
    endpoint_ms: float  # Silence waited before deciding the user had finished.
    stt_ms: float = 0
    llm_first_token_ms: float = 0
    tts_first_audio_ms: float = 0  # Synthesis time of the first spoken piece.
    started: float = field(default_factory=time.perf_counter)

    def response_ms(self) -> float:
        """From the user's last word to the first assistant audio being sent."""
        return self.endpoint_ms + (time.perf_counter() - self.started) * 1000

    def to_payload(self, first_audio_ms: float) -> dict[str, object]:
        return {"type": "metrics", "endpoint_ms": round(self.endpoint_ms),
                "stt_ms": round(self.stt_ms), "llm_first_token_ms": round(self.llm_first_token_ms),
                "tts_first_audio_ms": round(self.tts_first_audio_ms),
                "response_ms": round(first_audio_ms)}


Send = Callable[[dict[str, object] | bytes], Awaitable[None]]


def decode_pcm(packet: bytes) -> Samples:
    if not packet or len(packet) > MAX_PACKET_BYTES or len(packet) % 4:
        raise ValueError("Send up to one second of complete float32 PCM samples")
    samples = np.frombuffer(packet, dtype="<f4")
    if not np.isfinite(samples).all():
        raise ValueError("PCM contains non-finite samples")
    return samples


class Call:
    """One live call. Mic audio is always consumed; at most one reply runs at a time."""

    def __init__(self, engines: Engines, send: Send, conversation_id: str) -> None:
        self.engines = engines
        self.send = send
        self.conversation_id = conversation_id
        self.detector = engines.new_detector()
        self.reply_task: asyncio.Task[None] | None = None
        self.playing = False  # Reported by the browser player.

    @property
    def assistant_busy(self) -> bool:
        return self.playing or (self.reply_task is not None and not self.reply_task.done())

    def set_playing(self, active: bool) -> None:
        self.playing = active

    async def hear(self, packet: bytes) -> None:
        samples = decode_pcm(packet)
        busy = self.assistant_busy
        utterances = await asyncio.to_thread(self.detector.feed, samples)
        if busy:
            # Barge-in is not built yet: speech during a reply is the user talking
            # over it or echo of our own voice. Neither starts a new turn yet.
            if utterances:
                logger.info("Ignored %d utterance(s) while the assistant was speaking", len(utterances))
            self.detector.discard()
            return
        for utterance in utterances:
            self.reply_task = asyncio.create_task(self.respond(utterance))

    async def respond(self, utterance: Utterance) -> None:
        metrics = TurnMetrics(endpoint_ms=utterance.pause_ms)
        started = time.perf_counter()
        try:
            text = (await self.engines.transcribe(utterance.audio)).strip()
        except Exception as error:
            logger.warning("Transcription failed: %s", type(error).__name__)
            await self.send({"type": "notice", "text": "Didn't catch that. Please say it again."})
            return
        metrics.stt_ms = (time.perf_counter() - started) * 1000
        if not text:
            return
        await self.send({"type": "transcript", "text": text})

        pieces: asyncio.Queue[str | None] = asyncio.Queue(maxsize=8)
        speaker = asyncio.create_task(self.speak(pieces, metrics))
        try:
            try:
                await self.think(text, pieces, metrics)
            except Exception as error:
                # Pieces already queued are still spoken; nothing new is generated.
                logger.warning("Reply generation failed: %s", type(error).__name__)
                await self.send({"type": "notice", "text": "Sorry, I lost my train of thought."})
            await pieces.put(None)
            await speaker
        finally:
            if not speaker.done():  # Cancelled mid-reply (call ended, or later: barge-in).
                speaker.cancel()
                await asyncio.gather(speaker, return_exceptions=True)
        await self.send({"type": "reply_end"})

    async def think(self, text: str, pieces: "asyncio.Queue[str | None]", metrics: TurnMetrics) -> None:
        """Stream LLM text to the browser and hand speakable pieces to TTS."""
        sentences = SentenceBuffer()
        started = time.perf_counter()
        async for delta in self.engines.reply(self.conversation_id, text):
            if not metrics.llm_first_token_ms:
                metrics.llm_first_token_ms = (time.perf_counter() - started) * 1000
            await self.send({"type": "text", "text": delta})
            for piece in sentences.push(delta):
                await pieces.put(piece)
        if (rest := sentences.flush()) is not None:
            await pieces.put(rest)

    async def speak(self, pieces: "asyncio.Queue[str | None]", metrics: TurnMetrics) -> None:
        """Synthesize pieces in order while the LLM is still generating."""
        first = True
        while (piece := await pieces.get()) is not None:
            started = time.perf_counter()
            try:
                audio = await self.engines.synthesize(piece)
            except Exception as error:
                logger.warning("Speech synthesis failed: %s", type(error).__name__)
                continue
            if first and len(audio):
                metrics.tts_first_audio_ms = (time.perf_counter() - started) * 1000
            for start in range(0, len(audio), OUTPUT_PACKET_SAMPLES):
                await self.send(audio[start:start + OUTPUT_PACKET_SAMPLES].astype("<f4").tobytes())
            if first and len(audio):
                first = False
                payload = metrics.to_payload(metrics.response_ms())
                logger.info("Turn latency: %s", payload)
                await self.send(payload)

    async def close(self) -> None:
        if self.reply_task is not None and not self.reply_task.done():
            self.reply_task.cancel()
            await asyncio.gather(self.reply_task, return_exceptions=True)
