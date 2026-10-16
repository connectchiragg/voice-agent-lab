"""InterviewSlotWorkflow and BlockCallWorkflow in Temporal's time-skipping server.

The activities are small fakes registered under their names; they record what was asked
and when (in workflow time), so the timers can be checked: research at T-60, calls at
T-15, T-10 and T-5, nothing after an answer, and only the targeted work after a diff.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.callback_workflow import TASK_QUEUE as CALLBACK_QUEUE
from app.callback_workflow import CallResult, DialSlotRequest, PlaceCallInput, workflow_runner
from app.interview_types import Person, SlotDiff, SlotEvent
from app.interview_workflow import (TASK_QUEUE, BlockCallWorkflow, BlockInput, BlockRecord,
                                    InterviewPartInput, InterviewPartResult, InterviewSlotWorkflow,
                                    PlanInput, PlanResult, SlotFinish, SlotInput, SlotPlan,
                                    SlotSynthInput, SlotSynthResult, SlotUpdate)

OWNER = "tel:+15550100001"


class Fakes:
    def __init__(self, env: WorkflowEnvironment, answer_on: int | None = None, cache_hit=False,
                 ready=True, paused=False):
        self.env = env
        self.paused = paused  # The dashboard's pause switch, as dials_paused reads it.
        self.answer_on = answer_on
        self.cache_hit = cache_hit
        self.is_ready = ready
        self.plans: list[tuple[datetime, PlanInput]] = []
        self.parts: list[InterviewPartInput] = []
        self.synths: list[SlotSynthInput] = []
        self.dials: list[tuple[datetime, PlaceCallInput]] = []
        self.budgets: list[DialSlotRequest] = []
        self.records: list[BlockRecord] = []

    def research(self):
        @activity.defn(name="slot_plan")
        async def slot_plan(plan: PlanInput) -> PlanResult:
            self.plans.append((await self.env.get_current_time(), plan))
            if self.cache_hit:
                return PlanResult("", [], ["slot"])
            parts = [p for p in plan.parts if p != "resynth"]
            return PlanResult(f"job{len(self.plans)}", parts, [])

        @activity.defn(name="interview_part")
        async def interview_part(task: InterviewPartInput) -> InterviewPartResult:
            self.parts.append(task)
            return InterviewPartResult(task.part, True, '{"summary": "s", "claims": []}', "", [],
                                       0.01, researched=[p.email for p in task.interviewers])

        @activity.defn(name="slot_synthesize")
        async def slot_synthesize(task: SlotSynthInput) -> SlotSynthResult:
            self.synths.append(task)
            return SlotSynthResult("done", 0.02, 180)

        @activity.defn(name="slot_finish")
        async def slot_finish(task: SlotFinish) -> None:
            return None

        @activity.defn(name="slots_ready")
        async def slots_ready(keys: list[str]) -> bool:
            return self.is_ready

        @activity.defn(name="block_record")
        async def block_record(record: BlockRecord) -> None:
            self.records.append(record)

        @activity.defn(name="dials_paused")
        async def dials_paused() -> bool:
            return self.paused

        return [slot_plan, interview_part, slot_synthesize, slot_finish, slots_ready, block_record,
                dials_paused]

    def calls(self, client):
        @activity.defn(name="reserve_dial_slot")
        async def reserve_dial_slot(request: DialSlotRequest) -> bool:
            self.budgets.append(request)
            return True

        @activity.defn(name="place_call")
        async def place_call(request: PlaceCallInput) -> str:
            self.dials.append((await self.env.get_current_time(), request))
            return f"call-{request.attempt}"

        return [reserve_dial_slot, place_call]


async def drive(env, handle: WorkflowHandle, fakes: Fakes):
    """Answer the dials as the Telnyx webhook would, stepping time explicitly."""
    reported = 0
    for _ in range(400):
        while reported < len(fakes.dials):
            _, request = fakes.dials[reported]
            reported += 1
            result = "answered" if request.attempt == fakes.answer_on else "no_answer"
            await handle.signal(BlockCallWorkflow.call_result,
                                CallResult(f"call-{request.attempt}", result))
        if (await handle.describe()).status.name != "RUNNING":
            return await handle.result()
        await env.sleep(timedelta(seconds=20))
    raise AssertionError("the block workflow did not finish")


def run(test, **options):
    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            fakes = Fakes(env, **options)
            async with Worker(env.client, task_queue=TASK_QUEUE,
                              workflows=[InterviewSlotWorkflow, BlockCallWorkflow],
                              workflow_runner=workflow_runner(), activities=fakes.research()), \
                    Worker(env.client, task_queue=CALLBACK_QUEUE, activities=fakes.calls(env.client)):
                await test(env, fakes)
    asyncio.run(main())


def event(start: datetime, title="Interview with Acme", attendees=None) -> SlotEvent:
    return SlotEvent("ev1", title, start.isoformat(), (start + timedelta(hours=1)).isoformat(),
                     attendees=attendees if attendees is not None else
                     [Person("dev@acme.com", "Dev One")], company="Acme", company_domain="acme.com")


async def start_slot(env, start: datetime) -> WorkflowHandle:
    slot = SlotInput(event(start), OWNER, "block1", (start - timedelta(minutes=60)).isoformat())
    return await env.client.start_workflow(InterviewSlotWorkflow.run, slot, id="slot-1",
                                           task_queue=TASK_QUEUE)


async def until(predicate, env, step=timedelta(minutes=1), limit=600):
    for _ in range(limit):
        if predicate():
            return
        await env.sleep(step)
    raise AssertionError("condition not reached")


def test_slot_researches_at_t_minus_60_with_all_parts():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(hours=3)
        await start_slot(env, start)
        await env.sleep(timedelta(minutes=110))
        assert fakes.plans == []  # Nothing before T-60.
        await until(lambda: fakes.synths, env)
        at, plan = fakes.plans[0]
        assert start - timedelta(minutes=61) <= at <= start - timedelta(minutes=59)
        assert sorted(plan.parts) == ["company", "interviewers", "private", "round"]
        assert plan.interviewers == []  # First run: everyone on the invite.
        assert sorted(p.part for p in fakes.parts) == ["company", "interviewers", "private", "round"]
    run(test)


def test_time_change_moves_the_timer_without_research():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(hours=3)
        handle = await start_slot(env, start)
        later = start + timedelta(hours=2)
        await handle.signal(InterviewSlotWorkflow.event_updated,
                            SlotUpdate(event(later), SlotDiff(["time"])))
        await handle.signal(InterviewSlotWorkflow.set_plan,
                            SlotPlan("block2", (later - timedelta(minutes=60)).isoformat()))
        await env.sleep(timedelta(hours=2, minutes=30))
        assert fakes.plans == []  # The old T-60 passed: nothing ran.
        await until(lambda: fakes.plans, env)
        assert fakes.plans[0][0] >= later - timedelta(minutes=61)
        assert len(fakes.plans) == 1
    run(test)


def test_new_interviewer_after_research_runs_only_that_job():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(minutes=90)
        handle = await start_slot(env, start)
        await until(lambda: fakes.synths, env)
        fakes.parts.clear()
        newcomer = Person("lead@acme.com", "Lead Two")
        await handle.signal(InterviewSlotWorkflow.event_updated, SlotUpdate(
            event(start, attendees=[Person("dev@acme.com", "Dev One"), newcomer]),
            SlotDiff(["interviewers"], added=[newcomer])))
        await until(lambda: len(fakes.synths) == 2, env)
        assert [p.part for p in fakes.parts] == ["interviewers"]
        assert [p.email for p in fakes.parts[0].interviewers] == ["lead@acme.com"]
    run(test)


def test_removed_interviewer_only_resynthesizes_and_round_change_reruns_round():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(minutes=90)
        handle = await start_slot(env, start)
        await until(lambda: fakes.synths, env)
        fakes.parts.clear()
        await handle.signal(InterviewSlotWorkflow.event_updated, SlotUpdate(
            event(start, attendees=[]), SlotDiff(["interviewers"], removed=["dev@acme.com"])))
        await until(lambda: len(fakes.synths) == 2, env)
        assert fakes.parts == [] and fakes.synths[1].removed == ["dev@acme.com"]
        await handle.signal(InterviewSlotWorkflow.event_updated, SlotUpdate(
            event(start, title="System design round with Acme"), SlotDiff(["round"])))
        await until(lambda: len(fakes.synths) == 3, env)
        assert [p.part for p in fakes.parts] == ["round"]
    run(test)


def test_cancelled_slot_ends_and_cache_hit_runs_nothing():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(minutes=30)  # Sooner than T-60: research at once.
        handle = await start_slot(env, start)
        await until(lambda: fakes.plans, env)
        assert fakes.parts == [] and fakes.synths == []  # Plan said: all cached.
        await handle.signal(InterviewSlotWorkflow.cancel)
        status = await handle.result()
        assert status.state == "cancelled"
    run(test, cache_hit=True)


async def start_block(env, start: datetime, on_demand=False) -> WorkflowHandle:
    request = BlockInput("briefing-x", start.isoformat(), ["ev1"], OWNER,
                         OWNER.removeprefix("tel:"), "your 4:00 PM interview with Acme",
                         on_demand=on_demand)
    return await env.client.start_workflow(BlockCallWorkflow.run, request, id="briefing-x",
                                           task_queue=TASK_QUEUE)


def test_block_calls_once_at_t30_with_no_retries():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(hours=1)
        status = await (await start_block(env, start)).result()
        assert status.state == "unanswered" and status.detail == "no answer" and len(fakes.dials) == 1
        assert round((start - fakes.dials[0][0]).total_seconds() / 60) == 30
        assert {b.budget for b in fakes.budgets} == {"briefing"}
        assert {b.limit for b in fakes.budgets} == {12}
        assert fakes.dials[0][1].research_job == "block:briefing-x"
    run(test)


def test_block_stops_after_an_answer():
    async def test(env, fakes):
        start = await env.get_current_time() + timedelta(hours=1)
        status = await drive(env, await start_block(env, start), fakes)
        assert status.state == "briefed" and len(fakes.dials) == 1
        assert fakes.records[-1].state == "briefed"
    run(test, answer_on=1)


def test_a_late_interview_is_called_once_research_is_ready_with_five_minutes_left():
    async def test(env, fakes):
        now = await env.get_current_time()
        start = now + timedelta(minutes=20)  # Added after T-30.
        handle = await start_block(env, start)
        await env.sleep(timedelta(minutes=3))
        assert fakes.dials == []  # Research not ready yet.
        fakes.is_ready = True
        status = await drive(env, handle, fakes)
        assert status.state == "briefed" and len(fakes.dials) == 1
        assert timedelta(minutes=15) <= start - fakes.dials[0][0] <= timedelta(minutes=17, seconds=30)
    run(test, ready=False, answer_on=1)


def test_no_call_with_under_five_minutes_left():
    async def test(env, fakes):
        now = await env.get_current_time()
        status = await (await start_block(env, now + timedelta(minutes=8))).result()
        assert status.state == "missed" and fakes.dials == []
    run(test, ready=False)


def test_cancelled_block_never_dials():
    async def test(env, fakes):
        start = await env.get_current_time() + timedelta(hours=1)
        handle = await start_block(env, start)
        await env.sleep(timedelta(minutes=20))
        await handle.signal(BlockCallWorkflow.cancel)
        assert (await handle.result()).state == "cancelled" and fakes.dials == []
    run(test)


def test_on_demand_waits_for_the_call_to_end_then_calls_back():
    async def test(env, fakes):
        now = await env.get_current_time()
        handle = await start_block(env, now, on_demand=True)
        await env.sleep(timedelta(minutes=2))
        assert fakes.dials == []
        await handle.signal(BlockCallWorkflow.call_ended_signal)
        status = await drive(env, handle, fakes)
        assert status.state == "briefed" and len(fakes.dials) == 1
    run(test, answer_on=1)


def test_on_demand_calls_by_the_promise_even_if_research_is_not_ready():
    async def test(env, fakes):
        now = await env.get_current_time()
        request = BlockInput("briefing-od", now.isoformat(), ["ev1", "ev2"], OWNER,
                             OWNER.removeprefix("tel:"), "today's interviews", on_demand=True,
                             call_by_utc=(now + timedelta(minutes=5)).isoformat(), ready_keys=["ev1"])
        handle = await env.client.start_workflow(BlockCallWorkflow.run, request, id="briefing-od",
                                                 task_queue=TASK_QUEUE)
        await handle.signal(BlockCallWorkflow.call_ended_signal)
        status = await drive(env, handle, fakes)
        assert status.state == "briefed"
        dialled_at = fakes.dials[0][0]
        assert now + timedelta(minutes=3, seconds=50) <= dialled_at <= now + timedelta(minutes=4, seconds=5)
    run(test, answer_on=1, ready=False)


def test_research_within_limits_the_researchers_for_the_promise():
    from app.interview_workflow import ResearchNow

    async def test(env, fakes):
        now = await env.get_current_time()
        handle = await start_slot(env, now + timedelta(hours=5))
        await handle.signal(InterviewSlotWorkflow.research_within, ResearchNow(165))
        await until(lambda: fakes.synths, env, step=timedelta(seconds=10))
        assert {p.seconds for p in fakes.parts} == {165}
        assert fakes.plans[0][0] < now + timedelta(minutes=2)  # At once, not at T-60.
    run(test)


def test_paused_automatic_calls_never_dial():
    async def test(env, fakes):
        start = await env.get_current_time() + timedelta(hours=1)
        status = await (await start_block(env, start)).result()
        assert status.state == "paused" and fakes.dials == [] and fakes.budgets == []
        assert fakes.records[-1].state == "paused"
    run(test, paused=True)


def test_resuming_before_t30_still_calls():
    async def test(env, fakes):
        start = await env.get_current_time() + timedelta(hours=1)
        handle = await start_block(env, start)
        await env.sleep(timedelta(minutes=10))
        fakes.paused = False
        status = await drive(env, handle, fakes)
        assert status.state == "briefed" and len(fakes.dials) == 1
        assert round((start - fakes.dials[0][0]).total_seconds() / 60) == 30
    run(test, paused=True, answer_on=1)


def test_pause_does_not_stop_a_callback_the_owner_asked_for():
    async def test(env, fakes):
        handle = await start_block(env, await env.get_current_time(), on_demand=True)
        await handle.signal(BlockCallWorkflow.call_ended_signal)
        status = await drive(env, handle, fakes)
        assert status.state == "briefed" and len(fakes.dials) == 1
    run(test, paused=True, answer_on=1)


def test_call_now_signals_at_once_and_picks_the_next_interview():
    from app import call_now

    class Client:
        def __init__(self):
            self.started, self.signals = [], []

        async def start_workflow(self, run, request, **kwargs):
            self.started.append((kwargs["id"], request))
            client = self

            class Handle:
                async def signal(self, signal, *args, **kw):
                    client.signals.append(kwargs["id"])
            return Handle()

        def get_workflow_handle(self, workflow_id):
            client = self

            class Handle:
                async def signal(self, signal, *args, **kw):
                    client.signals.append(workflow_id)
            return Handle()

    now = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
    past, soon, later = (event(now - timedelta(hours=1)), event(now + timedelta(hours=2)),
                         event(now + timedelta(hours=5)))
    soon.key, later.key, past.key = "soon", "later", "past"
    assert call_now.pick([later, past, soon], "next", now).key == "soon"
    assert call_now.pick([later, soon], "later", now).key == "later"
    client = Client()
    workflow_id = asyncio.run(call_now.call_now(client, soon, OWNER, OWNER.removeprefix("tel:"), False))
    ids = [i for i, _ in client.started]
    assert ids[0].startswith("slot-") and ids[1] == workflow_id  # Research first, then the call.
    request = client.started[1][1]
    assert request.on_demand and request.ready_keys == ["soon"] and workflow_id in client.signals
