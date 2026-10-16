"""New recordings -> the per-interview evaluation (owned by the valr-eval work).

Every recording VALR hears of (the Fathom or Granola webhook, a Fireflies history import, "Check for new
recordings" on the dashboard, an uploaded Granola export) goes through `intake`:

1. its links are stored (`notetaker_recordings`, app/notetakers/store.py); the
   normalised meeting, when we already have it (a webhook that carried the
   transcript, an export), is kept encrypted until the evaluation takes it;
2. it is matched to a calendar event (time window + attendee emails), so a past
   interview on the dashboard can show its recording;
3. a new recording is handed to the evaluation hook.

The hook (the interface the evaluation side implements):

    VALR_EVAL_HOOK="package.module:function"   (default app.profile.notetaker_hook:evaluate_recording)

    async def evaluate_recording(provider: str, external_id: str, meeting: dict | None) -> str

`meeting` is the profile importer's normalised meeting (transcript included) when
VALR already has it, else None: the hook then fetches it with
`app.notetakers.fetch_meeting(provider, external_id)`. The hook must return quickly
(start a workflow or queue the job, never score inline in the web process) and
returns a short status for the dashboard. Without a hook the recording stays
`no_hook` and `python -m app.notetakers pending` lists what is waiting.
"""
import importlib
import inspect
import json
import logging
import os
from typing import Any, Awaitable, Callable

from app import google_oauth
from app.notetakers import store
from app.notetakers.base import Recording

logger = logging.getLogger("uvicorn.error")
HOOK_ENV = "VALR_EVAL_HOOK"
DEFAULT_HOOK = "app.profile.notetaker_hook:evaluate_recording"
Hook = Callable[[str, str, dict | None], Awaitable[str] | str]


def resolve_hook() -> Hook | None:
    target = os.getenv(HOOK_ENV, DEFAULT_HOOK).strip()
    module_name, _, function = target.partition(":")
    if not (module_name and function):
        return None
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None
    hook = getattr(module, function, None)
    return hook if callable(hook) else None


async def hand_off(connection: Any, provider: str, external_id: str, meeting: dict | None,
                   hook: Hook | None = None) -> str:
    """Give one recording to the evaluation; records and returns the status."""
    hook = hook or resolve_hook()
    if hook is None:
        await store.set_eval(connection, provider, external_id, "no_hook",
                             "Waiting: the per-interview evaluation is not wired in yet.")
        logger.info("VALR recording %s waiting for the evaluation hook", provider)
        return "no_hook"
    try:
        result = hook(provider, external_id, meeting)
        detail = await result if inspect.isawaitable(result) else result
    except Exception as error:  # noqa: BLE001 - the recording stays pending; the page says so.
        logger.warning("VALR evaluation hook failed for a %s recording: %s", provider, type(error).__name__)
        await store.set_eval(connection, provider, external_id, "failed", type(error).__name__)
        return "failed"
    await store.set_eval(connection, provider, external_id, "handed_off", str(detail or "")[:300])
    await store.clear_sealed(connection, provider, external_id)
    logger.info("VALR recording %s handed to the evaluation", provider)
    return "handed_off"


async def intake(connection: Any, recording: Recording, origin: str, hook: Hook | None = None,
                 match: Callable[[Recording], Awaitable[dict | None]] | None = None) -> dict[str, Any]:
    """Store, match and (when new) hand off one recording. -> {new, event_key, eval}.

    `match` returns the calendar event as {key, title, company, start}, or None."""
    await store.setup(connection)
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    sealed = (google_oauth.encrypt(key, json.dumps(recording.meeting))
              if key and recording.meeting else None)
    new = await store.upsert_recording(connection, recording.public(), origin, sealed)
    event = await match(recording) if match else None
    event_key = event["key"] if event else None
    if event:
        await store.set_event(connection, recording.provider, recording.external_id, event["key"],
                              event.get("title", ""), event.get("company", ""), event.get("start"))
    status = "known"
    if new:
        status = await hand_off(connection, recording.provider, recording.external_id,
                                recording.meeting, hook)
    return {"new": new, "event_key": event_key, "eval": status}
