"""Pinned wire contract and authenticated proxy; CPU only, no remote calls."""

import asyncio
import base64
import json
import struct
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as real_connect
from websockets.asyncio.server import serve

from app import minicpm, minicpm_proxy

TOKEN = "synthetic-test-token-never-use-remotely"
PCM = struct.pack("<ff", 0.1, -0.1)


class SocketStub:
    def __init__(self, frames: list[dict | bytes | str]) -> None:
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(json.dumps(frame) if isinstance(frame, dict) else frame)
        self.sent: list[dict] = []
        self.closed = False

    async def recv(self) -> str | bytes:
        return await self.frames.get()

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def configure(monkeypatch, frames):
    socket = SocketStub(frames)

    @asynccontextmanager
    async def connect(url, **options):
        assert url == "wss://model.example/v1/realtime?mode=audio"
        assert options["additional_headers"] == {"Authorization": f"Bearer {TOKEN}"}
        assert "ssl" not in options  # Default certificate verification, no bypass.
        assert options["max_size"] == minicpm.MAX_FRAME_BYTES
        assert options["max_queue"] == 4
        try:
            yield socket
        finally:
            socket.closed = True

    monkeypatch.setattr(minicpm, "connect", connect)
    return socket


def settings():
    return minicpm.MiniCPMSettings("wss://model.example/v1/realtime?mode=audio", TOKEN)


@pytest.mark.parametrize("url", [
    "ws://model.example/v1/realtime?mode=audio",
    "ws://localhost/v1/realtime?mode=audio",
    "wss://user:secret@model.example/v1/realtime?mode=audio",
    "wss://model.example/v1/realtime?mode=video",
    "wss://model.example/v1/realtime?mode=audio&token=secret",
    "wss://model.example/v1/realtime?mode=audio#fragment",
    "wss://model.example:wrong/v1/realtime?mode=audio",
    "wss://[broken/v1/realtime?mode=audio",
])
def test_insecure_or_ambiguous_endpoints_rejected(url):
    with pytest.raises(minicpm.MiniCPMError):
        minicpm.MiniCPMSettings(url, TOKEN)


def test_credentials_fail_closed_and_do_not_appear_in_repr():
    with pytest.raises(minicpm.MiniCPMError):
        minicpm.MiniCPMSettings("wss://model.example/v1/realtime?mode=audio", "")
    assert TOKEN not in repr(settings())


def test_queue_initialize_audio_and_separate_output_branches(monkeypatch):
    socket = configure(monkeypatch, [
        {"type": "session.queued"}, {"type": "session.queue_update"},
        {"type": "session.queue_done"}, {"type": "session.created", "session_id": "one"},
        {"type": "response.output.delta", "kind": "text", "text": "Hello", "response_id": "r1"},
        {"type": "response.output.delta", "kind": "audio", "audio": base64.b64encode(PCM).decode(), "response_id": "r1"},
        {"type": "response.output.delta", "kind": "listen"},
        {"type": "session.closed"},
    ])

    async def exercise():
        async with minicpm.open_session(settings(), "Be helpful") as session:
            await session.send_audio(PCM)
            outputs = [event async for event in session.outputs()]
            assert [event.kind for event in outputs] == [
                minicpm.OutputKind.TEXT, minicpm.OutputKind.AUDIO,
                minicpm.OutputKind.LISTEN, minicpm.OutputKind.CLOSED,
            ]
            assert outputs[0].text == "Hello"
            assert outputs[1].audio == PCM
            assert outputs[1].response_id == "r1"
            with pytest.raises(minicpm.MiniCPMError):
                await session.send_audio(PCM)

    asyncio.run(exercise())
    assert socket.sent == [
        {"type": "session.init", "payload": {"system_prompt": "Be helpful"}},
        {"type": "input.append", "input": {"audio": base64.b64encode(PCM).decode(), "force_listen": False}},
    ]
    assert socket.closed


@pytest.mark.parametrize("frame", [
    b"binary-not-json", "{broken", [],
    {"type": "error", "error": {"message": TOKEN}},
    {"type": "response.output.delta", "kind": "audio", "audio": "!!!"},
    {"type": "response.output.delta", "kind": "audio", "audio": "AA=="},
    {"type": "response.output.delta", "kind": "text", "text": 1},
    {"type": "response.output.delta", "kind": "unknown"},
])
def test_malformed_outputs_fail_safely_and_close(monkeypatch, frame):
    if isinstance(frame, list):
        frame = json.dumps(frame)
    socket = configure(monkeypatch, [
        {"type": "session.queue_done"}, {"type": "session.created"}, frame,
    ])

    async def exercise():
        with pytest.raises(minicpm.MiniCPMError) as error:
            async with minicpm.open_session(settings(), "test") as session:
                await anext(session.outputs())
        assert TOKEN not in str(error.value)

    asyncio.run(exercise())
    assert socket.sent[-1]["type"] == "session.close"
    assert socket.closed


@pytest.mark.parametrize("pcm", [b"", b"bad", b"\0" * (minicpm.MAX_INPUT_BYTES + 4), struct.pack("<f", float("nan"))])
def test_invalid_input_is_not_sent(pcm):
    socket = SocketStub([])

    async def exercise():
        with pytest.raises(minicpm.MiniCPMError):
            await minicpm.MiniCPMSession(socket).send_audio(pcm)
        assert socket.sent == []

    asyncio.run(exercise())


def test_cancellation_closes_session_and_transport(monkeypatch):
    socket = configure(monkeypatch, [{"type": "session.queue_done"}, {"type": "session.created"}])

    async def exercise():
        ready = asyncio.Event()

        async def run():
            async with minicpm.open_session(settings(), "test") as session:
                ready.set()
                await anext(session.outputs())

        task = asyncio.create_task(run())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert socket.closed
    assert socket.sent[-1]["type"] == "session.close"


@pytest.mark.parametrize("token,headers,path", [
    (TOKEN, {}, "/v1/realtime?mode=audio"),
    (TOKEN, {"Authorization": "Bearer wrong"}, "/v1/realtime?mode=audio"),
    ("", {"Authorization": "Bearer "}, "/v1/realtime?mode=audio"),
    (TOKEN, {"Authorization": f"Bearer {TOKEN}", "Origin": "https://browser.example"}, "/v1/realtime?mode=audio"),
    (TOKEN, {"Authorization": f"Bearer {TOKEN}"}, "/v1/realtime?mode=video"),
])
def test_proxy_denies_without_contacting_official_server(monkeypatch, token, headers, path):
    monkeypatch.setenv("MINICPM_TOKEN", token)

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("Unauthorized call must not contact the model gateway")

    monkeypatch.setattr(minicpm_proxy, "connect", unexpected_connection)
    with TestClient(minicpm_proxy.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers=headers):
                pass
        assert client.get("/admin").status_code == 404
        assert client.get("/workers").status_code == 404


def test_proxy_rejects_nonprivate_upstream(monkeypatch):
    monkeypatch.setenv("MINICPM_TOKEN", TOKEN)
    monkeypatch.setenv("MINICPM_UPSTREAM_URL", "ws://public.example/v1/realtime?mode=audio")
    with TestClient(minicpm_proxy.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/realtime?mode=audio", headers={"Authorization": f"Bearer {TOKEN}"}):
                pass


def test_real_loopback_proxy_and_adapter_full_duplex(monkeypatch):
    """Real sockets, mock inference: output arrives while more input is still sent."""
    import socket
    import uvicorn

    monkeypatch.setenv("MINICPM_TOKEN", TOKEN)
    monkeypatch.setattr(minicpm, "connect", real_connect)

    async def exercise():
        received = []
        upstream_closed = asyncio.Event()

        async def official(ws):
            try:
                await ws.send(json.dumps({"type": "session.queue_done"}))
                received.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({"type": "session.created", "session_id": "synthetic"}))
                received.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({"type": "response.output.delta", "kind": "audio", "audio": base64.b64encode(PCM).decode()}))
                received.append(json.loads(await ws.recv()))
                received.append(json.loads(await ws.recv()))
                await ws.send(json.dumps({"type": "session.closed"}))
            finally:
                upstream_closed.set()

        async with serve(official, "127.0.0.1", 0) as upstream:
            port = upstream.sockets[0].getsockname()[1]
            monkeypatch.setenv("MINICPM_UPSTREAM_URL", f"ws://127.0.0.1:{port}/v1/realtime?mode=audio")
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            proxy_port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(minicpm_proxy.app, log_level="critical", lifespan="off", ws="websockets", ws_max_size=minicpm.MAX_FRAME_BYTES))
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        await asyncio.sleep(0.01)
                    cfg = minicpm.MiniCPMSettings(f"ws://127.0.0.1:{proxy_port}/v1/realtime?mode=audio", TOKEN)
                    async with minicpm.open_session(cfg, "test") as session:
                        # No second model session is allowed while the first owns the proxy.
                        from websockets.exceptions import InvalidStatus
                        with pytest.raises(InvalidStatus):
                            async with real_connect(cfg.url, additional_headers={"Authorization": f"Bearer {TOKEN}"}):
                                pass
                        await session.send_audio(PCM)
                        assert (await anext(session.outputs())).audio == PCM
                        await session.send_audio(PCM)  # Input after the first audio output.
                    await upstream_closed.wait()
                    assert [message["type"] for message in received] == [
                        "session.init", "input.append", "input.append", "session.close",
                    ]
            finally:
                server.should_exit = True
                await asyncio.wait_for(server_task, timeout=5)
                listener.close()

    asyncio.run(exercise())


def test_untrusted_tls_certificate_is_rejected(monkeypatch, tmp_path):
    """Default WSS verification is exercised against a real local TLS socket."""
    import ssl
    import subprocess

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
    ], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    contacted = False

    async def handler(ws):
        nonlocal contacted
        contacted = True

    monkeypatch.setattr(minicpm, "connect", real_connect)

    async def exercise():
        async with serve(handler, "127.0.0.1", 0, ssl=context) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = minicpm.MiniCPMSettings(f"wss://127.0.0.1:{port}/v1/realtime?mode=audio", TOKEN)
            with pytest.raises(minicpm.MiniCPMError, match="connection failed"):
                async with minicpm.open_session(cfg, "test"):
                    raise AssertionError("Untrusted TLS must not succeed")
        assert not contacted

    asyncio.run(exercise())
