"""The evaluation hook of the notetaker pipeline (valr-notetakers: app/notetakers/pipeline.py,
VALR_EVAL_HOOK default `app.profile.notetaker_hook:evaluate_recording`).

Runs in the web process, so it only queues: the meeting (fetched with
`app.notetakers.fetch_meeting` when the pipeline did not pass it) is stored encrypted in
`post_call_reports` (app/post_call.py) together with the recording's share link, and the
research worker's post-call loop scores it (rubric, interviewer cues, coach read) and
sends the Telegram report when that evaluation completes. Returns a short status.
"""
import logging
from typing import Any

logger = logging.getLogger("valr")


async def _fetch(provider: str, external_id: str) -> dict | None:
    try:
        from app.notetakers import fetch_meeting
    except ImportError:  # the notetakers package is not in this build
        return None
    try:
        return await fetch_meeting(provider, external_id)
    except Exception as error:  # noqa: BLE001 - the worker fetches it again later
        logger.warning("VALR post-call: %s transcript not fetched yet (%s)", provider, type(error).__name__)
        return None


async def _recording_url(connection: Any, provider: str, external_id: str) -> str:
    try:
        from app.notetakers import store
        row = await store.recording(connection, provider, external_id)
    except Exception:  # noqa: BLE001 - no notetaker table: no link
        return ""
    return str((row or {}).get("share_url") or "")


async def evaluate_recording(provider: str, external_id: str, meeting: dict | None,
                             connect: Any = None, fetch: Any = None) -> str:
    if meeting is None:
        meeting = await (fetch or _fetch)(provider, external_id)
    if connect is None:
        from app import research
        if research.pool is not None:
            async with research.pool.connection() as connection:
                return await _queue(connection, provider, external_id, meeting)
        import psycopg
        from app import research_jobs

        async def connect():
            return await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True)
    async with await connect() as connection:
        return await _queue(connection, provider, external_id, meeting)


async def _queue(connection: Any, provider: str, external_id: str, meeting: dict | None) -> str:
    from app import post_call
    new = await post_call.enqueue(connection, provider, external_id, meeting,
                                  (meeting or {}).get("title", ""),
                                  await _recording_url(connection, provider, external_id))
    if not new:
        return "already queued for the post-call report"
    return ("queued for the post-call report" if meeting else
            "queued for the post-call report (transcript fetched by the worker)")
