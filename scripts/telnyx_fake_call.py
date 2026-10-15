"""A fake Telnyx for local phone-call tests: signed webhook, Call Control and the media stream.

    python scripts/telnyx_fake_call.py CLIP.wav [--env-file PATH] [--out reply.wav] [--barge-in]
                                       [--byte-order little|big] [--record]

Starts the app on --port with phone calls pointed at this script: a fresh
ed25519 key pair signs the webhook, and Call Control requests (answer, hangup)
come here instead of api.telnyx.com. Deepgram and Groq keys are read from
--env-file (default: this checkout's .env) and never printed.

Then, like Telnyx on an inbound call: POST a signed `call.initiated`, wait for
the `answer`, open its stream URL, send `connected` and `start`, let the
greeting play, and stream the clip (16 kHz mono 16-bit WAV) as 20 ms base64
L16 `media` frames at real time (little-endian, as Telnyx sends; --byte-order
sets both this line's and the server's TELNYX_L16_BYTE_ORDER), followed by silence, as a phone
line does. Reply audio is "played" at
real time: each mark is returned when the audio before it would have played,
and a `clear` returns the queued marks at once. Make a clip with:

    say -o q.aiff "What time is it in London?" && afconvert -f WAVE -d LEI16@16000 -c 1 q.aiff q.wav

Reported, on this client's clock: from the end of speech (the frame holding the
last non-silent sample) to the first reply `media` frame; with --barge-in, the
clip is spoken again 1 s into the reply and we time from that speech starting
to `clear`. The server logs its own per-stage `Turn latency`.
"""
import argparse
import asyncio
import base64
import json
import os
import socket as sockets
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import uvicorn
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from dotenv import dotenv_values
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.telnyx import L16, codec_named  # noqa: E402

RATE, FRAME = L16.sample_rate, L16.sample_rate // 50  # 20 ms frames, as Telnyx sends.
SILENT = 0.01
CALL_ID = "v3:fake-call-control-id"


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit(f"{path}: expected 16 kHz mono 16-bit PCM WAV.")
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768


def free_port() -> int:
    with sockets.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeCallControl:
    """Records Call Control commands, as api.telnyx.com/v2 would receive them."""

    def __init__(self) -> None:
        self.commands: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self.app = FastAPI()

        @self.app.post("/v2/calls/{call_control_id}/actions/{action}")
        async def command(call_control_id: str, action: str, request: Request) -> dict:
            await self.commands.put((action, await request.json()))
            return {"data": {"result": "ok"}}


class Line:
    """The phone side of the stream: sends caller audio, plays reply audio at real time."""

    def __init__(self, socket, codec=L16) -> None:
        self.socket = socket
        self.codec = codec
        self.reply: list[np.ndarray] = []
        self.first_audio: float | None = None
        self.cleared: float | None = None
        self.playhead = 0.0  # When the audio received so far will have played.
        self.timers: list[tuple[asyncio.TimerHandle, str]] = []  # Marks waiting to play.
        self.sequence = 1

    async def hear_greeting(self, timeout_s: float = 5.0) -> None:
        """The assistant speaks first: wait until its greeting has played, then forget it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self.first_audio is None and loop.time() < deadline:
            await asyncio.sleep(0.01)
        if self.first_audio is None:
            print("No greeting heard.")
            return
        while loop.time() < self.playhead + 0.3:  # Played, and no more is coming.
            await asyncio.sleep(0.05)
        greeting = sum(len(chunk) for chunk in self.reply) / RATE
        print(f"Greeting: {greeting:.1f} s of audio")
        self.reply, self.first_audio = [], None

    async def send(self, message: dict) -> None:
        self.sequence += 1
        await self.socket.send(json.dumps({**message, "sequence_number": str(self.sequence),
                                           "stream_id": "fake-stream"}))

    async def speak(self, audio: np.ndarray, sends: list[float]) -> None:
        started = time.perf_counter()
        for index, offset in enumerate(range(0, len(audio), FRAME)):
            payload = base64.b64encode(self.codec.encode(audio[offset:offset + FRAME])).decode()
            await self.send({"event": "media", "media": {"track": "inbound", "chunk": str(index + 1),
                                                         "timestamp": str(offset), "payload": payload}})
            sends.append(time.perf_counter())
            await asyncio.sleep(max(0.0, started + (index + 1) * FRAME / RATE - time.perf_counter()))

    def return_mark(self, name: str) -> None:
        asyncio.ensure_future(self.send_mark(name))

    async def send_mark(self, name: str) -> None:
        try:
            await self.send({"event": "mark", "mark": {"name": name}})
        except websockets.ConnectionClosed:
            pass  # The call ended before this audio would have played.

    async def listen(self) -> None:
        loop = asyncio.get_running_loop()
        async for raw in self.socket:
            message = json.loads(raw)
            now = loop.time()
            if message["event"] == "media":
                audio = self.codec.decode(base64.b64decode(message["media"]["payload"]))
                self.first_audio = self.first_audio or time.perf_counter()
                self.reply.append(audio)
                self.playhead = max(self.playhead, now) + len(audio) / RATE
            elif message["event"] == "mark":
                name = message["mark"]["name"]
                self.timers.append((loop.call_at(self.playhead, self.return_mark, name), name))
            elif message["event"] == "clear":
                self.cleared = self.cleared or time.perf_counter()
                for timer, name in self.timers:  # Queued marks come back at once.
                    if timer.when() > now:
                        timer.cancel()
                        self.return_mark(name)
                self.timers, self.playhead = [], now


def end_of_speech(audio: np.ndarray, sends: list[float], offset: int) -> float:
    last = offset + int(np.nonzero(np.abs(audio) > SILENT)[0][-1])
    return sends[last // FRAME] + (last % FRAME) / RATE


async def run(args: argparse.Namespace) -> None:
    speech = load(args.clip)
    key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    fake = FakeCallControl()
    fake_port = free_port()
    fake_server = uvicorn.Server(uvicorn.Config(fake.app, host="127.0.0.1", port=fake_port,
                                                log_level="warning"))
    fake_task = asyncio.create_task(fake_server.serve())

    env = {**os.environ, **{name: value for name, value in dotenv_values(args.env_file).items() if value},
           "TELNYX_API_KEY": "fake-local-key", "TELNYX_PUBLIC_KEY": public_key,
           "TELNYX_STREAM_URL": f"ws://127.0.0.1:{args.port}/ws/telnyx",
           "TELNYX_API_URL": f"http://127.0.0.1:{fake_port}/v2",
           "TELNYX_CODEC": "L16", "TELNYX_L16_BYTE_ORDER": args.byte_order,
           "TELNYX_RECORD": "1" if args.record else "0"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(args.port)],
                              cwd=ROOT, env=env)
    base = f"http://127.0.0.1:{args.port}"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    if (await client.get(f"{base}/healthz")).json().get("speech"):
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise SystemExit("The server did not start with speech configured.")
            body = json.dumps({"data": {"event_type": "call.initiated", "id": "fake-event-1",
                                        "record_type": "event", "payload": {
                                            "call_control_id": CALL_ID, "direction": "incoming",
                                            "state": "parked", "from": "+15550000000",
                                            "to": "+15550000001"}}}).encode()
            stamp = str(int(time.time()))
            signature = base64.b64encode(key.sign(f"{stamp}|".encode() + body)).decode()
            response = await client.post(f"{base}/telnyx/webhook", content=body, headers={
                "telnyx-signature-ed25519": signature, "telnyx-timestamp": stamp,
                "content-type": "application/json"})
            response.raise_for_status()
        action, answer = await asyncio.wait_for(fake.commands.get(), 5)
        assert action == "answer", action
        print(f"Answered: {', '.join(f'{k}={v}' for k, v in answer.items() if k != 'stream_url')}")
        await call(answer["stream_url"], speech, args)
    finally:
        server.terminate()
        server.wait(10)
        fake_server.should_exit = True
        await fake_task
    commands = []
    while not fake.commands.empty():
        commands.append(fake.commands.get_nowait()[0])
    print(f"Call Control commands after the call: {commands or 'none'}")


async def call(stream_url: str, speech: np.ndarray, args: argparse.Namespace) -> None:
    lead, tail = RATE // 2, np.zeros(RATE * args.listen_s, np.float32)
    async with websockets.connect(stream_url) as socket:
        line = Line(socket, codec_named("L16", args.byte_order))
        await line.send({"event": "connected", "version": "1.0.0"})
        await line.send({"event": "start", "start": {
            "call_control_id": CALL_ID, "call_session_id": "fake-session", "user_id": "fake-user",
            "media_format": {"encoding": "L16", "sample_rate": RATE, "channels": 1}}})
        listener = asyncio.create_task(line.listen())
        await line.hear_greeting()
        sends: list[float] = []
        audio = np.concatenate([np.zeros(lead, np.float32), speech])
        speaking = asyncio.create_task(line.speak(np.concatenate([audio, tail]), sends))
        while line.first_audio is None and not speaking.done():
            await asyncio.sleep(0.01)
        if line.first_audio is None:
            raise SystemExit("No reply audio.")
        ended = end_of_speech(audio, sends, 0)
        print(f"End of speech -> first reply audio: {(line.first_audio - ended) * 1000:.0f} ms")
        if args.barge_in:
            await asyncio.sleep(1.0)
            speaking.cancel()
            interrupt_sends: list[float] = []
            speaking = asyncio.create_task(line.speak(np.concatenate([speech, tail]), interrupt_sends))
            while line.cleared is None and not speaking.done():
                await asyncio.sleep(0.01)
            voiced = int(np.nonzero(np.abs(speech) > SILENT)[0][0])
            if line.cleared is None:
                print("Barge-in: no clear received.")
            else:
                started = interrupt_sends[voiced // FRAME] + (voiced % FRAME) / RATE
                print(f"Speech start -> clear: {(line.cleared - started) * 1000:.0f} ms")
        await speaking
        await line.send({"event": "stop", "stop": {"call_control_id": CALL_ID, "user_id": "fake-user"}})
        listener.cancel()
        for timer, _ in line.timers:
            timer.cancel()
    reply = np.concatenate(line.reply) if line.reply else np.zeros(0, np.float32)
    print(f"Reply audio received: {len(reply) / RATE:.1f} s")
    if args.out:
        with wave.open(str(args.out), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes((reply * 32768).clip(-32768, 32767).astype("<i2").tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clip", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--listen-s", type=int, default=8, help="Silence streamed after speech.")
    parser.add_argument("--out", type=Path, help="Write the reply audio here (never commit it).")
    parser.add_argument("--barge-in", action="store_true")
    parser.add_argument("--byte-order", choices=("little", "big"), default="little")
    parser.add_argument("--record", action="store_true", help="Expect a record_start command.")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
