"""Turn detection, sentence chunking and the per-call reply flow, with fake models."""
import asyncio

import numpy as np
import pytest

from app import pipeline
from app.pipeline import Call, Engines, SentenceBuffer, TurnDetector, decode_pcm
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


def detector(completion=0.9):
    return TurnDetector(LevelVad(), FixedTurn(completion), start_ms=96, pause_ms=192,
                        max_pause_ms=960, preroll_ms=128)


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


def test_sentence_buffer_starts_early_then_speaks_whole_sentences():
    buffer = SentenceBuffer(first_clause_chars=10)
    pieces = []
    for delta in ["Sure thing, ", "Coorg is lovely", " in winter. It's about ", "five hours away! ", "Want tips?"]:
        pieces += buffer.push(delta)
    pieces.append(buffer.flush())
    assert pieces == ["Sure thing,", "Coorg is lovely in winter.", "It's about five hours away!", "Want tips?"]
    assert buffer.flush() is None


def test_decode_pcm_rejects_bad_packets():
    assert len(decode_pcm(np.zeros(4, "<f4").tobytes())) == 4
    for bad in [b"", b"abc", np.zeros(16001, "<f4").tobytes(), np.array([np.nan], "<f4").tobytes()]:
        with pytest.raises(ValueError):
            decode_pcm(bad)


def fake_engines(transcript="what's up", reply=("Hi there. ", "All good!"), tts_samples=6000):
    seen = {"replies": []}

    async def transcribe(audio):
        return transcript

    async def generate(conversation_id, text):
        seen["replies"].append((conversation_id, text))
        for delta in reply:
            await asyncio.sleep(0)
            yield delta

    async def synthesize(text):
        return np.full(tts_samples, 0.25, dtype=np.float32)

    return Engines(new_detector=detector, transcribe=transcribe, reply=generate,
                   synthesize=synthesize), seen


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
    assert events[0] == {"type": "transcript", "text": "what's up"}
    assert [e["text"] for e in events if e["type"] == "text"] == ["Hi there. ", "All good!"]
    assert events[-1] == {"type": "reply_end"}
    (metrics,) = [e for e in events if e["type"] == "metrics"]
    assert metrics["endpoint_ms"] == round(6 * 32)
    # Two spoken pieces, each 6000 samples, sent in 200 ms (4800-sample) packets.
    assert len(audio) == 2 * 6000 * 4
    assert max(len(item) for item in sent if isinstance(item, bytes)) == pipeline.OUTPUT_PACKET_SAMPLES * 4
    assert seen["replies"] == [("call-1", "what's up")]


def test_silence_or_empty_transcript_does_not_reply():
    engines, seen = fake_engines(transcript="  ")
    sent = asyncio.run(run_call(engines, speech_then_pause()))
    assert sent == []
    assert seen["replies"] == []


def test_speech_while_assistant_is_playing_is_ignored():
    engines, seen = fake_engines()
    sent = asyncio.run(run_call(engines, speech_then_pause(), playing=True))
    assert sent == []
    assert seen["replies"] == []


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
