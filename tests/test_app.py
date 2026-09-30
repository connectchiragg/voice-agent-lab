from uuid import uuid4
import pytest
import asyncio
from langgraph.checkpoint.memory import InMemorySaver
from app import database
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setattr(database, "graph", database.build_graph(InMemorySaver()))


def test_http_routes():
    assert client.get("/health").json() == {"status": "ok"}
    assert "Voice Agent Lab" in client.get("/").text
    assert client.get("/static/chat.js").status_code == 200


@pytest.mark.parametrize("fail", [False, True])
def test_stream_persistence_and_reconnect(monkeypatch, storage, fail):
    async def stream(message, history):
        yield "Hello"
        if fail:
            raise ValueError("private provider error")
        yield " there"

    monkeypatch.setattr("app.database.stream_reply", stream)
    cid = str(uuid4())
    url = f"/ws/chat?conversation_id={cid}"
    with client.websocket_connect(url) as socket:
        assert socket.receive_json() == {"history": []}
        socket.send_text("hi")
        assert socket.receive_json()["type"] == "start"
        assert socket.receive_json() == {"type": "delta", "text": "Hello"}
        if not fail:
            assert socket.receive_json()["text"] == " there"
        event = socket.receive_json()
        assert event["type"] == ("interrupted" if fail else "done")
        assert "private" not in str(event)
    with client.websocket_connect(url) as socket:
        history = socket.receive_json()["history"]
        assert history[0]["parts"][0]["text"] == "hi"
        assert history[1]["parts"][0]["text"] == ("Hello" if fail else "Hello there")
        assert history[1]["status"] == ("interrupted" if fail else "completed")
    with client.websocket_connect(f"/ws/chat?conversation_id={uuid4()}") as socket:
        assert socket.receive_json() == {"history": []}


def test_continue_after_interruption(monkeypatch, storage):
    async def stream(message, history):
        if message == "first":
            yield "Partial"
            raise TimeoutError()
        assert history[-1]["status"] == "interrupted"
        assert history[-1]["parts"][0]["text"] == "Partial"
        yield "Continued"

    monkeypatch.setattr("app.database.stream_reply", stream)
    with client.websocket_connect("/ws/chat") as socket:
        for message, expected in [("first", "interrupted"), ("continue", "done")]:
            socket.send_text(message)
            assert socket.receive_json()["type"] == "start"
            assert socket.receive_json()["type"] == "delta"
            assert socket.receive_json()["type"] == expected


def test_failure_before_first_chunk(monkeypatch, storage):
    async def stream(message, history):
        raise ValueError("failure")
        yield "unreachable"

    monkeypatch.setattr("app.database.stream_reply", stream)
    cid = str(uuid4())
    with client.websocket_connect(f"/ws/chat?conversation_id={cid}") as socket:
        socket.receive_json()
        socket.send_text("preserve this")
        assert socket.receive_json()["type"] == "start"
        assert socket.receive_json()["type"] == "interrupted"
    assert asyncio.run(database.load_history(cid))[0]["parts"][0]["text"] == "preserve this"


def test_invalid_message(storage):
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("  ")
        assert "error" in socket.receive_json()
