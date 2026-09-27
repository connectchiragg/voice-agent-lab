from fastapi.testclient import TestClient
from uuid import uuid4

from app.main import app

client = TestClient(app)


def test_history_survives_reconnect_and_new_id_starts_empty(monkeypatch):
    async def fake_reply(message, history):
        return "answer"

    monkeypatch.setattr("app.main.generate_reply", fake_reply)
    url = f"/ws/chat?conversation_id={uuid4()}"
    with client.websocket_connect(url) as socket:
        assert socket.receive_json() == {"history": []}
        socket.send_text("remember me")
        assert socket.receive_json() == {"reply": "answer"}
    with client.websocket_connect(url) as socket:
        assert socket.receive_json()["history"] == [
            {"role": "user", "parts": [{"text": "remember me"}]},
            {"role": "model", "parts": [{"text": "answer"}]},
        ]
    with client.websocket_connect(f"/ws/chat?conversation_id={uuid4()}") as socket:
        assert socket.receive_json() == {"history": []}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home():
    response = client.get("/")
    assert response.status_code == 200
    assert "Voice Agent Lab" in response.text


def test_browser_script():
    assert client.get("/static/chat.js").status_code == 200


def test_conversation_history_and_connection_isolation(monkeypatch):
    received = []

    async def fake_reply(message, history):
        received.append((message, list(history)))
        return "answer: " + message

    monkeypatch.setattr("app.main.generate_reply", fake_reply)
    with client.websocket_connect("/ws/chat") as socket:
        for message in ["hello", "another question"]:
            socket.send_text(message)
            assert socket.receive_json() == {"reply": "answer: " + message}
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("fresh conversation")
        socket.receive_json()
    assert received == [
        ("hello", []),
        ("another question", [
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"text": "answer: hello"}]},
        ]),
        ("fresh conversation", []),
    ]


def test_invalid_message():
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("  ")
        assert "error" in socket.receive_json()
        socket.send_text("x" * 8001)
        assert "error" in socket.receive_json()


def test_provider_error_is_not_exposed(monkeypatch):
    async def fake_reply(message, history):
        assert history == []
        if message == "hello":
            raise ValueError("sensitive provider details")
        return "recovered"

    monkeypatch.setattr("app.main.generate_reply", fake_reply)
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("hello")
        response = socket.receive_json()
        assert "error" in response
        assert "sensitive" not in response["error"]
        socket.send_text("retry")
        assert socket.receive_json() == {"reply": "recovered"}


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("hello")
        assert "GEMINI_API_KEY" in socket.receive_json()["error"]
