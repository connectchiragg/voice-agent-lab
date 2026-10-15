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


class SpeechListener(Protocol):
    """Follows each utterance as it is heard (the streaming transcriber)."""
    def on_speech_start(self, preroll: Samples) -> None: ...
    def on_speech(self, samples: Samples) -> None: ...
    def on_speech_end(self, silence_s: float) -> str: ...  # Trailing silence already fed.
    def on_discard(self) -> None: ...


@dataclass(frozen=True)
class Utterance:
    audio: Samples
    pause_ms: float  # Silence waited after the last speech before ending the turn.
    completion: float | None  # Smart Turn probability, None if the pause timed out.
    confident_ms: float = 0  # Speech above the confident threshold, for barge-in checks.
    text: str = ""  # Final transcript from the streaming listener.
    stt_ms: float = 0  # Time to finalise the transcript after the turn ended.
    # From receiving the packet with the last speech to deciding the turn ended:
    # the pause plus any backlog of audio waiting to be processed.
    heard_to_decided_ms: float = 0


class TurnDetector:
    """Silero finds speech; Smart Turn decides whether a pause ends the turn.

    Speech starts after `start_ms` of voiced audio (with pre-roll so the first
    syllable is kept). After `pause_ms` of silence Smart Turn scores the whole
    utterance; a confident "complete" ends the turn immediately, otherwise we
    keep listening until speech resumes or `max_pause_ms` forces the end.

    It also counts chunks above a stricter `confident_threshold`; while the
    assistant talks, only that much clear speech counts as an interruption.

    `on_decided`, if set, gets the listener's live text the moment a turn ends,
    before the transcript is finalised (which takes ~0.5 s on one AWS core). It
    is called on the detector's worker thread.
    """

    def __init__(self, vad: SpeechProbability, turn: TurnCompletion, *,
                 speech_threshold: float = 0.5, confident_threshold: float = 0.8,
                 completion_threshold: float = 0.5,
                 start_ms: float = 96, pause_ms: float = 200, max_pause_ms: float = 1500,
                 preroll_ms: float = 320, max_turn_s: float = 30,
                 listener: SpeechListener | None = None) -> None:
        self.vad = vad
        self.turn = turn
        self.listener = listener
        self.speech_threshold = speech_threshold
        self.confident_threshold = confident_threshold
        self.completion_threshold = completion_threshold
        self.start_chunks = max(1, round(start_ms / CHUNK_MS))
        self.pause_chunks = max(1, round(pause_ms / CHUNK_MS))
        self.max_pause_chunks = max(self.pause_chunks, round(max_pause_ms / CHUNK_MS))
        self.max_turn_chunks = round(max_turn_s * 1000 / CHUNK_MS)
        self.preroll: deque[Samples] = deque(maxlen=max(1, round(preroll_ms / CHUNK_MS)))
        self.pending = np.empty(0, dtype=np.float32)
        self.on_decided: Callable[[str], None] | None = None
        self.received = self.last_voice_received = 0.0  # perf_counter times of packets.
        self.discard()

    @property
    def speaking(self) -> bool:
        return self.utterance is not None

    @property
    def confident_ms(self) -> float:
        """Clear speech in the current utterance so far (0 when not speaking)."""
        return self.confident * CHUNK_MS if self.speaking else 0

    def discard(self) -> None:
        """Drop any partial turn (for example, echo of the assistant's own voice)."""
        if getattr(self, "utterance", None) is not None and self.listener is not None:
            self.listener.on_discard()
        self.utterance: list[Samples] | None = None
        self.voiced = 0
        self.confident = 0  # Consecutive before speech starts; total during it.
        self.silent = 0
        self.preroll.clear()

    def feed(self, samples: Samples, received: float | None = None) -> list[Utterance]:
        """`received`: when the packet arrived (perf_counter), if earlier than now."""
        self.received = time.perf_counter() if received is None else received
        self.pending = np.concatenate((self.pending, samples))
        finished = []
        while len(self.pending) >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            utterance = self.step(chunk)
            if utterance is not None:
                finished.append(utterance)
        return finished

    def step(self, chunk: Samples) -> Utterance | None:
        probability = self.vad.probability(chunk)
        voiced = probability >= self.speech_threshold
        confident = probability >= self.confident_threshold
        if self.utterance is None:
            self.preroll.append(chunk)
            self.voiced = self.voiced + 1 if voiced else 0
            self.confident = self.confident + 1 if confident else 0
            if self.voiced >= self.start_chunks:
                self.utterance = list(self.preroll)
                self.silent = 0
                self.last_voice_received = self.received
                if self.listener is not None:
                    self.listener.on_speech_start(np.concatenate(self.utterance))
            return None
        self.utterance.append(chunk)
        if self.listener is not None:
            self.listener.on_speech(chunk)
        self.confident += confident
        if voiced:
            self.silent = 0
            self.last_voice_received = self.received
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
        pause_ms, confident_ms = self.silent * CHUNK_MS, self.confident_ms
        heard_to_decided_ms = (time.perf_counter() - self.last_voice_received) * 1000
        text, stt_ms = "", 0.0
        if self.listener is not None:
            if self.on_decided is not None:
                self.on_decided(getattr(self.listener, "text", ""))
            # Every chunk since speech start was fed, including the pause just heard.
            text = self.listener.on_speech_end(self.silent * CHUNK_MS / 1000)
            stt_ms = getattr(self.listener, "final_ms", 0.0)
        self.utterance = None  # Finished, not discarded: the listener already closed it.
        self.discard()
        return Utterance(audio, pause_ms, completion, confident_ms, text, stt_ms,
                         heard_to_decided_ms)


WORD = re.compile(r"\S+(\s+|$)")
SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*$")
CLAUSE_END = re.compile(r"[,;:—–]$")
# Words a spoken piece should not end on ("...a top choice for").
DANGLING = frozenset("a an the to of for in on at by with from and or but nor so as is are was "
                     "were be my your our their its his her this that these those which who".split())


class SpeechChunker:
    """Cut streamed LLM text into pieces for TTS, growing them over the reply.

    Kokoro costs ~240 ms plus ~0.22 s per second of speech on an M2, but about
    0.7 s per second on one AWS core, so a short first piece starts audio
    sooner. Each later piece is synthesized while the previous one plays, so it
    may only grow by about the inverse of that ratio, or the listener hears a
    gap. On that core (3, 5, 7, 10, 14) cut first-piece time by ~200 ms and
    estimated gaps by two thirds against (6, 12); (3, 10) made gaps worse.
    After the schedule, pieces are whole sentences for the most natural prosody.

    Within a piece's word budget (plus a little lookahead) we cut at the first
    sentence end, or a clause end once a few words are in. With no boundary,
    we cut at the budget, never on a dangling word like "the" or "for".
    """

    def __init__(self, schedule: tuple[int, ...] = (3, 5, 7, 10, 14), sentence_cap: int = 30,
                 lookahead: int = 3, min_clause_words: int = 3) -> None:
        self.text = ""
        self.pieces = 0
        self.schedule = schedule
        self.sentence_cap = sentence_cap
        self.lookahead = lookahead
        self.min_clause_words = min_clause_words

    def push(self, delta: str) -> list[str]:
        self.text += delta
        return self.drain(final=False)

    def flush(self) -> list[str]:
        return self.drain(final=True)

    def drain(self, *, final: bool) -> list[str]:
        pieces = []
        while (cut := self.next_cut(final)) is not None:
            piece, self.text = self.text[:cut].strip(), self.text[cut:]
            if piece:
                pieces.append(piece)
                self.pieces += 1
        return pieces

    def next_cut(self, final: bool) -> int | None:
        """Character offset ending the next piece, or None to wait for more text."""
        words = [match for match in WORD.finditer(self.text)
                 if final or match.group(1)]  # Unless final, the last word may be partial.
        if not words:
            return None
        sentences_only = self.pieces >= len(self.schedule)
        budget = self.sentence_cap if sentences_only else self.schedule[self.pieces]
        for count, match in enumerate(words, start=1):
            word = match.group(0).strip()
            if SENTENCE_END.search(word) or "\n" in match.group(1):
                return match.end()
            if (not sentences_only and count >= self.min_clause_words
                    and CLAUSE_END.search(word)):
                return match.end()
            if count >= budget + self.lookahead:
                return self.forced_cut(words, budget)
        return words[-1].end() if final else None

    def forced_cut(self, words: list[re.Match[str]], budget: int) -> int:
        """No natural boundary in reach: prefer the last clause end, else trim danglers.

        Here a two-word clause ("Sure thing,") still beats cutting mid-phrase.
        """
        for index in range(budget, 1, -1):
            if CLAUSE_END.search(words[index - 1].group(0).strip()):
                return words[index - 1].end()
        index = budget
        while index > 2 and self.dangling(words[index - 1]):
            index -= 1
        # Short budgets can run out of words to trim ("It is a"): extend instead.
        while self.dangling(words[index - 1]) and index < len(words):
            index += 1
        return words[index - 1].end()

    @staticmethod
    def dangling(word: re.Match[str]) -> bool:
        return word.group(0).strip().lower() in DANGLING


@dataclass(frozen=True)
class ReplyModel:
    """In a reply stream: the LLM that generated the text after it (for metrics only)."""
    name: str


ReplyEvent = str | ReplyModel


class Transcriber(SpeechListener, Protocol):
    def take_partial(self) -> str | None: ...


async def forget_interruption(conversation_id: str, user_text: str, heard: str) -> None:
    """Default for engines without history: nothing to record."""


def always() -> bool:
    return True


@dataclass
class Engines:
    """Everything a call needs, injected so tests can replace any stage."""
    new_transcriber: Callable[[], "Transcriber"]
    new_detector: Callable[["Transcriber"], TurnDetector]  # Wires the transcriber in.
    reply: Callable[[str, str], AsyncIterator[ReplyEvent]]  # (conversation_id, user text)
    synthesize: Callable[[str], Awaitable[Samples]]
    # (conversation_id, user text, assistant text the user actually heard)
    record_interruption: Callable[[str, str, str], Awaitable[None]] = forget_interruption
    output_sample_rate: int = 24000
    # Optional early reply from the partial transcript: (conversation_id, text,
    # committed). It must store nothing and run no side-effecting tool until
    # `committed` is set. None disables speculation.
    speculate: Callable[[str, str, asyncio.Event], AsyncIterator[ReplyEvent]] | None = None
    may_speculate: Callable[[], bool] = always  # False near the provider's rate limit.


def heard_text(spoken: list[tuple[str, float]], played_s: float) -> str:
    """The part of a reply the user heard, given seconds of its audio played.

    Whole pieces count while their audio fits; in the piece playing at the
    interruption, words are spread evenly over its audio and the word being
    spoken counts as heard. The browser reveals captions on the same rule.
    """
    heard = []
    for text, seconds in spoken:
        if played_s >= seconds:
            heard.append(text)
            played_s -= seconds
            continue
        words = text.split()
        keep = (min(len(words), int(len(words) * played_s / seconds) + 1)
                if played_s > 0 and seconds > 0 else 0)
        if keep:
            heard.append(" ".join(words[:keep]) + "...")
        break
    return " ".join(heard)


def same_words(first: str, second: str) -> bool:
    """Equal ignoring case, punctuation and spacing ("What's up?" == "whats up")."""
    def normal(text: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())
    return normal(first) == normal(second)


class Speculation:
    """A reply started from the live transcript while STT finalises the turn.

    Its events (text, filler, anything after tool calls) are buffered, never
    spoken, until `commit`; `replay` then yields the buffer and continues live.
    Cancelling closes the LLM stream, so no more tokens are generated.
    """

    def __init__(self, engines: Engines, conversation_id: str, text: str) -> None:
        assert engines.speculate is not None
        self.text = text
        self.started = time.perf_counter()
        self.first_token_at: float | None = None
        self.deltas: list[ReplyEvent] = []
        self.error: Exception | None = None
        self.done = False
        self.cancelled = False
        self.changed = asyncio.Event()
        self.committed = asyncio.Event()
        self.task = asyncio.create_task(
            self.run(engines.speculate(conversation_id, text, self.committed)))

    async def run(self, stream: AsyncIterator[ReplyEvent]) -> None:
        try:
            async for delta in stream:
                if self.first_token_at is None and isinstance(delta, str):
                    self.first_token_at = time.perf_counter()
                self.deltas.append(delta)
                self.changed.set()
        except Exception as error:
            self.error = error
        finally:
            self.done = True
            self.changed.set()

    @property
    def failed(self) -> bool:
        """Failed before the commit: a fresh call is better than a broken reply."""
        return self.error is not None

    def cancel(self, reason: str) -> None:
        """Close the LLM stream; nothing was spoken or stored."""
        if not self.cancelled:
            self.cancelled = True
            logger.info("Speculative reply cancelled: %s", reason)
        self.task.cancel()

    def commit(self) -> AsyncIterator[ReplyEvent]:
        self.committed.set()
        return self.replay()

    async def replay(self) -> AsyncIterator[ReplyEvent]:
        index = 0
        while True:
            while index < len(self.deltas):
                yield self.deltas[index]
                index += 1
            if self.done:
                if self.error is not None:
                    raise self.error
                return
            self.changed.clear()
            if index == len(self.deltas) and not self.done:
                await self.changed.wait()

    async def close(self) -> None:
        """Cancel the stream (a no-op once finished) and wait for it to stop."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@dataclass
class TurnMetrics:
    """Milliseconds for one turn; together they approximate perceived latency."""
    endpoint_ms: float  # Silence waited before deciding the user had finished.
    stt_ms: float = 0
    # From the final transcript; negative when a speculative reply started earlier.
    llm_first_token_ms: float | None = None
    tts_first_audio_ms: float = 0  # Synthesis time of the first spoken piece.
    heard_to_decided_ms: float = 0  # Last speech packet received -> turn ended.
    speculative: str = "skipped"  # Or "committed" / "cancelled".
    llm_model: str = ""  # The model that produced the reply's first token.
    started: float = field(default_factory=time.perf_counter)  # Final transcript ready.

    def first_token(self, at: float) -> None:
        if self.llm_first_token_ms is None:
            self.llm_first_token_ms = (at - self.started) * 1000

    def response_ms(self) -> float:
        """From the user's last word to the first assistant audio being sent."""
        return self.endpoint_ms + self.stt_ms + (time.perf_counter() - self.started) * 1000

    def to_payload(self, first_audio_ms: float) -> dict[str, object]:
        return {"type": "metrics", "endpoint_ms": round(self.endpoint_ms),
                "stt_ms": round(self.stt_ms), "llm_first_token_ms": round(self.llm_first_token_ms or 0),
                "tts_first_audio_ms": round(self.tts_first_audio_ms),
                "heard_to_decided_ms": round(self.heard_to_decided_ms),
                "speculative": self.speculative, "llm_model": self.llm_model,
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
    """One live call. Mic audio is always consumed; at most one reply runs at a time.

    Barge-in: while the assistant is busy, `barge_in_ms` of confident user speech
    cancels the reply, tells the browser to flush its audio, and records only
    what the user actually heard. That speech then continues as the next turn.

    Speculation: when a turn ends, at most one reply starts from the live text
    while STT finalises. It is committed if the final transcript has the same
    words, and cancelled if they differ or the user speaks again first.
    """

    def __init__(self, engines: Engines, send: Send, conversation_id: str, *,
                 barge_in: bool = True, barge_in_ms: float = 250,
                 played_report_timeout_s: float = 1.0) -> None:
        self.engines = engines
        self.send = send
        self.conversation_id = conversation_id
        self.transcriber = engines.new_transcriber()
        self.detector = engines.new_detector(self.transcriber)
        self.barge_in = barge_in
        self.barge_in_ms = barge_in_ms
        self.played_report_timeout_s = played_report_timeout_s
        self.reply_task: asyncio.Task[None] | None = None
        self.history_task: asyncio.Task[None] | None = None
        self.played: asyncio.Future[float] | None = None
        self.playing = False  # Reported by the browser player.
        self.user_text: str | None = None  # Transcript the current reply answers.
        self.spoken: list[tuple[str, float]] = []  # (piece, audio seconds) sent so far.
        self.overlapped = False  # Current utterance began while the assistant spoke.
        self.speculation: Speculation | None = None  # Started for the turn just decided.
        self.loop: asyncio.AbstractEventLoop | None = None
        self.detector.on_decided = self.turn_decided

    @property
    def assistant_busy(self) -> bool:
        return self.playing or (self.reply_task is not None and not self.reply_task.done())

    def set_playing(self, active: bool) -> None:
        self.playing = active

    def report_played(self, seconds: float) -> None:
        """Browser reply to an interrupt: seconds of the reply's audio it played."""
        if self.played is not None and not self.played.done():
            self.played.set_result(max(0.0, seconds))

    async def hear(self, packet: bytes) -> None:
        samples = decode_pcm(packet)
        self.loop = asyncio.get_running_loop()
        utterances = await asyncio.to_thread(self.detector.feed, samples, time.perf_counter())
        # A speculation started during feed belongs to its last utterance, if answered.
        speculation, self.speculation = self.speculation, None
        if speculation is not None and self.detector.speaking:
            speculation.cancel("the user spoke again")  # The turn is still answered.
        claimed = False
        try:
            claimed = await self.handle(utterances, speculation)
        finally:
            if speculation is not None and not claimed:
                speculation.cancel("the turn was not answered")

    def turn_decided(self, text: str) -> None:
        """Detector worker thread: a turn just ended with this live text."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.start_speculation, text)

    def start_speculation(self, text: str) -> None:
        if self.engines.speculate is None or self.speculation is not None:
            return  # Disabled, or this turn already has one (at most one per turn).
        history_pending = self.history_task is not None and not self.history_task.done()
        if not text.strip() or self.assistant_busy or history_pending:
            return  # Nothing to say yet, or the turn may not be answered as is.
        if not self.engines.may_speculate():
            logger.info("Speculative reply skipped: near the LLM rate limit")
            return
        logger.info("Speculative reply started from the live transcript")
        self.speculation = Speculation(self.engines, self.conversation_id, text)

    async def handle(self, utterances: list[Utterance], speculation: Speculation | None) -> bool:
        """Start replies to finished utterances; True if one took the speculation."""
        if self.assistant_busy:
            if self.detector.speaking:
                self.overlapped = True
            if self.barge_in and self.detector.confident_ms >= self.barge_in_ms:
                logger.info("Barge-in after %.0f ms of user speech", self.detector.confident_ms)
                await self.interrupt()
            else:
                # Too short or unclear to interrupt: a cough, "mm-hmm", or echo.
                if utterances:
                    logger.info("Ignored %d short utterance(s) during the reply", len(utterances))
                self.overlapped = self.detector.speaking
                return False
        # Live words, shown only once the user holds the floor (never echo under our voice).
        if (partial := self.transcriber.take_partial()) is not None:
            await self.send({"type": "partial", "text": partial})
        claimed = False
        for utterance in utterances:
            if self.overlapped and utterance.confident_ms < self.barge_in_ms:
                # Began under the assistant's voice and never became clear speech.
                self.overlapped = False
                continue
            self.overlapped = False
            # Only the last utterance can match the speculation; respond checks the words.
            claim = speculation if utterance is utterances[-1] else None
            claimed = claimed or claim is not None
            self.reply_task = asyncio.create_task(self.respond(utterance, claim))
        return claimed

    async def interrupt(self) -> None:
        """Stop the reply now; record what was heard once the browser reports it."""
        if self.reply_task is not None and not self.reply_task.done():
            self.reply_task.cancel()
            await asyncio.gather(self.reply_task, return_exceptions=True)
        self.playing = False
        user_text, spoken = self.user_text, self.spoken
        self.user_text, self.spoken = None, []
        self.played = asyncio.get_running_loop().create_future()
        await self.send({"type": "interrupt"})
        if user_text is not None:
            self.history_task = asyncio.create_task(self.record_heard(user_text, spoken))

    async def record_heard(self, user_text: str, spoken: list[tuple[str, float]]) -> None:
        assert self.played is not None
        try:
            played_s = await asyncio.wait_for(asyncio.shield(self.played), self.played_report_timeout_s)
        except TimeoutError:
            played_s = 0.0  # No report: assume nothing was heard rather than everything.
        heard = heard_text(spoken, played_s)
        logger.info("Interrupted after %.1f s of playback; heard %d chars", played_s, len(heard))
        await self.engines.record_interruption(self.conversation_id, user_text, heard)

    async def respond(self, utterance: Utterance, speculation: Speculation | None = None) -> None:
        try:
            await self.answer(utterance, speculation)
        finally:
            if speculation is not None:
                # Committed: wait until it stops, so its history is saved before
                # a barge-in records what was heard. Otherwise: just cancel.
                await speculation.close()

    async def answer(self, utterance: Utterance, speculation: Speculation | None) -> None:
        if self.history_task is not None:
            # The next reply must see the trimmed history of the interrupted one.
            await asyncio.gather(self.history_task, return_exceptions=True)
            self.history_task = None
        # The transcript was finalised by the streaming listener as the turn ended.
        metrics = TurnMetrics(endpoint_ms=utterance.pause_ms, stt_ms=utterance.stt_ms,
                              heard_to_decided_ms=utterance.heard_to_decided_ms)
        text = utterance.text.strip()
        if not text:
            await self.send({"type": "partial", "text": ""})  # Clear the live line.
            return
        deltas = self.reuse(speculation, text, metrics)
        # History stores the text the committed reply answered (same words).
        self.user_text = speculation.text if metrics.speculative == "committed" else text
        self.spoken = []
        await self.send({"type": "transcript", "text": text})

        pieces: asyncio.Queue[str | None] = asyncio.Queue(maxsize=8)
        speaker = asyncio.create_task(self.speak(pieces, metrics))
        try:
            try:
                await self.think(deltas or self.engines.reply(self.conversation_id, text),
                                 pieces, metrics, speculation if deltas else None)
            except Exception as error:
                # Pieces already queued are still spoken; nothing new is generated.
                logger.warning("Reply generation failed: %s", type(error).__name__)
                await self.send({"type": "notice", "text": "Sorry, I lost my train of thought."})
            await pieces.put(None)
            await speaker
        finally:
            if not speaker.done():  # Cancelled mid-reply: barge-in or the call ended.
                speaker.cancel()
                await asyncio.gather(speaker, return_exceptions=True)
        self.user_text = None
        await self.send({"type": "reply_end"})

    def reuse(self, speculation: Speculation | None, text: str,
              metrics: TurnMetrics) -> AsyncIterator[ReplyEvent] | None:
        """Commit the speculative reply if it answered these words, else cancel it."""
        if speculation is None:
            return None
        if speculation.cancelled or speculation.failed or not same_words(speculation.text, text):
            speculation.cancel("it failed" if speculation.failed else "the final transcript differs")
            metrics.speculative = "cancelled"
            return None
        metrics.speculative = "committed"
        logger.info("Speculative reply committed: started %.0f ms before the final transcript, "
                    "%d deltas buffered", (metrics.started - speculation.started) * 1000,
                    len(speculation.deltas))
        return speculation.commit()

    async def think(self, deltas: AsyncIterator[ReplyEvent], pieces: "asyncio.Queue[str | None]",
                    metrics: TurnMetrics, speculation: Speculation | None = None) -> None:
        """Hand speakable pieces of the streamed LLM reply to TTS.

        The browser gets text only as captions alongside each piece's audio, so
        the screen never runs ahead of (or past an interruption of) the speech.
        """
        chunker = SpeechChunker()
        async for delta in deltas:
            if isinstance(delta, ReplyModel):
                metrics.llm_model = metrics.llm_model or delta.name
                continue
            # A committed speculation's first token may predate the final transcript.
            first_at = speculation.first_token_at if speculation is not None else None
            metrics.first_token(first_at or time.perf_counter())
            for piece in chunker.push(delta):
                await pieces.put(piece)
        for piece in chunker.flush():
            await pieces.put(piece)

    async def speak(self, pieces: "asyncio.Queue[str | None]", metrics: TurnMetrics) -> None:
        """Synthesize pieces in order while the LLM is still generating.

        Also estimates silent gaps: if a piece is ready only after the previous
        audio would have finished playing, the listener hears a pause.
        """
        first = True
        playhead: float | None = None  # When the audio sent so far finishes playing.
        gaps_s = 0.0
        while (piece := await pieces.get()) is not None:
            started = time.perf_counter()
            try:
                audio = await self.engines.synthesize(piece)
            except Exception as error:
                logger.warning("Speech synthesis failed: %s", type(error).__name__)
                continue
            if not len(audio):
                continue
            if first:
                metrics.tts_first_audio_ms = (time.perf_counter() - started) * 1000
            seconds = len(audio) / self.engines.output_sample_rate
            now = time.perf_counter()
            if playhead is not None and now > playhead:
                gaps_s += now - playhead
            playhead = max(playhead or now, now) + seconds
            self.spoken.append((piece, seconds))
            # Sent just before the piece's audio; the browser reveals it as it plays.
            await self.send({"type": "caption", "text": piece, "seconds": round(seconds, 3)})
            for start in range(0, len(audio), OUTPUT_PACKET_SAMPLES):
                await self.send(audio[start:start + OUTPUT_PACKET_SAMPLES].astype("<f4").tobytes())
            if first:
                first = False
                payload = metrics.to_payload(metrics.response_ms())
                logger.info("Turn latency: %s", payload)
                await self.send(payload)
        if gaps_s > 0.05:
            logger.info("Estimated playback gaps: %.0f ms over %d pieces", gaps_s * 1000, len(self.spoken))

    async def close(self) -> None:
        if self.speculation is not None:
            await self.speculation.close()
        for task in (self.reply_task, self.history_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
