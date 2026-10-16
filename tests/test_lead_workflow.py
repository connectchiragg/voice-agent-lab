"""GmailSyncWorkflow: a pass, one more pass when poked meanwhile, then the lead rebuild."""
import asyncio

from temporalio import activity
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.callback_workflow import workflow_runner
from app.lead_workflow import (TASK_QUEUE, GmailSyncInput, GmailSyncSummary, GmailSyncWorkflow,
                               GmailWatchWorkflow, RefreshSummary, WatchSummary)


def test_pokes_make_one_more_pass_and_the_refresh_runs_once():
    calls = []
    gate = asyncio.Event()

    @activity.defn(name="gmail_sync")
    async def gmail_sync(request: GmailSyncInput) -> GmailSyncSummary:
        calls.append(("sync", request.reason))
        if len(calls) == 1:
            await gate.wait()
        return GmailSyncSummary(mode="history", fetched=1)

    @activity.defn(name="leads_refresh")
    async def leads_refresh(reason: str) -> RefreshSummary:
        calls.append(("refresh", reason))
        return RefreshSummary(leads=1)

    @activity.defn(name="gmail_watch")
    async def gmail_watch(reason: str) -> WatchSummary:
        calls.append(("watch", reason))
        return WatchSummary(expires="2026-10-23T00:00:00+00:00")

    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[GmailSyncWorkflow, GmailWatchWorkflow],
                              workflow_runner=workflow_runner(), activities=[gmail_sync, leads_refresh, gmail_watch]):
                start = dict(id="valr-gmail-sync", task_queue=TASK_QUEUE, start_signal="poke",
                             id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
                handle = await env.client.start_workflow(GmailSyncWorkflow.run, GmailSyncInput("push", 10), **start)
                while not calls:
                    await asyncio.sleep(0.05)
                for _ in range(3):  # A burst of pushes while the first pass runs.
                    await env.client.start_workflow(GmailSyncWorkflow.run, GmailSyncInput("push", 11), **start)
                gate.set()
                result = await handle.result()
                assert result.fetched == 1
                watch = await env.client.execute_workflow(GmailWatchWorkflow.run, "schedule", id="w", task_queue=TASK_QUEUE)
                assert watch.expires.startswith("2026-10-23")
        assert calls == [("sync", "push"), ("sync", "push"), ("refresh", "push"), ("watch", "schedule")]

    asyncio.run(main())
