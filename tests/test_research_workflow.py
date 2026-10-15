"""ResearchCallbackWorkflow in Temporal's time-skipping server, with stub activities.

The researchers, synthesis and saving are replaced by small fakes registered under the
activity names; a stand-in CallbackWorkflow records the callback the job starts.
"""
import asyncio
import json
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.callback_workflow import TASK_QUEUE as CALLBACK_QUEUE
from app.callback_workflow import CallbackRequest, workflow_runner
from app.callback_workflow import workflow_id as callback_workflow_id
from app.research_workflow import (APOLOGY, TASK_QUEUE, FinishInput, PartInput, PartResult,
                                   ResearchCallbackWorkflow, ResearchRequest, ResearchStatus,
                                   SynthInput, SynthResult, workflow_id)

CALLER = "tel:+15550100000"
FINDINGS = json.dumps({"summary": "S", "claims": [{"claim": "C", "source_url": "https://a.example"}]})


class Fakes:
    """What each stub activity does, and what they were given."""

    def __init__(self, parts=None, synth_fails=False):
        self.parts = parts or {}  # part -> "ok" | "fail" | "budget" | "empty"
        self.synth_fails = synth_fails
        self.part_inputs: list[PartInput] = []
        self.synth_inputs: list[SynthInput] = []
        self.finished: list[FinishInput] = []
        CALLBACKS.clear()
        self.callbacks = CALLBACKS

    def activities(self):
        @activity.defn(name="research_part")
        async def research_part(task: PartInput) -> PartResult:
            self.part_inputs.append(task)
            mode = self.parts.get(task.part, "ok")
            if mode == "fail":
                raise ApplicationError("boom", non_retryable=True)
            if mode == "empty":
                return PartResult(task.part, usd=0.001)
            if mode == "budget":
                return PartResult(task.part, False, "", "raw tool results", ["https://b.example"],
                                  0.04, "budget", {"budget_soft": 1})
            return PartResult(task.part, True, FINDINGS, "raw", ["https://a.example"], 0.03, "",
                              {"page_chars": 2})

        @activity.defn(name="research_synthesize")
        async def research_synthesize(task: SynthInput) -> SynthResult:
            self.synth_inputs.append(task)
            if self.synth_fails:
                raise ApplicationError("no", non_retryable=True)
            partial = not all(part.ok for part in task.parts)
            return SynthResult("Here is the answer. Want more on any of that?",
                               json.dumps([{"fact": "C", "source_ids": ["S1"], "sensitive": False}]),
                               json.dumps([{"id": "S1", "url": "https://a.example"}]), partial, 0.02,
                               {"claim_check_dropped": 1})

        @activity.defn(name="research_finish")
        async def research_finish(task: FinishInput) -> None:
            self.finished.append(task)

        return [research_part, research_synthesize, research_finish]


CALLBACKS: list[CallbackRequest] = []  # What the stand-in callback workflow was started with.


@workflow.defn(name="CallbackWorkflow", sandboxed=False)
class StubCallback:
    @workflow.run
    async def run(self, request: CallbackRequest) -> None:
        CALLBACKS.append(request)
        await workflow.sleep(timedelta(hours=1))  # Stays pending, as a ringing callback would.


def run(test, fakes: Fakes):
    async def main():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[ResearchCallbackWorkflow],
                              workflow_runner=workflow_runner(), activities=fakes.activities()), \
                    Worker(env.client, task_queue=CALLBACK_QUEUE, workflows=[StubCallback]):
                await test(env)
    asyncio.run(main())


async def start(env: WorkflowEnvironment, dial: bool = True, minutes: int = 3) -> WorkflowHandle:
    request = ResearchRequest("job1", CALLER, CALLER.removeprefix("tel:"), "Coorg for a weekend",
                              "conversation-1", minutes, dial=dial)
    return await env.client.start_workflow(ResearchCallbackWorkflow.run, request,
                                           id=workflow_id(CALLER), task_queue=TASK_QUEUE)


async def finish(handle: WorkflowHandle) -> ResearchStatus:
    await handle.signal(ResearchCallbackWorkflow.call_ended_signal)
    return await handle.result()


def test_research_waits_for_the_call_to_end_then_calls_back_with_the_answer():
    fakes = Fakes()

    async def test(env):
        handle = await start(env)
        await asyncio.sleep(0.5)
        assert (await handle.query(ResearchCallbackWorkflow.status_query)).state == "waiting_for_call_end"
        assert fakes.part_inputs == []  # Nothing runs while the call is on.
        status = await finish(handle)
        assert status.state == "done" and status.detail == "callback started"
        assert sorted(task.part for task in fakes.part_inputs) == ["facts", "reviews", "web"]
        # Limits from the promise: 3 minutes -> 120 s job; researchers stop 40 s before it,
        # each with a quarter of three quarters of the $0.25 budget.
        assert {task.seconds for task in fakes.part_inputs} == {80}
        assert {round(task.budget_usd, 4) for task in fakes.part_inputs} == {0.0625}
        assert fakes.synth_inputs[0].seconds <= 120
        saved = fakes.finished[0]
        assert saved.status == "done" and saved.spoken.startswith("Here is the answer")
        assert saved.cap_hits == {"page_chars": 6, "claim_check_dropped": 1}
        await asyncio.sleep(0.5)
        callback = fakes.callbacks[0]
        assert callback.research_job == "job1" and callback.phone_e164 == "+15550100000"
        assert callback.reason == "what I found about Coorg for a weekend"
        assert status.usd == 0.03 * 3 + 0.02
    run(test, fakes)


def test_without_the_call_end_signal_it_starts_after_the_longest_call():
    fakes = Fakes()

    async def test(env):
        handle = await start(env, dial=False)
        await env.sleep(timedelta(minutes=12))
        status = await handle.result()
        assert status.state == "done" and len(fakes.part_inputs) == 3
        assert fakes.callbacks == []  # dial=False: the VM smoke test stops before dialling.
    run(test, fakes)


def test_two_of_three_researchers_are_enough_for_a_partial_answer():
    fakes = Fakes({"reviews": "fail"})

    async def test(env):
        status = await finish(await start(env))
        assert status.state == "partial" and status.parts["reviews"] == "failed"
        assert len(fakes.synth_inputs[0].parts) == 3
        assert fakes.finished[0].status == "partial"
    run(test, fakes)


def test_a_researcher_stopped_by_its_budget_still_hands_its_evidence_to_synthesis():
    fakes = Fakes({"web": "budget", "reviews": "budget"})

    async def test(env):
        status = await finish(await start(env))
        given = {part.part: part for part in fakes.synth_inputs[0].parts}
        assert given["web"].stopped == "budget" and given["web"].evidence == "raw tool results"
        assert status.state == "partial" and fakes.finished[0].cap_hits["budget_soft"] == 2
        # Synthesis gets what is left of the job's $0.25.
        assert abs(fakes.synth_inputs[0].budget_usd - (0.25 - 0.04 - 0.04 - 0.03)) < 1e-9
    run(test, fakes)


def test_nothing_usable_means_a_brief_apology_and_still_a_callback():
    fakes = Fakes({"web": "fail", "reviews": "empty", "facts": "fail"})

    async def test(env):
        status = await finish(await start(env))
        assert status.state == "apologized" and fakes.synth_inputs == []
        assert fakes.finished[0].spoken == APOLOGY.format(topic="Coorg for a weekend")
        await asyncio.sleep(0.5)
        assert len(fakes.callbacks) == 1
    run(test, fakes)


def test_a_failed_synthesis_apologizes_instead():
    fakes = Fakes(synth_fails=True)

    async def test(env):
        status = await finish(await start(env))
        assert status.state == "apologized" and "synthesis failed" in fakes.finished[0].detail
    run(test, fakes)


def test_cancelled_before_the_call_ends_nothing_runs():
    fakes = Fakes()

    async def test(env):
        handle = await start(env)
        await handle.signal(ResearchCallbackWorkflow.cancel)
        status = await handle.result()
        assert status.state == "cancelled" and fakes.part_inputs == []
        assert fakes.finished[0].status == "cancelled" and fakes.callbacks == []
    run(test, fakes)


def test_a_pending_callback_for_the_caller_is_not_doubled():
    fakes = Fakes()

    async def test(env):
        await env.client.start_workflow("CallbackWorkflow", CallbackRequest(
            CALLER, "+15550100000", "2030-01-01T00:00:00+00:00", "other", "c0"),
            id=callback_workflow_id(CALLER), task_queue=CALLBACK_QUEUE)
        status = await finish(await start(env))
        assert status.state == "done" and "already pending" in status.detail
    run(test, fakes)
