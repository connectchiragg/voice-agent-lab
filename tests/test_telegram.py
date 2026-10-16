"""VALR on Telegram: the webhook's secret token, owner-only updates, the /start code,
the heads-up's buttons (call now, in 10 min, skip), the commands, the heads-up message
and activity, and that no Bot API error ever carries the token."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import (calendar_sync, call_now, callbacks, interview_store, interviews, research,
                 telegram_api, telegram_bot)
from app.interview_types import SlotEvent
from app.interview_workflow import HeadsUpInput

TOKEN = "123456:TEST-token-never-printed"
SECRET = "s" * 48
OWNER = 4242
STRANGER = 9999
BLOCK = "briefing-20261016T1030Z"


class FakeBot(telegram_api.Bot):
    def __init__(self):
        super().__init__(TOKEN)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, payload):
        self.calls.append((method, payload))
        return {"message_id": 77} if method == "sendMessage" else True

    def texts(self):
        return [p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    def toasts(self):
        return [p["text"] for m, p in self.calls if m == "answerCallbackQuery"]


class Pool:
    def connection(self):
        class Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False
        return Context()


def make_event(start: datetime, key="ev1", title="Kestrel screening call", company="Kestrel") -> SlotEvent:
    return SlotEvent(key, title, start.isoformat(), (start + timedelta(minutes=45)).isoformat(),
                     company=company)


class World:
    """Everything the bot reads and writes, in memory."""

    def __init__(self, start: datetime):
        self.owner: int | None = OWNER
        self.events = [make_event(start)]
        self.record: dict | None = {"outcome": "sent", "outcome_at": None, "call_workflow": None}
        self.outcomes: list[tuple] = []
        self.flags: dict[str, bool] = {}
        self.started: list[dict] = []
        self.cancelled: list[str] = []
        self.todo: list[SlotEvent] = []
        self.codes: dict[str, int] = {}


@pytest.fixture
def world(monkeypatch):
    start = datetime.now(timezone.utc) + timedelta(minutes=30)
    w = World(start)
    bot = FakeBot()
    w.bot = bot

    async def none(*args, **kwargs):
        return None

    async def owner_chat(connection):
        return w.owner

    async def redeem(connection, chat_id, code, now=None):
        if w.codes.get(code) == 0:
            w.codes[code] = 1
            w.owner = chat_id
            return True
        return False

    async def heads_up(connection, block_id):
        return w.record

    async def set_outcome(connection, block_id, outcome, at=None, workflow=None):
        w.outcomes.append((block_id, outcome, at, workflow))

    async def set_flag(connection, name, enabled):
        w.flags[name] = enabled

    async def block(connection, block_id):
        return {"slot_keys": [e.key for e in w.events]} if block_id == BLOCK else None

    async def snapshots(connection, keys=None):
        return {e.key: e for e in w.events}

    async def upcoming(connection, start, end):
        return list(w.events)

    async def load_slots(connection, keys):
        return [{"key": "ev1", "status": "done"}]

    async def split_ready(events, refresh=False):
        return [], list(w.todo)

    async def temporal():
        class Client:
            def get_workflow_handle(self, workflow_id):
                class Handle:
                    async def signal(self, *args, **kwargs):
                        w.cancelled.append(workflow_id)
                return Handle()
        return Client()

    async def call_block_now(client, events, todo, owner_key, phone, label, job_usd=0.4,
                             dials_per_day=12, dial_at=None, **rpc):
        w.started.append({"events": [e.key for e in events], "todo": todo, "dial_at": dial_at,
                          "label": label})
        return f"briefing-now-{len(w.started)}"

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("VALR_OWNER_PHONE", "+15550100001")
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(telegram_bot, "bot_override", bot)
    monkeypatch.setattr(telegram_bot, "seen", telegram_bot.deque(maxlen=500))
    for module, name, value in (
            (telegram_api, "setup", none), (telegram_api, "owner_chat", owner_chat),
            (telegram_api, "redeem", redeem), (telegram_api, "heads_up", heads_up),
            (telegram_api, "set_outcome", set_outcome), (interview_store, "setup", none),
            (interview_store, "set_flag", set_flag), (interview_store, "block", block),
            (interview_store, "load_slots", load_slots), (calendar_sync, "setup", none),
            (calendar_sync, "snapshots", snapshots), (calendar_sync, "upcoming", upcoming),
            (interviews, "split_ready", split_ready), (callbacks, "temporal", temporal),
            (call_now, "call_block_now", call_block_now)):
        monkeypatch.setattr(module, name, value)
    from app.main import app
    w.client = TestClient(app)
    w.update_id = 0
    return w


def post(w: World, update: dict, secret: str | None = SECRET):
    w.update_id += 1
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    return w.client.post("/hooks/telegram", json={"update_id": w.update_id, **update}, headers=headers)


def message(chat_id: int, text: str) -> dict:
    return {"message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id}, "text": text}}


def button(w: World, data: str, sender: int = OWNER) -> dict:
    text = ("Kestrel screening call at 4:00 PM IST, in 30 min.\nYour briefing is ready.\n\n"
            "Kestrel builds payroll software.\n\nWant a call? No reply means no call.")
    return {"callback_query": {"id": "q1", "from": {"id": sender}, "data": data,
                               "message": {"message_id": 77, "chat": {"id": sender, "type": "private"},
                                           "text": text}}}


# The webhook's own check.

def test_webhook_needs_the_secret_token(world):
    assert post(world, message(OWNER, "/next"), secret=None).status_code == 403
    assert post(world, message(OWNER, "/next"), secret="wrong").status_code == 403
    assert world.bot.calls == []
    response = post(world, message(OWNER, "/next"))
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert len(world.bot.texts()) == 1


def test_webhook_is_closed_until_configured(world, monkeypatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET")
    assert post(world, message(OWNER, "/next"), secret="").status_code == 503


def test_a_repeated_update_is_handled_once(world):
    update = {"update_id": 5, **message(OWNER, "/pause")}
    for _ in range(2):
        world.client.post("/hooks/telegram", json=update,
                          headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert len(world.bot.texts()) == 1


# Owner only.

def test_strangers_get_one_polite_line_and_nothing_else(world):
    for text in ("/next", "/pause", "/start", "/start 000000", "hello"):
        post(world, message(STRANGER, text))
    assert world.bot.texts() == [telegram_bot.PRIVATE] * 5
    assert world.flags == {} and world.owner == OWNER


def test_a_strangers_button_tap_does_nothing(world):
    post(world, button(world, f"c:{BLOCK}", sender=STRANGER))
    assert world.bot.toasts() == [telegram_bot.PRIVATE] and world.started == []
    assert world.outcomes == []


def test_groups_are_ignored(world):
    post(world, {"message": {"chat": {"id": OWNER, "type": "group"}, "text": "/next"}})
    assert world.bot.calls == []


def test_start_code_links_the_chat_once(world):
    world.owner = None
    world.codes["123456"] = 0
    post(world, message(STRANGER, "/start 654321"))
    assert world.owner is None and world.bot.texts()[-1] == telegram_bot.PRIVATE
    post(world, message(OWNER, "/start 123456"))
    assert world.owner == OWNER and world.bot.texts()[-1].startswith("Connected.")
    post(world, message(STRANGER, "/start 123456"))  # Used up.
    assert world.owner == OWNER and world.bot.texts()[-1] == telegram_bot.PRIVATE


# Buttons.

def test_call_me_now_starts_the_call_and_shows_the_outcome(world):
    post(world, button(world, f"c:{BLOCK}"))
    assert len(world.started) == 1 and world.started[0]["dial_at"] is None
    assert world.started[0]["events"] == ["ev1"] and "Kestrel" in world.started[0]["label"]
    assert world.bot.toasts() == ["Calling you now."]
    edit = [p for m, p in world.bot.calls if m == "editMessageText"][0]
    assert "📞 Called at" in edit["text"] and "Want a call?" not in edit["text"]
    assert "Kestrel screening call" in edit["text"] and edit["reply_markup"] == {"inline_keyboard": []}
    assert world.outcomes[-1][1] == "called"


def test_call_me_now_researches_first_when_the_brief_is_not_ready(world):
    world.todo = list(world.events)
    post(world, button(world, f"c:{BLOCK}"))
    assert world.started[0]["todo"] and world.outcomes[-1][1] == "calling"
    assert "within 5 minutes" in world.bot.toasts()[0]


def test_in_ten_minutes_schedules_the_call_and_keeps_call_now_and_skip(world):
    before = datetime.now(timezone.utc)
    post(world, button(world, f"l:{BLOCK}"))
    dial_at = world.started[0]["dial_at"]
    assert timedelta(minutes=9, seconds=55) <= dial_at - before <= timedelta(minutes=10, seconds=5)
    edit = [p for m, p in world.bot.calls if m == "editMessageText"][0]
    assert "⏰ Calling you at" in edit["text"]
    keys = [b["callback_data"] for b in edit["reply_markup"]["inline_keyboard"][0]]
    assert keys == [f"c:{BLOCK}", f"s:{BLOCK}"]
    assert world.outcomes[-1][1] == "later" and world.outcomes[-1][3] == "briefing-now-1"


def test_in_ten_minutes_too_close_to_the_interview(world):
    world.events = [make_event(datetime.now(timezone.utc) + timedelta(minutes=12))]
    post(world, button(world, f"l:{BLOCK}"))
    assert world.started == [] and "Call me now" in world.bot.toasts()[0]
    assert not [m for m, _ in world.bot.calls if m == "editMessageText"]


def test_skip_places_no_call_and_cancels_a_pending_one(world):
    world.record = {"outcome": "later", "outcome_at": datetime.now(timezone.utc),
                    "call_workflow": "briefing-now-x"}
    post(world, button(world, f"s:{BLOCK}"))
    assert world.started == [] and world.cancelled == ["briefing-now-x"]
    assert world.outcomes[-1][1] == "skipped"
    assert "Skipped: no call." in [p for m, p in world.bot.calls if m == "editMessageText"][0]["text"]


def test_call_now_replaces_a_pending_in_ten_minutes(world):
    world.record = {"outcome": "later", "outcome_at": datetime.now(timezone.utc),
                    "call_workflow": "briefing-now-x"}
    post(world, button(world, f"c:{BLOCK}"))
    assert world.cancelled == ["briefing-now-x"] and world.started[0]["dial_at"] is None


def test_no_call_once_the_interview_has_started(world):
    world.events = [make_event(datetime.now(timezone.utc) - timedelta(minutes=5))]
    post(world, button(world, f"c:{BLOCK}"))
    assert world.started == [] and world.outcomes == []
    assert "has started" in world.bot.texts()[-1]


def test_a_forged_button_is_refused(world):
    for data in ("c:slot-x", "x:" + BLOCK, "c:" + BLOCK + "'; DROP", ""):
        post(world, button(world, data))
    assert world.started == [] and set(world.bot.toasts()) == {"That button has expired."}


# Commands.

def test_pause_and_resume_set_the_dashboard_switch(world):
    post(world, message(OWNER, "/pause"))
    assert world.flags == {interview_store.DIALS_PAUSED: True}
    post(world, message(OWNER, "/resume@valr_bot"))
    assert world.flags == {interview_store.DIALS_PAUSED: False}
    assert "Paused" in world.bot.texts()[0] and "Resumed" in world.bot.texts()[1]


def test_next_and_today_are_text(world):
    post(world, message(OWNER, "/next"))
    post(world, message(OWNER, "/today"))
    post(world, message(OWNER, "what?"))
    next_text, today_text, help_text = world.bot.texts()
    assert next_text.startswith("Next: <b>") and "Kestrel screening call" in next_text
    assert "briefing ready" in next_text
    assert today_text.startswith("Today:") or today_text == "No more interviews today."
    assert "/pause" in help_text


# The heads-up.

def test_heads_up_text_and_teaser():
    now = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
    events = [make_event(now + timedelta(minutes=30)),
              make_event(now + timedelta(minutes=90), "ev2", "Round 2 <tech>", "Bolt")]
    spoken = {"ev1": "Kestrel builds payroll software. You meet Riya, their CTO. She likes trade-offs.",
              "ev2": ""}
    text = telegram_api.heads_up_text(events, spoken, now)
    assert "<b>Kestrel screening call</b> at 4:00 PM IST, in 30 min." in text
    assert "<b>Bolt: Round 2 &lt;tech&gt;</b> at 5:00 PM IST." in text
    assert "Part of your briefing is ready." in text
    assert "<i>Kestrel builds payroll software. You meet Riya, their CTO.</i>" in text
    assert "She likes" not in text and text.endswith("No reply means no call.")
    assert len(telegram_api.teaser("word " * 200)) <= telegram_api.TEASER_CHARS
    keys = [b["callback_data"] for b in telegram_api.keyboard(BLOCK)[0]]
    assert keys == [f"c:{BLOCK}", f"l:{BLOCK}", f"s:{BLOCK}"]
    assert all(len(k.encode()) <= 64 for k in keys)


def test_auto_dial_is_off_unless_asked(monkeypatch):
    monkeypatch.delenv("VALR_AUTO_DIAL", raising=False)
    assert telegram_api.auto_dial() is False
    monkeypatch.setenv("VALR_AUTO_DIAL", "1")
    assert telegram_api.auto_dial() is True


def test_heads_up_activity_sends_and_records(monkeypatch):
    from app import interview_worker
    bot = FakeBot()
    recorded = []
    start = datetime.now(timezone.utc) + timedelta(minutes=30)

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def connect():
        return Connection()

    async def none(*args):
        return None

    async def owner_chat(connection):
        return OWNER

    async def snapshots(connection, keys=None):
        return {"ev1": make_event(start)}

    async def load_slots(connection, keys):
        return [{"key": "ev1", "status": "done", "spoken": "Kestrel builds payroll software."}]

    async def record(connection, block_id, message_id):
        recorded.append((block_id, message_id))

    monkeypatch.setattr(interview_worker, "connect", connect)
    monkeypatch.setattr(telegram_api, "bot", lambda: bot)
    for module, name, value in ((telegram_api, "setup", none), (interview_store, "setup", none),
                                (calendar_sync, "setup", none), (telegram_api, "owner_chat", owner_chat),
                                (calendar_sync, "snapshots", snapshots),
                                (interview_store, "load_slots", load_slots),
                                (telegram_api, "record_heads_up", record)):
        monkeypatch.setattr(module, name, value)
    activities = interview_worker.InterviewActivities(client=None, http=object())
    result = asyncio.run(activities.telegram_heads_up(HeadsUpInput(BLOCK, start.isoformat(), ["ev1"], "x")))
    assert result == "sent" and recorded == [(BLOCK, 77)]
    method, payload = bot.calls[0]
    assert method == "sendMessage" and payload["chat_id"] == OWNER
    assert "Kestrel builds payroll software." in payload["text"]

    async def no_owner(connection):
        return None
    monkeypatch.setattr(telegram_api, "owner_chat", no_owner)
    result = asyncio.run(activities.telegram_heads_up(HeadsUpInput(BLOCK, start.isoformat(), ["ev1"], "x")))
    assert "not linked" in result and len(bot.calls) == 1
    monkeypatch.setattr(telegram_api, "bot", lambda: None)
    result = asyncio.run(activities.telegram_heads_up(HeadsUpInput(BLOCK, start.isoformat(), ["ev1"], "x")))
    assert "not configured" in result


# Secrets.

def test_bot_api_errors_never_carry_the_token():
    def refuse(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    def unreachable(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    for handler in (refuse, unreachable):
        bot = telegram_api.Bot(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(telegram_api.TelegramError) as error:
            asyncio.run(bot.send(OWNER, "hi"))
        assert TOKEN not in str(error.value) and "TEST-token" not in repr(error.value)
        assert error.value.__cause__ is None  # No chained httpx error (its message has the URL).
        assert error.value.__context__ is None or error.value.__suppress_context__


def test_start_codes_are_one_time_and_limited(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    state = {"code_mac": None, "code_expires": None, "code_tries": 0, "chat_id": None}

    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        async def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO telegram_link"):
                state.update(code_mac=params[0], code_expires=params[1], code_tries=0)
            elif "code_tries = code_tries + 1" in sql:
                state["code_tries"] += 1
            elif sql.startswith("UPDATE telegram_link SET chat_id"):
                state.update(chat_id=params[0], code_mac=None, code_expires=None, code_tries=0)
            elif sql.startswith("SELECT code_mac"):
                return Cursor((state["code_mac"], state["code_expires"], state["code_tries"]))
            return Cursor(None)

    connection = Connection()
    now = datetime.now(timezone.utc)
    code = asyncio.run(telegram_api.new_code(connection, now))
    assert len(code) == 6 and code.isdigit() and state["code_mac"] != code  # Stored as an HMAC.
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    assert not asyncio.run(telegram_api.redeem(connection, STRANGER, wrong, now))
    assert not asyncio.run(telegram_api.redeem(connection, OWNER, code, now + timedelta(minutes=16)))
    assert asyncio.run(telegram_api.redeem(connection, OWNER, code, now)) and state["chat_id"] == OWNER
    assert not asyncio.run(telegram_api.redeem(connection, STRANGER, code, now))  # Used up.
    code = asyncio.run(telegram_api.new_code(connection, now))
    for _ in range(telegram_api.CODE_TRIES):
        asyncio.run(telegram_api.redeem(connection, STRANGER, "000000" if code != "000000" else "111111", now))
    assert not asyncio.run(telegram_api.redeem(connection, OWNER, code, now))  # Locked after 5 tries.
