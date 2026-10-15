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

from app.persona import ASSISTANT_NAME
from app import memory
from app.callback_worker import TemporalSettings
from app.callback_workflow import (MAX_REASON_CHARS, TASK_QUEUE, CallbackRequest, CallbackStatus,
                                   CallbackWorkflow, CallResult, in_quiet_hours,
                                   parse_quiet_hours, quiet_hours_phrase, validate_due,
                                   workflow_id)

logger = logging.getLogger("uvicorn.error")
CONNECT_TIMEOUT_S = 3.0
MAX_AHEAD = timedelta(days=14)  # Further out, ask the caller to call again nearer the time.
RPC_TIMEOUT = timedelta(seconds=3)  # Within the tools' own 5 s limit.


def default_zone() -> str:
    return os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")


def quiet_hours() -> str:
    """CALLBACK_QUIET_HOURS: "21-9" (no calls 9 pm to 9 am, the caller's time); empty or
    "off" (the default for now) allows any hour. Invalid: logged and treated as off."""
    text = os.getenv("CALLBACK_QUIET_HOURS", "").strip().lower()
    try:
        return "" if parse_quiet_hours(text) is None else text
    except ValueError as error:
        logger.warning("CALLBACK_QUIET_HOURS ignored: %s", error)
        return ""


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


async def announce() -> None:
    """At startup: connect once and log whether callbacks are on, so the logs say so."""
    settings = TemporalSettings.from_env()
    if settings is None:
        logger.info("Callbacks are off (TEMPORAL_ADDRESS is not set).")
        return
    if await temporal() is not None:
        logger.info("Callbacks enabled: Temporal at %s, namespace %s", settings.address,
                    settings.namespace)


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


def when_error(when: str, zone: str, now: datetime,
               quiet: str = "") -> tuple[datetime | None, dict[str, Any] | None]:
    """The time asked for, or an error for the model to explain to the caller."""
    due = parse_when(when, now.astimezone(ZoneInfo(zone)))
    if due is None:
        return None, {"error": f"Could not understand the time {when!r}. Ask for a day and a "
                               "clock time, such as tomorrow at 5 pm."}
    if due - now > MAX_AHEAD:
        return None, {"error": "That is more than two weeks away; callbacks can be booked up "
                               "to two weeks ahead."}
    try:
        validate_due(due.astimezone(timezone.utc), now, zone, quiet)
    except ValueError as error:
        start, _, end = quiet_hours_phrase(quiet).partition(" to ")
        hint = (f" Ask for a time between {end} and {start}."
                if in_quiet_hours(due, zone, quiet) else "")
        return None, {"error": f"Cannot call back {spoken(due, zone)}: {error}.{hint}"}
    return due, None


# Booking and moving a callback take two steps, enforced here rather than left to the
# prompt: a proposal the model reads back, then a confirmation that only succeeds on
# the caller's very next turn, and only if that turn says yes.

@dataclass(frozen=True)
class TurnContext:
    """The caller's turn a tool call answers: its text and its number in the call."""
    user_text: str
    user_turn: int


@dataclass(frozen=True)
class Proposal:
    kind: str  # "schedule" or "reschedule"
    due: datetime
    zone: str
    reason: str
    user_turn: int  # The turn in which it was proposed; the yes must be the next one.


proposals: dict[str, Proposal] = {}  # Conversation id -> the time awaiting the caller's yes.
YES = frozenset("yes yeah yep yup sure correct right ok okay perfect great fine please "
                "absolutely definitely confirm confirmed".split())
NO = frozenset("no nope not dont don't never wrong cancel wait stop didnt didn't".split())


def affirmative(text: str) -> bool:
    """A clear yes ("Yes, please", "that's right"), with no no in it."""
    words = re.sub(r"[^\w\s']", " ", text.lower()).split()
    return any(word in YES for word in words) and not any(word in NO for word in words)


def confirmation_error(conversation_id: str | None, turn: "TurnContext | None",
                       kind: str) -> tuple[Proposal | None, dict[str, Any] | None]:
    """The proposal this confirmation may book, or why it may not."""
    proposal = proposals.get(conversation_id or "")
    if proposal is None or proposal.kind != kind or turn is None:
        return None, {"error": "Nothing is booked yet. First call this tool without confirmed "
                               "to propose the time, read it back and ask the caller."}
    if turn.user_turn != proposal.user_turn + 1:
        proposals.pop(conversation_id or "", None)
        return None, {"error": "Not booked: the caller must confirm on their very next reply. "
                               "Propose the time again if they still want it."}
    if not affirmative(turn.user_text):
        proposals.pop(conversation_id or "", None)
        return None, {"error": "Not booked: the caller did not say yes. Do not book it; ask "
                               "what they would like instead."}
    proposals.pop(conversation_id or "", None)
    return proposal, None


def propose(kind: str, conversation_id: str | None, turn: "TurnContext | None", due: datetime,
            zone: str, reason: str) -> dict[str, Any]:
    if len(proposals) >= 256:  # Calls that ended with one pending: drop the oldest.
        proposals.pop(next(iter(proposals)))
    proposals[conversation_id or ""] = Proposal(kind, due, zone, reason,
                                                turn.user_turn if turn else -1)
    return {"proposed": True, "booked": False, "when": spoken(due, zone), "timezone": zone,
            "reason": reason,
            "next": "Nothing is booked yet. Say this day and time back to the caller and ask "
                    "if that is right. Only if their next reply is yes, call this tool again "
                    "with confirmed=true."}


async def schedule_callback(when: str, reason: str, time_zone: str | None = None,
                            confirmed: bool = False, caller_key: str | None = None,
                            conversation_id: str | None = None,
                            turn: TurnContext | None = None) -> dict[str, Any]:
    phone = phone_of(caller_key)
    if phone is None or caller_key is None:
        return NOT_PHONE
    quiet = quiet_hours()
    if not confirmed:
        zone = caller_zone(time_zone).key
        due, error = when_error(when, zone, datetime.now(timezone.utc), quiet)
        if due is None:
            return error or UNAVAILABLE
        reason = " ".join(reason.split())[:MAX_REASON_CHARS] or "your question"
        return propose("schedule", conversation_id, turn, due, zone, reason)
    proposal, error = confirmation_error(conversation_id, turn, "schedule")
    if proposal is None:
        return error or UNAVAILABLE
    due, zone, reason = proposal.due, proposal.zone, proposal.reason
    try:
        validate_due(due.astimezone(timezone.utc), datetime.now(timezone.utc), zone, quiet)
    except ValueError as invalid:
        return {"error": f"Cannot call back {spoken(due, zone)}: {invalid}."}
    temporal_client = await temporal()
    if temporal_client is None:
        return UNAVAILABLE
    request = CallbackRequest(caller_key, phone, due.astimezone(timezone.utc).isoformat(), reason,
                              conversation_id or "", zone, quiet)
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


async def reschedule_callback(when: str, confirmed: bool = False,
                              caller_key: str | None = None, conversation_id: str | None = None,
                              turn: TurnContext | None = None) -> dict[str, Any]:
    handle, error = await find(caller_key)
    if handle is None:
        return error or UNAVAILABLE
    current: CallbackStatus = await handle.query(CallbackWorkflow.status_query,
                                                 rpc_timeout=RPC_TIMEOUT)
    if not confirmed:
        # The workflow checks again with the quiet hours it was booked under.
        due, error = when_error(when, current.timezone, datetime.now(timezone.utc),
                                quiet_hours())
        if due is None:
            return error or UNAVAILABLE
        return propose("reschedule", conversation_id, turn, due, current.timezone, current.reason)
    proposal, error = confirmation_error(conversation_id, turn, "reschedule")
    if proposal is None:
        return error or UNAVAILABLE
    due = proposal.due
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
    research_job: str = ""  # VALR: the job whose findings this call delivers.


def callback_of(client_state: object) -> CallbackCall | None:
    """The callback a webhook is about, from its client_state; None for other calls."""
    if not isinstance(client_state, str) or not client_state:
        return None
    try:
        state = json.loads(base64.b64decode(client_state, validate=True))
        return CallbackCall(str(state["callback"]), int(state["attempt"]),
                            str(state.get("reason", ""))[:MAX_REASON_CHARS],
                            str(state.get("research", ""))[:64])
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
    """The first words of a phone call: the callback's reason, else the usual greeting.

    For a callback the reason comes first: a callee often says "Hello?" over
    the opening words, and a generic opening then hides why we called.
    """
    call = calls.get(conversation_id)
    if call is None:
        from app import interviews  # Imports this module; loaded late to avoid a cycle.
        if interviews.is_owner_call(conversation_id):
            return interviews.owner_greeting(conversation_id)
        return memory.greeting(conversation_id)
    if call.research_job.startswith("block:"):
        from app import interviews  # Imports this module; loaded late to avoid a cycle.
        return interviews.greeting(conversation_id, call.reason)
    if call.research_job:
        from app import research  # Imports this module; loaded late to avoid a cycle.
        return research.greeting(conversation_id, call.reason)
    name = memory.display_name(conversation_id)
    return (f"Hi {name}, {ASSISTANT_NAME} calling back about {call.reason}, as you asked." if name
            else f"Hi, {ASSISTANT_NAME} calling back about {call.reason}, as you asked.")


def prompt_note(conversation_id: str) -> str:
    """For the system prompt of a callback call: why we called, and not to book another."""
    call = calls.get(conversation_id)
    if call is None:
        return ""
    if call.research_job.startswith("block:"):
        return ""  # The briefing is in interviews.prompt_note (owner only).
    if call.research_job:
        from app import research
        return research.prompt_note(conversation_id, call.reason.removeprefix("what I found about "))
    return (f"This call is the callback the caller asked for, about {call.reason}. Help with "
            "that now. Do not book another callback unless the caller clearly asks for one.")
