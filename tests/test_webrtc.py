"""WebRTC transport: resampling, pacing, interrupts and signalling, with fake tracks and calls."""
import asyncio
import fractions
import json

import av
import httpx
import numpy as np
import pytest
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.rtp import RtpPacket
from fastapi.testclient import TestClient

from app import ice, main, valr_dashboard, webrtc
from app import auth
from app.auth import Identity, gate
from app.webrtc import FRAME_S, Downsampler, LowDelayAudioBuffer, Peer, SpeechTrack
from tests.test_pipeline import FakeListener, FakeSpeaker, make_engines

RATE = 24000
FRAME = RATE // 50


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    """The call surfaces need the signed-in owner (app/auth, tested in tests/test_auth.py);
    these tests are about the call itself."""
    async def owner(connection):
        return Identity("owner@example.com", "session")
    monkeypatch.setattr(gate, "identify", owner)
    monkeypatch.setattr(auth, "identify", owner)  # The offer's own owner check (valr_dashboard).


def csrf():
    """The page's CSRF token for the signed-in owner (every offer needs it)."""
    return {"X-CSRF-Token": valr_dashboard.csrf_token("owner@example.com")}


def opus_frames(tone_hz, seconds, amplitude=0.5, rate=48000):
    """What aiortc's Opus decoder yields: 20 ms of s16 stereo, both channels equal."""
    samples = round(rate * FRAME_S)
    signal = amplitude * np.sin(2 * np.pi * tone_hz * np.arange(round(rate * seconds)) / rate)
    pcm = (signal * 32767).astype(np.int16)
    for index in range(len(pcm) // samples):
        mono = pcm[index * samples:(index + 1) * samples]
        frame = av.AudioFrame.from_ndarray(np.repeat(mono, 2)[np.newaxis], format="s16", layout="stereo")
        frame.sample_rate, frame.pts = rate, index * samples
        yield frame


def dominant_hz(samples, rate=16000):
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return np.argmax(spectrum) * rate / len(samples)


def test_downsampler_keeps_speech_band_level_and_pitch():
    downsample = Downsampler()
    out = np.concatenate([downsample(frame) for frame in opus_frames(440, 1.0)])
    assert out.dtype == np.float32
    assert abs(len(out) - 16000) <= 32  # Resampler delay holds back a few samples.
    steady = out[800:]
    assert abs(np.abs(steady).max() - 0.5) < 0.02  # One channel, not a +3 dB downmix.
    assert abs(dominant_hz(steady) - 440) < 5


def test_downsampler_filters_above_the_new_nyquist():
    downsample = Downsampler()
    out = np.concatenate([downsample(frame) for frame in opus_frames(11000, 0.5)])
    assert np.abs(out[800:]).max() < 0.02  # Not folded back to 5 kHz.


def test_downsampler_state_spans_frames():
    whole, framed = Downsampler(), Downsampler()
    frames = list(opus_frames(1000, 0.2))
    joined = av.AudioFrame.from_ndarray(
        np.concatenate([frame.to_ndarray() for frame in frames], axis=1), format="s16", layout="stereo")
    joined.sample_rate, joined.pts = 48000, 0
    one = whole(joined)
    many = np.concatenate([framed(frame) for frame in frames])
    count = min(len(one), len(many))
    assert np.abs(one[:count] - many[:count]).max() < 1e-4  # No clicks at frame edges.


def rtp(sequence):
    packet = RtpPacket(sequence_number=sequence, timestamp=sequence * 960)
    packet._data = bytes([sequence % 256])
    return packet


def test_low_delay_buffer_passes_packets_on_at_once_and_skips_gaps():
    buffer = LowDelayAudioBuffer()
    out = [buffer.add(rtp(sequence))[1] for sequence in (65534, 65535, 1, 0, 1, 2)]
    # 0 was lost (it arrives after 1): no waiting for it, and it is dropped once late.
    assert [frame and frame.timestamp for frame in out] == [65534 * 960, 65535 * 960, 960, None, None, 1920]


def run(coroutine):
    return asyncio.run(coroutine)


def test_speech_track_paces_20_ms_frames_and_pads_with_silence():
    async def scenario():
        track = SpeechTrack(RATE)
        track.push(np.full(FRAME * 3, 0.5, dtype=np.float32))
        loop = asyncio.get_running_loop()
        started = loop.time()
        frames = [await track.recv() for _ in range(6)]
        return track, frames, loop.time() - started

    track, frames, elapsed = run(scenario())
    assert 0.09 <= elapsed < 0.16  # Frames 2..6 each wait their 20 ms slot.
    assert [frame.pts for frame in frames] == [index * FRAME for index in range(6)]
    assert all(frame.samples == FRAME and frame.sample_rate == RATE for frame in frames)
    levels = [np.abs(frame.to_ndarray()).max() for frame in frames]
    assert levels[:3] == [16383] * 3 and levels[3:] == [0] * 3
    assert track.sent == track.written == FRAME * 3 and track.queued == 0


def test_speech_track_resyncs_after_a_stall_instead_of_bursting():
    async def scenario():
        track = SpeechTrack(RATE)
        await track.recv()
        await asyncio.sleep(0.3)  # The event loop was blocked.
        loop = asyncio.get_running_loop()
        await track.recv()
        started = loop.time()
        await track.recv()
        return loop.time() - started

    assert run(scenario()) >= 0.015  # The next frame waits its slot again.


def test_speech_track_reports_playing_until_the_last_audio_is_heard():
    changes = []

    async def scenario():
        track = SpeechTrack(RATE, changes.append)
        track.playout_delay_s = 0.04
        track.push(np.full(FRAME, 0.5, dtype=np.float32))
        await track.recv()  # The audio frame.
        await track.recv()  # 20 ms later: still in the browser's buffer.
        assert changes == [True]
        for _ in range(3):
            await track.recv()
        track.stop()
        with pytest.raises(MediaStreamError):
            await track.recv()

    run(scenario())
    assert changes == [True, False]


class FakeChannel:
    label = "control"
    readyState = "open"

    def __init__(self):
        self.sent, self.handlers = [], {}

    def send(self, text):
        self.sent.append(json.loads(text))

    def on(self, event, handler):
        self.handlers[event] = handler

    def close(self):
        self.readyState = "closed"


class FakePC:
    connectionState = "connected"

    def __init__(self):
        self.handlers, self.closed = {}, False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def close(self):
        self.closed = True


class FakeCall:
    def __init__(self):
        self.packets, self.playing, self.played, self.closed = [], [], [], False

    async def hear(self, packet):
        self.packets.append(packet)

    def set_playing(self, active):
        self.playing.append(active)

    def report_played(self, seconds):
        self.played.append(seconds)

    async def finished(self):
        await asyncio.Event().wait()  # Never: only the assistant's goodbye ends a call.

    async def close(self):
        self.closed = True


def connected_peer():
    peer = Peer(FakePC(), RATE)
    peer.call = FakeCall()
    channel = FakeChannel()
    peer.channel_opened(channel)
    return peer, channel


def audio(seconds):
    return np.full(round(RATE * seconds), 0.25, dtype="<f4").tobytes()


def test_events_go_to_the_data_channel_and_audio_to_the_track():
    async def scenario():
        peer, channel = connected_peer()
        await peer.send({"type": "transcript", "text": "hi"})
        await peer.send(audio(0.5))
        await peer.send({"type": "reply_end"})
        return peer, channel

    peer, channel = run(scenario())
    assert channel.sent == [{"type": "ready"}, {"type": "transcript", "text": "hi"}, {"type": "reply_end"}]
    assert peer.speech.queued == RATE // 2
    assert peer.call.playing == [True]


def test_caption_after_its_audio_starts_where_that_audio_began():
    async def scenario():
        peer, channel = connected_peer()
        peer.speech.playout_delay_s = 0.1
        await peer.send({"type": "transcript", "text": "hi"})
        await peer.send(audio(0.25))
        await peer.send(audio(0.25))
        await peer.send({"type": "caption", "text": "Hi there.", "seconds": 0.5})
        await peer.send(audio(0.5))
        await peer.send({"type": "caption", "text": "All good!", "seconds": 0.5})
        return [event["starts_in_s"] for event in channel.sent[2:]]

    assert run(scenario()) == [0.1, 0.6]


def test_interrupt_flushes_the_queue_and_reports_what_was_heard():
    async def scenario():
        peer, channel = connected_peer()
        peer.speech.playout_delay_s = 0.06
        await peer.send(audio(0.2))  # The end of an earlier reply.
        for _ in range(10):
            await peer.speech.recv()
        await peer.send({"type": "transcript", "text": "tell me more"})
        await peer.send({"type": "caption", "text": "Sure.", "seconds": 2.0})
        await peer.send(audio(2.0))
        for _ in range(25):  # Half a second of this reply sent.
            await peer.speech.recv()
        await peer.send({"type": "interrupt"})
        after = [np.abs((await peer.speech.recv()).to_ndarray()).max() for _ in range(3)]
        return peer, channel, after

    peer, channel, after = run(scenario())
    assert after == [0, 0, 0]  # Nothing queued is sent after the interrupt.
    assert peer.speech.queued == 0 and peer.speech.written == peer.speech.sent
    assert channel.sent[-1] == {"type": "interrupt"}
    (played,) = peer.call.played
    # 0.5 s sent; the last 60 ms (+ the frame in progress) not yet heard.
    assert played == pytest.approx(0.5 - 0.06 - FRAME_S, abs=0.03)
    assert peer.call.playing[-1] is False


def test_playout_reports_set_the_delay_and_bad_messages_close_the_call():
    async def scenario():
        peer, channel = connected_peer()
        channel.handlers["message"](json.dumps({"type": "playout", "delay_s": 0.08}))
        assert peer.speech.playout_delay_s == 0.08
        channel.handlers["message"](json.dumps({"type": "playout", "delay_s": 60}))
        await asyncio.gather(*peer.tasks)
        return peer

    peer = run(scenario())
    assert peer.closed and peer.close_reason == "invalid control message"
    assert peer.call.closed and peer.pc.closed


class FakeMicrophone(MediaStreamTrack):
    kind = "audio"

    def __init__(self, frames):
        super().__init__()
        self.frames = list(frames)

    async def recv(self):
        if not self.frames:
            raise MediaStreamError
        await asyncio.sleep(0)
        return self.frames.pop(0)


def test_microphone_reaches_the_call_in_16k_float_packets_once_connected():
    async def scenario():
        peer = Peer(FakePC(), RATE)
        peer.call = FakeCall()
        early = FakeMicrophone(opus_frames(440, 0.2))
        await peer.listen(early)  # Before the data channel opens: dropped.
        assert peer.call.packets == []
        peer.channel_opened(FakeChannel())
        await peer.listen(FakeMicrophone(opus_frames(440, 0.4)))
        return peer.call.packets

    packets = run(scenario())
    samples = [np.frombuffer(packet, "<f4") for packet in packets]
    assert len(samples) >= 9 and all(len(packet) == 640 for packet in samples[1:-1])
    assert np.isfinite(np.concatenate(samples)).all()


def test_peer_close_stops_tasks_the_call_and_the_connection_once():
    closed = []

    async def scenario():
        peer = Peer(FakePC(), RATE, closed.append)
        peer.call = FakeCall()
        never = asyncio.Event()
        peer.spawn(never.wait())
        await peer.close("connection failed")
        await peer.close("again")
        return peer

    peer = run(scenario())
    assert closed == [peer] and peer.close_reason == "connection failed"
    assert peer.call.closed and peer.pc.closed and peer.speech.readyState == "ended"


def test_peer_that_never_connects_is_closed(monkeypatch):
    monkeypatch.setattr(webrtc, "CONNECT_TIMEOUT_S", 0.01)

    async def scenario():
        peer = Peer(FakePC(), RATE)
        peer.start()
        await asyncio.gather(*peer.tasks)
        return peer

    assert run(scenario()).close_reason == "never connected"


def test_call_time_limit_warns_then_closes(monkeypatch):
    monkeypatch.setattr(webrtc, "MAX_CALL_S", 0.01)

    async def scenario():
        peer, channel = connected_peer()
        peer.start()
        # close() cancels the peer's other tasks (the wait for a goodbye among them).
        await asyncio.gather(*peer.tasks, return_exceptions=True)
        return peer, channel

    peer, channel = run(scenario())
    assert channel.sent[-1]["type"] == "error" and peer.close_reason == "time limit"


def test_minted_ice_servers_use_the_turn_key_and_drop_port_53(monkeypatch):
    monkeypatch.setenv("CF_TURN_KEY_ID", "key-id")
    monkeypatch.setenv("CF_TURN_KEY_API_TOKEN", "secret")
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
            {"urls": ["turn:turn.cloudflare.com:3478?transport=udp",
                      "turn:turn.cloudflare.com:53?transport=udp",
                      "turns:turn.cloudflare.com:443?transport=tcp"],
             "username": "user", "credential": "pass"}]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ice.mint_ice_servers(960, client)

    servers = run(scenario())
    assert seen == {"url": "https://rtc.live.cloudflare.com/v1/turn/keys/key-id/credentials/"
                           "generate-ice-servers", "auth": "Bearer secret", "body": {"ttl": 960}}
    assert servers == [{"urls": ["stun:stun.cloudflare.com:3478"]},
                       {"urls": ["turn:turn.cloudflare.com:3478?transport=udp",
                                 "turns:turn.cloudflare.com:443?transport=tcp"],
                        "username": "user", "credential": "pass"}]


def test_without_a_turn_key_only_public_stun_is_offered(monkeypatch):
    monkeypatch.delenv("CF_TURN_KEY_ID", raising=False)
    response = TestClient(main.app).get("/webrtc/ice-servers")
    assert response.json() == {"ice_servers": [ice.CLOUDFLARE_STUN]}


def test_turn_failure_is_a_clear_503(monkeypatch):
    async def refuse(ttl_s):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(ice, "mint_ice_servers", refuse)
    response = TestClient(main.app).get("/webrtc/ice-servers")
    assert response.status_code == 503 and "relay" in response.json()["detail"]


class EnergyListener(FakeListener):
    """Flux stand-in: a turn starts on loud audio and ends after 200 ms of quiet."""
    speaking, quiet = False, 0

    async def send(self, pcm, arrived=None):
        await super().send(pcm, arrived)
        loud = np.abs(np.frombuffer(pcm, "<i2")).max() > 3000
        if loud:
            self.quiet = 0
            if not self.speaking:
                self.speaking = True
                await self.emit("StartOfTurn", "what's")
        elif self.speaking:
            self.quiet += len(pcm) // 2
            if self.quiet >= 3200:
                self.speaking = False
                await self.emit("EndOfTurn", "what's up", speech_ended=arrived)


class ToneSpeaker(FakeSpeaker):
    """Aura stand-in with a tone: Opus does not carry the fake's constant level."""
    async def synthesize(self, text):
        self.segments.append(text)
        times = np.arange(6000) / RATE
        yield (0.25 * np.sin(2 * np.pi * 300 * times)).astype(np.float32)


@pytest.fixture
def engines(monkeypatch):
    engines, seen = make_engines()
    engines.new_listener, engines.new_speaker = EnergyListener, ToneSpeaker
    monkeypatch.setattr(main.app.state, "engines", engines, raising=False)
    monkeypatch.setattr(main.app.state, "webrtc_peers", set(), raising=False)

    async def no_servers(ttl_s):
        return []  # Host candidates only: everything runs on localhost.

    monkeypatch.setattr(ice, "mint_ice_servers", no_servers)
    return seen


def test_offer_is_validated_and_calls_are_limited(engines, monkeypatch):
    client = TestClient(main.app)
    bad = client.post("/webrtc/offer", json={"type": "offer", "sdp": "nonsense", "echo_cancelled": True},
                      headers=csrf())
    assert bad.status_code == 400 and main.app.state.webrtc_peers == set()
    assert client.post("/webrtc/offer", json={"type": "answer", "sdp": "x", "echo_cancelled": True},
                       headers=csrf()).status_code == 422
    monkeypatch.setattr(webrtc, "MAX_CALLS", 0)
    full = client.post("/webrtc/offer", json={"type": "offer", "sdp": "x", "echo_cancelled": True},
                       headers=csrf())
    assert full.status_code == 503


def test_offer_without_models_is_refused(monkeypatch):
    monkeypatch.delattr(main.app.state, "engines", raising=False)
    response = TestClient(main.app).post("/webrtc/offer", json={"type": "offer", "sdp": "x",
                                                                "echo_cancelled": True}, headers=csrf())
    assert response.status_code == 503


class ToneTrack(MediaStreamTrack):
    """A browser microphone: 0.8 s of loud tone, then silence, in 20 ms Opus-ready frames."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.frames = 0
        self.started = None

    async def recv(self):
        loop = asyncio.get_running_loop()
        self.started = self.started or loop.time()
        due = self.started + self.frames * FRAME_S
        await asyncio.sleep(max(0, due - loop.time()))
        level = 0.3 if self.frames < 40 else 0.0  # Opus drops DC, so a tone, not a level.
        times = (self.frames * 960 + np.arange(960)) / 48000
        pcm = (level * np.sin(2 * np.pi * 440 * times) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm[np.newaxis], format="s16", layout="mono")
        frame.sample_rate, frame.pts = 48000, self.frames * 960
        frame.time_base = fractions.Fraction(1, 48000)
        self.frames += 1
        return frame


def test_a_real_peer_connection_hears_a_turn_and_receives_the_reply(engines):
    """End to end on localhost: aiortc as the browser, the real route, fake models."""
    async def scenario():
        browser = RTCPeerConnection()
        browser.addTrack(ToneTrack())
        channel = browser.createDataChannel("control")
        events, received = [], asyncio.Event()
        channel.on("message", lambda text: (events.append(json.loads(text)), received.set()))
        heard = []

        @browser.on("track")
        def on_track(track):
            async def drain():
                while True:
                    try:
                        frame = await track.recv()
                    except MediaStreamError:
                        return
                    heard.append(float(np.abs(frame.to_ndarray()).max()))
            asyncio.ensure_future(drain())

        await browser.setLocalDescription(await browser.createOffer())
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/webrtc/offer", json={
                "type": "offer", "sdp": browser.localDescription.sdp, "echo_cancelled": True}, headers=csrf())
        assert response.status_code == 200
        answer = response.json()
        await browser.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        async with asyncio.timeout(10):
            while not any(event["type"] == "reply_end" for event in events):
                received.clear()
                await received.wait()
            while max(heard[-10:], default=0) < 1000:  # The reply's audio arrives.
                await asyncio.sleep(0.02)
        (peer,) = main.app.state.webrtc_peers
        await browser.close()
        async with asyncio.timeout(5):
            while main.app.state.webrtc_peers:  # Closed and forgotten.
                await asyncio.sleep(0.05)
        return [event["type"] for event in events], peer

    types, peer = run(scenario())
    assert types[:3] == ["ready", "partial", "transcript"]
    assert "caption" in types and "metrics" in types and types[-1] == "reply_end"
    assert engines["replies"] == ["what's up"]
    assert peer.close_reason in ("data channel closed", "connection closed")


def test_refused_speech_service_closes_the_peer_with_a_clear_503(engines):
    class RefusedListener(FakeListener):
        async def start(self):
            raise ConnectionError("402")

    main.app.state.engines.new_listener = RefusedListener

    async def scenario():
        browser = RTCPeerConnection()
        browser.addTransceiver("audio")
        browser.createDataChannel("control")
        await browser.setLocalDescription(await browser.createOffer())
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/webrtc/offer", json={
                "type": "offer", "sdp": browser.localDescription.sdp, "echo_cancelled": True}, headers=csrf())
        await browser.close()
        return response

    response = run(scenario())
    assert response.status_code == 503 and "speech service" in response.json()["detail"]
    assert main.app.state.webrtc_peers == set()


def test_rate_limit_allows_a_burst_per_client_then_recovers():
    limit = webrtc.RateLimit(2, window_s=60)
    assert [limit.allow("a", t) for t in (0, 1, 2)] == [True, True, False]
    assert limit.allow("b", 2)  # Other clients are counted separately.
    assert limit.allow("a", 60.5)  # The first request has left the window.


def test_ice_servers_are_rate_limited_per_client(monkeypatch):
    monkeypatch.delenv("CF_TURN_KEY_ID", raising=False)
    monkeypatch.setattr(webrtc, "ice_limit", webrtc.RateLimit(2))
    client = TestClient(main.app)
    codes = [client.get("/webrtc/ice-servers").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
