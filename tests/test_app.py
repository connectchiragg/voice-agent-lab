from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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


def test_independent_messages(monkeypatch):
    received = []

    async def fake_reply(message):
        received.append(message)
        return "answer: " + message

    monkeypatch.setattr("app.main.generate_reply", fake_reply)
    with client.websocket_connect("/ws/chat") as socket:
        for message in ["hello", "another question"]:
            socket.send_text(message)
            assert socket.receive_json() == {"reply": "answer: " + message}
    assert received == ["hello", "another question"]


def test_invalid_message():
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("  ")
        assert "error" in socket.receive_json()
        socket.send_text("x" * 8001)
        assert "error" in socket.receive_json()


def test_provider_error_is_not_exposed(monkeypatch):
    async def fake_reply(message):
        raise ValueError("sensitive provider details")

    monkeypatch.setattr("app.main.generate_reply", fake_reply)
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("hello")
        response = socket.receive_json()
        assert "error" in response
        assert "sensitive" not in response["error"]


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with client.websocket_connect("/ws/chat") as socket:
        socket.send_text("hello")
        assert "GEMINI_API_KEY" in socket.receive_json()["error"]
