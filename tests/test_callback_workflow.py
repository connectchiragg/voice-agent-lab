"""CallbackWorkflow in Temporal's time-skipping test server: hours pass in milliseconds.

The real activities run, with Telnyx replaced by an httpx mock transport; the
test plays the app's webhook by signalling each dialled call's result.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from temporalio.client import WorkflowHandle, WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.callback_worker import Activities, DialSettings
from app.callback_workflow import (MAX_DIALS_PER_DAY, TASK_QUEUE, CallbackRequest, CallbackStatus,
                                   CallbackWorkflow, CallResult, DialBudgetWorkflow, workflow_id,
                                   workflow_runner)

ZONE = "Asia/Kolkata"
CALLER = "tel:+15550002222"


def next_local(now: datetime, hour: int, minute: int = 0) -> datetime:
    """The next hour:minute in ZONE after `now`, a day ahead at least an hour away."""
    local = now.astimezone(ZoneInfo(ZONE))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while due <= local + timedelta(hours=1):
        due += timedelta(days=1)
    return due.astimezone(timezone.utc)


class FakeTelnyx:
    """Telnyx's Dial endpoint: records each dial and returns a new call_control_id."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.dials: list[dict] = []
        self.dialled: asyncio.Queue[str] = asyncio.Queue()

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(dial["command_id"] == body["command_id"] for dial in self.dials):
            raise AssertionError("dialled twice for one attempt")
        self.dials.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"errors": [{"title": "nope"}]})
        call_id = f"v3:call-{len(self.dials)}"
        self.dialled.put_nowait(call_id)
        return httpx.Response(200, json={"data": {"call_control_id": call_id}})


def run_with_worker(test, telnyx: FakeTelnyx | None = None):
    """Run `test(env, telnyx)` against a fresh time-skipping server and worker."""
    telnyx = telnyx or FakeTelnyx()

    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            http = httpx.AsyncClient(transport=httpx.MockTransport(telnyx.handler))
            activities = Activities(env.client, DialSettings("test-key"), http)
            async with Worker(env.client, task_queue=TASK_QUEUE,
                              workflows=[CallbackWorkflow, DialBudgetWorkflow], workflow_runner=workflow_runner(),
                              activities=[activities.reserve_dial_slot, activities.place_call]):
                await test(env, telnyx)
    asyncio.run(main())
    return telnyx


async def start(env: WorkflowEnvironment, due: datetime, caller: str = CALLER,
                quiet_hours: str = "") -> WorkflowHandle:
    request = CallbackRequest(caller, caller.removeprefix("tel:"), due.isoformat(),
                              "the weather in Mysuru", "conversation-1", ZONE, quiet_hours)
    return await env.client.start_workflow(CallbackWorkflow.run, request, id=workflow_id(caller),
                                           task_queue=TASK_QUEUE)


async def dial_after(env: WorkflowEnvironment, telnyx: FakeTelnyx, skip: timedelta) -> str:
    """Skip `skip` of workflow time, then wait (in real time) for the dial it brings."""
    await env.sleep(skip)
    return await asyncio.wait_for(telnyx.dialled.get(), 10)


async def answer(handle: WorkflowHandle, call_id: str, result: str) -> None:
    """Plays the app's webhook: the call's result."""
    await handle.signal(CallbackWorkflow.call_result, CallResult(call_id, result))

# Time skips only on env.sleep in these tests: with automatic skipping, a call's
# 3-minute result timeout could expire before the test's webhook signal arrives.


def test_the_timer_fires_and_places_the_call():
    async def test(env, telnyx):
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            due = next_local(now, 17)
            handle = await start(env, due)
            assert (await handle.query(CallbackWorkflow.status_query)).state == "scheduled"
            await env.sleep(due - now - timedelta(seconds=10))
            assert telnyx.dials == []  # Not yet.
            await answer(handle, await dial_after(env, telnyx, timedelta(seconds=11)), "answered")
            status: CallbackStatus = await handle.result()
        assert status.state == "completed"
        assert [attempt.result for attempt in status.attempts] == ["answered"]
        placed = datetime.fromisoformat(status.attempts[0].placed_at_utc)
        assert due <= placed < due + timedelta(seconds=5)
        dial = telnyx.dials[0]
        assert (dial["to"], dial["from"], dial["connection_id"]) == (
            "+15550002222", "+12288888007", "3071391970178893742")
    assert len(run_with_worker(test).dials) == 1


def test_no_answer_retries_every_15_minutes_then_fails():
    async def test(env, telnyx):
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            due = next_local(now, 10)
            handle = await start(env, due)
            skip = due - now + timedelta(seconds=1)
            for result in ("no_answer", "busy", "no_answer"):
                await answer(handle, await dial_after(env, telnyx, skip), result)
                skip = timedelta(minutes=15, seconds=1)
            status = await handle.result()
        assert status.state == "failed" and "3 attempts" in status.detail
        assert [attempt.result for attempt in status.attempts] == ["no_answer", "busy", "no_answer"]
        placed = [datetime.fromisoformat(attempt.placed_at_utc) for attempt in status.attempts]
        assert all(timedelta(minutes=15) <= later - earlier < timedelta(minutes=16)
                   for earlier, later in zip(placed, placed[1:]))
    assert len(run_with_worker(test).dials) == 3


def test_an_unreported_call_counts_as_no_answer_and_a_retry_can_succeed():
    async def test(env, telnyx):
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            due = next_local(now, 10)
            handle = await start(env, due)
            await dial_after(env, telnyx, due - now + timedelta(seconds=1))  # No webhook comes.
            retry = await dial_after(env, telnyx, timedelta(minutes=3 + 15, seconds=2))
            await answer(handle, "v3:call-1", "answered")  # Too late, for the old call: ignored.
            await answer(handle, retry, "answered")
            status = await handle.result()
        assert [attempt.result for attempt in status.attempts] == ["no_answer", "answered"]
        assert status.state == "completed"
    run_with_worker(test)


def test_reschedule_moves_the_timer():
    async def test(env, telnyx):
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            handle = await start(env, next_local(now, 10))
            later = next_local(now, 15) + timedelta(days=1)
            status = await handle.execute_update(CallbackWorkflow.reschedule, later.isoformat())
            assert status.state == "scheduled" and status.due_at_utc == later.isoformat()
            await env.sleep(later - now - timedelta(seconds=10))
            assert telnyx.dials == []  # The old time passed without a call.
            await answer(handle, await dial_after(env, telnyx, timedelta(seconds=11)), "answered")
            status = await handle.result()
        placed = datetime.fromisoformat(status.attempts[0].placed_at_utc)
        assert later <= placed < later + timedelta(seconds=5)
    assert len(run_with_worker(test).dials) == 1


def test_cancel_ends_it_without_calling():
    async def test(env, telnyx):
        handle = await start(env, next_local(await env.get_current_time(), 10))
        await handle.signal(CallbackWorkflow.cancel)
        status = await handle.result()
        assert status.state == "cancelled" and status.attempts == []
    assert run_with_worker(test).dials == []


def test_cancel_while_ringing_ends_at_once():
    async def test(env, telnyx):
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            due = next_local(now, 10)
            handle = await start(env, due)
            await dial_after(env, telnyx, due - now + timedelta(seconds=1))
            await handle.signal(CallbackWorkflow.cancel)
            status = await handle.result()
        assert status.state == "cancelled"
        assert [attempt.result for attempt in status.attempts] == ["cancelled"]
    assert len(run_with_worker(test).dials) == 1


def test_quiet_hours_when_booked_with_them_are_refused_at_start_and_on_reschedule():
    async def test(env, telnyx):
        now = await env.get_current_time()
        night = await start(env, next_local(now, 22, 30), caller="tel:+15550000001",
                            quiet_hours="21-9")
        status = await night.result()
        assert status.state == "rejected" and "quiet hours (9 pm to 9 am" in status.detail
        handle = await start(env, next_local(now, 10), quiet_hours="21-9")
        for early in (next_local(now, 7), next_local(now, 21), now - timedelta(minutes=1)):
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(CallbackWorkflow.reschedule, early.isoformat())
        assert (await handle.query(CallbackWorkflow.status_query)).state == "scheduled"
        await handle.signal(CallbackWorkflow.cancel)
        await handle.result()
    assert run_with_worker(test).dials == []


def test_without_quiet_hours_a_night_time_is_accepted_and_movable():
    async def test(env, telnyx):
        now = await env.get_current_time()
        handle = await start(env, next_local(now, 22, 30))  # Quiet hours off (the default).
        status = await handle.execute_update(CallbackWorkflow.reschedule,
                                             next_local(now, 3).isoformat())
        assert status.state == "scheduled"
        with pytest.raises(WorkflowUpdateFailedError):  # The past is still refused.
            await handle.execute_update(CallbackWorkflow.reschedule,
                                        (now - timedelta(minutes=1)).isoformat())
        await handle.signal(CallbackWorkflow.cancel)
        assert (await handle.result()).state == "cancelled"
    assert run_with_worker(test).dials == []


def test_one_pending_callback_per_caller():
    async def test(env, telnyx):
        due = next_local(await env.get_current_time(), 10)
        handle = await start(env, due)
        with pytest.raises(Exception, match="already"):
            await start(env, due)
        await handle.signal(CallbackWorkflow.cancel)
        await handle.result()
    run_with_worker(test)


def test_a_refused_dial_is_not_retried():
    async def test(env, telnyx):
        status = await (await start(env, next_local(await env.get_current_time(), 10))).result()
        # 422 (e.g. an unverified number on a trial account) on each of the 3 attempts.
        assert [attempt.result for attempt in status.attempts] == ["failed"] * 3
    assert len(run_with_worker(test, FakeTelnyx(status=422)).dials) == 3


def test_at_most_five_dials_a_day_for_everyone():
    async def test(env, telnyx):
        callers = [f"tel:+1555000000{index}" for index in range(4)]
        by_id = {}
        with env.auto_time_skipping_disabled():
            now = await env.get_current_time()
            due = next_local(now, 10)
            for caller in callers:
                by_id[workflow_id(caller)] = await start(env, due, caller=caller)
            await env.sleep(due - now + timedelta(seconds=1))
            # Nobody answers: 4 first calls and 1 retry fit in the day, the 6th does not.
            for _ in range(4):
                await reply_no_answer(telnyx, by_id)
            await env.sleep(timedelta(minutes=15, seconds=1))
            await reply_no_answer(telnyx, by_id)
            await env.sleep(timedelta(minutes=15, seconds=1))
            statuses = [await handle.result() for handle in by_id.values()]
        results = [attempt.result for status in statuses for attempt in status.attempts]
        # Refused: three second attempts, then the one caller's third attempt.
        assert results.count("daily_limit") == 4
        assert len(telnyx.dials) == MAX_DIALS_PER_DAY
    run_with_worker(test)


async def reply_no_answer(telnyx: FakeTelnyx, by_id: dict[str, WorkflowHandle]) -> None:
    call_id = await asyncio.wait_for(telnyx.dialled.get(), 10)
    dial = telnyx.dials[int(call_id.rsplit("-", 1)[1]) - 1]
    await answer(by_id[dial["command_id"].rsplit("-", 1)[0]], call_id, "no_answer")
