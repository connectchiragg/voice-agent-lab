"""Smoke-test a running server over /ws/voice: speak one turn, expect a transcript and reply audio.

    python scripts/ws_smoke.py https://example.trycloudflare.com speech.wav

The WAV must be 16 kHz mono 16-bit PCM (on macOS:
`say -o s.aiff "What time is it?" && afconvert -f WAVE -d LEI16@16000 -c 1 s.aiff speech.wav`).
Without a WAV it sends silence, which only checks the `ready` handshake.
"""
import asyncio
import json
import sys
import time
import wave

import numpy as np
import websockets

RATE, PACKET = 16000, 1600  # 100 ms packets, as the browser sends.


def load_speech(path: str | None) -> np.ndarray:
    if path is None:
        return np.zeros(RATE, dtype=np.float32)
    with wave.open(path) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit("Expected 16 kHz mono 16-bit PCM WAV.")
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768


async def main(base: str, wav: str | None) -> None:
    url = base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws/voice"
    speech = load_speech(wav)
    # Trailing silence lets the turn detector end the turn.
    audio = np.concatenate([np.zeros(RATE // 2, np.float32), speech, np.zeros(3 * RATE, np.float32)])
    started = time.perf_counter()
    async with websockets.connect(url, max_size=None) as socket:
        await socket.send(json.dumps({"type": "start", "sample_rate": RATE,
                                      "encoding": "pcm_f32le", "echo_cancelled": False}))
        ready = json.loads(await socket.recv())
        print(f"{time.perf_counter() - started:6.2f}s ready: {ready}")
        speech_end = None

        async def stream() -> None:
            nonlocal speech_end
            for offset in range(0, len(audio), PACKET):
                await socket.send(audio[offset:offset + PACKET].astype("<f4").tobytes())
                if speech_end is None and offset >= RATE // 2 + len(speech):
                    speech_end = time.perf_counter()
                await asyncio.sleep(PACKET / RATE)

        sender = asyncio.create_task(stream())
        audio_bytes, first_audio = 0, None
        try:
            async with asyncio.timeout(60):
                async for frame in socket:
                    now = time.perf_counter()
                    since = f"+{now - speech_end:5.2f}s after speech" if speech_end else ""
                    if isinstance(frame, bytes):
                        if first_audio is None:
                            first_audio = now
                            print(f"{now - started:6.2f}s first reply audio {since}")
                        audio_bytes += len(frame)
                        continue
                    event = json.loads(frame)
                    if event["type"] != "partial":
                        print(f"{now - started:6.2f}s {event} {since}")
                    if event["type"] in ("reply_end", "error"):
                        break
        except TimeoutError:
            print("Timed out waiting for a reply.")
        finally:
            sender.cancel()
        print(f"reply audio: {audio_bytes / 4 / ready.get('sample_rate', 24000):.2f}s")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
