"""Hand-built cascaded voice pipeline: Flux (STT + turn detection) -> LLM -> Aura (TTS).

Audio timing lives here as plain asyncio tasks. Deepgram's sockets are
wrapped in `app/deepgram.py`; this module decides what each turn event means
for the conversation. The agent (LangGraph) is called once per finished user
turn and only streams text back.
"""
import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from app import closing
from app.deepgram import TurnEvent, to_linear16

logger = logging.getLogger("uvicorn.error")
Samples = NDArray[np.float32]
MAX_PACKET_BYTES = 16000 * 4  # At most one second of float32 PCM per message.


class Listener(Protocol):
    """Streaming speech-to-text with turn detection (Flux): audio in, TurnEvents out."""
    async def start(self) -> None: ...
    async def send(self, pcm: bytes, arrived: float | None = None) -> None: ...  # linear16
    async def close(self) -> None: ...


class Speaker(Protocol):
    """Streaming text-to-speech (Aura): one segment's audio at a time."""
    async def start(self) -> None: ...
    def synthesize(self, text: str) -> AsyncIterator[Samples]: ...
    async def close(self) -> None: ...


SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s+|\n+")
CLAUSE_END = re.compile(r"[,;:—–]\s+")
# A period that ends an abbreviation, not a sentence: "a.m.", "e.g.", "U.S.", "Dr.".
INITIALISM = re.compile(r"(?:[A-Za-z]\.){2,}")
TITLES = frozenset({"mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "vs.", "approx.", "etc."})


def abbreviation_end(text: str, match: re.Match[str]) -> bool:
    """Whether a SENTENCE_END match is only an abbreviation's period ("at ten a.m. with
    Priya"): cutting there would split one phrase into two Aura segments, each with
    its own falling intonation and a pause between. A title never ends a sentence; an
    initialism does only when a capitalised word follows ("at ten a.m. Then...")."""
    head = text[:match.start() + 1]
    if not head.endswith("."):
        return False
    token = head.split()[-1].lstrip("(\"'“‘") if head.split() else ""
    if token.lower() in TITLES:
        return True
    if not INITIALISM.fullmatch(token):
        return False
    rest = text[match.end():]
    return not rest or not rest[0].isupper()


RETRY_FILLER = "One sec."
# The LLM failed twice: the caller was heard fine, so never "I didn't catch that".
RETRY_SORRY = "Sorry, I'm having trouble on my end. Could you say that again?"


class ReplyText:
    """Streamed LLM text, handed to TTS in segments of whole sentences.

    Aura's first audio takes the same time for two words or twenty (about
    80 ms from Mumbai), and the LLM writes a sentence in tens of milliseconds,
    so the first segment is simply the first sentence: no short opening piece.
    Each later segment is every whole sentence written by the time the
    previous one has been synthesized. That keeps flushes few (Deepgram allows
    20 a minute) and prosody whole-sentence: Aura shapes the intonation of each
    Speak message on its own, so a segment never ends mid-phrase (an
    abbreviation's period is not a sentence end) and is never cut at commas.
    A run-on with no sentence end is cut after `max_words`, at the last clause
    end if there is one.
    """

    def __init__(self, max_words: int = 40) -> None:
        self.text = ""
        self.total = 0  # Characters pushed so far (taken or not).
        self.done = False
        self.max_words = max_words
        self.changed = asyncio.Event()

    def push(self, delta: str) -> None:
        self.text += delta
        self.total += len(delta)
        self.changed.set()

    def finish(self) -> None:
        self.done = True
        self.changed.set()

    def take(self) -> str:
        """Every whole sentence so far (everything once finished), or "" to wait."""
        if self.done:
            cut: int | None = len(self.text)
        else:
            ends = [match.end() for match in SENTENCE_END.finditer(self.text)
                    if not abbreviation_end(self.text, match)]
            cut = ends[-1] if ends else self.forced_cut()
        if not cut:
            return ""
        segment, self.text = self.text[:cut].strip(), self.text[cut:]
        return segment

    def forced_cut(self) -> int | None:
        words = list(re.finditer(r"\S+\s+", self.text))
        if len(words) < self.max_words:
            return None
        clauses = [match.end() for match in CLAUSE_END.finditer(self.text, 0, words[-1].end())]
        return clauses[-1] if clauses else words[-1].end()

    async def next_segment(self) -> str | None:
        """The next segment to speak, waiting for text; None once the reply is over."""
        while True:
            self.changed.clear()
            if segment := self.take():
                return segment
            if self.done:
                return None
            await self.changed.wait()


@dataclass(frozen=True)
class ReplyModel:
    """In a reply stream: the LLM that generated the text after it (for metrics only)."""
    name: str


ReplyEvent = str | ReplyModel
TurnEventHandler = Callable[[TurnEvent], Awaitable[None]]


async def forget_interruption(conversation_id: str, user_text: str, heard: str) -> None:
    """Default for engines without history: nothing to record."""


def always() -> bool:
    return True


async def no_caller_memory(conversation_id: str, caller_id: str | None) -> None:
    """Default for engines without caller memory: nothing to load."""


def nothing_to_remember(conversation_id: str) -> None:
    """Default for engines without caller memory: nothing to store."""


async def forget_greeting(conversation_id: str, text: str, status: str) -> None:
    """Default for engines without history: nothing to record."""


async def forget_turn(conversation_id: str, user_text: str | None, reply: str) -> None:
    """Default for engines without history: nothing to record."""


DEFAULT_BACKCHANNEL = ("okay, ok, sure, yeah, yes, yep, uh huh, mm, mhm, mm hmm, hmm, right, "
                       "alright, got it, i see, cool, great, nice, oh, ah")


def normal_words(text: str) -> list[str]:
    """ "Mm-hmm, OK." -> ["mm", "hmm", "ok"]."""
    return re.sub(r"[^\w\s]", "", re.sub(r"[-–—]", " ", text.lower())).split()


@dataclass(frozen=True)
class Backchannel:
    """Short words a listener says while someone talks ("sure", "uh huh"): not a barge-in.

    While the assistant speaks, a StartOfTurn is held until Flux's first words
    (or `hold_s` of speech with no words yet, which then interrupts). Words
    that are only backchannel phrases, at most `max_words` of them, are
    ignored; anything else interrupts at once. When the assistant is silent
    they are an ordinary turn ("okay" as an answer).
    """
    phrases: frozenset[tuple[str, ...]]
    hold_s: float = 0.35
    max_words: int = 3

    @classmethod
    def from_words(cls, words: str = DEFAULT_BACKCHANNEL, hold_s: float = 0.35) -> "Backchannel":
        phrases = frozenset(tuple(normal_words(phrase)) for phrase in words.split(","))
        return cls(frozenset(phrase for phrase in phrases if phrase), hold_s)

    def only(self, text: str) -> bool:
        """Whether `text` is nothing but backchannel phrases (False for no words)."""
        words = normal_words(text)
        if not words or len(words) > self.max_words:
            return False
        ends = {0}  # Word positions reachable by whole phrases.
        for start in range(len(words)):
            if start in ends:
                ends |= {start + len(phrase) for phrase in self.phrases
                         if tuple(words[start:start + len(phrase)]) == phrase}
        return len(words) in ends


@dataclass
class Engines:
    """Everything a call needs, injected so tests can replace any stage."""
    new_listener: Callable[[TurnEventHandler], Listener]  # Delivers events to the handler.
    new_speaker: Callable[[], Speaker]
    reply: Callable[[str, str], AsyncIterator[ReplyEvent]]  # (conversation_id, user text)
    # (conversation_id, user text, assistant text the user actually heard)
    record_interruption: Callable[[str, str, str], Awaitable[None]] = forget_interruption
    output_sample_rate: int = 24000
    # Optional early reply from Flux's EagerEndOfTurn transcript: (conversation_id,
    # text, committed). It must store nothing and run no side-effecting tool
    # until `committed` is set. None disables speculation.
    speculate: Callable[[str, str, asyncio.Event], AsyncIterator[ReplyEvent]] | None = None
    may_speculate: Callable[[], bool] = always  # False near the provider's rate limit.
    # Caller memory (app/memory.py). `begin_call` (conversation_id, caller_id) runs
    # beside the Deepgram sockets as the call starts, bounded by its own timeout,
    # and never raises; `end_call` (conversation_id) runs once the call has closed
    # and must return at once (it hands any work to a background task).
    begin_call: Callable[[str, str | None], Awaitable[None]] = no_caller_memory
    end_call: Callable[[str], None] = nothing_to_remember
    # Speaking first (phone calls, see Call.greet): the fixed greeting for a call
    # (conversation_id), and how to store what of it was heard (conversation_id,
    # text, "completed" or "interrupted"). None: the caller speaks first.
    greeting: Callable[[str], str] | None = None
    record_greeting: Callable[[str, str, Literal["completed", "interrupted"]],
                              Awaitable[None]] = forget_greeting
    # Ignore backchannel words while the assistant speaks; None: any turn barges in.
    backchannel: Backchannel | None = None
    # (conversation_id) -> whether a proposal awaits the caller's yes (a callback, research
    # or briefing). Then a backchannel said over our speech ("okay", "yes") is not dropped:
    # it is answered as the caller's next turn once the reply ends.
    awaiting_answer: Callable[[str], bool] = lambda conversation_id: False
    # A fixed line spoken without the LLM (a goodbye, "Still there?"), stored as the
    # reply to `user_text` (None: said unprompted).
    record_turn: Callable[[str, str | None, str], Awaitable[None]] = forget_turn
    # Silence (no one speaking) before "Still there?", and before a goodbye and the
    # end of the call. None: no idle handling (the browser call page).
    idle_prompt_s: float | None = None
    idle_end_s: float | None = None


def heard_text(spoken: list[tuple[str, float]], played_s: float) -> str:
    """The part of a reply the user heard, given seconds of its audio played.

    Whole segments count while their audio fits; in the segment playing at
    the interruption, words are spread evenly over its audio and the word being
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
    """A reply started from Flux's EagerEndOfTurn transcript, before EndOfTurn.

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
    """Milliseconds for one turn, timed by packet arrival on the server.

    `speech_ended` is when the audio holding the user's last word arrived
    (Flux word timings mapped to packet arrival), so endpoint_ms is Flux's
    end-of-turn decision time including the network round trip to Deepgram.
    """
    speech_ended: float  # perf_counter.
    turn_ended: float  # perf_counter: Flux EndOfTurn received.
    eager_lead_ms: float | None = None  # EagerEndOfTurn before EndOfTurn, if committed.
    # From EndOfTurn; negative when a committed speculative reply started earlier.
    llm_first_token_ms: float | None = None
    tts_first_audio_ms: float = 0  # First segment sent to Aura -> its first audio.
    # "committed": a speculative reply was used; "cancelled": one or more were
    # started (TurnResumed, or different words) but none used; "skipped": none.
    speculative: str = "skipped"
    speculations: int = 0  # Speculative LLM requests started during the turn.
    llm_model: str = ""  # The model that produced the reply's first token.

    def first_token(self, at: float) -> None:
        if self.llm_first_token_ms is None:
            self.llm_first_token_ms = (at - self.turn_ended) * 1000

    def to_payload(self, first_audio_at: float) -> dict[str, object]:
        def ms(value: float | None) -> int | None:
            return None if value is None else round(value)
        return {"type": "metrics",
                "endpoint_ms": round((self.turn_ended - self.speech_ended) * 1000),
                "eager_lead_ms": ms(self.eager_lead_ms),
                "llm_first_token_ms": ms(self.llm_first_token_ms),
                "tts_first_audio_ms": round(self.tts_first_audio_ms),
                "speculative": self.speculative, "speculations": self.speculations,
                "llm_model": self.llm_model,
                # From the user's last word to the first assistant audio being sent.
                "response_ms": round((first_audio_at - self.speech_ended) * 1000)}


Send = Callable[[dict[str, object] | bytes], Awaitable[None]]


def decode_pcm(packet: bytes) -> Samples:
    if not packet or len(packet) > MAX_PACKET_BYTES or len(packet) % 4:
        raise ValueError("Send up to one second of complete float32 PCM samples")
    samples = np.frombuffer(packet, dtype="<f4")
    if not np.isfinite(samples).all():
        raise ValueError("PCM contains non-finite samples")
    return samples


class Call:
    """One live call. Mic audio always streams to Flux; at most one reply runs at a time.

    Flux turn events drive the call:

    - StartOfTurn while the assistant is busy is a barge-in: the reply is
      cancelled, the browser flushes its audio, and only what the user heard
      is recorded. Without browser echo cancellation barge-in is off, and a
      turn that starts while the assistant speaks is ignored (it may be our
      own voice).
    - Update: live words for the browser (`partial`).
    - EagerEndOfTurn starts one speculative reply from its transcript;
      TurnResumed cancels it. EndOfTurn commits it if the words match (Flux
      guarantees they do unless the turn resumed), else answers afresh.
    """

    def __init__(self, engines: Engines, send: Send, conversation_id: str, *,
                 barge_in: bool = True, played_report_timeout_s: float = 1.0,
                 caller_id: str | None = None) -> None:
        self.engines = engines
        self.send = send
        self.conversation_id = conversation_id
        self.caller_id = caller_id  # Normalized caller key ("tel:+…", "email:…"); None: unknown.
        self.listener = engines.new_listener(self.on_turn_event)
        self.speaker = engines.new_speaker()
        self.barge_in = barge_in
        self.played_report_timeout_s = played_report_timeout_s
        self.reply_task: asyncio.Task[None] | None = None
        self.history_task: asyncio.Task[None] | None = None
        self.played: asyncio.Future[float] | None = None
        self.playing = False  # Reported by the browser player.
        self.user_text: str | None = None  # Transcript the current reply answers.
        self.spoken: list[tuple[str, float]] = []  # (segment, audio seconds) sent so far.
        self.current: tuple[str, float] | None = None  # Segment whose audio is still arriving.
        self.overlapped = False  # Current turn began while the assistant spoke (no barge-in).
        self.partial = ""  # Live text last shown to the browser.
        self.speculation: Speculation | None = None
        self.speculations = 0  # Started during the current turn.
        self.greeting: str | None = None  # Being spoken; not yet stored.
        # A StartOfTurn during our speech, held until its words show it is more
        # than backchannel (see Backchannel).
        self.holding = False
        self.held_text = ""
        self.hold_timer: asyncio.Task[None] | None = None
        # A backchannel that answers a pending proposal, said over our speech: answered as
        # the next turn once the current reply (or greeting) has ended.
        self.deferred: TurnEvent | None = None
        self.answered = 0  # User turns answered so far.
        self.user_speaking = False  # Between Flux's StartOfTurn and EndOfTurn.
        self.last_activity = time.monotonic()  # Anyone speaking, for the idle timer.
        self.idle_prompted = False
        self.idle_task: asyncio.Task[None] | None = None
        # Set once the call should end (a goodbye was said): the transport hangs up
        # after `finished`.
        self.ended = asyncio.Event()

    async def start(self) -> None:
        """Open the Deepgram sockets, and load the caller's memory meanwhile.

        The browser starts streaming once this returns.
        """
        await asyncio.gather(self.listener.start(), self.speaker.start(),
                             self.engines.begin_call(self.conversation_id, self.caller_id))
        if self.engines.idle_prompt_s is not None and self.engines.idle_end_s is not None:
            self.idle_task = asyncio.create_task(
                self.watch_idle(self.engines.idle_prompt_s, self.engines.idle_end_s))

    async def finished(self, playback_limit_s: float = 15.0) -> None:
        """Return once the call has ended and its last words have played."""
        await self.ended.wait()
        if self.reply_task is not None and not self.reply_task.done():
            await asyncio.gather(self.reply_task, return_exceptions=True)
        deadline = time.monotonic() + playback_limit_s
        while self.playing and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def watch_idle(self, prompt_s: float, end_s: float, tick_s: float = 0.25) -> None:
        """After `prompt_s` of silence, ask "Still there?"; once `end_s` of silence has
        passed in all, say goodbye and end the call."""
        while not self.ended.is_set():
            await asyncio.sleep(tick_s)
            if self.assistant_busy or self.user_speaking or self.holding:
                self.touch()
                continue
            idle = time.monotonic() - self.last_activity
            if not self.idle_prompted and idle >= prompt_s:
                self.idle_prompted = True
                logger.info("Idle for %.0f s: asking if the caller is still there", idle)
                self.reply_task = asyncio.create_task(self.say_fixed(closing.STILL_THERE))
            elif self.idle_prompted and idle >= end_s - prompt_s:
                logger.info("Still silent after the idle prompt: ending the call")
                self.reply_task = asyncio.create_task(
                    self.say_fixed(closing.IDLE_GOODBYE, end=True))
                return

    async def say_fixed(self, text: str, user_text: str | None = None, end: bool = False) -> None:
        """Speak a fixed line (no LLM), store it, and end the call after it if `end`."""
        self.user_text, self.spoken, self.current = None, [], None
        reply_text = ReplyText()
        reply_text.push(text)
        reply_text.finish()
        await self.speak(reply_text)  # A barge-in cancels it, and the call goes on.
        if end:
            self.ended.set()
        self.history_task = asyncio.create_task(
            self.engines.record_turn(self.conversation_id, user_text, text))
        await self.send({"type": "reply_end"})

    def greet(self) -> None:
        """Speak the engines' greeting before the caller says anything.

        Fixed text (no LLM), spoken through the same speaker as any reply, so
        barge-in interrupts it the same way. It is stored as an assistant
        message: whole once synthesized, or only what was heard if interrupted.
        """
        if self.engines.greeting is None or self.assistant_busy:
            return
        text = self.engines.greeting(self.conversation_id)
        self.greeting, self.spoken, self.current = text, [], None
        self.reply_task = asyncio.create_task(self.say_greeting(text))

    async def say_greeting(self, text: str) -> None:
        reply_text = ReplyText()
        reply_text.push(text)
        reply_text.finish()
        await self.speak(reply_text)
        self.greeting = None
        self.history_task = asyncio.create_task(
            self.engines.record_greeting(self.conversation_id, text, "completed"))
        await self.send({"type": "reply_end"})
        self.answer_deferred()

    async def record_heard_greeting(self, spoken: list[tuple[str, float]]) -> None:
        assert self.played is not None
        try:
            played_s = await asyncio.wait_for(asyncio.shield(self.played), self.played_report_timeout_s)
        except TimeoutError:
            played_s = 0.0
        if heard := heard_text(spoken, played_s):
            await self.engines.record_greeting(self.conversation_id, heard, "interrupted")

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
        await self.listener.send(to_linear16(decode_pcm(packet)), time.perf_counter())

    async def on_turn_event(self, event: TurnEvent) -> None:
        """Flux events for this call, in order."""
        if event.event in ("StartOfTurn", "EndOfTurn", "Reset"):
            self.user_speaking = event.event == "StartOfTurn"
            self.touch()
            if event.event == "StartOfTurn":
                self.idle_prompted = False
        if event.event == "StartOfTurn":
            await self.turn_started(event.transcript)
        elif self.holding and event.event in ("Update", "EagerEndOfTurn", "TurnResumed"):
            await self.judge_held(event.transcript)
        if event.event in ("StartOfTurn", "Update", "EagerEndOfTurn", "TurnResumed"):
            await self.show_partial(event.transcript)
        if event.event == "EagerEndOfTurn":
            self.start_speculation(event.transcript)
        elif event.event == "TurnResumed":
            self.drop_speculation("the user kept speaking")
        elif event.event == "EndOfTurn":
            await self.turn_ended(event)
        elif event.event == "Reset":
            self.stop_holding()
            self.drop_speculation("the speech socket reconnected")
            self.overlapped = False
            await self.show_partial("")

    async def turn_started(self, text: str = "") -> None:
        if not self.assistant_busy:
            return
        if not self.barge_in:
            self.overlapped = True
        elif self.engines.backchannel is None:
            await self.barge_in_now("the user started a turn while the assistant was speaking")
        else:
            self.holding, self.held_text = True, ""
            self.hold_timer = asyncio.create_task(self.hold_expired(self.engines.backchannel.hold_s))
            await self.judge_held(text)

    async def judge_held(self, text: str) -> None:
        """A held turn's words so far: interrupt unless they are only backchannel."""
        self.held_text = text
        backchannel = self.engines.backchannel
        if self.holding and text.strip() and backchannel and not self.ignorable(text):
            await self.barge_in_now("the user's words while the assistant was speaking")

    def ignorable(self, text: str) -> bool:
        """Words said over our speech that do not interrupt it: backchannel, and a
        pickup "Hello?" over the greeting (it would otherwise cut the call's purpose)."""
        backchannel = self.engines.backchannel
        return bool(backchannel and backchannel.only(text)) or (
            self.greeting is not None and closing.only_hello(text))

    async def hold_expired(self, hold_s: float) -> None:
        await asyncio.sleep(hold_s)
        if self.holding and not self.held_text.strip():
            await self.barge_in_now(f"the user kept speaking for {hold_s * 1000:.0f} ms")

    async def barge_in_now(self, reason: str) -> None:
        self.stop_holding()
        logger.info("Barge-in: %s", reason)
        await self.interrupt()

    def stop_holding(self) -> None:
        self.holding = False
        timer, self.hold_timer = self.hold_timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def show_partial(self, text: str) -> None:
        """Live words, shown only once the user holds the floor (never echo under our voice)."""
        if not self.overlapped and not self.holding and text != self.partial:
            self.partial = text
            await self.send({"type": "partial", "text": text})

    def start_speculation(self, text: str) -> None:
        if self.engines.speculate is None or self.overlapped or self.holding:
            return
        self.drop_speculation("a newer eager end of turn")  # At most one in flight.
        history_pending = self.history_task is not None and not self.history_task.done()
        if not text.strip() or self.assistant_busy or history_pending:
            return  # Nothing to say yet, or the turn may not be answered as is.
        if not self.engines.may_speculate():
            logger.info("Speculative reply skipped: near the LLM rate limit")
            return
        logger.info("Speculative reply started from the eager end of turn")
        self.speculation = Speculation(self.engines, self.conversation_id, text)
        self.speculations += 1

    def drop_speculation(self, reason: str) -> None:
        if self.speculation is not None:
            self.speculation.cancel(reason)
            self.speculation = None

    async def turn_ended(self, event: TurnEvent) -> None:
        speculation, self.speculation = self.speculation, None
        speculations, self.speculations = self.speculations, 0
        self.partial = ""
        if self.holding:
            backchannel = self.engines.backchannel
            if not event.transcript.strip() or (backchannel and self.ignorable(event.transcript)):
                self.stop_holding()
                if speculation is not None:
                    speculation.cancel("the turn was a backchannel")
                if event.transcript.strip() and self.engines.awaiting_answer(self.conversation_id):
                    logger.info("A backchannel over our speech answers a pending proposal; "
                                "it is answered once the reply ends")
                    self.deferred = event
                    return
                logger.info("Ignored a backchannel while the assistant was speaking")
                return
            await self.barge_in_now("the user's words while the assistant was speaking")
        if self.greeting is not None and closing.only_hello(event.transcript):
            # A pickup "Hello?" that began before the greeting and ended during it.
            logger.info("Ignored a hello said over the greeting")
            if speculation is not None:
                speculation.cancel("the turn was a hello over the greeting")
            return
        if self.overlapped:
            self.overlapped = False
            logger.info("Ignored a turn that began while the assistant was speaking")
            if speculation is not None:
                speculation.cancel("the turn was not answered")
            return
        self.deferred = None  # A newer turn supersedes a held backchannel.
        if self.reply_task is not None and not self.reply_task.done():
            await self.interrupt()  # Defensive: never two replies at once.
        self.reply_task = asyncio.create_task(self.respond(event, speculation, speculations))

    async def interrupt(self) -> None:
        """Stop the reply now; record what was heard once the browser reports it.

        Cancelling the reply closes its TTS stream, which sends Aura a Clear.
        """
        self.deferred = None  # Barged in: whatever was held over the old reply is moot.
        if self.reply_task is not None and not self.reply_task.done():
            self.reply_task.cancel()
            await asyncio.gather(self.reply_task, return_exceptions=True)
        self.playing = False
        user_text, spoken = self.user_text, self.spoken + ([self.current] if self.current else [])
        greeting = self.greeting
        self.user_text, self.spoken, self.current, self.greeting = None, [], None, None
        self.played = asyncio.get_running_loop().create_future()
        await self.send({"type": "interrupt"})
        if user_text is not None:
            self.history_task = asyncio.create_task(self.record_heard(user_text, spoken))
        elif greeting is not None:
            self.history_task = asyncio.create_task(self.record_heard_greeting(spoken))

    async def record_heard(self, user_text: str, spoken: list[tuple[str, float]]) -> None:
        assert self.played is not None
        try:
            played_s = await asyncio.wait_for(asyncio.shield(self.played), self.played_report_timeout_s)
        except TimeoutError:
            played_s = 0.0  # No report: assume nothing was heard rather than everything.
        heard = heard_text(spoken, played_s)
        logger.info("Interrupted after %.1f s of playback; heard %d chars", played_s, len(heard))
        await self.engines.record_interruption(self.conversation_id, user_text, heard)

    async def respond(self, event: TurnEvent, speculation: Speculation | None = None,
                      speculations: int = 0) -> None:
        try:
            await self.answer(event, speculation, speculations)
        finally:
            if speculation is not None:
                # Committed: wait until it stops, so its history is saved before
                # a barge-in records what was heard. Otherwise: just cancel.
                await speculation.close()

    async def answer(self, event: TurnEvent, speculation: Speculation | None,
                     speculations: int = 0) -> None:
        if self.history_task is not None:
            # The next reply must see the trimmed history of the interrupted one.
            await asyncio.gather(self.history_task, return_exceptions=True)
            self.history_task = None
        metrics = TurnMetrics(speech_ended=event.speech_ended or event.received,
                              turn_ended=event.received, speculations=speculations,
                              speculative="cancelled" if speculations else "skipped")
        text = event.transcript.strip()
        if not text:
            if speculation is not None:
                speculation.cancel("the turn had no words")
            await self.send({"type": "partial", "text": ""})  # Clear the live line.
            return
        self.answered += 1
        if await self.answered_without_llm(text, speculation):
            return
        deltas = self.reuse(speculation, text, metrics)
        # History stores the text the committed reply answered (same words).
        self.user_text = speculation.text if metrics.speculative == "committed" else text
        self.spoken, self.current = [], None
        await self.send({"type": "transcript", "text": text})

        reply_text = ReplyText()
        speaker = asyncio.create_task(self.speak(reply_text, metrics))
        try:
            try:
                await self.think(deltas or self.engines.reply(self.conversation_id, text),
                                 reply_text, metrics, speculation if deltas else None)
            except Exception as error:
                logger.warning("Reply generation failed: %s", type(error).__name__)
                if reply_text.total:
                    # Text already written is still spoken; nothing new is generated.
                    await self.send({"type": "notice", "text": "Sorry, I lost my train of thought."})
                else:
                    # Nothing said yet: a short filler, then the same turn once more (the
                    # agent continues it rather than storing the words twice).
                    reply_text.push(RETRY_FILLER + " ")
                    try:
                        await self.think(self.engines.reply(self.conversation_id, text),
                                         reply_text, metrics)
                    except Exception as again:
                        logger.warning("Reply retry failed: %s", type(again).__name__)
                        if closing.closing(text):  # "That's all": a goodbye, LLM or not.
                            reply_text.push(closing.goodbye_line(self.conversation_id))
                            closing.request_end(self.conversation_id)
                        else:
                            reply_text.push(RETRY_SORRY)
            reply_text.finish()
            await speaker
        finally:
            if not speaker.done():  # Cancelled mid-reply: barge-in or the call ended.
                speaker.cancel()
                await asyncio.gather(speaker, return_exceptions=True)
        self.user_text = None
        if closing.take_end_request(self.conversation_id):  # end_call, or the goodbye above.
            logger.info("Ending the call after the goodbye")
            self.ended.set()
        await self.send({"type": "reply_end"})
        self.answer_deferred()

    async def answered_without_llm(self, text: str, speculation: Speculation | None) -> bool:
        """A clear goodbye ends the call, and a lone "Hello?" before anything was answered
        repeats the greeting (the caller missed it): both without the LLM."""
        goodbye = closing.closing(text) == "strong"
        if goodbye:
            logger.info("The caller said goodbye: ending the call")
            line = closing.goodbye_line(self.conversation_id)
        elif self.answered == 1 and self.engines.greeting is not None and closing.only_hello(text):
            logger.info("A lone hello before any answer: repeating the greeting")
            line = self.engines.greeting(self.conversation_id)
        else:
            return False
        if speculation is not None:
            speculation.cancel("answered without the LLM")
        await self.send({"type": "transcript", "text": text})
        await self.say_fixed(line, user_text=text, end=goodbye)
        self.answer_deferred()
        return True

    def answer_deferred(self) -> None:
        """Once a reply has ended: answer a held yes to a pending proposal (see turn_ended)."""
        event, self.deferred = self.deferred, None
        if event is None:
            return
        logger.info("Answering the backchannel held during the reply")
        self.reply_task = asyncio.create_task(self.respond(event))

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
        metrics.eager_lead_ms = (metrics.turn_ended - speculation.started) * 1000
        logger.info("Speculative reply committed: started %.0f ms before EndOfTurn, "
                    "%d deltas buffered", metrics.eager_lead_ms, len(speculation.deltas))
        return speculation.commit()

    async def think(self, deltas: AsyncIterator[ReplyEvent], reply_text: ReplyText,
                    metrics: TurnMetrics, speculation: Speculation | None = None) -> None:
        """Hand the streamed LLM reply to TTS.

        The browser gets text only as captions alongside each segment's audio,
        so the screen never runs ahead of (or past an interruption of) the speech.
        """
        async for delta in deltas:
            if isinstance(delta, ReplyModel):
                metrics.llm_model = metrics.llm_model or delta.name
                continue
            # A committed speculation's first token may predate EndOfTurn.
            first_at = speculation.first_token_at if speculation is not None else None
            metrics.first_token(first_at or time.perf_counter())
            reply_text.push(delta)

    async def speak(self, reply_text: ReplyText, metrics: TurnMetrics | None = None) -> None:
        """Stream each segment's audio to the browser as Aura produces it.

        A segment's caption (text and exact audio length) follows its audio;
        the browser reveals it from where that audio started. Also estimates
        silent gaps: audio that arrived after the audio before it had finished.
        """
        rate = self.engines.output_sample_rate
        first = True
        playhead: float | None = None  # When the audio sent so far finishes playing.
        gaps_s = 0.0
        while (segment := await reply_text.next_segment()) is not None:
            started = time.perf_counter()
            seconds = 0.0
            self.current = (segment, seconds)
            try:
                async with aclosing(self.speaker.synthesize(segment)) as stream:
                    async for audio in stream:
                        if not len(audio):
                            continue
                        now = time.perf_counter()
                        if first:
                            if metrics is not None:
                                metrics.tts_first_audio_ms = (now - started) * 1000
                        elif playhead is not None and now > playhead:
                            gaps_s += now - playhead
                        playhead = max(playhead or now, now) + len(audio) / rate
                        seconds += len(audio) / rate
                        self.current = (segment, seconds)
                        await self.send(audio.astype("<f4").tobytes())
                        if first:
                            first = False
                            if metrics is None:  # The greeting: no user turn to time.
                                logger.info("Greeting: first audio after %.0f ms",
                                            (now - started) * 1000)
                            else:
                                payload = metrics.to_payload(time.perf_counter())
                                logger.info("Turn latency: %s", payload)
                                await self.send(payload)
            except Exception as error:
                logger.warning("Speech synthesis failed: %s", type(error).__name__)
            self.current = None
            if seconds:
                self.spoken.append((segment, seconds))
                await self.send({"type": "caption", "text": segment, "seconds": round(seconds, 3)})
        if gaps_s > 0.05:
            logger.info("Estimated playback gaps: %.0f ms over %d segments", gaps_s * 1000, len(self.spoken))

    async def close(self) -> None:
        if self.idle_task is not None:
            self.idle_task.cancel()
        closing.end_requests.discard(self.conversation_id)
        self.stop_holding()
        self.drop_speculation("the call ended")
        for task in (self.reply_task, self.history_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(self.listener.close(), self.speaker.close(), return_exceptions=True)
        self.engines.end_call(self.conversation_id)
