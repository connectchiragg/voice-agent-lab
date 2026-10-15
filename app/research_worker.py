"""The VALR research worker: `python -m app.research_worker` (container `voice-research`).

Runs ResearchCallbackWorkflow and its activities on task queue `voice-research`, from
the `worker` image target (Python 3.13 with crewai; the app image stays without it).
The only process with ANTHROPIC_API_KEY in use: the live call path never calls Claude.

Activities:

- `research_part`: one researcher (app/research_agents.py) in a thread, heartbeating;
  on timeout or cancel its next LLM call stops it. Its usage goes to `research_spend`.
- `research_synthesize`: the spoken answer and notes, plus the claim check.
- `research_finish`: saves the result and the job's duration to `research_jobs`.

The callback itself is the existing CallbackWorkflow, on the callback worker's queue.
"""
import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app import research_jobs  # noqa: E402
from app.callback_workflow import workflow_runner  # noqa: E402
from app.research_workflow import (TASK_QUEUE, FinishInput, PartInput, PartResult,  # noqa: E402
                                   ResearchCallbackWorkflow, SynthInput, SynthResult)

logger = logging.getLogger("valr")
HEARTBEAT_S = 5


async def connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True)


async def save_spend(job_id: str, rows: list[dict]) -> float:
    """Store a step's usage; returns its $. Never raises (the job goes on)."""
    if not rows:
        return 0.0
    try:
        async with await connect() as connection:
            return await research_jobs.record_spend(connection, job_id, rows)
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR spend not stored: %s", type(error).__name__)
        return sum(research_jobs.price(r["model"], r["input_tokens"], r["output_tokens"],
                                       r["cache_read"], r["cache_write"], r["searches"]) for r in rows)


async def month_over_cap() -> bool:
    limits = research_jobs.Limits.from_env()
    try:
        async with await connect() as connection:
            return await research_jobs.month_spend(connection) >= limits.month_usd
    except Exception:  # noqa: BLE001 - booking already checked; do not block on a blip
        return False


async def heartbeat_while(work: asyncio.Future) -> None:
    while not work.done():
        activity.heartbeat()
        await asyncio.wait([work], timeout=HEARTBEAT_S)


@activity.defn(name="research_part")
async def research_part(task: PartInput) -> PartResult:
    from app import research_agents  # Heavy: loaded on first use, not at worker start.
    if await month_over_cap():
        logger.warning("VALR researcher %s skipped: the monthly cap is reached", task.part)
        return PartResult(task.part, stopped="month_cap")
    async with await connect() as connection:
        await research_jobs.mark_started(connection, task.job_id)
    meter = research_agents.Meter(task.part)
    box: list = []
    work = asyncio.ensure_future(asyncio.to_thread(
        research_agents.run_part, task.part, task.topic, task.seconds, task.budget_usd, meter, box))
    try:
        await heartbeat_while(work)
        outcome = await work
    except asyncio.CancelledError:
        for ctx in box:
            ctx.killed = True  # Its thread stops at the next LLM call.
        await save_spend(task.job_id, meter.rows)
        raise
    usd = await save_spend(task.job_id, meter.rows)
    logger.info("VALR researcher %s: %s, %d sourced claims, $%.4f%s", task.part,
                "ok" if outcome.ok else "no findings",
                len((outcome.findings or {}).get("claims", [])), usd,
                f", stopped ({outcome.stopped})" if outcome.stopped else "")
    return PartResult(task.part, outcome.ok, json.dumps(outcome.findings) if outcome.findings else "",
                      outcome.evidence, outcome.seen_urls, usd, outcome.stopped,
                      dict(meter.cap_hits), outcome.error)


@activity.defn(name="research_synthesize")
async def research_synthesize(task: SynthInput) -> SynthResult:
    from app import research_agents
    parts = [{"part": p.part, "ok": p.ok, "findings": json.loads(p.findings) if p.findings else None,
              "evidence": p.evidence} for p in task.parts]
    known = {url for p in task.parts for url in p.seen_urls}
    meter = research_agents.Meter("synth")
    work = asyncio.ensure_future(asyncio.to_thread(
        research_agents.synthesize, task.topic, parts, known, task.seconds, meter))
    try:
        await heartbeat_while(work)
        outcome = await work
    finally:
        usd = await save_spend(task.job_id, meter.rows)
    logger.info("VALR synthesis: %d words, %d notes, $%.4f", len(outcome.spoken.split()),
                len(outcome.notes), usd)
    return SynthResult(outcome.spoken, json.dumps(outcome.notes), json.dumps(outcome.sources),
                       outcome.partial, usd, dict(meter.cap_hits))


@activity.defn(name="research_finish")
async def research_finish(task: FinishInput) -> None:
    async with await connect() as connection:
        await research_jobs.finish(connection, task.job_id, task.status, task.duration_s,
                                   task.spoken, json.loads(task.notes or "[]"),
                                   json.loads(task.sources or "[]"), task.cap_hits, task.detail)
        spend = await research_jobs.job_spend(connection, task.job_id)
    logger.info("VALR job %s %s: %.0f s, $%.4f, cap hits %s", task.job_id, task.status,
                task.duration_s, spend, json.dumps(task.cap_hits, sort_keys=True))


ACTIVITIES = [research_part, research_synthesize, research_finish]
TEST_CALLER = "tel:+15550100000"  # A fictitious number (555-01xx); test jobs never dial.


async def test_job(topic: str) -> None:
    """`python -m app.research_worker test "<topic>"`: one real job, end to end, for a fake
    caller and with the callback dial disabled. Counts toward the spend and day limits."""
    import uuid
    from app.research_workflow import ResearchCallbackWorkflow, ResearchRequest, workflow_id
    limits = research_jobs.Limits.from_env()
    job_id = "test-" + uuid.uuid4().hex[:11]
    async with await connect() as connection:
        await research_jobs.setup(connection)
        minutes = await research_jobs.current_promise(connection)
        refusal = await research_jobs.reserve(
            connection, research_jobs.Booking(job_id, TEST_CALLER, topic, minutes), limits)
    if refusal:
        sys.exit(f"Refused by the limits: {refusal}")
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
                                  namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
    request = ResearchRequest(job_id, TEST_CALLER, TEST_CALLER.removeprefix("tel:"), topic,
                              "test", minutes, job_usd=limits.job_usd, dial=False)
    handle = await client.start_workflow(ResearchCallbackWorkflow.run, request,
                                         id=workflow_id(TEST_CALLER) + "-" + job_id, task_queue=TASK_QUEUE)
    await handle.signal(ResearchCallbackWorkflow.call_ended_signal)  # The "call" has ended.
    status = await handle.result()
    async with await connect() as connection:
        spend = await research_jobs.job_spend(connection, job_id)
        job = await research_jobs.load(connection, job_id)
    print(json.dumps({"job_id": job_id, "promised_minutes": minutes, "state": status.state,
                      "duration_s": round(status.duration_s, 1), "spend_usd": round(spend, 4),
                      "parts": status.parts, "detail": status.detail,
                      "spoken_words": len(status.spoken.split())}, indent=2))
    print("SPOKEN:", status.spoken)
    for note in (job or {}).get("notes", []):
        print(" -", note["fact"], note["source_ids"])


SWEEP_SCHEDULE = "valr-calendar-sweep"
SYNC_WORKFLOW = "valr-calendar-sync"


async def ensure_sweep(client: Client) -> None:
    """The 6-hourly safety sweep of the calendar (also renews the push channel)."""
    from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError,
                                   ScheduleIntervalSpec, ScheduleOverlapPolicy, SchedulePolicy,
                                   ScheduleSpec)
    from app.interview_workflow import CalendarSyncWorkflow, SyncInput
    hours = float(os.getenv("VALR_CALENDAR_SWEEP_HOURS", "6"))
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(CalendarSyncWorkflow.run, SyncInput("sweep"),
                                           id="valr-calendar-sweep-run", task_queue=TASK_QUEUE),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=hours))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP))
    try:
        await client.create_schedule(SWEEP_SCHEDULE, schedule, trigger_immediately=True)
        logger.info("VALR calendar sweep scheduled every %g h", hours)
    except ScheduleAlreadyRunningError:
        pass


async def start_sync(client: Client, reason: str, full: bool = False):
    """Sync now (signal-with-start: a sync already running makes one more pass)."""
    from temporalio.common import WorkflowIDConflictPolicy
    from app.interview_workflow import CalendarSyncWorkflow, SyncInput
    return await client.start_workflow(
        CalendarSyncWorkflow.run, SyncInput(reason, full), id=SYNC_WORKFLOW, task_queue=TASK_QUEUE,
        start_signal="poke", id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)


async def temporal_client() -> Client:
    return await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
                                namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))


async def show_blocks(start_workflows: bool) -> None:
    """`sync`: run a sync and list the upcoming blocks and their workflows.
    `blocks`: a dry run: parse the calendar and list blocks, starting nothing.
    Titles and times only: no attendee emails, names or descriptions."""
    from zoneinfo import ZoneInfo
    from app import calendar_sync, interview_worker
    from app.interview_types import group_blocks
    from app.interview_workflow import BlockCallWorkflow, InterviewSlotWorkflow
    from app.interview_types import slot_workflow_id
    zone = ZoneInfo(os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata"))
    now = datetime.now(timezone.utc)
    client = await temporal_client() if start_workflows else None
    if client is not None:
        summary = await (await start_sync(client, "manual")).result()
        print(f"sync: source {summary.source}; changes {summary.changes}; slots started "
              f"{summary.slots_started}; blocks started {summary.blocks_started}, cancelled "
              f"{summary.blocks_cancelled}; channel {summary.channel or '-'}"
              f"{'; error ' + summary.error if summary.error else ''}")
        async with await connect() as connection:
            events = await calendar_sync.upcoming(connection, now - timedelta(hours=1),
                                                  now + interview_worker.HORIZON)
    else:
        import httpx
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            response = await http.get(os.environ["VALR_CALENDAR_ICS_URL"])
            response.raise_for_status()
        events = [e for e in calendar_sync.from_ics_text(response.text, interview_worker.owner_emails())
                  if calendar_sync.in_window(e, now)]
        print(f"dry run (ICS): {len(events)} events in the window; attendee roles "
              f"{await interview_worker.guard_report(events)}")
    by_key = {event.key: event for event in events}
    for block in group_blocks(events):
        start = datetime.fromisoformat(block.start_utc)
        if start <= now:
            continue
        print(f"{block.block_id}  {start.astimezone(zone):%a %d %b %I:%M %p}  "
              f"research {(start - timedelta(minutes=60)).astimezone(zone):%I:%M %p}, calls "
              f"{', '.join(f'{(start - timedelta(minutes=m)).astimezone(zone):%I:%M}' for m in (15, 10, 5))}")
        for key in block.slot_keys:
            event = by_key[key]
            line = f"    {datetime.fromisoformat(event.start_utc).astimezone(zone):%I:%M %p}  {event.title[:70]}"
            if client is not None:
                try:
                    slot = await client.get_workflow_handle(slot_workflow_id(key)).query(
                        InterviewSlotWorkflow.status_query)
                    line += f"  [slot: {slot.state}]"
                except Exception:  # noqa: BLE001
                    line += "  [slot: none]"
            print(line)
        if client is not None:
            try:
                status = await client.get_workflow_handle(block.block_id).query(
                    BlockCallWorkflow.status_query)
                print(f"    call workflow: {status.state}")
            except Exception:  # noqa: BLE001
                print("    call workflow: none")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "crewai", "LiteLLM", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: VALR research cannot run.")
    from app.interview_worker import InterviewActivities
    from app.interview_workflow import BlockCallWorkflow, CalendarSyncWorkflow, InterviewSlotWorkflow
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    async with await connect() as connection:
        await research_jobs.setup(connection)
    client = await Client.connect(address, namespace=namespace)
    interviews = InterviewActivities(client)
    # Researchers of one job at a time, plus synthesis, saving and the calendar bookkeeping.
    worker = Worker(client, task_queue=TASK_QUEUE,
                    workflows=[ResearchCallbackWorkflow, InterviewSlotWorkflow, BlockCallWorkflow,
                               CalendarSyncWorkflow],
                    workflow_runner=workflow_runner(), activities=ACTIVITIES + interviews.all(),
                    activity_executor=ThreadPoolExecutor(6), max_concurrent_activities=6)
    logger.info("VALR research worker on %s, namespace %s, task queue %s; topic research limits %s; interview "
                "limits %s", address, namespace, TASK_QUEUE, asdict(research_jobs.Limits.from_env()),
                asdict(research_jobs.Limits.interview_from_env()))
    if os.getenv("VALR_BRIEFINGS", "1") == "1" and (os.getenv("VALR_CALENDAR_ICS_URL")
                                                     or os.getenv("GOOGLE_OAUTH_CLIENT_ID")):
        await ensure_sweep(client)
    else:
        logger.info("VALR interview briefings are off (VALR_BRIEFINGS=0 or no calendar source)")
    await worker.run()


USAGE = 'usage: python -m app.research_worker [test "<topic>" | sync | blocks]'

if __name__ == "__main__":
    if sys.argv[1:2] == ["test"] and len(sys.argv) == 3:
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(test_job(sys.argv[2]))
    elif sys.argv[1:] in (["sync"], ["blocks"]):
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(show_blocks(sys.argv[1] == "sync"))
    elif len(sys.argv) > 1:
        sys.exit(USAGE)
    else:
        asyncio.run(main())
