"""GmailSyncWorkflow: a pass, one more pass when poked meanwhile, then the lead rebuild."""
import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

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


def test_a_rate_limited_or_capped_pass_resumes_after_a_wait_until_done():
    calls = []
    results = [GmailSyncSummary(mode="paused-ratelimit", more=True, error="rate limited, resuming"),
               GmailSyncSummary(mode="backfill", fetched=150, more=True, left=10),
               GmailSyncSummary(mode="backfill", fetched=10)]

    @activity.defn(name="gmail_sync")
    async def gmail_sync(request: GmailSyncInput) -> GmailSyncSummary:
        calls.append(("sync", request.reason))
        return results[sum(1 for c in calls if c[0] == "sync") - 1]

    @activity.defn(name="leads_refresh")
    async def leads_refresh(reason: str) -> RefreshSummary:
        calls.append(("refresh", reason))
        return RefreshSummary()

    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[GmailSyncWorkflow],
                              workflow_runner=workflow_runner(), activities=[gmail_sync, leads_refresh]):
                handle = await env.client.start_workflow(GmailSyncWorkflow.run, GmailSyncInput("poll"),
                                                         id="valr-gmail-poll-run", task_queue=TASK_QUEUE)
                result = await handle.result()  # Follows the continue-as-new runs.
                assert result.mode == "backfill" and result.fetched == 10 and not result.more
                # The first run (patch marker, 2-minute timer, continue-as-new) replays cleanly.
                first = env.client.get_workflow_handle("valr-gmail-poll-run", run_id=handle.result_run_id)
                history = await first.fetch_history()
                assert any(e.HasField("marker_recorded_event_attributes") for e in history.events)
                timers = [e.timer_started_event_attributes.start_to_fire_timeout.seconds for e in history.events
                          if e.HasField("timer_started_event_attributes")]
                assert timers == [2, 120]  # The push settle, then the rate-limit pause.
                await Replayer(workflows=[GmailSyncWorkflow], workflow_runner=workflow_runner()).replay_workflow(history)

    asyncio.run(main())
    assert calls == [("sync", "poll"), ("refresh", "poll"), ("sync", "resume"), ("refresh", "resume"),
                     ("sync", "resume"), ("refresh", "resume")]


@workflow.defn(name="GmailSyncWorkflow")
class Before:  # GmailSyncWorkflow as deployed at bc1d438.
    def __init__(self) -> None:
        self.pokes = 0

    @workflow.run
    async def run(self, request: GmailSyncInput) -> GmailSyncSummary:
        await workflow.sleep(timedelta(seconds=2))
        summary = GmailSyncSummary()
        for _ in range(3):
            self.pokes = 0
            summary = await workflow.execute_activity(
                "gmail_sync", request, result_type=GmailSyncSummary, start_to_close_timeout=timedelta(minutes=20))
            if not self.pokes:
                break
        await workflow.execute_activity("leads_refresh", request.reason, result_type=RefreshSummary,
                                        start_to_close_timeout=timedelta(minutes=3))
        return summary

    @workflow.signal
    def poke(self) -> None:
        self.pokes += 1



def test_a_workflow_started_before_the_patch_replays_on_the_new_code():
    @activity.defn(name="gmail_sync")
    async def gmail_sync(request: GmailSyncInput) -> GmailSyncSummary:
        return GmailSyncSummary(mode="paused-ratelimit", more=True)  # The new activity, the old workflow.

    @activity.defn(name="leads_refresh")
    async def leads_refresh(reason: str) -> RefreshSummary:
        return RefreshSummary()

    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[Before],
                              workflow_runner=workflow_runner(), activities=[gmail_sync, leads_refresh]):
                handle = await env.client.start_workflow(Before.run, GmailSyncInput("poll"), id="old",
                                                         task_queue=TASK_QUEUE)
                await handle.result()
                history = await handle.fetch_history()
        await Replayer(workflows=[GmailSyncWorkflow], workflow_runner=workflow_runner()).replay_workflow(history)

    asyncio.run(main())
