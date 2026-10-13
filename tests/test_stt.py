"""StreamingTranscriber against a fake sherpa recognizer (no model download needed)."""
import threading

import numpy as np

from app import stt


class FakeRecognizer:
    """Emits one word per second of audio received; tracks lead/tail padding."""
    def __init__(self):
        self.streams = []

    def create_stream(self):
        stream = type("Stream", (), {})()
        stream.samples, stream.finished = [], False
        stream.accept_waveform = lambda rate, x: stream.samples.append(np.asarray(x))
        stream.input_finished = lambda: setattr(stream, "finished", True)
        self.streams.append(stream)
        return stream

    def is_ready(self, stream):
        return False

    def decode_stream(self, stream):
        pass

    def get_result(self, stream):
        seconds = sum(len(x) for x in stream.samples) / stt.SAMPLE_RATE
        return " ".join(["word"] * int(seconds))


def test_partials_then_final_with_lead_and_tail_padding():
    recognizer = FakeRecognizer()
    transcriber = stt.StreamingTranscriber(recognizer, threading.Lock())
    transcriber.on_speech_start(np.zeros(int(0.68 * 16000), np.float32))  # 0.32 s lead + 0.68 s = 1 s
    assert transcriber.take_partial() == "word"
    assert transcriber.take_partial() is None  # Only new text is reported.
    transcriber.on_speech(np.zeros(16000, np.float32))
    assert transcriber.take_partial() == "word word"
    final = transcriber.on_speech_end()
    stream = recognizer.streams[0]
    assert stream.finished
    assert len(stream.samples[0]) == int(stt.LEAD_SECONDS * 16000) + int(0.68 * 16000)
    assert len(stream.samples[-1]) == int(stt.TAIL_SECONDS * 16000)
    assert final == "word word"  # 2.5 s of audio including padding.
    assert transcriber.final_ms >= 0 and transcriber.take_partial() is None


def test_discard_and_end_without_start_are_safe():
    transcriber = stt.StreamingTranscriber(FakeRecognizer(), threading.Lock())
    assert transcriber.on_speech_end() == ""
    transcriber.on_speech(np.zeros(1600, np.float32))  # Ignored: no utterance open.
    transcriber.on_speech_start(np.zeros(32000, np.float32))
    transcriber.on_discard()
    assert transcriber.take_partial() is None and transcriber.on_speech_end() == ""
