"""Per-stage latency benchmark for the local models: STT finalization and TTS first piece.

Runs the real pipeline code (TurnDetector, StreamingTranscriber, SpeechChunker,
KokoroVoice) without the network or Groq, and prints p50/p90 per stage. Run it
where the app runs, for example inside the image on the VM:

    sudo docker run --rm -v ~/bench:/bench -e PYTHONPATH=/app voice-agent-lab \\
        python scripts/bench_latency.py --audio-dir /bench/utts

Utterances: Kokoro speaks a fixed set of user questions in a few voices (not
the assistant's), plus any `name.wav` (16 kHz mono 16-bit) + `name.txt` pairs in
--audio-dir, such as macOS `say` output. Recordings are never committed.

STT: each utterance is fed in 100 ms packets with silence around it, exactly as
the browser sends it; we report the turn's pause, finalization time and word
error rate. TTS: canned replies go through SpeechChunker; we time the first
piece, then synthesize the rest back to back and estimate playback gaps the way
Call.speak does, assuming the LLM text is all available at once.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.pipeline import SpeechChunker, TurnDetector  # noqa: E402
from app.stt import MODEL_DIR, StreamingTranscriber, load_recognizer  # noqa: E402
from app.tts import OUTPUT_SAMPLE_RATE, KokoroVoice  # noqa: E402
from app.turn import SmartTurn  # noqa: E402
from app.vad import CHUNK, SileroVad, load_session  # noqa: E402

Samples = NDArray[np.float32]
PACKET = 1600  # 100 ms at 16 kHz, as the browser sends.
QUESTIONS = [
    "What's the weather like in Bengaluru this weekend?",
    "Can you tell me a fun fact about octopuses?",
    "I need to book a table for four people tomorrow evening.",
    "How long does it take to drive from Mysore to the airport?",
    "Remind me what time it is in London right now.",
    "Actually, cancel that and play some relaxing music instead.",
    "Which is better for a beginner, Python or JavaScript?",
    "Thanks, that was really helpful.",
]
USER_VOICES = ("am_michael", "bf_emma")
REPLIES = [
    "It should be mostly sunny in Bengaluru this weekend, with highs around twenty eight degrees "
    "and a small chance of rain on Sunday evening. A light jacket should be enough.",
    "Here's a fun one: octopuses have three hearts and blue blood. Two hearts pump blood to the "
    "gills, and the third pumps it to the rest of the body.",
    "Sure, I can help with that. For four people tomorrow evening, the best option is to call the "
    "restaurant directly, since most places hold only a few tables for walk-ins.",
    "The drive from Mysore to the airport usually takes about three and a half hours, depending on "
    "traffic around the city. Leaving early in the morning is the safest bet.",
    "In London it's a quarter past four in the afternoon right now.",
    "Okay, I've cancelled that. I can't play music myself, but a calm piano playlist on your phone "
    "would be a great way to relax.",
    "For a beginner, Python is usually the easier start, because its syntax reads almost like plain "
    "English. JavaScript is the better pick if you mainly want to build websites.",
    "You're welcome! Let me know if there's anything else you need.",
]


def percentiles(values: list[float]) -> str:
    if not values:
        return "n/a"
    p50, p90 = np.percentile(values, [50, 90])
    return f"p50 {p50:7.0f}  p90 {p90:7.0f}" if p90 >= 10 else f"p50 {p50:7.3f}  p90 {p90:7.3f}"


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("-", " ")).split()


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, start=1):
        previous, row[0] = row[0], i
        for j, hyp in enumerate(hypothesis, start=1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (ref != hyp))
    return row[-1]


def resample(audio: Samples, source: int, target: int) -> Samples:
    """Windowed-sinc low-pass, then linear interpolation; good enough for speech tests."""
    if source == target:
        return audio
    cutoff = 0.45 * min(source, target) / source
    taps = np.arange(-32, 33)
    kernel = 2 * cutoff * np.sinc(2 * cutoff * taps) * np.hamming(len(taps))
    filtered = np.convolve(audio, kernel / kernel.sum(), mode="same")
    times = np.arange(0, len(audio) / source, 1 / target)
    return np.interp(times, np.arange(len(audio)) / source, filtered).astype(np.float32)


def read_wav(path: Path) -> Samples:
    with wave.open(str(path)) as file:
        if file.getframerate() != 16000 or file.getnchannels() != 1 or file.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16 kHz mono 16-bit")
        return np.frombuffer(file.readframes(file.getnframes()), "<i2").astype(np.float32) / 32768


def write_wav(path: Path, audio: Samples, rate: int) -> None:
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def utterances(voice: KokoroVoice, audio_dir: Path | None) -> list[tuple[str, str, Samples]]:
    """(name, reference text, 16 kHz audio) for every test utterance."""
    found = []
    for speaker in USER_VOICES:
        voice.voice = speaker
        for index, text in enumerate(QUESTIONS):
            audio = resample(voice.synthesize(text), OUTPUT_SAMPLE_RATE, 16000)
            found.append((f"kokoro_{speaker}_{index}", text, audio))
    if audio_dir is not None:
        for path in sorted(audio_dir.glob("*.wav")):
            found.append((path.stem, path.with_suffix(".txt").read_text().strip(), read_wav(path)))
    return found


def bench_stt(found: list[tuple[str, str, Samples]], recognizer, sessions) -> dict[str, list[float]]:
    """Feed each utterance like a live call; time finalization and score the transcript."""
    vad_session, turn = sessions
    lock = threading.Lock()
    stats: dict[str, list[float]] = {"endpoint_ms": [], "stt_ms": [], "feed_rtf": []}
    errors = reference_words = 0
    for name, reference, audio in found:
        transcriber = StreamingTranscriber(recognizer, lock)
        detector = TurnDetector(SileroVad(vad_session), turn, listener=transcriber)
        stream = np.concatenate((np.zeros(8000, np.float32), audio, np.zeros(32000, np.float32)))
        finished, busy, busy_to_end, heard_to_end = [], 0.0, 0.0, 0.0
        for start in range(0, len(stream), PACKET):
            started = time.perf_counter()
            turns = detector.feed(stream[start:start + PACKET])
            busy += time.perf_counter() - started
            for utterance in turns:
                busy -= utterance.stt_ms / 1000  # Finalization is reported on its own.
                stats["endpoint_ms"].append(utterance.pause_ms)
                stats["stt_ms"].append(utterance.stt_ms)
            if turns:
                busy_to_end, heard_to_end = busy, (start + PACKET) / 16000
            finished += turns
        if not finished:
            print(f"  {name}: no turn end detected", file=sys.stderr)
            continue
        # Busy time (VAD, Smart Turn, streaming STT) per second of audio up to the
        # last turn end, finalization excluded: above 1, a live call falls behind.
        stats["feed_rtf"].append(busy_to_end / heard_to_end)
        # A pause can split one clip into two turns; score the words of all of them.
        text = " ".join(utterance.text for utterance in finished)
        distance = edit_distance(words(reference), words(text))
        errors += distance
        reference_words += len(words(reference))
        if distance or len(finished) > 1:
            print(f"  {name}: {len(finished)} turn(s) {text!r} vs {reference!r}", file=sys.stderr)
    stats["wer_pct"] = [100 * errors / max(1, reference_words)]
    return stats


def transcribe(audio: Samples, recognizer) -> str:
    transcriber = StreamingTranscriber(recognizer, threading.Lock())
    transcriber.on_speech_start(resample(audio, OUTPUT_SAMPLE_RATE, 16000))
    return transcriber.on_speech_end()


def bench_tts(voice: KokoroVoice, schedule: tuple[int, ...], recognizer, save_dir: Path | None,
              background: "Load | None" = None) -> dict[str, list[float]]:
    """Time the first piece of each reply and estimate gaps for the rest."""
    stats: dict[str, list[float]] = {"first_piece_ms": [], "first_piece_words": [],
                                     "first_audio_s": [], "rtf": [], "gaps_ms": []}
    errors = reference_words = 0
    for index, reply in enumerate(REPLIES):
        chunker = SpeechChunker(schedule=schedule)
        pieces = [piece for start in range(0, len(reply), 3) for piece in chunker.push(reply[start:start + 3])]
        pieces += chunker.flush()
        if background is not None:
            background.start()
        started = time.perf_counter()
        playhead: float | None = None
        gaps = 0.0
        spoken: list[Samples] = []
        for number, piece in enumerate(pieces):
            began = time.perf_counter()
            audio = voice.synthesize(piece)
            now = time.perf_counter()
            seconds = len(audio) / OUTPUT_SAMPLE_RATE
            stats["rtf"].append((now - began) / seconds)
            if number == 0:
                stats["first_piece_ms"].append((now - started) * 1000)
                stats["first_piece_words"].append(len(piece.split()))
                stats["first_audio_s"].append(seconds)
            if playhead is not None and now > playhead:
                gaps += now - playhead
            playhead = max(playhead or now, now) + seconds
            spoken.append(audio)
        if background is not None:
            background.stop()
        stats["gaps_ms"].append(gaps * 1000)
        # Quality checks run after timing, so they do not count as synthesis time.
        for number, (piece, audio) in enumerate(zip(pieces, spoken)):
            if save_dir is not None:
                write_wav(save_dir / f"reply{index}_piece{number}.wav", audio, OUTPUT_SAMPLE_RATE)
            if recognizer is not None:
                heard = transcribe(audio, recognizer)
                distance = edit_distance(words(piece), words(heard))
                errors += distance
                reference_words += len(words(piece))
                if distance:
                    print(f"  piece {index}.{number}: {heard!r} vs {piece!r}", file=sys.stderr)
    if recognizer is not None:
        stats["roundtrip_wer_pct"] = [100 * errors / max(1, reference_words)]
    return stats


class Load:
    """Background audio work during synthesis, paced in real time like a live call.

    "vad": Silero on every 32 ms chunk of silence (an idle microphone).
    "speech": the full detector + streaming STT on speech (echo or barge-in).
    """

    def __init__(self, kind: str, audio: Samples, vad_session, turn, recognizer) -> None:
        self.kind, self.audio = kind, audio
        self.vad_session, self.turn, self.recognizer = vad_session, turn, recognizer
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()

    def start(self) -> None:
        self.stopping.clear()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        assert self.thread is not None
        self.thread.join()

    def run(self) -> None:
        listener = (StreamingTranscriber(self.recognizer, threading.Lock())
                    if self.kind == "speech" else None)
        detector = TurnDetector(SileroVad(self.vad_session), self.turn, listener=listener,
                                max_turn_s=3600)
        audio = self.audio if self.kind == "speech" else np.zeros(16000, np.float32)
        position, next_at = 0, time.perf_counter()
        while not self.stopping.is_set():
            packet = np.take(audio, range(position, position + PACKET), mode="wrap")
            position = (position + PACKET) % len(audio)
            detector.feed(packet)
            next_at += PACKET / 16000
            time.sleep(max(0.0, next_at - time.perf_counter()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", type=Path, default=Path(os.getenv("MODELS_DIR", ".cache/models")))
    parser.add_argument("--kokoro", type=Path, help="Kokoro ONNX file (default: the app's KOKORO_MODEL)")
    parser.add_argument("--voice", default=os.getenv("KOKORO_VOICE", "af_heart"))
    parser.add_argument("--schedule", default="6,12", help="SpeechChunker word schedule")
    parser.add_argument("--audio-dir", type=Path, help="Extra name.wav + name.txt utterances")
    parser.add_argument("--save-wavs", type=Path, help="Write each synthesized piece here")
    parser.add_argument("--roundtrip", action="store_true", help="Transcribe TTS pieces with Nemotron")
    parser.add_argument("--load", choices=("vad", "speech"), help="Background work during TTS")
    parser.add_argument("--skip-stt", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    args = parser.parse_args()

    models = args.models
    kokoro = args.kokoro or models / os.getenv("KOKORO_MODEL", "kokoro-v1.0.onnx")
    loading = time.perf_counter()
    vad_session = load_session(str(models / "silero_vad.onnx"))
    turn = SmartTurn(load_session(str(models / "smart-turn-v3.2-cpu.onnx")))
    voice = KokoroVoice(str(kokoro), str(models / "voices-v1.0.bin"), args.voice)
    recognizer = load_recognizer(str(models / MODEL_DIR), int(os.getenv("STT_THREADS", "2")))
    voice.synthesize("Hello.")  # Warm-up, as the app does.
    transcribe(np.zeros(24000, np.float32), recognizer)
    print(f"Loaded in {time.perf_counter() - loading:.1f} s: {kokoro.name}, "
          f"STT_THREADS={os.getenv('STT_THREADS', '2')}, TTS_THREADS={os.getenv('TTS_THREADS', '0')}, "
          f"schedule={args.schedule}", file=sys.stderr)

    results: dict[str, list[float]] = {}
    if not args.skip_stt or args.load == "speech":
        reference = KokoroVoice(str(models / "kokoro-v1.0.onnx"), str(models / "voices-v1.0.bin"),
                                USER_VOICES[0]) if kokoro.name != "kokoro-v1.0.onnx" else voice
        found = utterances(reference, args.audio_dir)
        voice.voice = args.voice
    if not args.skip_stt:
        results.update(bench_stt(found, recognizer, (vad_session, turn)))
    if not args.skip_tts:
        if args.save_wavs is not None:
            args.save_wavs.mkdir(parents=True, exist_ok=True)
        schedule = tuple(int(n) for n in args.schedule.split(",") if n)
        load = None
        if args.load is not None:
            speech = np.concatenate([audio for _, _, audio in found]) if args.load == "speech" else None
            load = Load(args.load, speech, vad_session, turn, recognizer)
        results.update(bench_tts(voice, schedule, recognizer if args.roundtrip else None,
                                 args.save_wavs, load))
    for stage, values in results.items():
        print(f"{stage:18s} {percentiles(values)}   (n={len(values)})")
    print(json.dumps({stage: [round(v, 3) for v in values] for stage, values in results.items()}),
          file=sys.stderr)


if __name__ == "__main__":
    main()
