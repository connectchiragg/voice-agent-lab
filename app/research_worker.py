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


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "crewai", "LiteLLM", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: VALR research cannot run.")
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    async with await connect() as connection:
        await research_jobs.setup(connection)
    client = await Client.connect(address, namespace=namespace)
    # Three researchers of one job at a time, plus synthesis and saving.
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[ResearchCallbackWorkflow],
                    workflow_runner=workflow_runner(), activities=ACTIVITIES,
                    activity_executor=ThreadPoolExecutor(4), max_concurrent_activities=4)
    logger.info("VALR research worker on %s, namespace %s, task queue %s; limits %s",
                address, namespace, TASK_QUEUE, asdict(research_jobs.Limits.from_env()))
    await worker.run()


if __name__ == "__main__":
    if sys.argv[1:2] == ["test"] and len(sys.argv) == 3:
        logging.basicConfig(level=logging.WARNING)
        asyncio.run(test_job(sys.argv[2]))
    elif len(sys.argv) > 1:
        sys.exit('usage: python -m app.research_worker [test "<topic>"]')
    else:
        asyncio.run(main())
