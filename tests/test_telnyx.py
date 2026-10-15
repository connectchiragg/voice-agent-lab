"""Telnyx phone transport: codec, webhook signatures, Call Control and the media stream.

Fixtures follow the message shapes in Telnyx's media streaming and webhook
documentation (checked 2026-10-15); numbers in stream messages are strings there.
"""
import asyncio
import base64
import json
import time

import httpx
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import g711, main, telnyx
from app.deepgram import DeepgramSettings
from app.telnyx import (CallControl, PhoneSession, Playout, Switchboard, TelnyxSettings,
                        signature_valid)
from tests.test_app import TalkingListener
from tests.test_pipeline import make_engines

PRIVATE_KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY_B64 = base64.b64encode(PRIVATE_KEY.public_key().public_bytes(
    Encoding.Raw, PublicFormat.Raw)).decode()
CALL_ID = "v3:call-control-id-1"
STREAM_URL = "wss://voice.example.test/ws/telnyx"


# Codec.

def test_mulaw_matches_the_g711_reference():
    # CPython's audioop (the ITU reference quantiser) on these 16-bit values.
    linear = np.array([-32768, -20000, -1000, -100, -1, 0, 1, 100, 1000, 20000, 32767])
    reference = [0, 12, 78, 114, 126, 255, 255, 242, 206, 140, 128]
    assert list(g711.encode(linear.astype(np.float32) / 32768)) == reference
    assert g711.DECODE[[0x00, 0x7F, 0x80, 0xFF]].tolist() == [-32124, 0, 32124, 0]


def test_mulaw_round_trip_is_close_and_decoding_is_exact():
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(800) / 8000)).astype(np.float32)
    decoded = g711.decode(g711.encode(tone))
    assert decoded.dtype == np.float32 and len(decoded) == 800
    assert np.abs(decoded - tone).max() < 0.02  # µ-law step near 0.5 full scale is ~1/64.
    codes = bytes(range(256))
    assert g711.encode(g711.decode(codes)) == bytes(  # Both zero codes decode to 0 -> 0xFF.
        0xFF if code == 0x7F else code for code in codes)


# Webhook signatures.

def signed(body: bytes, timestamp: int | None = None) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = PRIVATE_KEY.sign(f"{timestamp}|".encode() + body)
    return {"telnyx-signature-ed25519": base64.b64encode(signature).decode(),
            "telnyx-timestamp": str(timestamp), "content-type": "application/json"}


def test_signature_checks_key_body_and_age():
    key = telnyx.load_public_key(PUBLIC_KEY_B64)
    body = b'{"data":{}}'
    headers = signed(body, 1_800_000_000)
    signature, timestamp = headers["telnyx-signature-ed25519"], headers["telnyx-timestamp"]
    assert signature_valid(key, body, signature, timestamp, now=1_800_000_100)
    assert not signature_valid(key, body + b" ", signature, timestamp, now=1_800_000_100)
    assert not signature_valid(key, body, signature, "1800000001", now=1_800_000_100)
    assert not signature_valid(key, body, signature, timestamp, now=1_800_000_301)  # Stale.
    assert not signature_valid(key, body, None, timestamp)
    assert not signature_valid(key, body, "not base64!", timestamp)
    other = Ed25519PrivateKey.generate().public_key()
    assert not signature_valid(other, body, signature, timestamp, now=1_800_000_100)


# Webhook route and Call Control.

def call_initiated(event_id="evt-1", direction="incoming"):
    return {"data": {"event_type": "call.initiated", "id": event_id, "record_type": "event",
                     "occurred_at": "2026-10-15T10:00:00Z",
                     "payload": {"call_control_id": CALL_ID, "direction": direction,
                                 "state": "parked", "from": "+15550001111", "to": "+12288888007"}},
            "meta": {"attempt": 1}}


@pytest.fixture
def board(monkeypatch):
    """A switchboard whose Call Control requests are recorded instead of sent."""
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content),
                         request.headers["authorization"]))
        return httpx.Response(200, json={"data": {"result": "ok"}})

    settings = TelnyxSettings(api_key="test-key", public_key=telnyx.load_public_key(PUBLIC_KEY_B64),
                              stream_url=STREAM_URL, max_call_s=600, max_calls=1)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    switchboard = Switchboard(settings, CallControl(client, settings.api_key))
    switchboard.requests = requests
    monkeypatch.setattr(main.app.state, "switchboard", switchboard, raising=False)
    monkeypatch.setattr(telnyx, "CONNECT_TIMEOUT_S", 0.3)
    return switchboard


def webhook_client(board):
    """The Telnyx routes alone, with a running event loop for the answer task."""
    app = FastAPI()
    app.include_router(telnyx.router)
    app.state.switchboard = board
    return TestClient(app)


def post_event(client, event, headers=None):
    body = json.dumps(event).encode()
    return client.post("/telnyx/webhook", content=body,
                       headers=signed(body) if headers is None else headers)


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        time.sleep(0.01)


def test_unsigned_or_forged_webhooks_are_refused(board):
    client = TestClient(main.app)
    assert post_event(client, call_initiated(), headers={}).status_code == 401
    body = json.dumps(call_initiated()).encode()
    forged = signed(json.dumps(call_initiated("evt-2")).encode())
    assert client.post("/telnyx/webhook", content=body, headers=forged).status_code == 401
    assert board.requests == []


def test_webhook_is_refused_when_phone_calls_are_not_configured(monkeypatch):
    monkeypatch.setattr(main.app.state, "switchboard", None, raising=False)
    assert post_event(TestClient(main.app), call_initiated()).status_code == 503


def test_incoming_call_is_answered_with_a_bidirectional_pcmu_stream(board):
    with webhook_client(board) as client:
        assert post_event(client, call_initiated()).json() == {"ok": True}
        assert post_event(client, call_initiated()).status_code == 200  # A retry: ignored.
        wait_for(lambda: len(board.requests) >= 1)
        path, body, auth = board.requests[0]
        assert path == f"/v2/calls/{CALL_ID}/actions/answer" and auth == "Bearer test-key"
        token = body.pop("stream_url").removeprefix(STREAM_URL + "?token=")
        assert len(token) >= 32 and token in board.pending
        assert body == {"stream_track": "inbound_track", "stream_codec": "PCMU",
                        "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU",
                        "command_id": "evt-1"}
        # The stream never connects: the call is hung up and the token expires.
        wait_for(lambda: len(board.requests) == 2)
        assert board.requests[1][0] == f"/v2/calls/{CALL_ID}/actions/hangup"
        assert board.claim(token) is None


def test_calls_beyond_the_limit_are_rejected_and_outbound_events_ignored(board):
    with webhook_client(board) as client:
        board.live.add("v3:another-call")  # max_calls is 1.
        post_event(client, call_initiated("evt-3"))
        post_event(client, call_initiated("evt-4", direction="outgoing"))
        wait_for(lambda: len(board.requests) >= 1)
        time.sleep(0.05)
        assert [(path, body) for path, body, _ in board.requests] == [
            (f"/v2/calls/{CALL_ID}/actions/reject", {"cause": "USER_BUSY", "command_id": "evt-3"})]


def test_settings_need_key_public_key_and_stream_url(monkeypatch):
    for name in ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "TELNYX_STREAM_URL"):
        monkeypatch.delenv(name, raising=False)
    assert TelnyxSettings.from_env() is None
    monkeypatch.setenv("TELNYX_API_KEY", "k")
    monkeypatch.setenv("TELNYX_PUBLIC_KEY", PUBLIC_KEY_B64)
    monkeypatch.setenv("TELNYX_STREAM_URL", STREAM_URL)
    monkeypatch.setenv("TELNYX_MAX_CALL_S", "300")
    settings = TelnyxSettings.from_env()
    assert settings is not None and settings.max_call_s == 300 and settings.max_calls == 2


def test_phone_engines_run_deepgram_at_8_khz_both_ways(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    engines = main.load_engines(telnyx.SAMPLE_RATE)
    assert engines is not None and engines.output_sample_rate == 8000
    settings = DeepgramSettings(api_key="k", input_sample_rate=8000, output_sample_rate=8000)
    assert "sample_rate=8000" in settings.flux_url() and "sample_rate=8000" in settings.aura_url()
    assert "encoding=linear16" in settings.flux_url()


# Playout: pacing, marks and clear.

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_playout_paces_by_marks_and_clear_reports_what_was_heard():
    playout = Playout(8000)
    playout.start_reply()
    playout.push(np.full(16000, 0.1, np.float32))  # 2 s of reply.
    sent = []
    while (message := playout.next_chunk(0.0)) is not None:
        sent.append(message)
    assert len(sent) == 10 and all(len(payload) == 800 for payload, _ in sent)  # LEAD_S of 100 ms chunks.
    assert sent[0][1] == "0:1:800" and playout.playing(0.0)
    playout.marked(sent[2][1], now=0.35)  # 300 ms has played by 0.35 s.
    assert playout.heard_by(0.35) == 2400 and playout.heard_by(0.4) == 2800
    assert playout.next_chunk(0.35) is not None  # Room again under the lead.
    played_s = playout.clear(0.4)
    assert played_s == pytest.approx(0.35)
    assert not playout.playing(0.4) and playout.next_chunk(0.4) is None
    playout.marked(sent[9][1], now=0.45)  # Returned by the clear: ignored.
    assert playout.heard_by(0.45) == 2800


def test_playout_without_marks_follows_the_clock_and_restarts_after_silence():
    playout = Playout(8000)
    playout.push(np.zeros(800, np.float32))
    assert playout.next_chunk(10.0) is not None
    assert playout.playing(10.05) and not playout.playing(10.1)
    playout.push(np.zeros(800, np.float32))  # Next reply, long after.
    assert playout.next_chunk(50.0) is not None
    assert playout.heard_by(50.0) == 800 and playout.playing(50.05)


class RecordingCall:
    def __init__(self):
        self.played, self.playing, self.heard = [], [], []

    def report_played(self, seconds):
        self.played.append(seconds)

    def set_playing(self, active):
        self.playing.append(active)

    async def hear(self, packet):
        self.heard.append(np.frombuffer(packet, "<f4"))


def test_session_batches_80_ms_for_flux_and_turns_interrupt_into_clear():
    async def scenario():
        clock, out = Clock(), []

        async def to_telnyx(message):
            out.append(message)

        session = PhoneSession(to_telnyx, clock=clock)
        session.call = RecordingCall()
        frame = base64.b64encode(g711.encode(np.full(160, 0.1, np.float32))).decode()
        for chunk in range(1, 6):
            await session.handle({"event": "media", "sequence_number": str(chunk + 2),
                                  "stream_id": "s-1", "media": {"track": "inbound", "chunk": str(chunk),
                                                                "timestamp": str(chunk * 20),
                                                                "payload": frame}})
        assert [len(packet) for packet in session.call.heard] == [640]  # 4 x 20 ms; one pending.
        assert session.call.heard[0][0] == pytest.approx(0.1, abs=0.005)

        await session.send({"type": "transcript", "text": "hi"})
        await session.send(np.full(4000, 0.2, np.float32).astype("<f4").tobytes())
        pump = asyncio.create_task(session.pump())
        await asyncio.sleep(0.01)
        media = [m for m in out if m["event"] == "media"]
        marks = [m for m in out if m["event"] == "mark"]
        assert len(media) == 5 and len(marks) == 5  # All 0.5 s fits under the lead.
        assert base64.b64decode(media[0]["media"]["payload"]) == g711.encode(np.full(800, 0.2, np.float32))
        assert session.call.playing == [True]
        await session.handle({"event": "mark", "sequence_number": "9", "stream_id": "s-1",
                              "mark": {"name": marks[1]["mark"]["name"]}})
        clock.now += 0.05
        await session.send({"type": "interrupt"})
        assert out[-1] == {"event": "clear"}
        assert session.call.played == [pytest.approx(0.25)]
        assert session.call.playing == [True, False]
        for ignored in ({"type": "caption", "text": "x", "seconds": 1.0}, {"type": "reply_end"}):
            await session.send(ignored)
        assert await session.handle({"event": "dtmf", "dtmf": {"digit": "5"}, "stream_id": "s-1"})
        assert await session.handle({"event": "error", "payload": {"code": 100005,
                                                                   "title": "rate_limit_reached"}})
        assert not await session.handle({"event": "stop", "stop": {"call_control_id": CALL_ID}})
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    asyncio.run(scenario())


def test_oversized_or_invalid_media_is_refused():
    async def scenario():
        async def to_telnyx(message):
            pass

        session = PhoneSession(to_telnyx)
        session.call = RecordingCall()
        with pytest.raises(ValueError):
            await session.hear("A" * (telnyx.MAX_PAYLOAD_CHARS + 4))
        with pytest.raises(ValueError):
            await session.hear("not base64!")
    asyncio.run(scenario())


# The media stream WebSocket, end to end with fake Deepgram stages.

def start_message(call_control_id=CALL_ID, encoding="PCMU", rate=8000):
    return {"event": "start", "sequence_number": "1", "stream_id": "stream-1",
            "start": {"user_id": "u-1", "call_control_id": call_control_id,
                      "call_session_id": "session-1", "from": "+15550001111", "to": "+12288888007",
                      "client_state": None, "tags": [], "custom_parameters": {},
                      "media_format": {"encoding": encoding, "sample_rate": rate, "channels": 1}}}


def media_frames(count, value=0.1):
    payload = base64.b64encode(g711.encode(np.full(160, value, np.float32))).decode()
    return [{"event": "media", "sequence_number": str(index + 2), "stream_id": "stream-1",
             "media": {"track": "inbound", "chunk": str(index + 1), "timestamp": str(index * 160),
                       "payload": payload}} for index in range(count)]


@pytest.fixture
def phone_engines(monkeypatch):
    engines, seen = make_engines()
    engines.new_listener = TalkingListener  # A turn after its third packet.
    engines.output_sample_rate = 8000
    monkeypatch.setattr(main.app.state, "phone_engines", engines, raising=False)
    return seen


def test_stream_needs_a_valid_one_time_token(board, phone_engines):
    client = TestClient(main.app)
    for url in ("/ws/telnyx", "/ws/telnyx?token=guess"):
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(url):
                pass
        assert error.value.code == 1008


def test_phone_call_hears_a_turn_and_replies_in_mulaw_with_marks(board, phone_engines):
    board.pending["tok"] = (CALL_ID, time.monotonic() + 5)
    with TestClient(main.app).websocket_connect("/ws/telnyx?token=tok") as socket:
        socket.send_json({"event": "connected", "version": "1.0.0"})
        socket.send_json(start_message())
        for frame in media_frames(12):  # 3 Flux packets of 80 ms.
            socket.send_json(frame)
        audio, marks = b"", []
        while len(marks) < 15:  # Two fake Aura segments of 6000 samples, in 800-sample chunks.
            message = socket.receive_json()
            if message["event"] == "media":
                audio += base64.b64decode(message["media"]["payload"])
            elif message["event"] == "mark":
                marks.append(message["mark"]["name"])
                socket.send_json({"event": "mark", "sequence_number": "99", "stream_id": "stream-1",
                                  "mark": {"name": marks[-1]}})
        assert audio == g711.encode(np.full(12000, 0.25, np.float32))
        assert len(marks) == 15
        socket.send_json({"event": "stop", "sequence_number": "100", "stream_id": "stream-1",
                          "stop": {"user_id": "u-1", "call_control_id": CALL_ID}})
    assert phone_engines["replies"] == ["what's up"]
    assert CALL_ID not in board.live
    time.sleep(0.05)
    assert board.requests == []  # Telnyx ended the call: no hangup from us.


def test_stream_for_another_call_or_codec_is_refused_and_hung_up(board, phone_engines):
    client = TestClient(main.app)
    for token, start in (("a", start_message("v3:someone-else")),
                         ("b", start_message(encoding="L16", rate=16000))):
        board.pending[token] = (CALL_ID, time.monotonic() + 5)
        with client.websocket_connect(f"/ws/telnyx?token={token}") as socket:
            socket.send_json(start)
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 1008
    wait_for(lambda: len(board.requests) == 2)
    assert {path for path, _, _ in board.requests} == {f"/v2/calls/{CALL_ID}/actions/hangup"}


def test_time_limit_says_goodbye_then_hangs_up(board, phone_engines, monkeypatch):
    board.settings = TelnyxSettings(api_key="test-key", public_key=board.settings.public_key,
                                    stream_url=STREAM_URL, max_call_s=0, max_calls=1)
    board.pending["tok"] = (CALL_ID, time.monotonic() + 5)
    with TestClient(main.app).websocket_connect("/ws/telnyx?token=tok") as socket:
        socket.send_json(start_message())
        audio = b""
        with pytest.raises(WebSocketDisconnect):  # We close the stream after the goodbye.
            while True:
                message = socket.receive_json()
                if message["event"] == "media":
                    audio += base64.b64decode(message["media"]["payload"])
                elif message["event"] == "mark":
                    socket.send_json({"event": "mark", "mark": {"name": message["mark"]["name"]}})
        assert len(audio) == 6000
    assert phone_engines["speakers"][0].segments == [telnyx.GOODBYE]
    assert phone_engines["replies"] == []  # Nothing heard after the limit is answered.
    wait_for(lambda: len(board.requests) == 1)
    assert board.requests[0][0] == f"/v2/calls/{CALL_ID}/actions/hangup"
