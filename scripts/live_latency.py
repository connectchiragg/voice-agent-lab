"""Live end-to-end latency and accuracy over the real call WebSocket (replaces the local-model bench).

    python scripts/live_latency.py https://<host> CLIPS_DIR [--rounds 2] [--out turns.jsonl]
    python scripts/live_latency.py https://<host> CLIPS_DIR --barge-in

Each `name.wav` (16 kHz mono 16-bit) with a `name.txt` reference is one turn on
a fresh call, streamed in 100 ms float32 packets at real time, exactly like the
browser. Make clips with macOS `say`, for example:

    say -v Rishi -o q.aiff "What time is it in London?" &&
        afconvert -f WAVE -d LEI16@16000 -c 1 q.aiff q.wav

Client clock, per turn: the true end of speech is when the packet holding the
last non-silent sample was sent, plus that sample's offset in it. Reported
after it: the transcript, and the first reply audio, received over the network
(uplink and downlink included). The server's own `metrics` event adds Flux's
end-of-turn time, the eager lead, LLM first token, Aura first audio and its
response time, all by packet arrival on the server. Word error rate compares
the transcript to the reference. Recordings are never committed.

--barge-in: after the first reply audio, the next clip is spoken over the
reply; we time from that speech starting to the server's `interrupt`.
"""
import argparse
import asyncio
import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np
import websockets

RATE, PACKET = 16000, 1600  # 100 ms packets, as the browser sends.
SILENT = 0.01  # Below this amplitude a sample counts as silence.


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit(f"{path}: expected 16 kHz mono 16-bit PCM WAV.")
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("-", " ")).split()


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, start=1):
        previous, row[0] = row[0], i
        for j, hyp in enumerate(hypothesis, start=1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (ref != hyp))
    return row[-1]


async def stream(socket, audio: np.ndarray, sends: list[float]) -> None:
    """Send `audio` at real time; sends[i] is when packet i went out."""
    started = time.perf_counter()
    for index, offset in enumerate(range(0, len(audio), PACKET)):
        await socket.send(audio[offset:offset + PACKET].astype("<f4").tobytes())
        sends.append(time.perf_counter())
        await asyncio.sleep(max(0.0, started + (index + 1) * PACKET / RATE - time.perf_counter()))


def sent_at(sends: list[float], sample: int) -> float:
    """When a sample of the streamed audio was sent (its packet's send time plus offset)."""
    index = sample // PACKET
    return sends[index] + (sample - index * PACKET) / RATE


async def turn(url: str, speech: np.ndarray, interrupter: np.ndarray | None = None) -> dict:
    lead = RATE // 2
    voiced = np.nonzero(np.abs(speech) > SILENT)[0]
    end = lead + int(voiced[-1])
    audio = np.concatenate([np.zeros(lead, np.float32), speech, np.zeros(4 * RATE, np.float32)])
    out: dict = {}
    sends: list[float] = []
    async with websockets.connect(url, max_size=None) as socket:
        await socket.send(json.dumps({"type": "start", "sample_rate": RATE, "encoding": "pcm_f32le",
                                      "echo_cancelled": True}))
        ready = json.loads(await socket.recv())
        if ready.get("type") != "ready":
            return {"error": ready}
        sender = asyncio.create_task(stream(socket, audio, sends))
        barge: asyncio.Task | None = None
        barge_sends: list[float] = []
        try:
            async with asyncio.timeout(40):
                async for frame in socket:
                    now = time.perf_counter()
                    if isinstance(frame, bytes):
                        if "first_audio" not in out:
                            out["first_audio"] = now
                            if interrupter is not None:
                                # As the browser would: report playback, then talk over it.
                                await socket.send(json.dumps({"type": "playback", "active": True}))
                                sender.cancel()
                                barge = asyncio.create_task(stream(
                                    socket, np.concatenate([interrupter, np.zeros(4 * RATE, np.float32)]),
                                    barge_sends))
                        continue
                    event = json.loads(frame)
                    kind = event["type"]
                    if kind == "partial" and event["text"] and "first_partial" not in out:
                        out["first_partial"] = now
                    elif kind == "transcript":
                        out.setdefault("transcripts", []).append(event["text"])
                        out.setdefault("transcript", now)
                    elif kind == "caption":  # Text Aura spoke (billed per character).
                        out["tts_chars"] = out.get("tts_chars", 0) + len(event["text"])
                        out["tts_audio_s"] = out.get("tts_audio_s", 0) + event["seconds"]
                    elif kind == "metrics":
                        out.setdefault("metrics", []).append(event)
                    elif kind == "interrupt":
                        out["interrupt"] = now
                        await socket.send(json.dumps({"type": "interrupted",
                                                      "played_s": now - out["first_audio"]}))
                    elif kind in ("reply_end", "error"):
                        out.setdefault("ends", []).append(kind)
                        if (kind == "error" or interrupter is None or "interrupt" in out
                                or len(out["ends"]) == 2):
                            break
        except TimeoutError:
            out["timeout"] = True
        finally:
            for task in (sender, barge):
                if task is not None:
                    task.cancel()
    true_end = sent_at(sends, end)
    for key in ("first_partial", "transcript", "first_audio"):
        if key in out:
            out[f"{key}_ms"] = round((out.pop(key) - true_end) * 1000)
    if "interrupt" in out and barge_sends:
        start = int(np.nonzero(np.abs(interrupter) > SILENT)[0][0])
        out["barge_in_ms"] = round((out.pop("interrupt") - sent_at(barge_sends, start)) * 1000)
    return out


def percentiles(values: list[float]) -> str:
    if not values:
        return "n/a"
    p50, p90 = np.percentile(values, [50, 90])
    return f"p50 {p50:6.0f}  p90 {p90:6.0f}  (n={len(values)})"


async def main(args: argparse.Namespace) -> None:
    url = args.base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws/voice"
    clips = sorted(Path(args.clips).glob("*.wav"))
    results = []
    output = open(args.out, "w") if args.out else None
    for round_index in range(args.rounds):
        for index, clip in enumerate(clips):
            reference = clip.with_suffix(".txt").read_text().strip()
            interrupter = load(clips[(index + 1) % len(clips)]) if args.barge_in else None
            result = await turn(url, load(clip), interrupter)
            result.update(name=clip.stem, round=round_index, reference=reference)
            heard = (result.get("transcripts") or [""])[0]
            result["errors"], result["words"] = edit_distance(words(reference), words(heard)), len(words(reference))
            results.append(result)
            if output:
                output.write(json.dumps(result) + "\n")
                output.flush()
            metrics = (result.get("metrics") or [{}])[0]
            print(f"{clip.stem:16s} transcript {result.get('transcript_ms', '-'):>5} ms  "
                  f"audio {result.get('first_audio_ms', '-'):>5} ms  {metrics.get('speculative', '-'):9s} "
                  f"{heard!r}", flush=True)
            await asyncio.sleep(args.gap)
    summarize(results)


def summarize(results: list[dict]) -> None:
    ok = [r for r in results if "metrics" in r]
    first = [r["metrics"][0] for r in ok]
    print(f"\n{len(ok)} of {len(results)} turns answered")
    rows = {
        "client: transcript after true end": [r["transcript_ms"] for r in ok if "transcript_ms" in r],
        "client: first audio after true end": [r["first_audio_ms"] for r in ok if "first_audio_ms" in r],
        "server: endpoint (speech end -> EndOfTurn)": [m["endpoint_ms"] for m in first],
        "server: eager lead (committed)": [m["eager_lead_ms"] for m in first if m["eager_lead_ms"] is not None],
        "server: LLM first token from EndOfTurn": [m["llm_first_token_ms"] for m in first
                                                   if m["llm_first_token_ms"] is not None],
        "server: Aura first audio": [m["tts_first_audio_ms"] for m in first],
        "server: response (speech end -> audio sent)": [m["response_ms"] for m in first],
        "barge-in: speech start -> interrupt": [r["barge_in_ms"] for r in results if "barge_in_ms" in r],
    }
    for name, values in rows.items():
        print(f"{name:44s} {percentiles(values)}")
    outcomes = [m["speculative"] for m in first]
    requests = sum(m.get("speculations", 0) for m in first)
    print(f"speculative: {outcomes.count('committed')} committed, {outcomes.count('cancelled')} cancelled, "
          f"{outcomes.count('skipped')} skipped; {requests} speculative LLM requests")
    models = [m["llm_model"] for m in first]
    print("LLM:", {model: models.count(model) for model in set(models)})
    chars, seconds = sum(r.get("tts_chars", 0) for r in ok), sum(r.get("tts_audio_s", 0) for r in ok)
    print(f"Aura: {chars / max(1, len(ok)):.0f} characters and {seconds / max(1, len(ok)):.1f} s "
          f"of speech per reply ({chars / max(seconds, 1e-9):.1f} characters per second)")
    errors, total = sum(r["errors"] for r in results), sum(r["words"] for r in results)
    print(f"word error rate: {100 * errors / max(1, total):.1f}% ({errors} of {total} words)")
    for r in results:
        if r["errors"]:
            print(f"  {r['name']}: {r['reference']!r} -> {(r.get('transcripts') or [''])[0]!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base")
    parser.add_argument("clips")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--gap", type=float, default=2.0, help="Seconds between turns (Groq's 30/min limit).")
    parser.add_argument("--barge-in", action="store_true")
    parser.add_argument("--out")
    if len(sys.argv) < 3:
        parser.print_help()
        raise SystemExit(2)
    asyncio.run(main(parser.parse_args()))
