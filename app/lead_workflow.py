"""The lead tracker's Temporal workflows (task queue `voice-research`, the research worker).

- `GmailSyncWorkflow`: one Gmail pass (activity `gmail_sync`: history.list from the stored
  historyId, or the 30-day backfill the first time), then a lead rebuild (`leads_refresh`:
  calendar interviews, Fathom recordings and interview scores merged in, stages,
  temperatures, expiry). Started by each Pub/Sub push (signal-with-start on the id
  `valr-gmail-sync`: a burst of pushes makes one more pass, not many) and by the Temporal
  Schedule `valr-gmail-poll` every 30 minutes, the safety net for missed pushes.
- `GmailWatchWorkflow`: renews users.watch (activity `gmail_watch`). The Schedule
  `valr-gmail-watch` runs it once a day; Google ends a watch after 7 days.

Workflow code reads time only through workflow.now() and does all I/O in activities.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

TASK_QUEUE = "voice-research"
SYNC_WORKFLOW_ID = "valr-gmail-sync"
POLL_SCHEDULE = "valr-gmail-poll"
WATCH_SCHEDULE = "valr-gmail-watch"


@dataclass
class GmailSyncInput:
    reason: str  # push | poll | manual | backfill
    history_id: int = 0  # From the push, when there is one (skip if already processed).


@dataclass
class GmailSyncSummary:
    mode: str = ""  # history | backfill | recovery | skipped
    fetched: int = 0
    kept: int = 0
    dropped: int = 0
    deferred: int = 0
    llm_calls: int = 0
    usd: float = 0.0
    error: str = ""


@dataclass
class RefreshSummary:
    leads: int = 0
    active: int = 0
    expired: int = 0
    touches: int = 0
    sources: dict[str, int] = field(default_factory=dict)


@dataclass
class WatchSummary:
    expires: str = ""
    history_id: int = 0
    error: str = ""


@workflow.defn
class GmailSyncWorkflow:
    """One pass; pushes that arrive while it runs make it run once more."""

    def __init__(self) -> None:
        self.pokes = 0

    @workflow.run
    async def run(self, request: GmailSyncInput) -> GmailSyncSummary:
        await workflow.sleep(timedelta(seconds=2))  # Let a burst of pushes settle.
        summary = GmailSyncSummary()
        for _ in range(3):
            self.pokes = 0
            summary = await workflow.execute_activity(
                "gmail_sync", request, result_type=GmailSyncSummary,
                start_to_close_timeout=timedelta(minutes=20), heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=15)))
            if not self.pokes:
                break
        await workflow.execute_activity(
            "leads_refresh", request.reason, result_type=RefreshSummary,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10)))
        return summary

    @workflow.signal
    def poke(self) -> None:
        self.pokes += 1


@workflow.defn
class GmailWatchWorkflow:
    @workflow.run
    async def run(self, reason: str) -> WatchSummary:
        return await workflow.execute_activity(
            "gmail_watch", reason, result_type=WatchSummary,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=5, initial_interval=timedelta(minutes=1)))
