"""Interview briefings as Temporal workflows (task queue `voice-research`).

Three workflows, all run by the research worker:

- `CalendarSyncWorkflow`: one sync of the owner's calendar (activity `calendar_sync`):
  Google push notifications start it (signal-with-start, so a burst of notifications
  makes one more pass, not many), a Temporal Schedule runs it every 6 hours as a safety
  sweep (and renews the push channel), and `python -m app.research_worker sync` runs
  it by hand. The activity diffs each event against its stored snapshot and signals:
- `InterviewSlotWorkflow`, one per calendar event (id `slot-<hash of the event key>`),
  long-lived until the event is over. It owns the research timer (T-60 of its block,
  or at once if sooner) and the work still to do, which each diff narrows:
    new -> full research (R1 private context, R2 round intel + interviewers, R3 company,
           R3 from the 7-day company cache when fresh);
    interviewers added -> only the new interviewers; removed -> their notes dropped;
    time only -> the timer moves, no research;
    round (title/description) -> R2's round part; company -> R1, R2 round, R3;
    cancelled -> the workflow ends; the company cache stays;
    none -> nothing (the sync does not even signal).
  Each run is one research job (limits in app/research_jobs.py); after it, the slot's
  stored context is merged and its spoken brief re-written (slot_synthesize).
- `BlockCallWorkflow`, one per block of back-to-back interviews (id from the block's
  start, `briefing-<UTC start>`): calls the owner at T-15, then T-10 and T-5 if
  unanswered, and stops after an answer. Its own daily dial budget ("briefing").
  Before each scheduled dial it asks `dials_paused` (the dashboard's pause switch);
  a paused attempt is skipped, so resuming before T-5 still gets a call.
  The on-demand variant ("research tomorrow's interviews" on a call) waits for that
  call to end and for the slots' research, then calls back at once.

Workflow code reads time only through workflow.now() and does all I/O in activities.
Imports only the standard library, temporalio and the pure modules.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from app.callback_workflow import (ANSWERED, RESULT_TIMEOUT, RESULTS, CallResult, DialSlotRequest,
                                   PlaceCallInput)
from app.callback_workflow import TASK_QUEUE as CALLBACK_QUEUE
from app.interview_types import Person, SlotDiff, SlotEvent

TASK_QUEUE = "voice-research"
RESEARCH_LEAD = timedelta(minutes=60)
CALL_LEADS = (timedelta(minutes=15), timedelta(minutes=10), timedelta(minutes=5))
LINGER_AFTER_END = timedelta(minutes=30)
CALL_END_WAIT = timedelta(minutes=11)
READY_WAIT = timedelta(minutes=12)  # On-demand: longest wait for research before calling.
READY_POLL = timedelta(seconds=15)
DIAL_LEAD = timedelta(seconds=60)  # Dial this long before the promised time.
BRIEFING_DIALS_PER_DAY = 12
PART_SECONDS = 240
SYNTH_SECONDS = 150
FULL = ("private", "round", "interviewers", "company")
DIAL_PAUSE_PATCH = "valr-dial-pause"


@dataclass
class SlotInput:
    event: SlotEvent
    owner_key: str
    block_id: str
    research_due_utc: str
    job_usd: float = 0.40
    part_seconds: int = PART_SECONDS


@dataclass
class ResearchNow:
    """On demand: research at once, each researcher within `seconds` (the promise)."""
    seconds: int
    refresh: bool = False


@dataclass
class SlotUpdate:
    event: SlotEvent
    diff: SlotDiff


@dataclass
class SlotPlan:
    block_id: str
    research_due_utc: str


@dataclass
class PlanInput:
    event: SlotEvent
    owner_key: str
    parts: list[str]
    interviewers: list[Person]  # For "interviewers": only these (the new ones).
    removed: list[str]
    refresh: bool
    job_usd: float


@dataclass
class PlanResult:
    job_id: str = ""
    parts: list[str] = field(default_factory=list)  # After the caches: what really runs.
    cache_hits: list[str] = field(default_factory=list)
    refusal: str = ""


@dataclass
class InterviewPartInput:
    job_id: str
    part: str  # private, round, interviewers, company.
    event: SlotEvent
    interviewers: list[Person]
    seconds: float
    budget_usd: float


@dataclass
class InterviewPartResult:
    part: str
    ok: bool = False
    findings: str = ""  # JSON {"summary", "claims": [{"claim", "source_url"}]}
    evidence: str = ""
    seen_urls: list[str] = field(default_factory=list)
    usd: float = 0.0
    stopped: str = ""
    cap_hits: dict[str, int] = field(default_factory=dict)
    error: str = ""
    researched: list[str] = field(default_factory=list)  # Interviewer emails researched.
    skipped: int = 0  # People the HR guard or classifier left out.


@dataclass
class SlotSynthInput:
    job_id: str
    event: SlotEvent
    parts: list[InterviewPartResult]
    removed: list[str]
    seconds: float
    budget_usd: float


@dataclass
class SlotSynthResult:
    status: str  # done, partial, apologized.
    usd: float = 0.0
    words: int = 0
    cap_hits: dict[str, int] = field(default_factory=dict)


@dataclass
class SlotFinish:
    job_id: str
    status: str
    duration_s: float
    cap_hits: dict[str, int]
    detail: str


@dataclass
class SlotStatus:
    state: str  # waiting, researching, ready, cancelled, over.
    key: str
    block_id: str = ""
    research_due_utc: str = ""
    pending: list[str] = field(default_factory=list)
    runs: int = 0
    last_detail: str = ""


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


@workflow.defn
class InterviewSlotWorkflow:
    def __init__(self) -> None:
        self.event: SlotEvent | None = None
        self.pending: set[str] = set()
        self.new_people: dict[str, Person] = {}
        self.removed: set[str] = set()
        self.refresh = False
        self.cancelled = False
        self.changed = False
        self.status = SlotStatus("waiting", "")
        self.due = datetime.max.replace(tzinfo=timezone.utc)
        self.part_limit: int | None = None  # On demand: the promise's researcher limit.

    @workflow.run
    async def run(self, slot: SlotInput) -> SlotStatus:
        self.event = slot.event
        self.status = SlotStatus("waiting", slot.event.key, slot.block_id, slot.research_due_utc)
        self.due = parse(slot.research_due_utc)
        self.pending = set(FULL)
        while not self.cancelled:
            assert self.event is not None
            over = parse(self.event.end_utc) + LINGER_AFTER_END
            now = workflow.now()
            if now >= over:
                self.status.state = "over"
                break
            if self.pending and now >= self.due and now < parse(self.event.start_utc):
                await self.research(slot)
                continue
            if self.pending and now >= parse(self.event.start_utc):
                self.pending.clear()  # Too late to brief: drop what was left.
            wake = min(self.due, over) if self.pending else over
            self.changed = False
            try:
                await workflow.wait_condition(lambda: self.cancelled or self.changed,
                                              timeout=max(wake - now, timedelta(seconds=1)))
            except TimeoutError:
                pass
        if self.cancelled:
            self.status.state = "cancelled"
        self.status.pending = sorted(self.pending)
        return self.status

    async def research(self, slot: SlotInput) -> None:
        assert self.event is not None
        parts = sorted(self.pending)
        # The first run researches every interviewer on the invite; later runs only the new ones.
        people = list(self.new_people.values()) if self.status.runs else []
        removed, refresh = sorted(self.removed), self.refresh
        self.pending.clear()
        self.new_people.clear()
        self.removed.clear()
        self.refresh = False
        self.status.state, self.status.pending = "researching", parts
        started = workflow.now()
        plan: PlanResult = await workflow.execute_activity(
            "slot_plan", PlanInput(self.event, slot.owner_key, parts, people, removed, refresh,
                                   slot.job_usd),
            result_type=PlanResult, start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3))
        self.status.runs += 1
        if plan.refusal:
            self.status.state, self.status.last_detail = "ready", f"refused: {plan.refusal}"
            return
        if not plan.job_id:  # The stored research is still valid: nothing to run.
            self.status.state, self.status.last_detail = "ready", "cache: " + ", ".join(plan.cache_hits)
            return
        until_start = (parse(self.event.start_utc) - workflow.now()).total_seconds()
        part_s = max(min(self.part_limit or slot.part_seconds, until_start - 16 * 60), 60)
        self.part_limit = None
        part_usd = slot.job_usd * 0.75 / max(len(plan.parts), 1)
        results = []
        for part in plan.parts:  # Started together, awaited together.
            results.append(workflow.start_activity(
                "interview_part", InterviewPartInput(plan.job_id, part, self.event, people,
                                                     part_s, part_usd),
                result_type=InterviewPartResult,
                schedule_to_close_timeout=timedelta(seconds=part_s + 20),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=2))))
        outcomes: list[InterviewPartResult] = []
        for part, handle in zip(plan.parts, results):
            try:
                outcomes.append(await handle)
            except ActivityError as error:
                outcomes.append(InterviewPartResult(part, error=str(error.cause or error)[:200]))
        status, detail, cap_hits = "apologized", "", {}
        usd = sum(o.usd for o in outcomes)
        try:
            synth: SlotSynthResult = await workflow.execute_activity(
                "slot_synthesize", SlotSynthInput(plan.job_id, self.event, outcomes, removed,
                                                  SYNTH_SECONDS, max(slot.job_usd - usd, 0.03)),
                result_type=SlotSynthResult, start_to_close_timeout=timedelta(seconds=SYNTH_SECONDS + 30),
                retry_policy=RetryPolicy(maximum_attempts=2))
            status, cap_hits = synth.status, synth.cap_hits
        except ActivityError as error:
            detail = f"synthesis failed: {error.cause or error}"[:200]
        if plan.cache_hits:
            detail = (detail + "; " if detail else "") + "cache: " + ", ".join(plan.cache_hits)
        for outcome in outcomes:
            for name, count in outcome.cap_hits.items():
                cap_hits[name] = cap_hits.get(name, 0) + count
        try:
            await workflow.execute_activity(
                "slot_finish", SlotFinish(plan.job_id, status,
                                          (workflow.now() - started).total_seconds(), cap_hits, detail),
                start_to_close_timeout=timedelta(seconds=20), retry_policy=RetryPolicy(maximum_attempts=5))
        except ActivityError:
            workflow.logger.warning("VALR slot job %s not saved", plan.job_id)
        self.status.state, self.status.last_detail, self.status.pending = "ready", status, []

    @workflow.signal
    def event_updated(self, update: SlotUpdate) -> None:
        kinds = set(update.diff.kinds)
        if "cancelled" in kinds:
            self.cancelled = True
        else:
            self.event = update.event
            if "new" in kinds:  # Re-created after a cancellation: start over.
                self.pending |= set(FULL)
            if "interviewers" in kinds:
                self.pending.add("interviewers")
                for person in update.diff.added:
                    self.new_people[person.email.lower()] = person
                self.removed |= set(update.diff.removed)
                if update.diff.removed and not update.diff.added:
                    self.pending.discard("interviewers")
                    self.pending.add("resynth")
            if "round" in kinds:
                self.pending.add("round")
            if "company" in kinds:
                self.pending |= {"private", "round", "company"}
        self.changed = True

    @workflow.signal
    def set_plan(self, plan: SlotPlan) -> None:
        """The block it belongs to (and so the research time) changed."""
        self.status.block_id, self.status.research_due_utc = plan.block_id, plan.research_due_utc
        self.due = parse(plan.research_due_utc)
        self.changed = True

    @workflow.signal
    def research_now(self) -> None:
        self.due = workflow.now()
        self.changed = True

    @workflow.signal
    def research_within(self, request: ResearchNow) -> None:
        if request.refresh:
            self.pending |= set(FULL)
            self.refresh = True
        self.part_limit = request.seconds
        self.due = workflow.now()
        self.changed = True

    @workflow.signal
    def refresh_research(self) -> None:
        """The owner asked to refresh: everything again, caches bypassed."""
        self.pending |= set(FULL)
        self.refresh = True
        self.due = workflow.now()
        self.changed = True

    @workflow.signal
    def cancel(self) -> None:
        self.cancelled = True
        self.changed = True

    @workflow.query
    def status_query(self) -> SlotStatus:
        self.status.pending = sorted(self.pending)
        return self.status


# The briefing call for a block.

@dataclass
class BlockInput:
    block_id: str
    start_utc: str
    slot_keys: list[str]
    owner_key: str
    phone_e164: str
    label: str  # What the call is about, for the greeting ("your 4 PM interview with Kestrel").
    on_demand: bool = False
    dial: bool = True
    dials_per_day: int = BRIEFING_DIALS_PER_DAY
    call_by_utc: str = ""  # On demand: the promised time; dial by then, ready or not.
    ready_keys: list[str] = field(default_factory=list)  # Wait for these (empty: all).


@dataclass
class BlockRecord:
    block_id: str
    start_utc: str
    slot_keys: list[str]
    label: str
    state: str


@dataclass
class CallAttempt:
    number: int
    placed_at_utc: str
    call_control_id: str = ""
    result: str = ""


@dataclass
class BlockStatus:
    state: str  # waiting, waiting_for_call_end, waiting_for_research, calling, briefed,
    #             unanswered, cancelled, missed, refused.
    block_id: str
    start_utc: str
    attempts: list[CallAttempt] = field(default_factory=list)
    detail: str = ""


@workflow.defn
class BlockCallWorkflow:
    def __init__(self) -> None:
        self.cancelled = False
        self.call_ended = False
        self.result: CallResult | None = None
        self.slot_keys: list[str] = []
        self.status = BlockStatus("waiting", "", "")

    @workflow.run
    async def run(self, block: BlockInput) -> BlockStatus:
        self.status = BlockStatus("waiting", block.block_id, block.start_utc)
        self.slot_keys = list(block.slot_keys)
        start = parse(block.start_utc)
        await self.record(block, "waiting")
        if block.on_demand:
            self.status.state = "waiting_for_call_end"
            try:
                await workflow.wait_condition(lambda: self.call_ended or self.cancelled,
                                              timeout=CALL_END_WAIT)
            except TimeoutError:
                pass
            self.status.state = "waiting_for_research"
            deadline = (parse(block.call_by_utc) - DIAL_LEAD) if block.call_by_utc \
                else workflow.now() + READY_WAIT
            while not self.cancelled and workflow.now() < deadline:
                if await self.ready(block.ready_keys or self.slot_keys):
                    break
                await self.sleep(min(READY_POLL, max(deadline - workflow.now(), timedelta(seconds=1))))
            first = workflow.now()
            times = [first + timedelta(minutes=5) * index for index in range(len(CALL_LEADS))]
        else:
            times = [start - lead for lead in CALL_LEADS]
        paused = 0
        for number, due in enumerate(times, 1):
            if not block.on_demand and workflow.now() >= start:
                return await self.finish(block, "missed", "the block started before an answer")
            if not await self.sleep_until(due):
                return await self.finish(block, "cancelled")
            if not block.on_demand and workflow.now() >= start:
                return await self.finish(block, "missed", "the block started before an answer")
            if not block.dial:
                return await self.finish(block, "briefed", "dry run: not dialled")
            # Patched: blocks already running when this shipped replay without the check.
            if not block.on_demand and workflow.patched(DIAL_PAUSE_PATCH) and await self.paused():
                paused += 1
                self.status.detail = "automatic calls are paused"
                continue
            attempt = CallAttempt(number, workflow.now().isoformat())
            self.status.attempts.append(attempt)
            if not await self.reserve(block, attempt):
                attempt.result = "daily_limit"
                return await self.finish(block, "refused", "the daily briefing call limit is reached")
            self.status.state = "calling"
            next_due = times[number] if number < len(times) else workflow.now() + RESULT_TIMEOUT
            attempt.result = await self.call(block, attempt, min(next_due - workflow.now(), RESULT_TIMEOUT))
            if attempt.result == ANSWERED:
                return await self.finish(block, "briefed")
            if self.cancelled:
                return await self.finish(block, "cancelled")
            self.status.state = "waiting"
        if paused == len(times):
            return await self.finish(block, "paused", "automatic calls were paused")
        return await self.finish(block, "unanswered", f"no answer after {len(times)} attempts")

    async def paused(self) -> bool:
        try:
            return await workflow.execute_activity(
                "dials_paused", result_type=bool, start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=3))
        except ActivityError:
            return False  # Unknown: dial as before; the daily budget still applies.

    async def sleep(self, duration: timedelta) -> None:
        try:
            await workflow.wait_condition(lambda: self.cancelled, timeout=duration)
        except TimeoutError:
            pass

    async def sleep_until(self, due: datetime) -> bool:
        """False if cancelled meanwhile."""
        remaining = due - workflow.now()
        if remaining > timedelta(0):
            await self.sleep(remaining)
        return not self.cancelled

    async def ready(self, keys: list[str]) -> bool:
        return await workflow.execute_activity(
            "slots_ready", list(keys), result_type=bool,
            start_to_close_timeout=timedelta(seconds=20), retry_policy=RetryPolicy(maximum_attempts=3))

    async def record(self, block: BlockInput, state: str) -> None:
        try:
            await workflow.execute_activity(
                "block_record", BlockRecord(block.block_id, block.start_utc, list(self.slot_keys),
                                            block.label, state),
                start_to_close_timeout=timedelta(seconds=20), retry_policy=RetryPolicy(maximum_attempts=5))
        except ActivityError:
            workflow.logger.warning("VALR block %s not recorded", block.block_id)

    async def reserve(self, block: BlockInput, attempt: CallAttempt) -> bool:
        info = workflow.info()
        return await workflow.execute_activity(
            "reserve_dial_slot",
            DialSlotRequest(workflow.now().date().isoformat(),
                            f"{info.workflow_id}/{info.run_id}/{attempt.number}", "briefing",
                            block.dials_per_day),
            result_type=bool, task_queue=CALLBACK_QUEUE, start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5))

    async def call(self, block: BlockInput, attempt: CallAttempt, wait: timedelta) -> str:
        self.result = None
        try:
            attempt.call_control_id = await workflow.execute_activity(
                "place_call",
                PlaceCallInput(workflow.info().workflow_id, attempt.number, block.owner_key,
                               block.phone_e164, block.label[:120], f"block:{block.block_id}"),
                result_type=str, task_queue=CALLBACK_QUEUE, start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3, non_retryable_error_types=["DialRefused"]))
        except ActivityError:
            return "failed"
        try:
            await workflow.wait_condition(lambda: self.result is not None or self.cancelled,
                                          timeout=max(wait, timedelta(seconds=60)))
        except TimeoutError:
            return "no_answer"
        return self.result.result if self.result is not None else "cancelled"

    async def finish(self, block: BlockInput, state: str, detail: str = "") -> BlockStatus:
        self.status.state, self.status.detail = state, detail
        await self.record(block, state)
        workflow.logger.info("VALR briefing %s %s %s", block.block_id, state, detail)
        return self.status

    @workflow.signal
    def call_result(self, result: CallResult) -> None:
        """From the Telnyx webhook (same signal name as CallbackWorkflow's)."""
        attempts = self.status.attempts
        if (self.result is None and result.result in RESULTS and attempts
                and attempts[-1].call_control_id == result.call_control_id):
            self.result = result

    @workflow.signal
    def update_slots(self, slot_keys: list[str]) -> None:
        self.slot_keys = list(slot_keys)

    @workflow.signal
    def call_ended_signal(self) -> None:
        self.call_ended = True

    @workflow.signal
    def cancel(self) -> None:
        self.cancelled = True

    @workflow.query
    def status_query(self) -> BlockStatus:
        return self.status


# Calendar sync.

@dataclass
class SyncInput:
    reason: str  # push, sweep, manual, call.
    full: bool = False


@dataclass
class SyncSummary:
    source: str = ""
    changes: dict[str, int] = field(default_factory=dict)
    slots_started: int = 0
    blocks_started: int = 0
    blocks_cancelled: int = 0
    channel: str = ""
    error: str = ""


@workflow.defn
class CalendarSyncWorkflow:
    """One sync; pokes that arrive while it runs make it run once more."""

    def __init__(self) -> None:
        self.pokes = 0

    @workflow.run
    async def run(self, request: SyncInput) -> SyncSummary:
        await workflow.sleep(timedelta(seconds=3))  # Let a burst of notifications settle.
        summary = SyncSummary()
        for _ in range(3):
            self.pokes = 0
            summary = await workflow.execute_activity(
                "calendar_sync", request, result_type=SyncSummary,
                start_to_close_timeout=timedelta(minutes=3),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10)))
            if not self.pokes:
                break
        return summary

    @workflow.signal
    def poke(self) -> None:
        self.pokes += 1
