"""VALR research callbacks as a Temporal workflow (task queue `voice-research`).

One `ResearchCallbackWorkflow` per caller (`workflow_id`) carries a research job from
the caller's yes to the callback:

1. It waits for the call to end (`call_ended`, signalled by the app when the call
   closes; if that signal is lost, it starts anyway after CALL_END_WAIT).
2. Three researchers run at once, each its own activity on the research worker:
   web search and page reading, reviews and recent news, structured facts from
   free APIs. Each has a deadline and a slice of the job's $ budget.
3. A synthesis activity merges whatever came back (two of three is enough; one
   is still "here's what I found so far") into a spoken answer and cited notes,
   and checks each spoken sentence against the cited text. Nothing usable: an
   apology instead.
4. The result is saved, and the existing `CallbackWorkflow` is started as a child
   to call the caller at once, with its dial limits, retries and one pending
   callback per caller. `dial=False` (tests, the VM smoke test) stops before it.

The whole job must fit the promise made on the call: `job_seconds(minutes)` is the
promise minus the time to dial and ring. This module imports only the standard
library, temporalio and the callback workflow, so the sandbox can load it.
"""
import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

from app.callback_workflow import TASK_QUEUE as CALLBACK_QUEUE
from app.callback_workflow import CallbackRequest, CallbackWorkflow
from app.callback_workflow import workflow_id as callback_workflow_id

TASK_QUEUE = "voice-research"
PARTS = ("web", "reviews", "facts")
CALL_END_WAIT = timedelta(minutes=11)  # Longer than a phone call may last (TELNYX_MAX_CALL_S).
SYNTH_RESERVE_S = 40  # Researchers stop this long before the job's limit, for synthesis.
MIN_PART_S = 30
SYNTH_SHARE = 0.25  # Of the job's $ budget, kept for synthesis and the claim check.
MAX_TOPIC_CHARS = 120

APOLOGY = ("I'm sorry, I looked into {topic} but couldn't find anything reliable enough to "
           "pass on this time. You're welcome to ask me again later.")


def workflow_id(caller_key: str) -> str:
    """One pending research job per caller; hashed so the number is not the id."""
    return "research-" + hashlib.sha256(caller_key.encode()).hexdigest()[:20]


def job_seconds(minutes: int) -> int:
    # Same as research_jobs.job_seconds; repeated so the sandbox imports no psycopg.
    return minutes * 60 - 60


@dataclass
class ResearchRequest:
    job_id: str
    caller_key: str  # "tel:+E164", from the call; never from the model.
    phone_e164: str
    topic: str
    conversation_id: str
    promised_minutes: int
    timezone: str = "Asia/Kolkata"
    quiet_hours: str = ""
    job_usd: float = 0.25
    dial: bool = True


@dataclass
class PartInput:
    job_id: str
    part: str  # One of PARTS.
    topic: str
    seconds: float  # The researcher's wall-clock limit.
    budget_usd: float  # Its slice of the job's budget.


@dataclass
class PartResult:
    part: str
    ok: bool = False  # Findings with at least one sourced claim.
    findings: str = ""  # JSON: {"summary", "claims": [{"claim", "source_url"}]}.
    evidence: str = ""  # What its tools returned, for synthesis when it did not finish.
    seen_urls: list[str] = field(default_factory=list)
    usd: float = 0.0
    stopped: str = ""  # "", "deadline", "budget".
    cap_hits: dict[str, int] = field(default_factory=dict)
    error: str = ""


@dataclass
class SynthInput:
    job_id: str
    topic: str
    parts: list[PartResult]
    seconds: float
    budget_usd: float


@dataclass
class SynthResult:
    spoken: str
    notes: str  # JSON list of {"fact", "source_ids", "sensitive"}.
    sources: str  # JSON list of {"id", "url"}.
    partial: bool
    usd: float = 0.0
    cap_hits: dict[str, int] = field(default_factory=dict)


@dataclass
class FinishInput:
    job_id: str
    status: str  # done, partial, apologized, cancelled.
    duration_s: float
    spoken: str
    notes: str
    sources: str
    cap_hits: dict[str, int]
    detail: str


@dataclass
class ResearchStatus:
    state: str  # waiting_for_call_end, researching, synthesizing, calling, done, partial,
    #             apologized, cancelled.
    job_id: str
    spoken: str = ""
    duration_s: float = 0.0
    usd: float = 0.0
    detail: str = ""
    parts: dict[str, str] = field(default_factory=dict)  # part -> ok / failed / stopped.


def merge(counts: list[dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for count in counts:
        for key, value in count.items():
            total[key] = total.get(key, 0) + value
    return total


@workflow.defn
class ResearchCallbackWorkflow:
    def __init__(self) -> None:
        self.call_ended = False
        self.cancelled = False
        self.status = ResearchStatus("waiting_for_call_end", "")

    @workflow.run
    async def run(self, request: ResearchRequest) -> ResearchStatus:
        self.status.job_id = request.job_id
        topic = request.topic[:MAX_TOPIC_CHARS]
        try:
            await workflow.wait_condition(lambda: self.call_ended or self.cancelled,
                                          timeout=CALL_END_WAIT)
        except TimeoutError:
            workflow.logger.info("VALR: no call-end signal; starting anyway")
        if self.cancelled:
            await self.save(FinishInput(request.job_id, "cancelled", 0, "", "[]", "[]", {},
                                        "cancelled before the research started"))
            self.status.state = "cancelled"
            return self.status

        started = workflow.now()
        limit_s = job_seconds(request.promised_minutes)
        part_s = max(limit_s - SYNTH_RESERVE_S, MIN_PART_S)
        part_usd = request.job_usd * (1 - SYNTH_SHARE) / len(PARTS)
        self.status.state = "researching"
        parts = await asyncio.gather(*(self.research(request.job_id, part, topic, part_s, part_usd)
                                       for part in PARTS))
        usd = sum(part.usd for part in parts)
        cap_hits = merge([part.cap_hits for part in parts])
        usable = [part for part in parts if part.ok or part.evidence]

        spoken, notes, sources, state, detail = APOLOGY.format(topic=topic), "[]", "[]", "apologized", ""
        if usable:
            self.status.state = "synthesizing"
            elapsed = (workflow.now() - started).total_seconds()
            synth_s = max(limit_s - elapsed, 20)
            synth_usd = max(request.job_usd - usd, 0.02)
            try:
                result: SynthResult = await workflow.execute_activity(
                    "research_synthesize", SynthInput(request.job_id, topic, parts, synth_s, synth_usd),
                    result_type=SynthResult, start_to_close_timeout=timedelta(seconds=synth_s),
                    retry_policy=RetryPolicy(maximum_attempts=1))
                spoken, notes, sources = result.spoken, result.notes, result.sources
                usd += result.usd
                cap_hits = merge([cap_hits, result.cap_hits])
                state = "partial" if result.partial else "done"
            except ActivityError as error:
                detail = f"synthesis failed: {error.cause or error}"[:200]
                workflow.logger.warning("VALR synthesis failed: %s", detail)
        else:
            detail = "no researcher returned anything usable"

        duration = (workflow.now() - started).total_seconds()
        self.status.spoken, self.status.duration_s, self.status.usd = spoken, duration, usd
        await self.save(FinishInput(request.job_id, state, duration, spoken, notes, sources,
                                    cap_hits, detail))
        self.status.state, self.status.detail = state, detail
        if request.dial and not self.cancelled:
            self.status.detail = await self.call_back(request, topic)
            self.status.state = state
        workflow.logger.info("VALR job %s %s in %.0f s, about $%.3f", request.job_id, state,
                             duration, usd)
        return self.status

    async def research(self, job_id: str, part: str, topic: str, seconds: float,
                       budget_usd: float) -> PartResult:
        try:
            result: PartResult = await workflow.execute_activity(
                "research_part", PartInput(job_id, part, topic, seconds, budget_usd),
                result_type=PartResult,
                # A quick crash may retry, but never past the researcher's limit.
                schedule_to_close_timeout=timedelta(seconds=seconds + 10),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=2)))
        except ActivityError as error:
            workflow.logger.warning("VALR researcher %s failed: %s", part, error.cause or error)
            result = PartResult(part, error=str(error.cause or error)[:200])
        self.status.parts[part] = "ok" if result.ok else (result.stopped or "failed")
        return result

    async def save(self, finish: FinishInput) -> None:
        try:
            await workflow.execute_activity(
                "research_finish", finish, start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=5))
        except ActivityError:
            # The result is still in this workflow's history; the callback goes ahead.
            workflow.logger.warning("VALR job %s not saved", finish.job_id)

    async def call_back(self, request: ResearchRequest, topic: str) -> str:
        """Start the caller's callback now, as a child that outlives this workflow."""
        self.status.state = "calling"
        due = workflow.now() + timedelta(seconds=2)
        callback = CallbackRequest(request.caller_key, request.phone_e164, due.isoformat(),
                                   f"what I found about {topic}"[:120], request.conversation_id,
                                   request.timezone, request.quiet_hours,
                                   research_job=request.job_id)
        try:
            await workflow.start_child_workflow(
                CallbackWorkflow.run, callback, id=callback_workflow_id(request.caller_key),
                task_queue=CALLBACK_QUEUE, parent_close_policy=ParentClosePolicy.ABANDON)
        except WorkflowAlreadyStartedError:  # The caller already has a callback pending.
            workflow.logger.warning("VALR callback not started: one is already pending")
            return "the callback could not start: one is already pending"
        return "callback started"

    @workflow.signal
    def call_ended_signal(self) -> None:
        self.call_ended = True

    @workflow.signal
    def cancel(self) -> None:
        self.cancelled = True

    @workflow.query
    def status_query(self) -> ResearchStatus:
        return self.status
