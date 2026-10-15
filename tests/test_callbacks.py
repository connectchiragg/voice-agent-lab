"""Callback tools (with a fake Temporal client), natural times, webhooks and the greeting."""
import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app import callback_worker, callbacks, memory, tools
from app.callback_worker import DialSettings, client_state, dial_body
from app.callback_workflow import (CallbackStatus, CallbackWorkflow, CallResult, PlaceCallInput,
                                   in_quiet_hours, validate_due, workflow_id)
from app.callbacks import CallbackCall, parse_when
from tests.test_telnyx import CALL_ID, STREAM_URL, board, post_event, wait_for, webhook_client  # noqa: F401

ZONE = ZoneInfo("Asia/Kolkata")
CALLER = "tel:+15550002222"
# Wednesday 15 October 2026, 2:00 PM in Kolkata.
NOW = datetime(2026, 10, 15, 14, 0, tzinfo=ZONE)


# Natural times.

@pytest.mark.parametrize("phrase, expected", [
    ("tomorrow at 5 pm", datetime(2026, 10, 16, 17, 0)),
    ("5pm tomorrow", datetime(2026, 10, 16, 17, 0)),
    ("at 6:30 p.m.", datetime(2026, 10, 15, 18, 30)),
    ("today at 4 PM", datetime(2026, 10, 15, 16, 0)),
    ("10 am", datetime(2026, 10, 16, 10, 0)),  # Already past today: tomorrow.
    ("Friday at 10:30 am", datetime(2026, 10, 16, 10, 30)),
    ("wednesday 11am", datetime(2026, 10, 21, 11, 0)),  # Today is Wednesday: next week.
    ("monday at noon", datetime(2026, 10, 19, 12, 0)),
    ("tomorrow 17:15", datetime(2026, 10, 16, 17, 15)),
    ("in 2 hours", datetime(2026, 10, 15, 16, 0)),
    ("in an hour", datetime(2026, 10, 15, 15, 0)),
    ("in 45 minutes", datetime(2026, 10, 15, 14, 45)),
    ("2026-10-17T11:00", datetime(2026, 10, 17, 11, 0)),
])
def test_natural_times_are_read_in_the_callers_zone(phrase, expected):
    assert parse_when(phrase, NOW) == expected.replace(tzinfo=ZONE)


@pytest.mark.parametrize("phrase", ["sometime soon", "next month at 5 pm", "13 pm", "25:00", ""])
def test_unclear_times_are_not_guessed(phrase):
    assert parse_when(phrase, NOW) is None


def test_quiet_hours_are_9_pm_to_9_am_local():
    def at(hour, minute=0):
        return datetime(2026, 10, 15, hour, minute, tzinfo=ZONE)
    assert [in_quiet_hours(at(hour), "Asia/Kolkata") for hour in (8, 9, 20, 21, 23)] == [
        True, False, False, True, True]
    assert not in_quiet_hours(at(20, 59), "Asia/Kolkata")
    # 10 am in Kolkata is 9:30 pm the evening before in San Francisco.
    assert in_quiet_hours(at(10), "America/Los_Angeles")
    with pytest.raises(ValueError, match="passed"):
        validate_due(at(13), at(14), "Asia/Kolkata")


# A fake Temporal client: one workflow per id, behaving as CallbackWorkflow does.

@dataclass
class FakeWorkflow:
    request: object
    status: CallbackStatus
    running: bool = True


class FakeHandle:
    def __init__(self, temporal, workflow_id):
        self.temporal, self.id = temporal, workflow_id

    @property
    def workflow(self):
        workflow = self.temporal.workflows.get(self.id)
        if workflow is None:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
        return workflow

    async def describe(self, **_):
        status = (WorkflowExecutionStatus.RUNNING if self.workflow.running
                  else WorkflowExecutionStatus.COMPLETED)
        return SimpleNamespace(status=status)

    async def query(self, query, **_):
        assert query == CallbackWorkflow.status_query
        return self.workflow.status

    async def result(self, **_):
        return self.workflow.status

    async def execute_update(self, update, due, **_):
        assert update == CallbackWorkflow.reschedule
        try:
            validate_due(datetime.fromisoformat(due), datetime.now(timezone.utc),
                         self.workflow.status.timezone)
        except ValueError as error:
            raise WorkflowUpdateFailedError(ApplicationError(str(error))) from error
        self.workflow.status.due_at_utc = due
        return self.workflow.status

    async def signal(self, signal, *args, **_):
        self.temporal.signals.append((self.id, signal.__name__, args))
        if signal == CallbackWorkflow.cancel:
            self.workflow.running = False
            self.workflow.status.state = "cancelled"


class FakeTemporal:
    def __init__(self):
        self.workflows: dict[str, FakeWorkflow] = {}
        self.signals = []

    async def start_workflow(self, run, request, *, id, task_queue, **_):
        assert run == CallbackWorkflow.run and task_queue == "voice-callbacks"
        if id in self.workflows and self.workflows[id].running:
            raise WorkflowAlreadyStartedError(id, "CallbackWorkflow")
        self.workflows[id] = FakeWorkflow(request, CallbackStatus(
            "scheduled", request.due_at_utc, request.reason, request.timezone))
        return FakeHandle(self, id)

    def get_workflow_handle(self, workflow_id, **_):
        return FakeHandle(self, workflow_id)


@pytest.fixture
def temporal(monkeypatch):
    fake = FakeTemporal()

    async def connected():
        return fake
    monkeypatch.setattr(callbacks, "temporal", connected)
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")
    return fake


def run(name, arguments, caller_key=CALLER):
    return json.loads(asyncio.run(tools.run_tool(name, json.dumps(arguments), caller_key, "conv-1")))


def daytime_tomorrow(hour=11):
    tomorrow = datetime.now(ZONE) + timedelta(days=1)
    return f"{tomorrow:%Y-%m-%d}T{hour:02d}:00"


def test_schedule_starts_the_callers_workflow_with_the_number_from_the_call(temporal):
    result = run("schedule_callback", {"when": daytime_tomorrow(), "reason": "  the weather  ",
                                       # The model cannot choose who is called.
                                       "caller_key": "tel:+15550009999"})
    assert result["scheduled"] and result["when"].endswith("11:00 AM")
    request = temporal.workflows[workflow_id(CALLER)].request
    assert (request.caller_key, request.phone_e164, request.reason, request.conversation_id,
            request.timezone) == (CALLER, "+15550002222", "the weather", "conv-1", "Asia/Kolkata")
    assert datetime.fromisoformat(request.due_at_utc).utcoffset() == timedelta(0)


def test_a_second_callback_is_refused_while_one_is_pending(temporal):
    run("schedule_callback", {"when": daytime_tomorrow(), "reason": "a"})
    result = run("schedule_callback", {"when": daytime_tomorrow(15), "reason": "b"})
    assert "already has a callback" in result["error"]
    assert result["existing"]["state"] == "scheduled" and result["existing"]["reason"] == "a"


def test_quiet_hours_past_times_and_unclear_times_are_refused(temporal):
    for when, message in ((daytime_tomorrow(22), "quiet hours"), (daytime_tomorrow(7), "9 am"),
                          ("2020-01-01T10:00", "passed"), ("whenever", "understand"),
                          ("2027-06-01T10:00", "two weeks")):
        assert message in run("schedule_callback", {"when": when, "reason": "x"})["error"]
    assert temporal.workflows == {}


def test_a_callers_time_zone_moves_quiet_hours(temporal):
    tomorrow = datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=1)
    when = f"{tomorrow:%Y-%m-%d}T10:00"  # 10 am in California: 10:30 pm or later in Kolkata.
    assert run("schedule_callback", {"when": when, "reason": "x",
                                     "time_zone": "America/Los_Angeles"})["scheduled"]
    assert "error" in run("schedule_callback", {"when": "tomorrow at 10 am", "reason": "x",
                                                "time_zone": "Mars/Base"})


def test_web_and_anonymous_callers_are_declined(temporal):
    for caller in ("email:someone@example.com", None):
        for name, arguments in (("schedule_callback", {"when": "tomorrow at 5 pm", "reason": "x"}),
                                ("get_callback", {}), ("cancel_callback", {}),
                                ("reschedule_callback", {"when": "tomorrow at 5 pm"})):
            assert "only for phone callers" in run(name, arguments, caller)["error"]
    assert temporal.workflows == {}


def test_get_reschedule_and_cancel_find_the_callers_own_callback(temporal):
    assert run("get_callback", {}) == {"state": "none"}
    assert "no callback booked" in run("cancel_callback", {})["error"]
    run("schedule_callback", {"when": daytime_tomorrow(), "reason": "the weather"})
    assert run("get_callback", {})["state"] == "scheduled"
    moved = run("reschedule_callback", {"when": daytime_tomorrow(16)})
    assert moved["rescheduled"] and moved["when"].endswith("4:00 PM")
    assert "quiet hours" in run("reschedule_callback", {"when": daytime_tomorrow(23)})["error"]
    assert "no callback booked" in run("cancel_callback", {}, "tel:+15550001111")["error"]
    assert run("cancel_callback", {}) == {"cancelled": True}
    assert run("get_callback", {})["state"] == "cancelled"
    assert "no callback booked" in run("reschedule_callback", {"when": daytime_tomorrow()})["error"]


def test_tools_say_callbacks_are_unavailable_without_temporal(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    assert "unavailable" in run("schedule_callback", {"when": daytime_tomorrow(), "reason": "x"})["error"]


def test_only_state_changing_callback_tools_wait_for_a_committed_reply():
    assert [tools.has_side_effects(name) for name in (
        "schedule_callback", "reschedule_callback", "cancel_callback", "get_callback")] == [
        True, True, True, False]


# Dialling.

def test_dial_request_carries_the_callback_in_client_state():
    request = PlaceCallInput("callback-abc", 2, CALLER, "+15550002222", "the weather")
    body = dial_body(DialSettings("key"), request)
    assert (body["connection_id"], body["from"], body["to"]) == (
        "3071391970178893742", "+12288888007", "+15550002222")
    assert body["command_id"] == "callback-abc-2" and "answering_machine_detection" not in body
    assert callbacks.callback_of(body["client_state"]) == CallbackCall("callback-abc", 2,
                                                                       "the weather")
    assert callbacks.callback_of("not base64!") is None
    assert callbacks.callback_of(base64.b64encode(b'{"other": 1}').decode()) is None


def test_place_call_only_dials_the_number_that_asked():
    activities = callback_worker.Activities(None, DialSettings("key"), httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("dialled"))))
    with pytest.raises(ApplicationError, match="not the caller"):
        asyncio.run(activities.place_call(
            PlaceCallInput("callback-abc", 1, CALLER, "+15550009999", "x")))


# Webhooks for a callback's call.

def callback_event(kind, event_id, **payload):
    state = client_state("callback-abc", 1, "the weather")
    return {"data": {"event_type": kind, "id": event_id, "payload": {
        "call_control_id": CALL_ID, "client_state": state, "from": "+12288888007",
        "to": "+15550002222", **payload}}}


@pytest.fixture
def reported(monkeypatch):
    results = []

    async def report(call, call_control_id, result):
        results.append((call.workflow_id, call_control_id, result))
    monkeypatch.setattr(callbacks, "report", report)
    return results


def test_an_answered_callback_reports_and_streams_like_an_inbound_call(board, reported):
    with webhook_client(board) as client:
        post_event(client, callback_event("call.answered", "evt-a"))
        wait_for(lambda: board.requests and reported)
        path, body, _ = board.requests[0]
        assert path == f"/v2/calls/{CALL_ID}/actions/streaming_start"
        token = body.pop("stream_url").removeprefix(STREAM_URL + "?token=")
        assert body["stream_bidirectional_mode"] == "rtp" and body["command_id"] == "evt-a"
        pending = board.pending[token]
        assert pending.caller_key == CALLER and pending.callback.reason == "the weather"
        post_event(client, callback_event("call.hangup", "evt-b", hangup_cause="normal_clearing"))
        wait_for(lambda: len(reported) == 2)
    assert reported == [("callback-abc", CALL_ID, "answered"), ("callback-abc", CALL_ID, "failed")]


@pytest.mark.parametrize("cause, result", [("timeout", "no_answer"), ("user_busy", "busy"),
                                           ("call_rejected", "busy"), ("unspecified", "failed")])
def test_an_unanswered_callback_reports_why(board, reported, cause, result):
    with webhook_client(board) as client:
        post_event(client, callback_event("call.hangup", "evt-h", hangup_cause=cause))
        wait_for(lambda: reported)
    assert reported == [("callback-abc", CALL_ID, result)] and board.requests == []


def test_inbound_answered_and_hangup_events_report_nothing(board, reported):
    with webhook_client(board) as client:
        for kind in ("call.answered", "call.hangup"):
            event = callback_event(kind, f"evt-{kind}")
            del event["data"]["payload"]["client_state"]
            post_event(client, event)
    assert reported == [] and board.requests == []


def test_report_signals_the_workflow(temporal):
    asyncio.run(callbacks.report(CallbackCall("callback-abc", 1, "x"), CALL_ID, "busy"))
    assert temporal.signals == [("callback-abc", "call_result", (CallResult(CALL_ID, "busy"),))]


# The greeting.

def test_a_callback_opens_with_its_reason_and_the_callers_name(monkeypatch):
    monkeypatch.setattr(memory, "calls", {})
    monkeypatch.setitem(callbacks.calls, "conv-cb", CallbackCall("callback-abc", 1, "the weather"))
    assert callbacks.greeting("conv-cb") == (
        "Hi, this is Cadence. You asked me to call back about the weather.")
    monkeypatch.setattr(memory, "display_name", lambda conversation_id: "Chirag")
    assert callbacks.greeting("conv-cb") == "Hi Chirag, you asked me to call back about the weather."
    assert callbacks.greeting("conv-other") == memory.greeting("conv-other")
