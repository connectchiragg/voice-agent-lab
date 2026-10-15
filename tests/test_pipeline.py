"""Reply segmentation and the per-call flow driven by Flux turn events, with fake Deepgram stages."""
import asyncio

import numpy as np
import pytest

from app.deepgram import TurnEvent
from app.pipeline import Call, Engines, ReplyModel, ReplyText, decode_pcm, heard_text, same_words

SEGMENT_SAMPLES = 6000  # Each fake segment: two 3000-sample chunks, 0.25 s at 24 kHz.


class FakeListener:
    """Stands in for Flux: records the audio sent; tests emit turn events by hand."""
    def __init__(self, on_event):
        self.on_event = on_event
        self.audio, self.closed = [], False

    async def start(self):
        pass

    async def send(self, pcm, arrived=None):
        self.audio.append((pcm, arrived))

    async def close(self):
        self.closed = True

    async def emit(self, kind, text="", speech_ended=None):
        import time
        await self.on_event(TurnEvent(kind, text, received=time.perf_counter(),
                                      speech_ended=speech_ended))


class FakeSpeaker:
    """Stands in for Aura: each segment yields two chunks; `hold` pauses after the first."""
    def __init__(self, hold=None):
        self.segments, self.abandoned, self.hold = [], [], hold

    async def start(self):
        pass

    async def synthesize(self, text):
        self.segments.append(text)
        finished = False
        try:
            yield np.full(SEGMENT_SAMPLES // 2, 0.25, dtype=np.float32)
            if self.hold is not None:
                await self.hold.wait()
            yield np.full(SEGMENT_SAMPLES // 2, 0.25, dtype=np.float32)
            finished = True
        finally:
            if not finished:
                self.abandoned.append(text)  # Aura would get a Clear here.

    async def close(self):
        pass


def make_engines(reply=("Hi there. ", "All good!"), speculative=None, allowed=True,
                 record_interruption=None):
    seen = {"replies": [], "speculations": [], "closed": [], "speakers": []}
    hold_speculation = asyncio.Event()

    async def generate(conversation_id, text):
        seen["replies"].append(text)
        for delta in reply:
            await asyncio.sleep(0)
            yield delta

    async def speculate(conversation_id, text, committed):
        seen["speculations"].append(text)
        try:
            for delta in speculative:
                yield delta
            await hold_speculation.wait()  # Stays open like a stream still generating.
        finally:
            seen["closed"].append(committed.is_set())

    def new_speaker():
        seen["speakers"].append(FakeSpeaker())
        return seen["speakers"][-1]

    engines = Engines(new_listener=FakeListener, new_speaker=new_speaker, reply=generate,
                      speculate=speculate if speculative is not None else None,
                      may_speculate=lambda: allowed)
    if record_interruption is not None:
        engines.record_interruption = record_interruption
    seen["release"] = hold_speculation
    return engines, seen


async def start_call(engines, sent, **options):
    async def send(data):
        sent.append(data)

    call = Call(engines, send, "call-1", **options)
    await call.start()
    return call, call.listener


async def finish(call):
    if call.reply_task:
        await call.reply_task
    await call.close()


async def wait_until(condition):
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def events_of(sent):
    return [item for item in sent if isinstance(item, dict)]


def run_turn(engines, transcript="what's up"):
    async def scenario():
        import time
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "what's")
        await flux.emit("Update", "what's")  # Unchanged text is not resent.
        await flux.emit("EndOfTurn", transcript, speech_ended=time.perf_counter() - 0.3)
        await finish(call)
        return sent
    return asyncio.run(scenario())


def test_reply_text_cuts_whole_sentences_and_merges_what_is_ready():
    text = ReplyText()
    for delta in ("Sure", " thing, Coorg is lovely", ". It is five", " hours away. Want"):
        text.push(delta)
    assert text.take() == "Sure thing, Coorg is lovely. It is five hours away."
    assert text.take() == ""  # "Want" may continue.
    text.push(" tips?")
    assert text.take() == ""  # No space after "?" yet: the sentence may go on ("?!").
    text.finish()
    assert text.take() == "Want tips?"
    assert text.take() == ""


def test_reply_text_does_not_cut_decimals_and_caps_run_ons():
    text = ReplyText(max_words=6)
    text.push("It is 3.5 km")
    assert text.take() == ""
    text.push(" away from here, past the old bridge and then")
    assert text.take() == "It is 3.5 km away from here,"  # The last clause end within the cap.
    text.push(" left at the temple gate near the market ")
    assert text.take() == "past the old bridge and then left at the temple gate near the market"


def test_next_segment_waits_for_text_and_ends_with_the_reply():
    async def scenario():
        text = ReplyText()
        waiting = asyncio.create_task(text.next_segment())
        await asyncio.sleep(0)
        assert not waiting.done()
        text.push("Hello there. ")
        assert await waiting == "Hello there."
        text.finish()
        assert await text.next_segment() is None
    asyncio.run(scenario())


def test_decode_pcm_rejects_bad_packets():
    assert len(decode_pcm(np.zeros(4, "<f4").tobytes())) == 4
    for bad in [b"", b"abc", np.zeros(16001, "<f4").tobytes(), np.array([np.nan], "<f4").tobytes()]:
        with pytest.raises(ValueError):
            decode_pcm(bad)


def test_microphone_audio_goes_to_flux_as_linear16_with_its_arrival_time():
    engines, _ = make_engines()

    async def scenario():
        call, flux = await start_call(engines, [])
        await call.hear(np.array([0.5, -1.0, 2.0], "<f4").tobytes())
        await call.close()
        return flux

    flux = asyncio.run(scenario())
    ((pcm, arrived),) = flux.audio
    assert np.frombuffer(pcm, "<i2").tolist() == [16383, -32767, 32767]
    assert arrived > 0
    assert flux.closed


def test_finished_turn_is_answered_and_spoken_with_captions_after_audio():
    engines, seen = make_engines()
    sent = run_turn(engines)
    events = events_of(sent)
    assert events[0] == {"type": "partial", "text": "what's"}
    assert events[1] == {"type": "transcript", "text": "what's up"}
    assert seen["replies"] == ["what's up"]
    # The first sentence is its own segment; the rest arrives while it is spoken.
    assert seen["speakers"][0].segments == ["Hi there.", "All good!"]
    captions = [e for e in events if e["type"] == "caption"]
    assert [(c["text"], c["seconds"]) for c in captions] == [("Hi there.", 0.25), ("All good!", 0.25)]
    first_caption = sent.index(captions[0])
    assert isinstance(sent[first_caption - 1], bytes)  # Each caption follows its audio.
    assert events[-1] == {"type": "reply_end"}
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert 290 <= metrics["endpoint_ms"] <= 2000  # Speech ended 0.3 s before EndOfTurn.
    assert metrics["response_ms"] >= metrics["endpoint_ms"]
    assert metrics["speculative"] == "skipped" and metrics["eager_lead_ms"] is None
    audio = b"".join(item for item in sent if isinstance(item, bytes))
    assert len(audio) == 2 * SEGMENT_SAMPLES * 4


def test_empty_transcript_does_not_reply():
    engines, seen = make_engines()
    sent = run_turn(engines, transcript="  ")
    assert events_of(sent)[-1] == {"type": "partial", "text": ""}  # Only clears the live line.
    assert seen["replies"] == []


def test_without_echo_cancellation_a_turn_during_playback_is_ignored():
    engines, seen = make_engines()

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent, barge_in=False)
        call.set_playing(True)
        await flux.emit("StartOfTurn", "hello")
        await flux.emit("Update", "hello there")
        call.set_playing(False)
        await flux.emit("EndOfTurn", "hello there")
        await finish(call)
        return sent

    assert asyncio.run(scenario()) == []  # No partials, no interrupt, no reply.
    assert seen["replies"] == []


def test_heard_text_keeps_only_what_was_played():
    spoken = [("Coorg is lovely.", 1.0), ("It is five hours away by road.", 2.0)]
    assert heard_text(spoken, 0) == ""
    assert heard_text(spoken, 1.0) == "Coorg is lovely."
    assert heard_text(spoken, 2.0) == "Coorg is lovely. It is five hours..."  # 4th word playing.
    assert heard_text(spoken, 1.01) == "Coorg is lovely. It..."
    assert heard_text(spoken, 9.0) == "Coorg is lovely. It is five hours away by road."


def test_barge_in_cancels_reply_clears_tts_records_heard_text_and_answers_new_turn():
    recorded = []

    async def record_interruption(conversation_id, user_text, heard):
        recorded.append((conversation_id, user_text, heard))

    engines, seen = make_engines(reply=("First sentence here. ", "Second one. "),
                                 record_interruption=record_interruption)

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        speaker = call.speaker
        speaker.hold = asyncio.Event()  # The segment's second chunk never arrives.
        await flux.emit("StartOfTurn", "what's")
        await flux.emit("EndOfTurn", "what's up")
        await wait_until(lambda: call.current is not None and call.current[1] > 0)
        call.set_playing(True)
        first_reply = call.reply_task
        speaker.hold = None
        await flux.emit("StartOfTurn", "wait")  # Barge-in.
        assert first_reply.cancelled()
        assert speaker.abandoned == ["First sentence here."]
        call.report_played(0.05)  # The browser flushed after 50 ms.
        await flux.emit("EndOfTurn", "wait, stop")
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    types = [e["type"] for e in events]
    assert types.index("interrupt") < types.index("transcript", types.index("interrupt"))
    # 0.05 of the 0.125 s received so far was played: the first two of three words.
    assert recorded == [("call-1", "what's up", "First sentence...")]
    assert seen["replies"] == ["what's up", "wait, stop"]
    assert events[-1] == {"type": "reply_end"}


def test_missing_played_report_records_nothing_heard():
    recorded = []

    async def record_interruption(conversation_id, user_text, heard):
        recorded.append(heard)

    engines, _ = make_engines(record_interruption=record_interruption)

    async def scenario():
        call, _ = await start_call(engines, [], played_report_timeout_s=0.01)
        call.user_text, call.spoken = "hello", [("Hi there.", 1.0)]
        call.set_playing(True)
        await call.interrupt()
        await call.history_task
        await call.close()

    asyncio.run(scenario())
    assert recorded == [""]


def test_llm_failure_still_speaks_what_was_generated():
    engines, _ = make_engines()

    async def failing(conversation_id, text):
        yield "Partial answer. "
        raise RuntimeError("provider dropped")

    engines.reply = failing
    events = events_of(run_turn(engines))
    assert {"type": "notice", "text": "Sorry, I lost my train of thought."} in events
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Partial answer."]
    assert events[-1] == {"type": "reply_end"}


def test_same_words_ignores_case_punctuation_and_spacing():
    assert same_words("What's the weather in Mysuru?", "whats the  weather in mysuru")
    assert not same_words("What's the weather", "What's the weather in Mysuru?")


def test_eager_end_of_turn_reply_is_buffered_then_committed_at_end_of_turn():
    engines, seen = make_engines(speculative=("Hi there. ", "All good!"))

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "What's")
        await flux.emit("EagerEndOfTurn", "What's up?")
        await wait_until(lambda: call.speculation.deltas)
        assert not any(isinstance(item, bytes) for item in sent)  # Nothing spoken before the commit.
        await flux.emit("EndOfTurn", "What's up?")
        await wait_until(lambda: len(call.spoken) == 1)  # Spoken while the stream is still open.
        seen["release"].set()
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["speculations"] == ["What's up?"]
    assert seen["replies"] == []  # No second LLM call.
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Hi there.", "All good!"]
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["speculative"] == "committed" and metrics["speculations"] == 1
    assert metrics["eager_lead_ms"] >= 0
    assert metrics["llm_first_token_ms"] <= 0  # Tokens arrived before EndOfTurn.
    assert seen["closed"] == [True]


def test_turn_resumed_cancels_the_speculation_and_the_next_eager_one_is_used():
    engines, seen = make_engines(speculative=("Sure. ",))

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "Which")
        await flux.emit("EagerEndOfTurn", "Which is better?")
        await wait_until(lambda: call.speculation.deltas)
        await flux.emit("TurnResumed", "Which is better, Python")
        await wait_until(lambda: seen["closed"])
        assert seen["closed"] == [False]  # Closed uncommitted: its stream is gone.
        await flux.emit("EagerEndOfTurn", "Which is better, Python or Java?")
        await flux.emit("EndOfTurn", "Which is better, Python or Java?")
        seen["release"].set()
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["speculations"] == ["Which is better?", "Which is better, Python or Java?"]
    assert seen["replies"] == []
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["speculative"] == "committed" and metrics["speculations"] == 2


def test_resumed_turn_without_a_new_eager_end_is_answered_afresh():
    engines, seen = make_engines(speculative=("Sure. ",))

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "Book")
        await flux.emit("EagerEndOfTurn", "Book a table.")
        await flux.emit("TurnResumed", "Book a table for four")
        await flux.emit("EndOfTurn", "Book a table for four.")  # Ended by eot_timeout_ms.
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["replies"] == ["Book a table for four."]
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["speculative"] == "cancelled" and metrics["speculations"] == 1


def test_different_final_words_cancel_the_speculation():
    engines, seen = make_engines(speculative=("Sure. ",))

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "what's")
        await flux.emit("EagerEndOfTurn", "what's")
        await flux.emit("EndOfTurn", "what's up")
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["closed"] == [False]
    assert seen["replies"] == ["what's up"]
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Hi there.", "All good!"]


def test_speculation_is_skipped_near_the_rate_limit():
    engines, seen = make_engines(speculative=("Sure. ",), allowed=False)

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "hi")
        await flux.emit("EagerEndOfTurn", "what's up")
        await flux.emit("EndOfTurn", "what's up")
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["speculations"] == []
    assert seen["replies"] == ["what's up"]
    assert [e for e in events if e["type"] == "metrics"][0]["speculative"] == "skipped"


def test_reconnect_reset_drops_the_speculation_and_the_live_line():
    engines, seen = make_engines(speculative=("Sure. ",))

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "what's")
        await flux.emit("EagerEndOfTurn", "what's up")
        await wait_until(lambda: call.speculation.deltas)
        await flux.emit("Reset")
        await wait_until(lambda: seen["closed"])
        assert call.speculation is None
        await call.close()
        return sent

    sent = asyncio.run(scenario())
    assert seen["closed"] == [False]
    assert events_of(sent)[-1] == {"type": "partial", "text": ""}


def test_barge_in_during_a_committed_speculative_reply():
    recorded = []

    async def record_interruption(conversation_id, user_text, heard):
        recorded.append((user_text, heard))

    engines, seen = make_engines(speculative=("First sentence here. ",),
                                 record_interruption=record_interruption)

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "What's")
        await flux.emit("EagerEndOfTurn", "What's up?")
        await flux.emit("EndOfTurn", "What's up?")
        await wait_until(lambda: call.spoken)
        call.set_playing(True)
        first_reply = call.reply_task
        await flux.emit("StartOfTurn", "wait")
        assert first_reply.cancelled()
        call.report_played(0.1)
        await flux.emit("EndOfTurn", "wait, stop")
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["closed"] == [True]  # Committed, then closed by the barge-in.
    # Stored against the text the speculative reply answered.
    assert recorded == [("What's up?", "First sentence...")]
    assert seen["replies"] == ["wait, stop"]
    assert [e["speculative"] for e in events if e["type"] == "metrics"] == ["committed", "skipped"]
    assert events[-1] == {"type": "reply_end"}


def test_model_marker_reaches_metrics_and_is_never_spoken():
    engines, _ = make_engines(reply=(ReplyModel("small"), "Hi there. ", ReplyModel("big"), "All good!"))
    events = events_of(run_turn(engines))
    assert [e["text"] for e in events if e["type"] == "caption"] == ["Hi there.", "All good!"]
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["llm_model"] == "small"  # The model behind the first token.


def test_caller_memory_loads_beside_the_sockets_and_is_handed_off_after_close():
    engines, _ = make_engines()
    events = []
    release = asyncio.Event()

    class SlowListener(FakeListener):
        async def start(self):
            events.append("flux opening")
            await release.wait()

    async def begin_call(conversation_id, caller_id):
        events.append(("memory", conversation_id, caller_id))
        release.set()  # Runs while Flux is still opening: they are concurrent.

    engines.new_listener = SlowListener
    engines.begin_call = begin_call
    engines.end_call = lambda conversation_id: events.append(("ended", conversation_id))

    async def scenario():
        call, _ = await start_call(engines, [], caller_id="tel:+919812345678")
        assert events == ["flux opening", ("memory", "call-1", "tel:+919812345678")]
        await call.close()
    asyncio.run(scenario())
    assert events[-1] == ("ended", "call-1")


def greeting_engines():
    engines, seen = make_engines()
    stored = []

    async def record_greeting(conversation_id, text, status):
        stored.append((conversation_id, text, status))

    engines.greeting = lambda conversation_id: "Hi Asha, this is VALR. How can I help?"
    engines.record_greeting = record_greeting
    return engines, seen, stored


def test_the_greeting_is_spoken_first_without_the_llm_and_stored():
    engines, seen, stored = greeting_engines()

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        call.greet()
        await call.reply_task
        await call.history_task
        # The caller's first turn is answered after it, with the greeting in history.
        await flux.emit("StartOfTurn", "hi")
        await flux.emit("EndOfTurn", "what's the weather")
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert seen["replies"] == ["what's the weather"]  # The greeting itself asked no LLM.
    # One segment: the whole greeting is known, so it is synthesized at once.
    assert [e["text"] for e in events if e["type"] == "caption"][0] == (
        "Hi Asha, this is VALR. How can I help?")
    assert stored == [("call-1", "Hi Asha, this is VALR. How can I help?", "completed")]
    assert events.index({"type": "reply_end"}) < [e["type"] for e in events].index("transcript")
    assert not any(e["type"] == "metrics" for e in events[:events.index({"type": "reply_end"})])


def test_barge_in_cuts_the_greeting_and_stores_only_what_was_heard():
    engines, seen, stored = greeting_engines()

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        call.speaker.hold = asyncio.Event()
        call.greet()
        await wait_until(lambda: call.current is not None and call.current[1] > 0)
        call.set_playing(True)
        greeting = call.reply_task
        await flux.emit("StartOfTurn", "sorry")  # Barge-in during the greeting.
        assert greeting.cancelled() and call.speaker.abandoned == [
            "Hi Asha, this is VALR. How can I help?"]
        call.speaker.hold = None
        call.report_played(0.06)  # 0.06 of the 0.125 s received: five of nine words.
        await flux.emit("EndOfTurn", "sorry, who is this")
        await finish(call)
        return sent

    events = events_of(asyncio.run(scenario()))
    assert {"type": "interrupt"} in events
    assert stored == [("call-1", "Hi Asha, this is VALR....", "interrupted")]
    assert seen["replies"] == ["sorry, who is this"]


def test_without_a_greeting_the_caller_speaks_first():
    engines, _ = make_engines()

    async def scenario():
        sent = []
        call, _ = await start_call(engines, sent)
        call.greet()
        assert call.reply_task is None
        await call.close()
        return sent
    assert asyncio.run(scenario()) == []


# Backchannel: "sure" while the assistant speaks is not a barge-in.

def test_backchannel_phrases_are_recognized():
    from app.pipeline import Backchannel
    backchannel = Backchannel.from_words()
    for text in ("Sure.", "uh-huh", "Mm-hmm, okay", "OK", "got it", "Yeah, right.", "I see"):
        assert backchannel.only(text), text
    for text in ("", "stop", "sure, but wait", "okay what about Chennai", "yes yes yes yes",
                 "no", "got"):
        assert not backchannel.only(text), text
    custom = Backchannel.from_words("achha, theek hai")
    assert custom.only("Theek hai") and not custom.only("okay")


def speaking_call(engines, sent):
    """A call whose reply is mid-segment and playing, with backchannel filtering on."""
    from app.pipeline import Backchannel
    engines.backchannel = Backchannel.from_words(hold_s=0.05)

    async def setup():
        call, flux = await start_call(engines, sent)
        call.speaker.hold = asyncio.Event()
        await flux.emit("StartOfTurn", "what's")
        await flux.emit("EndOfTurn", "what's up")
        await wait_until(lambda: call.current is not None and call.current[1] > 0)
        call.set_playing(True)
        return call, flux
    return setup()


def test_a_backchannel_while_speaking_is_ignored():
    engines, seen = make_engines(reply=("I can hear you loud and clear. ", "What would you like? "))

    async def scenario():
        sent = []
        call, flux = await speaking_call(engines, sent)
        reply = call.reply_task
        await flux.emit("StartOfTurn", "")
        await flux.emit("Update", "Sure.")
        await asyncio.sleep(0.1)  # Past the hold: words were heard, and they were backchannel.
        await flux.emit("EndOfTurn", "Sure.")
        assert not reply.done() and not call.holding
        call.speaker.hold.set()
        call.set_playing(False)
        await finish(call)
        return sent
    events = events_of(asyncio.run(scenario()))
    assert {"type": "interrupt"} not in events
    assert seen["replies"] == ["what's up"]  # "Sure." got no reply of its own.


def test_a_real_interruption_still_clears():
    engines, seen = make_engines(reply=("I can hear you loud and clear. ", "What would you like? "))

    async def scenario():
        sent = []
        call, flux = await speaking_call(engines, sent)
        reply = call.reply_task
        await flux.emit("StartOfTurn", "")
        assert not reply.done()  # Held: no words yet.
        await flux.emit("Update", "Wait, stop")
        assert reply.cancelled()
        call.speaker.hold = None
        call.report_played(0.05)
        await flux.emit("EndOfTurn", "Wait, stop.")
        await finish(call)
        return sent
    events = events_of(asyncio.run(scenario()))
    assert {"type": "interrupt"} in events
    assert seen["replies"] == ["what's up", "Wait, stop."]


def test_speech_with_no_words_yet_interrupts_after_the_hold():
    engines, _ = make_engines(reply=("I can hear you loud and clear. ", "What would you like? "))

    async def scenario():
        sent = []
        call, flux = await speaking_call(engines, sent)
        reply = call.reply_task
        await flux.emit("StartOfTurn", "")
        await asyncio.sleep(0.1)  # hold_s is 50 ms here.
        assert reply.cancelled()
        call.speaker.hold = None
        await call.close()
        return sent
    assert {"type": "interrupt"} in events_of(asyncio.run(scenario()))


def test_a_backchannel_then_real_words_interrupts():
    engines, seen = make_engines(reply=("I can hear you loud and clear. ", "What would you like? "))

    async def scenario():
        sent = []
        call, flux = await speaking_call(engines, sent)
        reply = call.reply_task
        await flux.emit("StartOfTurn", "Okay")
        await flux.emit("Update", "Okay")
        await asyncio.sleep(0.1)
        assert not reply.done()
        await flux.emit("Update", "Okay so what about Chennai")
        assert reply.cancelled()
        call.speaker.hold = None
        call.report_played(0.05)
        await flux.emit("EndOfTurn", "Okay, so what about Chennai?")
        await finish(call)
        return sent
    asyncio.run(scenario())
    assert seen["replies"] == ["what's up", "Okay, so what about Chennai?"]


def test_okay_is_a_normal_answer_when_the_assistant_is_silent():
    from app.pipeline import Backchannel
    engines, seen = make_engines()
    engines.backchannel = Backchannel.from_words(hold_s=0.05)

    async def scenario():
        sent = []
        call, flux = await start_call(engines, sent)
        await flux.emit("StartOfTurn", "Okay")
        await flux.emit("EndOfTurn", "Okay.")
        await finish(call)
        return sent
    events = events_of(asyncio.run(scenario()))
    assert seen["replies"] == ["Okay."] and {"type": "transcript", "text": "Okay."} in events
