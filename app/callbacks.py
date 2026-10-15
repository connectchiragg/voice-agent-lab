"""Scheduled callbacks, the app's side: agent tools, Telnyx webhooks and the callback greeting.

The callback itself is a Temporal workflow (app/callback_workflow.py) run by a
separate worker (app/callback_worker.py). This module only talks to Temporal:

- Tools: `schedule_callback` starts the caller's workflow; `reschedule_callback`,
  `cancel_callback` and `get_callback` find it again by its id, derived from
  the caller. The caller's number always comes from the call (`caller_key`),
  never from the model, and only phone callers can ask.
- Webhooks (app/telnyx.py): the callback's call is answered or hung up, and the
  result is signalled to the workflow. An answered call streams to /ws/telnyx
  like an inbound one, and opens with `greeting`.

Without TEMPORAL_ADDRESS callbacks are off: the tools say so and nothing is dialled.
"""
import asyncio
import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from temporalio.client import (Client, WorkflowExecutionStatus, WorkflowFailureError,
                               WorkflowUpdateFailedError)
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from app import memory
from app.callback_worker import TemporalSettings
from app.callback_workflow import (MAX_REASON_CHARS, TASK_QUEUE, CallbackRequest, CallbackStatus,
                                   CallbackWorkflow, CallResult, in_quiet_hours, validate_due,
                                   workflow_id)

logger = logging.getLogger("uvicorn.error")
CONNECT_TIMEOUT_S = 3.0
MAX_AHEAD = timedelta(days=14)  # Further out, ask the caller to call again nearer the time.
RPC_TIMEOUT = timedelta(seconds=3)  # Within the tools' own 5 s limit.


def default_zone() -> str:
    return os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")


# The Temporal client: connected on first use, shared by every call.

class TemporalClient(Protocol):
    """The part of temporalio's Client the tools use, so tests can pass a fake."""
    async def start_workflow(self, *args: Any, **kwargs: Any) -> Any: ...
    def get_workflow_handle(self, workflow_id: str, **kwargs: Any) -> Any: ...


client: TemporalClient | None = None
connecting = asyncio.Lock()


async def temporal() -> TemporalClient | None:
    """The shared client, or None when callbacks are off or Temporal is unreachable."""
    global client
    settings = TemporalSettings.from_env()
    if settings is None:
        return None
    async with connecting:
        if client is None:
            try:
                client = await asyncio.wait_for(settings.connect(), CONNECT_TIMEOUT_S)
            except Exception as error:
                logger.warning("Temporal unreachable at %s: %s", settings.address,
                               type(error).__name__)
                return None
        return client


# Natural times, in the caller's time zone.

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RELATIVE = re.compile(r"in (an?|\d+) (minute|min|hour|hr)s?", re.IGNORECASE)
CLOCK = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))? ?([ap])\.? ?m\b\.?|\b(\d{1,2})[:.](\d{2})\b|\bnoon\b",
                   re.IGNORECASE)


def parse_when(text: str, now: datetime) -> datetime | None:
    """"tomorrow at 5 pm", "in 2 hours", "Friday 10:30 am", "2026-10-16T17:00" -> a time.

    `now` is aware, in the caller's zone, which a time without an offset is read in.
    A clock time with no day is the next such time. None if it is not understood.
    """
    phrase = " ".join(text.strip().lower().split())
    try:
        moment = datetime.fromisoformat(phrase.upper().replace(" ", "T", 1))
        return moment if moment.tzinfo else moment.replace(tzinfo=now.tzinfo)
    except ValueError:
        pass
    if match := RELATIVE.fullmatch(phrase):
        count = 1 if match[1].startswith("a") else int(match[1])
        unit = timedelta(hours=1) if match[2].startswith("h") else timedelta(minutes=1)
        return now + count * unit
    clock = CLOCK.search(phrase)
    if clock is None:
        return None
    if clock[0].lower() == "noon":
        hour, minute = 12, 0
    elif clock[1]:
        hour, minute = int(clock[1]) % 12 + (12 if clock[3].lower() == "p" else 0), int(clock[2] or 0)
        if not 1 <= int(clock[1]) <= 12:
            return None
    else:
        hour, minute = int(clock[4]), int(clock[5])
    if hour > 23 or minute > 59:
        return None
    rest = (phrase[:clock.start()] + phrase[clock.end():]).replace(" at ", " ")
    words = set(re.findall(r"[a-z]+", rest))
    weekdays = [index for index, name in enumerate(WEEKDAYS) if name in words]
    day = now.date()
    if "tomorrow" in words:
        day += timedelta(days=1)
    elif weekdays:  # The next such day, a week ahead if it is today.
        day += timedelta(days=(weekdays[0] - now.weekday() - 1) % 7 + 1)
    elif words - {"today", "tonight", "this", "evening", "afternoon", "morning", "o", "clock",
                  "around", "by", "at"}:
        return None  # Words we do not understand: the model asks again.
    moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
    if moment <= now and not words & {"today", "tonight", "tomorrow"}:
        moment += timedelta(days=1)
    return moment


def spoken(moment: datetime, zone: str) -> str:
    local = moment.astimezone(ZoneInfo(zone))
    return f"{local:%A, %d %B} at {local.strftime('%I').lstrip('0')}:{local:%M %p}"


# Tools. Each gets caller_key and conversation_id from the call, never from the model.

NOT_PHONE = {"error": "Callbacks are only for phone callers, whose number we know from the call. "
                      "Politely say you cannot call them back from a web call."}
UNAVAILABLE = {"error": "Callbacks are unavailable right now."}


def phone_of(caller_key: str | None) -> str | None:
    return caller_key.removeprefix("tel:") if caller_key and caller_key.startswith("tel:") else None


def caller_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or default_zone())
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown time zone {name!r}; use an IANA name like Europe/London") from error


def when_error(when: str, zone: str, now: datetime) -> tuple[datetime | None, dict[str, Any] | None]:
    """The time asked for, or an error for the model to explain to the caller."""
    due = parse_when(when, now.astimezone(ZoneInfo(zone)))
    if due is None:
        return None, {"error": f"Could not understand the time {when!r}. Ask for a day and a "
                               "clock time, such as tomorrow at 5 pm."}
    if due - now > MAX_AHEAD:
        return None, {"error": "That is more than two weeks away; callbacks can be booked up "
                               "to two weeks ahead."}
    try:
        validate_due(due.astimezone(timezone.utc), now, zone)
    except ValueError as error:
        hint = (" Ask for a time between 9 am and 9 pm." if in_quiet_hours(due, zone) else "")
        return None, {"error": f"Cannot call back {spoken(due, zone)}: {error}.{hint}"}
    return due, None


async def schedule_callback(when: str, reason: str, time_zone: str | None = None,
                            caller_key: str | None = None,
                            conversation_id: str | None = None) -> dict[str, Any]:
    phone = phone_of(caller_key)
    if phone is None or caller_key is None:
        return NOT_PHONE
    zone = caller_zone(time_zone).key
    due, error = when_error(when, zone, datetime.now(timezone.utc))
    if due is None:
        return error or UNAVAILABLE
    temporal_client = await temporal()
    if temporal_client is None:
        return UNAVAILABLE
    reason = " ".join(reason.split())[:MAX_REASON_CHARS] or "your question"
    request = CallbackRequest(caller_key, phone, due.astimezone(timezone.utc).isoformat(), reason,
                              conversation_id or "", zone)
    try:
        await temporal_client.start_workflow(CallbackWorkflow.run, request,
                                             id=workflow_id(caller_key), task_queue=TASK_QUEUE,
                                             rpc_timeout=RPC_TIMEOUT)
    except WorkflowAlreadyStartedError:
        return {"error": "This caller already has a callback booked; offer to move or cancel it.",
                "existing": await describe(temporal_client, caller_key)}
    logger.info("Callback scheduled")
    return {"scheduled": True, "when": spoken(due, zone), "timezone": zone, "reason": reason}


async def find(caller_key: str | None) -> tuple[Any, dict[str, Any] | None]:
    """The caller's pending callback's handle, or an error for the model."""
    if phone_of(caller_key) is None or caller_key is None:
        return None, NOT_PHONE
    temporal_client = await temporal()
    if temporal_client is None:
        return None, UNAVAILABLE
    handle = temporal_client.get_workflow_handle(workflow_id(caller_key),
                                                 result_type=CallbackStatus)
    if await workflow_state(handle) != WorkflowExecutionStatus.RUNNING:
        return None, {"error": "This caller has no callback booked."}
    return handle, None


async def workflow_state(handle: Any) -> WorkflowExecutionStatus | None:
    try:
        return (await handle.describe(rpc_timeout=RPC_TIMEOUT)).status
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return None
        raise


def summary(status: CallbackStatus) -> dict[str, Any]:
    due = datetime.fromisoformat(status.due_at_utc)
    result: dict[str, Any] = {"state": status.state, "when": spoken(due, status.timezone),
                              "reason": status.reason, "attempts": len(status.attempts)}
    if status.detail:
        result["detail"] = status.detail
    return result


async def describe(temporal_client: TemporalClient, caller_key: str) -> dict[str, Any]:
    """The caller's latest callback: pending (queried live) or finished (its result)."""
    handle = temporal_client.get_workflow_handle(workflow_id(caller_key),
                                                 result_type=CallbackStatus)
    state = await workflow_state(handle)
    if state is None:
        return {"state": "none"}
    if state == WorkflowExecutionStatus.RUNNING:
        return summary(await handle.query(CallbackWorkflow.status_query, rpc_timeout=RPC_TIMEOUT))
    if state == WorkflowExecutionStatus.COMPLETED:
        return summary(await handle.result(rpc_timeout=RPC_TIMEOUT))
    return {"state": state.name.lower()}


async def reschedule_callback(when: str, caller_key: str | None = None,
                              conversation_id: str | None = None) -> dict[str, Any]:
    handle, error = await find(caller_key)
    if handle is None:
        return error or UNAVAILABLE
    current: CallbackStatus = await handle.query(CallbackWorkflow.status_query,
                                                 rpc_timeout=RPC_TIMEOUT)
    due, error = when_error(when, current.timezone, datetime.now(timezone.utc))
    if due is None:
        return error or UNAVAILABLE
    try:
        status = await handle.execute_update(CallbackWorkflow.reschedule,
                                             due.astimezone(timezone.utc).isoformat(),
                                             rpc_timeout=RPC_TIMEOUT)
    except WorkflowUpdateFailedError as failure:
        cause = failure.cause.message if failure.cause else "refused"
        return {"error": f"Could not move the callback: {cause}."}
    logger.info("Callback rescheduled")
    return {"rescheduled": True, **summary(status)}


async def cancel_callback(caller_key: str | None = None,
                          conversation_id: str | None = None) -> dict[str, Any]:
    handle, error = await find(caller_key)
    if handle is None:
        return error or UNAVAILABLE
    await handle.signal(CallbackWorkflow.cancel, rpc_timeout=RPC_TIMEOUT)
    logger.info("Callback cancelled")
    return {"cancelled": True}


async def get_callback(caller_key: str | None = None,
                       conversation_id: str | None = None) -> dict[str, Any]:
    if phone_of(caller_key) is None or caller_key is None:
        return NOT_PHONE
    temporal_client = await temporal()
    if temporal_client is None:
        return UNAVAILABLE
    try:
        return await describe(temporal_client, caller_key)
    except WorkflowFailureError:
        return {"state": "failed"}


# Telnyx webhooks for a callback's call.

@dataclass(frozen=True)
class CallbackCall:
    """What a callback's outbound call carries in Telnyx's client_state."""
    workflow_id: str
    attempt: int
    reason: str


def callback_of(client_state: object) -> CallbackCall | None:
    """The callback a webhook is about, from its client_state; None for other calls."""
    if not isinstance(client_state, str) or not client_state:
        return None
    try:
        state = json.loads(base64.b64decode(client_state, validate=True))
        return CallbackCall(str(state["callback"]), int(state["attempt"]),
                            str(state.get("reason", ""))[:MAX_REASON_CHARS])
    except (ValueError, binascii.Error, KeyError, TypeError):
        return None


# Telnyx hangup_cause -> the workflow's call result (before an answer).
HANGUP_RESULTS = {"timeout": "no_answer", "no_answer": "no_answer", "originator_cancel": "no_answer",
                  "user_busy": "busy", "call_rejected": "busy"}


def hangup_result(cause: object) -> str:
    return HANGUP_RESULTS.get(str(cause), "failed")


async def report(call: CallbackCall, call_control_id: str, result: str) -> None:
    """Signal a call's result to its workflow; the first result for a call wins there."""
    temporal_client = await temporal()
    if temporal_client is None:
        logger.warning("Callback result %s not reported: Temporal is unavailable", result)
        return
    try:
        await temporal_client.get_workflow_handle(call.workflow_id).signal(
            CallbackWorkflow.call_result, CallResult(call_control_id, result),
            rpc_timeout=RPC_TIMEOUT)
    except RPCError as error:
        logger.warning("Callback result %s not reported: %s", result, error.status.name)


# The callback greeting.

calls: dict[str, CallbackCall] = {}  # Conversation id -> the callback it is, while it lasts.


def greeting(conversation_id: str) -> str:
    """The first words of a phone call: the callback's reason, else the usual greeting."""
    call = calls.get(conversation_id)
    if call is None:
        return memory.greeting(conversation_id)
    name = memory.display_name(conversation_id)
    return (f"Hi {name}, you asked me to call back about {call.reason}." if name
            else f"Hi, this is Cadence. You asked me to call back about {call.reason}.")
