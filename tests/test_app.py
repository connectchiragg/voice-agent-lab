from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


@pytest.fixture
def storage(monkeypatch):
    histories = {}

    async def load(cid):
        return [dict(turn) for turn in histories.setdefault(cid, [])]

    async def start(cid, message):
        histories[cid].extend([
            {"role": "user", "parts": [{"text": message}], "status": "completed"},
            {"role": "model", "parts": [{"text": ""}], "status": "interrupted"},
        ])
        return len(histories[cid]) - 1

    async def update(cid, mid, text, status="interrupted"):
        histories[cid][mid] = {"role": "model", "parts": [{"text": text}], "status": status}

    monkeypatch.setattr("app.main.load_history", load)
    monkeypatch.setattr("app.main.start_turn", start)
    monkeypatch.setattr("app.main.update_reply", update)
    return histories


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

    monkeypatch.setattr("app.main.stream_reply", stream)
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

    monkeypatch.setattr("app.main.stream_reply", stream)
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

    monkeypatch.setattr("app.main.stream_reply", stream)
    cid = str(uuid4())
    with client.websocket_connect(f"/ws/chat?conversation_id={cid}") as socket:
        socket.receive_json()
        socket.send_text("preserve this")
        assert socket.receive_json()["type"] == "start"
        assert socket.receive_json()["type"] == "interrupted"
    assert storage[cid][0]["parts"][0]["text"] == "preserve this"


def test_invalid_message(storage):
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("  ")
        assert "error" in socket.receive_json()
