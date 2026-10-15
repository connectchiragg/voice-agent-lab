"""Turn detection, sentence chunking and the per-call reply flow, with fake models."""
import asyncio

import numpy as np
import pytest

from app import pipeline
from app.pipeline import (Call, Engines, ReplyModel, SpeechChunker, TurnDetector, decode_pcm,
                          heard_text)
from app.vad import CHUNK

LOUD, QUIET = 0.3, 0.0


class LevelVad:
    """Treat any chunk with a loud sample as speech."""
    def probability(self, chunk):
        return 1.0 if np.abs(chunk).max() > 0.1 else 0.0

    def reset(self):
        pass


class FixedTurn:
    def __init__(self, completion):
        self.completion = completion
        self.calls = 0

    def completion_probability(self, samples):
        self.calls += 1
        return self.completion


def chunks(level, count):
    return np.full(CHUNK * count, level, dtype=np.float32)


def detector(completion=0.9, listener=None):
    return TurnDetector(LevelVad(), FixedTurn(completion), start_ms=96, pause_ms=192,
                        max_pause_ms=960, preroll_ms=128, listener=listener)


class FakeTranscriber:
    """Streams the first word as a partial, then finalises the next scripted transcript.

    `live` scripts the live text at the end of each turn (default: the final text).
    """
    def __init__(self, transcripts, live=None):
        self.transcripts, self.partial, self.final_ms, self.events = list(transcripts), None, 7.0, []
        self.live, self.text = list(live or transcripts), ""

    def on_speech_start(self, preroll):
        self.events.append(("start", len(preroll)))
        self.partial = self.transcripts[0].split()[0] if self.transcripts[0].split() else None
        self.text = self.live.pop(0) if len(self.live) > 1 else self.live[0]

    def on_speech(self, samples):
        self.events.append(("audio", len(samples)))

    def on_speech_end(self, silence_s):
        self.events.append(("end",))
        self.partial = None
        return self.transcripts.pop(0) if len(self.transcripts) > 1 else self.transcripts[0]

    def on_discard(self):
        self.events.append(("discard",))
        self.partial = None

    def take_partial(self):
        partial, self.partial = self.partial, None
        return partial


def test_listener_follows_each_utterance_from_preroll_to_final_text():
    listener = FakeTranscriber(["hello there"])
    turns = detector(listener=listener)
    turns.feed(chunks(QUIET, 10))
    turns.feed(chunks(LOUD, 20))
    (utterance,) = turns.feed(chunks(QUIET, 6))
    assert listener.events[0] == ("start", CHUNK * 4)  # The pre-roll, voiced start included.
    assert listener.events[-1] == ("end",)
    assert sum(n for kind, *n in listener.events if kind == "audio" for n in n) == CHUNK * (17 + 6)
    assert (utterance.text, utterance.stt_ms) == ("hello there", 7.0)
    turns.feed(chunks(LOUD, 10))
    turns.discard()
    assert listener.events[-1] == ("discard",)


def test_confident_turn_ends_after_short_pause():
    turns = detector(0.9)
    assert turns.feed(chunks(QUIET, 10)) == []
    assert turns.feed(chunks(LOUD, 20)) == []
    assert turns.speaking
    (utterance,) = turns.feed(chunks(QUIET, 6))
    assert utterance.completion == 0.9
    assert utterance.pause_ms == pytest.approx(6 * 32)
    # Pre-roll (4 chunks, ending with the voiced ones) + 20 voiced - 3 already in pre-roll + 6 silent.
    assert len(utterance.audio) == CHUNK * (4 + 17 + 6)
    assert not turns.speaking


def test_incomplete_turn_waits_for_more_speech_or_timeout():
    turn_model = FixedTurn(0.1)
    turns = TurnDetector(LevelVad(), turn_model, start_ms=96, pause_ms=192, max_pause_ms=960)
    turns.feed(chunks(LOUD, 10))
    assert turns.feed(chunks(QUIET, 6)) == []  # "I'd like to book a table for..."
    turns.feed(chunks(LOUD, 5))  # Speech resumes: same turn.
    assert turns.speaking
    assert turns.feed(chunks(QUIET, 29)) == []
    (utterance,) = turns.feed(chunks(QUIET, 1))
    assert utterance.completion is None  # Ended by the 960 ms timeout.
    assert turn_model.calls == 2


def test_short_noise_does_not_start_a_turn():
    turns = detector()
    turns.feed(np.concatenate([chunks(LOUD, 2), chunks(QUIET, 20)]))
    assert not turns.speaking


def test_audio_is_buffered_across_uneven_packets():
    turns = detector()
    audio = np.concatenate([chunks(LOUD, 20), chunks(QUIET, 6)])
    found = []
    for start in range(0, len(audio), 1600):  # 100 ms browser packets.
        found += turns.feed(audio[start:start + 1600])
    assert len(found) == 1


def test_discard_drops_partial_turn():
    turns = detector()
    turns.feed(chunks(LOUD, 10))
    turns.discard()
    assert not turns.speaking
    assert turns.feed(chunks(QUIET, 10)) == []


def chunk_all(deltas, **options):
    chunker = SpeechChunker(**options)
    pieces = []
    for delta in deltas:
        pieces += chunker.push(delta)
    return pieces + chunker.flush()


def stream_words(text):
    """Deliver text the way an LLM does: small deltas that split words."""
    return [text[i:i + 3] for i in range(0, len(text), 3)]


def test_chunker_grows_pieces_and_prefers_natural_boundaries():
    text = ("Sure thing, Coorg is lovely in winter. It is about five hours away, "
            "so most people stay a night or two. The coffee estates are worth a visit. Want tips?")
    assert chunk_all(stream_words(text), schedule=(6, 12)) == [
        "Sure thing, Coorg is lovely in winter.",     # Sentence end within 6 + 3 words.
        "It is about five hours away,",               # Clause end in piece two.
        "so most people stay a night or two.",        # Sentences from here on.
        "The coffee estates are worth a visit.",
        "Want tips?",
    ]
    assert chunk_all(stream_words(text))[:2] == [  # Default: a short first piece.
        "Sure thing,",                                # Forced cut at a two-word clause.
        "Coorg is lovely in winter.",
    ]


def test_chunker_cuts_long_openers_without_dangling_words():
    text = "Heading out to the Western Ghats for the weekend is a great plan for you."
    pieces = chunk_all(stream_words(text), schedule=(6, 12))
    assert pieces[0] == "Heading out to the Western Ghats"  # Not "...Ghats for".
    assert " ".join(pieces) == text


def test_chunker_short_first_piece_never_ends_on_a_dangling_word():
    text = "It is a lovely day for a long walk by the river with friends."
    pieces = chunk_all(stream_words(text), schedule=(3, 10))
    assert pieces[0] == "It is a lovely"  # Trimming would leave "It is"; extend instead.
    assert " ".join(pieces) == text


def test_chunker_waits_for_partial_words_and_flushes_the_rest():
    chunker = SpeechChunker()
    assert chunker.push("Hello there") == []          # "there" may be incomplete.
    assert chunker.push(". How") == ["Hello there."]
    assert chunker.flush() == ["How"]
    assert chunker.flush() == []


def test_chunker_caps_run_on_sentences():
    words = " ".join(f"word{i}" for i in range(80))
    pieces = chunk_all([words], schedule=(), sentence_cap=30, lookahead=3)
    assert all(len(piece.split()) <= 30 for piece in pieces)
    assert " ".join(pieces) == words


def test_decode_pcm_rejects_bad_packets():
    assert len(decode_pcm(np.zeros(4, "<f4").tobytes())) == 4
    for bad in [b"", b"abc", np.zeros(16001, "<f4").tobytes(), np.array([np.nan], "<f4").tobytes()]:
        with pytest.raises(ValueError):
            decode_pcm(bad)


def fake_engines(transcript="what's up", reply=("Hi there. ", "All good!"), tts_samples=6000):
    seen = {"replies": []}

    async def generate(conversation_id, text):
        seen["replies"].append((conversation_id, text))
        for delta in reply:
            await asyncio.sleep(0)
            yield delta

    async def synthesize(text):
        return np.full(tts_samples, 0.25, dtype=np.float32)

    return Engines(new_transcriber=lambda: FakeTranscriber([transcript]),
                   new_detector=lambda listener: detector(listener=listener),
                   reply=generate, synthesize=synthesize), seen


def speech_then_pause():
    return [packet.tobytes() for packet in np.split(
        np.concatenate([chunks(LOUD, 20), chunks(QUIET, 10)]).astype("<f4"), 30)]


async def run_call(engines, packets, playing=None):
    sent = []

    async def send(data):
        sent.append(data)

    call = Call(engines, send, "call-1")
    if playing is not None:
        call.set_playing(playing)
    for packet in packets:
        await call.hear(packet)
    if call.reply_task:
        await call.reply_task
    await call.close()
    return sent


def test_finished_turn_is_transcribed_answered_and_spoken():
    engines, seen = fake_engines()
    sent = asyncio.run(run_call(engines, speech_then_pause()))
    events = [item for item in sent if isinstance(item, dict)]
    audio = b"".join(item for item in sent if isinstance(item, bytes))
    assert events[0] == {"type": "partial", "text": "what's"}  # Live words while speaking.
    assert events[1] == {"type": "transcript", "text": "what's up"}
    # Each spoken piece is captioned just before its own audio.
    captions = [e for e in events if e["type"] == "caption"]
    assert [c["text"] for c in captions] == ["Hi there.", "All good!"]
    assert all(c["seconds"] == 0.25 for c in captions)
    first_caption = sent.index(captions[0])
    assert isinstance(sent[first_caption + 1], bytes)
    assert events[-1] == {"type": "reply_end"}
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["endpoint_ms"] == round(6 * 32)
    assert metrics["stt_ms"] == 7
    # Two spoken pieces, each 6000 samples, sent in 200 ms (4800-sample) packets.
    assert len(audio) == 2 * 6000 * 4
    assert max(len(item) for item in sent if isinstance(item, bytes)) == pipeline.OUTPUT_PACKET_SAMPLES * 4
    assert seen["replies"] == [("call-1", "what's up")]


def test_silence_or_empty_transcript_does_not_reply():
    engines, seen = fake_engines(transcript="  ")
    sent = asyncio.run(run_call(engines, speech_then_pause()))
    assert sent == [{"type": "partial", "text": ""}]  # Only clears the live line.
    assert seen["replies"] == []


def test_short_sound_during_playback_does_not_interrupt():
    engines, seen = fake_engines()
    blip = np.concatenate([chunks(LOUD, 5), chunks(QUIET, 40)]).astype("<f4")  # 160 ms "mm-hmm"
    sent = asyncio.run(run_call(engines, [p.tobytes() for p in np.split(blip, 45)], playing=True))
    assert sent == []
    assert seen["replies"] == []


def test_barge_in_disabled_without_echo_cancellation():
    engines, seen = fake_engines()

    async def scenario():
        sent = []

        async def send(data):
            sent.append(data)

        call = Call(engines, send, "call-1", barge_in=False)
        call.set_playing(True)
        for packet in speech_then_pause():
            await call.hear(packet)
        return sent

    assert asyncio.run(scenario()) == []
    assert seen["replies"] == []


def test_heard_text_keeps_only_what_was_played():
    spoken = [("Coorg is lovely.", 1.0), ("It is five hours away by road.", 2.0)]
    assert heard_text(spoken, 0) == ""
    assert heard_text(spoken, 1.0) == "Coorg is lovely."
    assert heard_text(spoken, 2.0) == "Coorg is lovely. It is five hours..."  # 4th word playing.
    assert heard_text(spoken, 1.01) == "Coorg is lovely. It..."
    assert heard_text(spoken, 9.0) == "Coorg is lovely. It is five hours away by road."


async def wait_until(condition):
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def test_barge_in_cancels_reply_records_heard_text_and_answers_new_turn():
    recorded, calls = [], []
    never = asyncio.Event()

    async def reply(conversation_id, text):
        calls.append(text)
        if len(calls) == 1:
            yield "First sentence here. "
            await never.wait()  # Still generating when the user interrupts.
        else:
            yield "Okay."

    async def synthesize(text):
        return np.zeros(24000, dtype=np.float32)  # One second per piece.

    async def record_interruption(conversation_id, user_text, heard):
        recorded.append((conversation_id, user_text, heard))

    engines = Engines(new_transcriber=lambda: FakeTranscriber(["what's up", "wait, stop"]),
                      new_detector=lambda listener: detector(listener=listener),
                      reply=reply, synthesize=synthesize, record_interruption=record_interruption)

    async def scenario():
        sent = []

        async def send(data):
            sent.append(data)

        call = Call(engines, send, "call-1")
        for packet in speech_then_pause():
            await call.hear(packet)
        await wait_until(lambda: call.spoken)
        call.set_playing(True)
        first_reply = call.reply_task
        for packet in speech_then_pause():
            await call.hear(packet)
            if {"type": "interrupt"} in sent and call.played is not None and not call.played.done():
                call.report_played(0.5)  # The browser flushed after half a second.
        assert first_reply.cancelled()
        await call.reply_task
        await call.close()
        return sent

    sent = asyncio.run(scenario())
    events = [item for item in sent if isinstance(item, dict)]
    types = [e["type"] for e in events]
    assert types.index("interrupt") < types.index("transcript", types.index("interrupt"))
    assert recorded == [("call-1", "what's up", "First sentence...")]
    assert calls == ["what's up", "wait, stop"]
    assert events[-1] == {"type": "reply_end"}


def test_missing_played_report_records_nothing_heard():
    recorded = []

    async def record_interruption(conversation_id, user_text, heard):
        recorded.append(heard)

    engines, _ = fake_engines()
    engines.record_interruption = record_interruption

    async def scenario():
        async def send(data):
            pass

        call = Call(engines, send, "call-1", played_report_timeout_s=0.01)
        call.user_text, call.spoken = "hello", [("Hi there.", 1.0)]
        call.set_playing(True)
        await call.interrupt()
        await call.history_task

    asyncio.run(scenario())
    assert recorded == [""]


def test_llm_failure_still_speaks_what_was_generated():
    engines, _ = fake_engines()

    async def failing(conversation_id, text):
        yield "Partial answer. "
        raise RuntimeError("provider dropped")

    engines.reply = failing
    sent = asyncio.run(run_call(engines, speech_then_pause()))
    events = [item for item in sent if isinstance(item, dict)]
    assert {"type": "notice", "text": "Sorry, I lost my train of thought."} in events
    assert any(isinstance(item, bytes) for item in sent)
    assert events[-1] == {"type": "reply_end"}


def test_same_words_ignores_case_punctuation_and_spacing():
    assert pipeline.same_words("What's the weather in Mysuru?", "whats the  weather in mysuru")
    assert not pipeline.same_words("What's the weather", "What's the weather in Mysuru?")


def speculative_engines(transcript="what's up", live=None, speculative=("Hi there. ", "All good!"),
                        allowed=True):
    """Engines whose speculative stream records whether it was committed or cancelled."""
    seen = {"replies": [], "speculations": [], "closed": [], "committed_at_first_send": []}
    hold = asyncio.Event()

    async def reply(conversation_id, text):
        seen["replies"].append(text)
        yield "Fresh answer."

    async def speculate(conversation_id, text, committed):
        seen["speculations"].append(text)
        try:
            for delta in speculative:
                yield delta
            await hold.wait()  # Stays open like a stream still generating.
        finally:
            seen["closed"].append(committed.is_set())

    async def synthesize(text):
        return np.full(6000, 0.25, dtype=np.float32)

    engines = Engines(new_transcriber=lambda: FakeTranscriber([transcript], live),
                      new_detector=lambda listener: detector(listener=listener),
                      reply=reply, synthesize=synthesize, speculate=speculate,
                      may_speculate=lambda: allowed)
    return engines, seen, hold


def events_of(sent):
    return [item for item in sent if isinstance(item, dict)]


def test_matching_transcript_commits_the_buffered_speculative_reply():
    engines, seen, hold = speculative_engines(live=["What's up?"])

    async def scenario():
        sent = []

        async def send(data):
            sent.append(data)

        call = Call(engines, send, "call-1")
        for packet in speech_then_pause():
            await call.hear(packet)
        await wait_until(lambda: call.spoken)  # Spoken while the stream is still open.
        hold.set()
        await call.reply_task
        await call.close()
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["speculations"] == ["What's up?"]  # Started from the live text.
    assert seen["replies"] == []  # No second LLM call.
    types = [e["type"] for e in events]
    assert types.index("transcript") < types.index("caption")  # Nothing spoken before the commit.
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Hi there.", "All good!"]
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["speculative"] == "committed"
    assert metrics["llm_first_token_ms"] <= 0  # Tokens arrived before the final transcript.
    assert seen["closed"] == [True]


def test_different_transcript_cancels_speculation_and_answers_the_final_text():
    engines, seen, _ = speculative_engines(live=["what's"])
    sent = asyncio.run(run_call(engines, speech_then_pause()))
    events = events_of(sent)
    assert seen["speculations"] == ["what's"]
    assert seen["closed"] == [False]  # The speculative stream was closed uncommitted.
    assert seen["replies"] == ["what's up"]
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Fresh answer."]
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["speculative"] == "cancelled"


def test_speaking_again_before_the_final_transcript_cancels_speculation():
    engines, seen, _ = speculative_engines()
    # The turn ends and speech resumes within one packet, before the transcript is handled.
    packets = [chunks(LOUD, 20).astype("<f4").tobytes(),
               np.concatenate([chunks(QUIET, 6), chunks(LOUD, 4)]).astype("<f4").tobytes()]
    sent = asyncio.run(run_call(engines, packets))
    assert seen["speculations"] == ["what's up"]
    assert seen["closed"] == [False]
    assert seen["replies"] == ["what's up"]  # The finished turn is still answered.
    (metrics,) = [e for e in events_of(sent) if e["type"] == "metrics"]
    assert metrics["speculative"] == "cancelled"


def test_speculation_is_skipped_near_the_rate_limit():
    engines, seen, _ = speculative_engines(allowed=False)
    sent = asyncio.run(run_call(engines, speech_then_pause()))
    assert seen["speculations"] == []
    assert seen["replies"] == ["what's up"]
    (metrics,) = [e for e in events_of(sent) if e["type"] == "metrics"]
    assert metrics["speculative"] == "skipped"
    assert metrics["heard_to_decided_ms"] >= 0


def test_at_most_one_speculation_per_turn():
    engines, seen, _ = speculative_engines()

    async def scenario():
        async def send(data):
            pass

        call = Call(engines, send, "call-1")
        call.loop = asyncio.get_running_loop()
        call.start_speculation("what's up")
        call.start_speculation("what's up")  # A second decision before the first is handled.
        await asyncio.sleep(0)
        await call.close()

    asyncio.run(scenario())
    assert seen["speculations"] == ["what's up"]


def test_barge_in_during_a_committed_speculative_reply():
    recorded, replies = [], []
    never = asyncio.Event()
    closed = []

    async def speculate(conversation_id, text, committed):
        try:
            yield "First sentence here. "
            await never.wait()  # Still generating when the user interrupts.
        finally:
            closed.append(committed.is_set())

    async def reply(conversation_id, text):
        replies.append(text)
        yield "Okay."

    async def synthesize(text):
        return np.zeros(24000, dtype=np.float32)

    async def record_interruption(conversation_id, user_text, heard):
        recorded.append((user_text, heard))

    engines = Engines(new_transcriber=lambda: FakeTranscriber(["what's up", "wait, stop"],
                                                              ["What's up?", "wait"]),
                      new_detector=lambda listener: detector(listener=listener),
                      reply=reply, synthesize=synthesize, record_interruption=record_interruption,
                      speculate=speculate)

    async def scenario():
        sent = []

        async def send(data):
            sent.append(data)

        call = Call(engines, send, "call-1")
        for packet in speech_then_pause():
            await call.hear(packet)
        await wait_until(lambda: call.spoken)
        call.set_playing(True)
        first_reply = call.reply_task
        for packet in speech_then_pause():
            await call.hear(packet)
            if {"type": "interrupt"} in sent and call.played is not None and not call.played.done():
                call.report_played(0.5)
        assert first_reply.cancelled()
        await call.reply_task
        await call.close()
        return sent

    events = events_of(asyncio.run(scenario()))
    # Committed, then closed by the barge-in; the next turn's live text ("wait") differed.
    assert closed == [True, False]
    # Stored against the text the speculative reply answered.
    assert recorded == [("What's up?", "First sentence...")]
    assert replies == ["wait, stop"]
    assert [e["speculative"] for e in events if e["type"] == "metrics"] == ["committed", "cancelled"]
    assert events[-1] == {"type": "reply_end"}


def test_model_marker_reaches_metrics_and_is_never_spoken():
    engines, _ = fake_engines(reply=(ReplyModel("small"), "Hi there. ", ReplyModel("big"), "All good!"))
    events = events_of(asyncio.run(run_call(engines, speech_then_pause())))
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Hi there.", "All good!"]
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["llm_model"] == "small"  # The model behind the first token.


def test_committed_speculation_reports_its_model():
    engines, _, hold = speculative_engines(live=["What's up?"],
                                           speculative=(ReplyModel("small"), "Hi there. ", "All good!"))
    hold.set()
    events = events_of(asyncio.run(run_call(engines, speech_then_pause())))
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["speculative"] == "committed" and metrics["llm_model"] == "small"
