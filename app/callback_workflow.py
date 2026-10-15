"""Scheduled callbacks as Temporal workflows (task queue `voice-callbacks`).

One `CallbackWorkflow` per caller (see `workflow_id`) holds a callback from the
moment the caller asks for it until it is answered, cancelled or given up:

1. It sleeps on a durable timer until `due_at`. The timer lives in the Temporal
   server, so restarts of the app or the worker do not lose it.
2. It reserves one of the day's dial slots (`DialBudgetWorkflow`, at most
   MAX_DIALS_PER_DAY for everyone), then runs the `place_call` activity, which
   asks Telnyx to dial the caller.
3. It waits for the app's Telnyx webhook to signal the result (`call_result`):
   answered, or no answer, busy, failed. (Voicemail counts as answered: Telnyx's
   answering-machine detection is a paid add-on, so it is not enabled.)
4. Not answered: it waits RETRY_AFTER and tries again, MAX_ATTEMPTS in all.

While it waits, `reschedule` (an update: the caller gets yes or no at once)
moves the timer and `cancel` (a signal) ends it; `status` is a query.

Workflow code must be deterministic: it reads time only through `workflow.now()`
and does all I/O in activities (app/callback_worker.py). This module imports
only the standard library and temporalio, so the worker's sandbox can load it.
"""
import hashlib
from dataclasses import dataclass, field
import dataclasses
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

TASK_QUEUE = "voice-callbacks"
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=15)
MAX_DIALS_PER_DAY = 5  # All callers together, per UTC day.
# From dialling to the webhook's answer or hangup. Telnyx rings for
# at most RING_TIMEOUT_S (the dial's timeout_secs); after this we count no answer.
RING_TIMEOUT_S = 30
RESULT_TIMEOUT = timedelta(minutes=3)
MAX_REASON_CHARS = 120

# Call results the webhook signals; only "answered" ends the callback successfully.
ANSWERED = "answered"
RESULTS = (ANSWERED, "no_answer", "busy", "failed")


def workflow_id(caller_key: str) -> str:
    """One id per caller, so Temporal itself refuses a second pending callback.

    Hashed so the phone number is not the id shown in workflow lists.
    """
    return "callback-" + hashlib.sha256(caller_key.encode()).hexdigest()[:20]


def parse_quiet_hours(text: str) -> tuple[int, int] | None:
    """ "21-9" -> (21, 9): no calls from 21:00 to 08:59 local; "" or "off" -> None (any hour)."""
    text = text.strip().lower()
    if text in ("", "off"):
        return None
    start, separator, end = text.partition("-")
    if not separator or not start.isdigit() or not end.isdigit():
        raise ValueError(f"quiet hours must look like 21-9 or be off, not {text!r}")
    hours = int(start), int(end)
    if not all(0 <= hour <= 23 for hour in hours) or hours[0] == hours[1]:
        raise ValueError(f"quiet hours must be two different hours from 0 to 23, not {text!r}")
    return hours


def in_quiet_hours(moment: datetime, zone: str, quiet_hours: str) -> bool:
    """Whether `moment` falls in `quiet_hours` ("21-9"; "" is never) in `zone`."""
    hours = parse_quiet_hours(quiet_hours)
    if hours is None:
        return False
    start, end = hours
    hour = moment.astimezone(ZoneInfo(zone)).hour
    return hour >= start or hour < end if start > end else start <= hour < end


def quiet_hours_phrase(quiet_hours: str) -> str:
    """ "21-9" -> "9 pm to 9 am"."""
    hours = parse_quiet_hours(quiet_hours)
    if hours is None:
        return ""

    def clock(hour: int) -> str:
        return f"{hour % 12 or 12} {'am' if hour < 12 else 'pm'}"
    return f"{clock(hours[0])} to {clock(hours[1])}"


def parse_utc(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("due_at must include a UTC offset")
    return moment.astimezone(timezone.utc)


def workflow_runner() -> SandboxedWorkflowRunner:
    """The worker's sandbox, letting ZoneInfo through for quiet hours.

    The default sandbox wraps ZoneInfo objects in a proxy that datetime refuses.
    A zone's rules do not change while a workflow runs, so replays stay deterministic.
    """
    members = SandboxRestrictions.invalid_module_members_default.with_child_unrestricted(
        "zoneinfo", "ZoneInfo")
    return SandboxedWorkflowRunner(dataclasses.replace(
        SandboxRestrictions.default, invalid_module_members=members))


@dataclass
class CallbackRequest:
    caller_key: str  # "tel:+E164", from the call; never from the model.
    phone_e164: str
    due_at_utc: str  # ISO 8601 with offset.
    reason: str
    conversation_id: str  # The call in which the caller asked.
    timezone: str = "Asia/Kolkata"  # The caller's, for quiet hours.
    # CALLBACK_QUIET_HOURS when it was booked ("21-9"; "" off), passed in so workflow
    # code never reads the environment; it also governs reschedules and retries.
    quiet_hours: str = ""
    research_job: str = ""  # A VALR job whose findings this callback delivers.


@dataclass
class Attempt:
    number: int
    placed_at_utc: str
    call_control_id: str = ""
    result: str = ""  # One of RESULTS, or "daily_limit".


@dataclass
class CallbackStatus:
    state: str  # scheduled, calling, retry_wait, completed, failed, cancelled, rejected.
    due_at_utc: str
    reason: str
    timezone: str
    attempts: list[Attempt] = field(default_factory=list)
    detail: str = ""  # Why it failed or was rejected.


@dataclass
class PlaceCallInput:
    workflow_id: str
    attempt: int
    caller_key: str
    phone_e164: str
    reason: str
    research_job: str = ""


@dataclass
class CallResult:
    call_control_id: str
    result: str


@dataclass
class DialSlotRequest:
    day: str  # UTC date, YYYY-MM-DD.
    attempt_id: str  # Repeats (activity retries) are counted once.


def validate_due(due: datetime, now: datetime, zone: str, quiet_hours: str = "") -> None:
    """ValueError naming the problem, for a time the callback cannot be placed at."""
    if due <= now:
        raise ValueError("that time has already passed")
    if in_quiet_hours(due, zone, quiet_hours):
        raise ValueError(f"that is within quiet hours ({quiet_hours_phrase(quiet_hours)} "
                         "the caller's time)")


@workflow.defn
class CallbackWorkflow:
    def __init__(self) -> None:
        self.status = CallbackStatus("scheduled", "", "", "")
        self.due = datetime.min.replace(tzinfo=timezone.utc)
        self.timer_changed = False
        self.cancelled = False
        self.result: CallResult | None = None
        self.quiet_hours = ""

    @workflow.run
    async def run(self, request: CallbackRequest) -> CallbackStatus:
        self.status = CallbackStatus("scheduled", request.due_at_utc,
                                     request.reason[:MAX_REASON_CHARS], request.timezone)
        self.quiet_hours = request.quiet_hours
        try:
            self.due = parse_utc(request.due_at_utc)
            validate_due(self.due, workflow.now(), request.timezone, self.quiet_hours)
        except ValueError as error:
            return self.finish("rejected", str(error))
        while True:
            await self.sleep_until_due()
            if self.cancelled:
                return self.finish("cancelled")
            # A retry that ran into the evening.
            if in_quiet_hours(self.due, request.timezone, self.quiet_hours):
                return self.finish("failed", "the next attempt would fall in quiet hours")
            attempt = Attempt(len(self.status.attempts) + 1, workflow.now().isoformat())
            self.status.attempts.append(attempt)
            if not await self.reserve_dial(attempt):
                attempt.result = "daily_limit"
                return self.finish("failed", "the daily limit of callback calls was reached")
            self.status.state = "calling"
            attempt.result = await self.call(request, attempt)
            if attempt.result == ANSWERED:
                return self.finish("completed")
            if self.cancelled:
                return self.finish("cancelled")
            if attempt.number >= MAX_ATTEMPTS:
                return self.finish("failed", f"no answer after {MAX_ATTEMPTS} attempts")
            self.set_due(workflow.now() + RETRY_AFTER)
            self.status.state = "retry_wait"

    async def sleep_until_due(self) -> None:
        """The durable timer; a reschedule restarts it and a cancel ends it."""
        while not self.cancelled:
            remaining = self.due - workflow.now()
            if remaining <= timedelta(0):
                return
            self.timer_changed = False
            try:
                await workflow.wait_condition(lambda: self.cancelled or self.timer_changed,
                                              timeout=remaining)
            except TimeoutError:
                return

    async def reserve_dial(self, attempt: Attempt) -> bool:
        info = workflow.info()
        return await workflow.execute_activity(
            "reserve_dial_slot",
            DialSlotRequest(workflow.now().date().isoformat(),
                            f"{info.workflow_id}/{info.run_id}/{attempt.number}"),
            result_type=bool, start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5))

    async def call(self, request: CallbackRequest, attempt: Attempt) -> str:
        """Dial once and wait for the webhook's verdict; a lost verdict counts as no answer."""
        self.result = None
        try:
            attempt.call_control_id = await workflow.execute_activity(
                "place_call",
                PlaceCallInput(workflow.info().workflow_id, attempt.number, request.caller_key,
                               request.phone_e164, self.status.reason, request.research_job),
                result_type=str, start_to_close_timeout=timedelta(seconds=30),
                # Telnyx de-duplicates on command_id, so a retried dial does not ring twice.
                retry_policy=RetryPolicy(maximum_attempts=3,
                                         non_retryable_error_types=["DialRefused"]))
        except ActivityError:
            return "failed"
        try:
            await workflow.wait_condition(lambda: self.result is not None or self.cancelled,
                                          timeout=RESULT_TIMEOUT)
        except TimeoutError:
            return "no_answer"
        # Cancelled while ringing: the ring is left to time out; nothing more is tried.
        return self.result.result if self.result is not None else "cancelled"

    def set_due(self, due: datetime) -> None:
        self.due = due
        self.status.due_at_utc = due.isoformat()
        self.timer_changed = True

    def finish(self, state: str, detail: str = "") -> CallbackStatus:
        self.status.state, self.status.detail = state, detail
        workflow.logger.info("Callback %s %s", state, detail)
        return self.status

    @workflow.update
    async def reschedule(self, new_due_at_utc: str) -> CallbackStatus:
        self.set_due(parse_utc(new_due_at_utc))
        self.status.state = "scheduled"
        return self.status

    @reschedule.validator
    def validate_reschedule(self, new_due_at_utc: str) -> None:
        """Runs before the update is accepted; raising rejects it with nothing recorded."""
        if self.status.state not in ("scheduled", "retry_wait") or self.cancelled:
            raise ValueError(f"the callback cannot be moved while it is {self.status.state}")
        validate_due(parse_utc(new_due_at_utc), workflow.now(), self.status.timezone,
                     self.quiet_hours)

    @workflow.signal
    def cancel(self) -> None:
        self.cancelled = True

    @workflow.signal
    def call_result(self, result: CallResult) -> None:
        """From the Telnyx webhook. The first verdict for the current call wins."""
        attempts = self.status.attempts
        if (self.result is None and result.result in RESULTS and attempts
                and attempts[-1].call_control_id == result.call_control_id):
            self.result = result

    @workflow.query
    def status_query(self) -> CallbackStatus:
        return self.status


@workflow.defn
class DialBudgetWorkflow:
    """The global daily cap: one per UTC day (id `callback-dials-<date>`) counts dials.

    Updates run one at a time, so two callbacks due at once cannot both take the
    last slot. It ends a day after its date, keeping the count visible meanwhile.
    """

    def __init__(self) -> None:
        self.granted: list[str] = []

    @workflow.run
    async def run(self, day: str) -> int:
        await workflow.sleep(timedelta(days=2))
        return len(self.granted)

    @workflow.update
    def reserve(self, attempt_id: str) -> bool:
        if attempt_id in self.granted:
            return True
        if len(self.granted) >= MAX_DIALS_PER_DAY:
            return False
        self.granted.append(attempt_id)
        return True

    @workflow.query
    def dials(self) -> int:
        return len(self.granted)
