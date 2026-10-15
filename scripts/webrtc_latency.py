"""End-of-speech to first reply audio heard, in headless Chrome, over WebRTC or the WebSocket.

    python scripts/webrtc_latency.py                       # Local stub pipeline, both transports.
    python scripts/webrtc_latency.py --loss 0.02           # Drop 2% of RTP packets each way.
    python scripts/webrtc_latency.py --url https://x.trycloudflare.com --clips CLIPS_DIR --turns 20
    python scripts/webrtc_latency.py --url ... --clips CLIPS_DIR --barge-in

--clips takes 16-bit mono WAVs (see scripts/live_latency.py for making them);
turns cycle through them on one call per transport. --barge-in speaks the
next clip over each reply, one second after its audio is first heard, and
times speech start -> the server's `interrupt` event on the page.

Chrome runs the real page (static/voice-webrtc.js or ?transport=ws) with a
synthetic microphone: the harness plays an utterance into a WebAudio stream
that getUserMedia returns, so it knows exactly when speech ended. Reply audio
is timed where the listener would hear it:

- WebRTC: the remote track (after the jitter buffer) goes through an
  AudioWorklet that timestamps the first non-silent block;
- WebSocket: when the page's player schedules the first audible sample.

Both skip near-silent samples (|x| <= 0.02): Aura's audio starts with some.

Without --url a local server runs here with a stub pipeline (energy VAD,
fixed transcript, instant LLM and a tone for TTS; no models loaded), so the
numbers isolate the transport: `transport_ms` is the total minus the
server's own response time from its metrics (end-of-turn wait included).
--loss drops RTP packets in the local server's aiortc both ways (WebRTC only).

Needs `pip install playwright` and Google Chrome (or `playwright install chromium`).
"""
import argparse
import asyncio
import json
import random
import socket
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TONE_S, TONE_HZ, TONE_LEVEL = 1.0, 220.0, 0.3
BROWSER_RATE = 48000

INIT_SCRIPT = r"""
(() => {
  const h = window.__harness = {onsets: [], events: [], scheduled: [], pcs: []};
  const ctx = h.ctx = new AudioContext({sampleRate: 48000});
  const mic = ctx.createMediaStreamDestination();
  // A real microphone never stops; a WebAudio stream with no sounding input
  // sends no RTP at all. Keep a -80 dB hum on it.
  const hum = ctx.createOscillator();
  const humLevel = ctx.createGain();
  humLevel.gain.value = 1e-4;
  hum.connect(humLevel).connect(mic);
  hum.start();
  // Render time, not output time: audio enters the microphone stream when it
  // is rendered, and device output latency is the same for both transports.
  const perfAt = contextTime => performance.now() + (contextTime - ctx.currentTime) * 1000;
  // getUserMedia returns the synthetic microphone, reporting echo cancellation on.
  const [micTrack] = mic.stream.getAudioTracks();
  const settings = micTrack.getSettings.bind(micTrack);
  micTrack.getSettings = () => ({...settings(), echoCancellation: true});
  navigator.mediaDevices.getUserMedia = async () => { await ctx.resume(); return mic.stream; };
  // Load each utterance once (samples at 48 kHz), then play them by index.
  h.utterances = [];
  h.load = samples => {
    const buffer = ctx.createBuffer(1, samples.length, 48000);
    buffer.copyToChannel(Float32Array.from(samples), 0);
    h.utterances.push(buffer);
  };
  // Returns the performance times speech starts and ends.
  h.speak = (index, voicedStartS, voicedEndS) => {
    const source = ctx.createBufferSource();
    source.buffer = h.utterances[index];
    source.connect(mic);
    const start = ctx.currentTime + 0.05;
    source.start(start);
    return [perfAt(start + voicedStartS), perfAt(start + voicedEndS)];
  };
  const detector = `
    class Onset extends AudioWorkletProcessor {
      constructor() { super(); this.loud = false; this.quiet = 0; }
      process(inputs) {
        const input = inputs[0]?.[0];
        if (!input) return true;
        let peak = 0;
        for (const x of input) peak = Math.max(peak, Math.abs(x));
        if (!this.loud && peak > 0.02) {
          this.loud = true;
          this.port.postMessage({onset: currentTime});
        } else if (this.loud) {
          this.quiet = peak > 0.02 ? 0 : this.quiet + input.length;
          if (this.quiet > sampleRate * 0.3) { this.loud = false; this.quiet = 0; }
        }
        return true;
      }
    }
    registerProcessor("onset", Onset);`;
  const workletReady = ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([detector], {type: "text/javascript"})));
  const record = (source, data) => {
    try {
      const message = JSON.parse(data);
      h.events.push({at: performance.now(), source, ...message});
    } catch {}
  };
  // WebRTC: tap the remote track and the data channel.
  const NativePC = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends NativePC {
    constructor(...args) {
      super(...args);
      h.pcs.push(this);
      this.addEventListener("track", async event => {
        await workletReady;
        const node = new AudioWorkletNode(ctx, "onset");
        node.port.onmessage = message => h.onsets.push(perfAt(message.data.onset));
        ctx.createMediaStreamSource(new MediaStream([event.track])).connect(node);
      });
    }
    createDataChannel(...args) {
      const channel = super.createDataChannel(...args);
      channel.addEventListener("message", event => record("dc", event.data));
      return channel;
    }
  };
  // WebSocket: events, and when the player schedules reply audio.
  const NativeWS = window.WebSocket;
  window.WebSocket = class extends NativeWS {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", event => {
        if (typeof event.data === "string") record("ws", event.data);
      });
    }
  };
  const start = AudioBufferSourceNode.prototype.start;
  // WebSocket: when the first audible sample of a reply buffer will play (TTS
  // audio starts with near-silence; WebRTC's detector skips it the same way).
  AudioBufferSourceNode.prototype.start = function (when = 0, ...rest) {
    if (this.context !== ctx && this.buffer) {
      const samples = this.buffer.getChannelData(0);
      const first = samples.findIndex(x => Math.abs(x) > 0.02);
      if (first >= 0) {
        const wait = Math.max(0, when - this.context.currentTime) + first / this.buffer.sampleRate;
        h.scheduled.push(performance.now() + wait * 1000);
      }
    }
    return start.call(this, when, ...rest);
  };
  h.stats = async () => {
    const pc = h.pcs.at(-1);
    if (!pc) return null;
    const out = {};
    (await pc.getStats()).forEach(report => {
      if (report.type === "inbound-rtp" && report.kind === "audio") {
        out.packetsLost = report.packetsLost;
        out.packetsReceived = report.packetsReceived;
        out.jitterBufferMs = report.jitterBufferEmittedCount ?
          1000 * report.jitterBufferDelay / report.jitterBufferEmittedCount : null;
        out.concealedSamples = report.concealedSamples;
      } else if (report.type === "remote-inbound-rtp" && report.kind === "audio") {
        out.uplinkPacketsLost = report.packetsLost;
        out.rttMs = report.roundTripTime ? 1000 * report.roundTripTime : null;
      } else if (report.type === "candidate-pair" && report.nominated && report.state === "succeeded") {
        out.pairRttMs = report.currentRoundTripTime ? 1000 * report.currentRoundTripTime : null;
      }
    });
    return out;
  };
})();
"""


Utterance = tuple[np.ndarray, float, float]  # 48 kHz samples, voiced start and end (s).


def tone() -> Utterance:
    times = np.arange(round(BROWSER_RATE * TONE_S)) / BROWSER_RATE
    return (TONE_LEVEL * np.sin(2 * np.pi * TONE_HZ * times)).astype(np.float32), 0.0, TONE_S


def load_wav(path: str | Path) -> Utterance:
    """A 16-bit mono WAV, resampled to 48 kHz; speech spans its first to last loud sample."""
    with wave.open(str(path)) as audio:
        if (audio.getnchannels(), audio.getsampwidth()) != (1, 2):
            raise SystemExit("Expected mono 16-bit PCM WAV.")
        rate = audio.getframerate()
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2") / 32768
    times = np.arange(round(len(pcm) * BROWSER_RATE / rate)) / BROWSER_RATE
    samples = np.interp(times, np.arange(len(pcm)) / rate, pcm).astype(np.float32)
    loud = np.flatnonzero(np.abs(samples) > 0.02)
    if not len(loud):
        raise SystemExit(f"{path}: no speech.")
    return samples, loud[0] / BROWSER_RATE, (loud[-1] + 1) / BROWSER_RATE


def stub_engines():
    """The real Call around instant stand-ins for Flux, the LLM and Aura: no Deepgram, no LLM.

    The turn ends 200 ms after the audio goes quiet, standing in for Flux's
    end-of-turn decision, so `transport_ms` (total - server response) is the
    transport alone.
    """
    from app.deepgram import TurnEvent
    from app.pipeline import Engines

    class EnergyListener:
        def __init__(self, on_event):
            self.on_event, self.speaking, self.quiet = on_event, False, 0

        async def start(self):
            pass

        async def send(self, pcm, arrived=None):
            loud = np.abs(np.frombuffer(pcm, "<i2")).max() > 1600
            arrived = arrived or time.perf_counter()
            if loud:
                self.quiet, self.last_loud = 0, arrived
                if not self.speaking:
                    self.speaking = True
                    await self.on_event(TurnEvent("StartOfTurn", "hello", arrived))
            elif self.speaking:
                self.quiet += len(pcm) // 2
                if self.quiet >= 3200:
                    self.speaking = False
                    await self.on_event(TurnEvent("EndOfTurn", "hello there", time.perf_counter(),
                                                  self.last_loud))

        async def close(self):
            pass

    class ToneSpeaker:
        async def start(self):
            pass

        async def synthesize(self, text):
            times = np.arange(48000) / 24000  # Two seconds of tone, long enough to talk over.
            yield (0.3 * np.sin(2 * np.pi * 330 * times)).astype(np.float32)

        async def close(self):
            pass

    async def reply(conversation_id, text, committed=None):
        yield "Sure, here you go."

    return Engines(new_listener=EnergyListener, new_speaker=ToneSpeaker, reply=reply)


def drop_rtp(loss: float) -> None:
    """Drop this share of RTP packets (not RTCP or the data channel) in both directions."""
    from aiortc import rtcdtlstransport
    from aiortc.rtp import is_rtcp

    transport = rtcdtlstransport.RTCDtlsTransport
    send, handle = transport._send_rtp, transport._handle_rtp_data

    async def lossy_send(self, data):
        if is_rtcp(data) or random.random() >= loss:
            await send(self, data)

    async def lossy_handle(self, data, arrival_time_ms):
        if is_rtcp(data) or random.random() >= loss:
            await handle(self, data, arrival_time_ms)

    transport._send_rtp, transport._handle_rtp_data = lossy_send, lossy_handle


def serve_stub(port: int, loss: float) -> None:
    """Child process: the app with the stub pipeline (its own event loop, like production)."""
    import uvicorn

    from app import main, webrtc

    if loss:
        drop_rtp(loss)
    main.app.state.engines = stub_engines()
    webrtc.peers_of(main.app)
    uvicorn.run(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")


async def start_stub(port: int, loss: float) -> asyncio.subprocess.Process:
    server = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "--serve-stub", str(port), "--loss", str(loss))
    for _ in range(200):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return server
        except OSError:
            await asyncio.sleep(0.05)
    server.kill()
    raise SystemExit("The stub server did not start.")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def speak(page, utterances: list[Utterance], index: int) -> tuple[float, float]:
    _, voiced_start, voiced_end = utterances[index % len(utterances)]
    return tuple(await page.evaluate("([i, a, b]) => window.__harness.speak(i, a, b)",
                                     [index % len(utterances), voiced_start, voiced_end]))


async def first_after(page, key: str, after: float, timeout_s: float = 30) -> float | None:
    try:
        found = await page.wait_for_function(
            f"(t) => window.__harness.{key}.find(x => x > t) ?? false", arg=after,
            timeout=timeout_s * 1000)
    except Exception:
        return None
    return await found.json_value()


async def run_transport(browser, base: str, transport: str, turns: int,
                        utterances: list[Utterance], barge_in: bool) -> list[dict]:
    page = await browser.new_page()
    await page.add_init_script(INIT_SCRIPT)
    query = "?transport=ws" if transport == "ws" else ""
    await page.goto(base + "/" + query, wait_until="networkidle")  # The client module is loaded.
    await page.click("#call")
    await page.wait_for_function(
        "window.__harness.events.some(e => e.type === 'ready')", timeout=20000)
    await asyncio.sleep(1.0)  # Let the jitter buffer and playout reports settle.
    for samples, _, _ in utterances:
        await page.evaluate("(s) => window.__harness.load(s)", samples.tolist())
    heard_key = "onsets" if transport == "webrtc" else "scheduled"
    results = []
    clip = 0
    for turn in range(turns):
        _, ended = await speak(page, utterances, clip)
        clip += 1
        heard_at = await first_after(page, heard_key, ended)
        if heard_at is None:
            print(f"{transport} turn {turn + 1}: no reply audio", file=sys.stderr)
            continue
        result: dict[str, float | None] = {"total_ms": heard_at - ended}
        if barge_in:
            await asyncio.sleep(1.0)  # Talk over the reply.
            started, ended = await speak(page, utterances, clip)
            clip += 1
            interrupted = await first_after(
                page, "events.filter(e => e.type === 'interrupt').map(e => e.at)", started, 10)
            result["barge_in_ms"] = interrupted - started if interrupted is not None else None
            # The interrupting speech is the next turn: let its reply play out below.
        try:
            await page.wait_for_function(
                "(t) => window.__harness.events.some(e => e.type === 'reply_end' && e.at > t)",
                arg=ended, timeout=30000)
        except Exception:
            print(f"{transport} turn {turn + 1}: no reply_end", file=sys.stderr)
        events = await page.evaluate("(t) => window.__harness.events.filter(e => e.at > t)", ended)
        metrics = next((e for e in events if e["type"] == "metrics"), {})
        transcript_at = next((e["at"] for e in events if e["type"] == "transcript"), None)
        if not barge_in:
            result["server_ms"] = metrics.get("response_ms")
            result["transport_ms"] = (result["total_ms"] - metrics["response_ms"]
                                      if metrics.get("response_ms") is not None else None)
            # Uplink + end-of-turn wait, then reply start -> heard (downlink + TTS).
            result["to_transcript_ms"] = transcript_at - ended if transcript_at else None
            result["after_transcript_ms"] = heard_at - transcript_at if transcript_at else None
        results.append(result)
        print(f"{transport} turn {turn + 1}: " + ", ".join(
            f"{key} {value:.0f}" for key, value in result.items() if value is not None),
            file=sys.stderr)
        # Let the reply finish playing: its captions say how long its audio is.
        audio_s = sum(e.get("seconds", 0) for e in events if e["type"] == "caption")
        now = await page.evaluate("performance.now()")
        reply_heard = (transcript_at or now) + audio_s * 1000
        await asyncio.sleep(max(0.0, (reply_heard - now) / 1000) + 1.0)
    stats = await page.evaluate("window.__harness.stats()")
    await page.click("#call")
    await page.close()
    if stats:
        print(f"{transport} stats: {json.dumps(stats)}", file=sys.stderr)
    return results


def summary(name: str, values: list[float]) -> str:
    if not values:
        return f"{name}: no data"
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return (f"{name}: median {statistics.median(values):.0f} ms, p90 {p90:.0f}, "
            f"min {ordered[0]:.0f}, max {ordered[-1]:.0f} (n={len(values)})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="A running server; default: a local stub server")
    parser.add_argument("--clips", help="Directory of mono 16-bit WAV utterances "
                                        "(default: a 1 s tone; real speech recognition needs words)")
    parser.add_argument("--barge-in", action="store_true", help="Speak over each reply")
    parser.add_argument("--transport", choices=["webrtc", "ws", "both"], default="both")
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument("--loss", type=float, default=0.0, help="Local server only: RTP loss share")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--serve-stub", type=int, metavar="PORT", help=argparse.SUPPRESS)
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    from playwright.async_api import async_playwright

    server = None
    base = (args.url or "").rstrip("/")
    if not args.url:
        port = free_port()
        server = await start_stub(port, args.loss)
        base = f"http://127.0.0.1:{port}"
    utterances = ([load_wav(path) for path in sorted(Path(args.clips).glob("*.wav"))]
                  if args.clips else [tone()])
    transports = ["webrtc", "ws"] if args.transport == "both" else [args.transport]
    try:
        async with async_playwright() as playwright:
            launch = dict(headless=not args.headed, args=[
                "--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
                "--mute-audio"])
            try:
                browser = await playwright.chromium.launch(channel="chrome", **launch)
            except Exception:
                browser = await playwright.chromium.launch(**launch)
            for transport in transports:
                results = await run_transport(browser, base, transport, args.turns, utterances,
                                              args.barge_in)
                print(summary(f"{transport} end of speech -> reply heard",
                              [r["total_ms"] for r in results]))
                if args.barge_in:
                    print(summary(f"{transport} barge-in: speech start -> interrupt on the page",
                                  [r["barge_in_ms"] for r in results if r.get("barge_in_ms") is not None]))
                    continue
                print(summary(f"{transport} server response (from its own end of speech)",
                              [r["server_ms"] for r in results if r["server_ms"] is not None]))
                if not args.url:
                    print(summary(f"{transport} transport only (total - server response)",
                                  [r["transport_ms"] for r in results if r["transport_ms"] is not None]))
                for key in ("to_transcript_ms", "after_transcript_ms"):
                    print(summary(f"{transport} {key}", [r[key] for r in results if r[key] is not None]))
            await browser.close()
    finally:
        if server is not None:
            server.terminate()
            await server.wait()


if __name__ == "__main__":
    arguments = parse_args()
    if arguments.serve_stub:
        serve_stub(arguments.serve_stub, arguments.loss)
    else:
        asyncio.run(main(arguments))
