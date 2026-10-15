"""The callback worker: runs CallbackWorkflow and its activities. `python -m app.callback_worker`.

A separate process (and container) from the voice server: it holds no call
audio, and restarting either one leaves the other alone. Temporal keeps the
timers; while the worker is down, due callbacks wait and run once it is back.

Activities, the only code here that touches the outside world:

- `reserve_dial_slot`: takes one of the day's MAX_DIALS_PER_DAY dial slots from
  that day's DialBudgetWorkflow (started on first use).
- `place_call`: Telnyx Call Control Dial from our number to the caller. The
  app's webhook answers for the rest (app/callbacks.py): when the call is
  answered it starts the media stream, and it signals the workflow the result.
"""
import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv
from temporalio import activity
from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from app.callback_workflow import (RING_TIMEOUT_S, TASK_QUEUE, CallbackWorkflow, DialBudgetWorkflow,
                                   DialSlotRequest, PlaceCallInput, workflow_runner)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger("callback_worker")
API_URL = os.getenv("TELNYX_API_URL", "https://api.telnyx.com/v2")
# The voice-agent-lab Call Control app (its id is the Dial's connection_id) and its number.
DEFAULT_CONNECTION_ID = "3071391970178893742"
DEFAULT_FROM_NUMBER = "+12288888007"


@dataclass(frozen=True)
class TemporalSettings:
    """Where the Temporal server is. Self-hosted on the VM: localhost, no TLS."""
    address: str = "localhost:7233"
    namespace: str = "default"

    @classmethod
    def from_env(cls) -> "TemporalSettings | None":
        """None (callbacks off) unless TEMPORAL_ADDRESS is set."""
        address = os.getenv("TEMPORAL_ADDRESS")
        if not address:
            return None
        return cls(address, os.getenv("TEMPORAL_NAMESPACE", cls.namespace))

    async def connect(self) -> Client:
        return await Client.connect(self.address, namespace=self.namespace)


@dataclass(frozen=True)
class DialSettings:
    api_key: str
    connection_id: str = DEFAULT_CONNECTION_ID
    from_number: str = DEFAULT_FROM_NUMBER

    @classmethod
    def from_env(cls) -> "DialSettings":
        return cls(os.environ["TELNYX_API_KEY"],
                   os.getenv("TELNYX_CONNECTION_ID", cls.connection_id),
                   os.getenv("TELNYX_FROM_NUMBER", cls.from_number))


def client_state(workflow_id: str, attempt: int, reason: str, research_job: str = "") -> str:
    """Telnyx echoes this (base64) in every webhook for the call, so the app knows
    which callback a call belongs to and what to say first (and, for a VALR
    research callback, which job's findings to load: only its id travels)."""
    state: dict[str, object] = {"callback": workflow_id, "attempt": attempt, "reason": reason}
    if research_job:
        state["research"] = research_job
    return base64.b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()


def dial_body(settings: DialSettings, request: PlaceCallInput) -> dict[str, object]:
    return {
        "connection_id": settings.connection_id,
        "to": request.phone_e164,
        "from": settings.from_number,
        "client_state": client_state(request.workflow_id, request.attempt, request.reason,
                                     request.research_job),
        "timeout_secs": RING_TIMEOUT_S,
        # Telnyx ignores a repeat with the same id: a retried activity does not ring twice.
        "command_id": f"{request.workflow_id}-{request.attempt}",
        # Answering-machine detection is left off: it is a paid add-on ($0.002 a call).
    }


class Activities:
    """Activities with their dependencies, so tests can swap the HTTP transport or client."""

    def __init__(self, client: Client, dial: DialSettings | None,
                 http: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self.dial = dial
        self.http = http or httpx.AsyncClient(timeout=10)

    @activity.defn(name="reserve_dial_slot")
    async def reserve_dial_slot(self, request: DialSlotRequest) -> bool:
        start = WithStartWorkflowOperation(
            DialBudgetWorkflow.run, request.day, id=f"callback-dials-{request.day}",
            task_queue=TASK_QUEUE, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        granted = await self.client.execute_update_with_start_workflow(
            DialBudgetWorkflow.reserve, request.attempt_id, start_workflow_operation=start)
        if not granted:
            logger.warning("Callback not dialled: the daily limit is reached")
        return granted

    @activity.defn(name="place_call")
    async def place_call(self, request: PlaceCallInput) -> str:
        """Dial the caller; returns the call_control_id the webhook will report on."""
        if self.dial is None:
            raise ApplicationError("TELNYX_API_KEY is not set", type="DialRefused",
                                   non_retryable=True)
        if request.caller_key != f"tel:{request.phone_e164}":
            # Only the number that asked for the callback is ever dialled.
            raise ApplicationError("The number is not the caller's own", type="DialRefused",
                                   non_retryable=True)
        response = await self.http.post(f"{API_URL}/calls", json=dial_body(self.dial, request),
                                        headers={"Authorization": f"Bearer {self.dial.api_key}"})
        if response.status_code in (400, 401, 403, 404, 422):
            # Bad number, unverified number on a trial account, no outbound profile...:
            # retrying will not help. Logged by status, never by body.
            logger.warning("Telnyx refused the callback dial: HTTP %d", response.status_code)
            raise ApplicationError(f"Telnyx refused the dial: HTTP {response.status_code}",
                                   type="DialRefused", non_retryable=True)
        response.raise_for_status()  # 429 and 5xx: the activity is retried.
        call_control_id = str(response.json()["data"]["call_control_id"])
        logger.info("Callback attempt %d dialled", request.attempt)
        return call_control_id


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = TemporalSettings.from_env() or TemporalSettings()
    dial = DialSettings.from_env() if os.getenv("TELNYX_API_KEY") else None
    if dial is None:
        logger.warning("TELNYX_API_KEY is not set: due callbacks will fail without dialling")
    client = await settings.connect()
    activities = Activities(client, dial)
    worker = Worker(client, task_queue=TASK_QUEUE,
                    workflows=[CallbackWorkflow, DialBudgetWorkflow], workflow_runner=workflow_runner(),
                    activities=[activities.reserve_dial_slot, activities.place_call])
    logger.info("Callback worker on %s, namespace %s, task queue %s",
                settings.address, settings.namespace, TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
