"""Flux and Aura adapters against fake sockets that speak Deepgram's documented protocol."""
import asyncio
import json
import time

import numpy as np
import pytest
from websockets.exceptions import ConnectionClosed

from app import deepgram
from app.deepgram import AuraSpeaker, DeepgramSettings, FluxListener, speakable

SETTINGS = DeepgramSettings(api_key="test-key", host="api.example.test")
DROP = object()  # Queued on a fake socket: the connection breaks.


class FakeSocket:
    def __init__(self, on_send=None):
        self.sent, self.closed, self.on_send = [], False, on_send
        self.incoming: asyncio.Queue = asyncio.Queue()

    def push(self, message):
        self.incoming.put_nowait(json.dumps(message) if isinstance(message, dict) else message)

    async def send(self, message):
        if self.closed:
            raise ConnectionClosed(None, None)
        self.sent.append(message)
        if self.on_send is not None:
            self.on_send(self, message)

    async def recv(self):
        return await self.__anext__()

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        if message is DROP:
            self.closed = True
            raise ConnectionClosed(None, None)
        return message

    def json_sent(self):
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


class Connector:
    """Hands out fake sockets in order; an Exception in the list fails that attempt."""
    def __init__(self, *sockets):
        self.sockets, self.urls, self.headers = list(sockets), [], []

    async def __call__(self, url, headers):
        self.urls.append(url)
        self.headers.append(headers)
        socket = self.sockets.pop(0)
        if isinstance(socket, Exception):
            raise socket
        return socket


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def turn_info(event, transcript="", end=None):
    words = [{"word": w, "confidence": 0.9, "start": 0.0, "end": end} for w in transcript.split()]
    return {"type": "TurnInfo", "request_id": "r", "sequence_id": 1, "event": event,
            "turn_index": 0, "audio_window_start": 0.0, "audio_window_end": 1.0,
            "transcript": transcript, "words": words, "end_of_turn_confidence": 0.8}


def test_urls_carry_the_documented_parameters_and_never_the_key():
    flux = SETTINGS.flux_url()
    assert flux.startswith("wss://api.example.test/v2/listen?model=flux-general-en&encoding=linear16"
                           "&sample_rate=16000")
    assert "eot_threshold=0.7" in flux and "eager_eot_threshold=0.4" in flux
    assert "eot_timeout_ms=3000" in flux
    aura = SETTINGS.aura_url()
    assert aura == ("wss://api.example.test/v1/speak?model=aura-2-odysseus-en&encoding=linear16"
                    "&sample_rate=24000")
    assert "test-key" not in flux + aura
    assert SETTINGS.headers == {"Authorization": "Token test-key"}
    no_eager = DeepgramSettings(api_key="k", eager_eot_threshold=None).flux_url()
    assert "eager" not in no_eager


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    monkeypatch.setenv("DEEPGRAM_HOST", "api.in.deepgram.com")
    monkeypatch.setenv("DEEPGRAM_VOICE", "aura-2-andromeda-en")
    monkeypatch.setenv("FLUX_EAGER_EOT_THRESHOLD", "")
    settings = DeepgramSettings.from_env()
    assert (settings.host, settings.voice, settings.eager_eot_threshold) == (
        "api.in.deepgram.com", "aura-2-andromeda-en", None)


def test_flux_streams_audio_and_maps_word_ends_to_packet_arrival():
    socket = FakeSocket()
    connect = Connector(socket)
    events = []

    async def on_event(event):
        events.append(event)

    async def scenario():
        flux = FluxListener(SETTINGS, on_event, connect)
        await flux.start()
        packet = bytes(3200)  # 100 ms of linear16 at 16 kHz.
        for index in range(5):
            await flux.send(packet, arrived=100.0 + index)
        socket.push({"type": "Connected", "request_id": "r", "sequence_id": 0})
        socket.push(turn_info("StartOfTurn", "Tell"))
        socket.push(turn_info("EndOfTurn", "Tell me a fact.", end=0.25))  # Third packet.
        socket.push({"type": "Warning", "code": "SLOW", "description": "x"})
        await settle()
        await flux.close()

    asyncio.run(scenario())
    assert connect.urls == [SETTINGS.flux_url()]
    assert socket.sent[:5] == [bytes(3200)] * 5
    assert socket.json_sent()[-1] == {"type": "CloseStream"}
    assert [(e.event, e.transcript) for e in events] == [("StartOfTurn", "Tell"),
                                                       ("EndOfTurn", "Tell me a fact.")]
    assert events[0].speech_ended is None  # No word timings: no speech end.
    assert events[1].speech_ended == 102.0
    assert events[1].received > 0


def test_speech_end_is_the_last_loud_packet_after_the_word_end():
    loud, quiet = (np.full(1600, 3000, "<i2").tobytes(), bytes(3200))

    async def on_event(event):
        pass

    async def scenario():
        flux = FluxListener(SETTINGS, on_event, Connector(FakeSocket()))
        await flux.start()
        # Flux says the word ended in packet 2, but the audio stays loud until packet 4;
        # packet 19 is loud too, but more than SPEECH_END_SEARCH_S later (a new sound).
        for index in range(20):
            await flux.send(loud if index <= 4 or index == 19 else quiet, arrived=float(index))
        ends = flux.speech_end_arrival(0.25), flux.speech_end_arrival(0.85), flux.speech_end_arrival(9.0)
        await flux.close()
        return ends

    assert asyncio.run(scenario()) == (4.0, 8.0, None)


def test_flux_reconnects_after_a_drop_and_resends_held_audio(monkeypatch):
    monkeypatch.setattr(deepgram, "RECONNECT_DELAYS_S", (0.0, 0.0))
    first, second = FakeSocket(), FakeSocket()
    connect = Connector(first, OSError("refused"), second)
    events = []

    async def on_event(event):
        events.append(event.event)
        if event.event == "Reset":
            # Audio that arrives while reconnecting is held, up to RECONNECT_BUFFER_S.
            for index in range(15):
                await flux.send(index.to_bytes(2, "little") * 1600)

    async def scenario():
        nonlocal flux
        flux = FluxListener(SETTINGS, on_event, connect)
        await flux.start()
        first.push(DROP)
        await settle()
        second.push(turn_info("StartOfTurn", "hello"))
        await settle()
        await flux.close()

    flux = None
    asyncio.run(scenario())
    assert events == ["Reset", "StartOfTurn"]
    assert len(connect.urls) == 3
    resent = [m for m in second.sent if isinstance(m, bytes)]
    # One second is 32000 bytes: the newest ten 100 ms packets survive.
    assert [m[:2] for m in resent] == [index.to_bytes(2, "little") for index in range(5, 15)]


def test_flux_send_fails_once_every_reconnect_failed(monkeypatch):
    monkeypatch.setattr(deepgram, "RECONNECT_DELAYS_S", (0.0, 0.0))
    socket = FakeSocket()
    connect = Connector(socket, OSError("down"), OSError("down"))

    async def on_event(event):
        pass

    async def scenario():
        flux = FluxListener(SETTINGS, on_event, connect)
        await flux.start()
        socket.push(DROP)
        await settle()
        with pytest.raises(ConnectionError):
            await flux.send(bytes(3200))
        await flux.close()

    asyncio.run(scenario())


def test_flux_sends_silence_when_the_browser_stops_sending(monkeypatch):
    monkeypatch.setattr(deepgram, "SILENCE_KEEPALIVE_S", 0.0)
    monkeypatch.setattr(deepgram, "KEEPALIVE_CHECK_S", 0.01)
    socket = FakeSocket()

    async def on_event(event):
        pass

    async def scenario():
        flux = FluxListener(SETTINGS, on_event, Connector(socket))
        await flux.start()
        await asyncio.sleep(0.05)
        await flux.close()

    asyncio.run(scenario())
    assert bytes(3200) in socket.sent


def aura_server(chunks=2, chunk=b"\x00\x40" * 960):
    """Answers each Flush with audio then Flushed, and Clear with Cleared."""
    def on_send(socket, message):
        kind = json.loads(message)["type"]
        if kind == "Flush":
            for _ in range(chunks):
                socket.push(chunk)
            socket.push({"type": "Flushed", "sequence_id": 0})
        elif kind == "Clear":
            socket.push({"type": "Cleared", "sequence_id": 0})
    return on_send


async def collect(speaker, text):
    return [audio async for audio in speaker.synthesize(text)]


def test_aura_streams_a_segment_until_flushed():
    socket = FakeSocket(aura_server())
    connect = Connector(socket)

    async def scenario():
        speaker = AuraSpeaker(SETTINGS, connect)
        await speaker.start()
        socket.push({"type": "Metadata", "request_id": "r", "model_name": "aura-2-odysseus-en"})
        audio = await collect(speaker, "**Hello** there.")
        second = await collect(speaker, "Again.")
        await speaker.close()
        return audio, second

    audio, second = asyncio.run(scenario())
    assert connect.urls == [SETTINGS.aura_url()]
    assert [len(a) for a in audio] == [960, 960] and len(second) == 2
    assert audio[0][0] == pytest.approx(0.5)  # 0x4000 / 32768.
    assert socket.json_sent()[:2] == [{"type": "Speak", "text": "Hello there."}, {"type": "Flush"}]


def test_abandoned_segment_is_cleared_before_the_next_one():
    socket = FakeSocket()
    flushes = []

    def on_send(sock, message):
        kind = json.loads(message)["type"]
        if kind == "Flush":
            flushes.append(1)
            if len(flushes) == 1:
                sock.push(b"\x01\x00" * 960)  # The rest of this segment is still coming.
            else:
                sock.push(b"\x02\x00" * 960)
                sock.push({"type": "Flushed", "sequence_id": 1})
        elif kind == "Clear":
            sock.push(b"\x01\x00" * 960)  # Stale audio generated before the Clear landed.
            sock.push({"type": "Cleared", "sequence_id": 0})

    socket.on_send = on_send

    async def scenario():
        speaker = AuraSpeaker(SETTINGS, Connector(socket))
        await speaker.start()
        stream = speaker.synthesize("A long first answer.")
        await anext(stream)
        await stream.aclose()  # Barge-in.
        audio = await collect(speaker, "Okay.")
        await speaker.close()
        return audio

    audio = asyncio.run(scenario())
    assert [m["type"] for m in socket.json_sent()] == ["Speak", "Flush", "Clear", "Speak", "Flush"]
    assert len(audio) == 1 and audio[0][0] == pytest.approx(2 / 32768)  # No stale audio.


def test_aura_reopens_after_the_socket_closes():
    first, second = FakeSocket(), FakeSocket(aura_server())
    connect = Connector(first, second)

    async def scenario():
        speaker = AuraSpeaker(SETTINGS, connect)
        await speaker.start()
        first.push(DROP)
        with pytest.raises(ConnectionError):
            await collect(speaker, "Hello.")
        audio = await collect(speaker, "Hello again.")
        await speaker.close()
        return audio

    assert len(asyncio.run(scenario())) == 2
    assert len(connect.urls) == 2


def test_aura_flushes_stay_within_the_documented_rate(monkeypatch):
    monkeypatch.setattr(deepgram, "FLUSH_LIMIT", 2)
    monkeypatch.setattr(deepgram, "FLUSH_WINDOW_S", 0.1)
    socket = FakeSocket(aura_server(chunks=1))

    async def scenario():
        speaker = AuraSpeaker(SETTINGS, Connector(socket))
        await speaker.start()
        started = time.monotonic()
        for text in ("One.", "Two.", "Three."):
            await collect(speaker, text)
        elapsed = time.monotonic() - started
        await speaker.close()
        return elapsed

    assert asyncio.run(scenario()) >= 0.09  # The third flush waited for the window.


def test_speakable_drops_markdown_and_extra_space():
    assert speakable("**Sure**,  here's `one`:\n# Tip") == "Sure, here's one: Tip"
    assert speakable(" * ") == ""
    assert np.frombuffer(deepgram.to_linear16(np.array([0.0, 1.0], np.float32)), "<i2").tolist() == [0, 32767]


def test_speakable_never_reads_web_addresses():
    assert speakable("Kestrel at kestrelapp.com raised a lot.") == "Kestrel at kestrelapp raised a lot."
    assert speakable("See https://x.io/a?b=1 for more.") == "See for more."
    assert speakable("Kestrel at kestrelapp dot com, yes.") == "Kestrel at kestrelapp, yes."


def test_speakable_writes_am_pm_the_way_aura_reads_them_well():
    assert speakable("At eleven thirty a.m. with Priya, and four p.m.") == (
        "At eleven thirty AM with Priya, and four PM.")
    assert speakable("It ends at 5 p.m. Then lunch.") == "It ends at 5 PM. Then lunch."


def test_speakable_respells_names_for_the_voice_only():
    from app import persona
    assert speakable("Your Brightline interview, with VALR.") == (
        f"Your Bright Line interview, with {persona.SPOKEN_NAME}.")
