"""Call the owner now with a briefing for one interview ("Call me now", and the admin CLI
`python -m app.research_worker call-now [next|<slot>]`), or for a block's interviews
(the heads-up's "tap for your briefing call" and Call me, `call_block_now`).

An on-demand BlockCallWorkflow for that one interview, signalled at once (there is no call
to wait for). If the interview's brief is not current, its slot workflow is asked to
research now, within the five-minute promise; the call goes out when the brief is ready,
and one minute before the promise at the latest. Temporal and the pure modules only, so
both the app and the research worker can import it.
"""
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from app.interview_types import SlotEvent, slot_workflow_id
from app.interview_workflow import (TASK_QUEUE, BlockCallWorkflow, BlockInput,
                                    InterviewSlotWorkflow, ResearchNow, SlotInput)

PROMISE = timedelta(minutes=5)
RESEARCH_S = 165


async def start_slot_research(client: Any, event: SlotEvent, owner_key: str, block_id: str,
                              refresh: bool, research_s: int, job_usd: float, **rpc: Any) -> None:
    """Make sure the slot's workflow runs, and ask it to research now within `research_s`."""
    workflow_id = slot_workflow_id(event.key)
    now = datetime.now(timezone.utc).isoformat()
    await client.start_workflow(
        InterviewSlotWorkflow.run, SlotInput(event, owner_key, block_id, now, job_usd),
        id=workflow_id, task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, **rpc)
    await client.get_workflow_handle(workflow_id).signal(
        InterviewSlotWorkflow.research_within, ResearchNow(research_s, refresh), **rpc)


def label_for(event: SlotEvent) -> str:
    return f"your interview with {event.company or event.title[:40]}"


async def call_now(client: Any, event: SlotEvent, owner_key: str, phone_e164: str,
                   brief_ready: bool, job_usd: float = 0.40, dials_per_day: int = 12,
                   **rpc: Any) -> str:
    """Start the call; returns its workflow id."""
    return await call_block_now(client, [event], [] if brief_ready else [event], owner_key,
                                phone_e164, label_for(event), job_usd, dials_per_day, **rpc)


async def call_block_now(client: Any, events: list[SlotEvent], todo: list[SlotEvent],
                         owner_key: str, phone_e164: str, label: str, job_usd: float = 0.40,
                         dials_per_day: int = 12, dial_at: datetime | None = None,
                         **rpc: Any) -> str:
    """Call about these interviews now, or at `dial_at`.

    `todo`: the ones whose brief is not current; they research first (within the
    five-minute promise when calling now). Returns the call workflow's id."""
    for event in todo:
        await start_slot_research(client, event, owner_key, "", False, RESEARCH_S, job_usd, **rpc)
    now = datetime.now(timezone.utc)
    keys = [event.key for event in events]
    workflow_id = ("briefing-now-" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:12]
                   + f"-{now:%H%M%S}")
    due = max(dial_at or now, now)
    request = BlockInput(workflow_id, now.isoformat(), keys, owner_key, phone_e164, label,
                         on_demand=True, dials_per_day=dials_per_day,
                         call_by_utc=(due + PROMISE).isoformat() if dial_at is None
                         else (due + timedelta(minutes=1)).isoformat(),
                         ready_keys=keys, dial_at_utc=due.isoformat() if dial_at else "")
    handle = await client.start_workflow(BlockCallWorkflow.run, request, id=workflow_id,
                                         task_queue=TASK_QUEUE, **rpc)
    await handle.signal(BlockCallWorkflow.call_ended_signal, **rpc)  # No call to wait for.
    return workflow_id


def pick(events: list[SlotEvent], ref: str, now: datetime) -> SlotEvent | None:
    """'next' (or empty): the next interview that has not started; else a slot id or key."""
    timed = [e for e in events if not e.all_day and not e.declined and e.status != "cancelled"]
    if ref in ("", "next"):
        return next((e for e in sorted(timed, key=lambda e: e.start_utc)
                     if datetime.fromisoformat(e.start_utc) > now), None)
    return next((e for e in timed if e.key == ref or slot_workflow_id(e.key) == ref), None)
