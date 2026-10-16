"""Provider "phone": call recordings the owner uploads from his iPhone (app/phone_recordings.py).

Not a notetaker with an account: nothing to connect, list or fetch. Its recordings arrive
through the upload hook and are processed by the research worker; their audio plays from
VALR itself (`/valr/api/recording/audio`), and the transcript is kept sealed in
`phone_uploads.meeting` for the key moments.
"""
from datetime import datetime

import httpx

from app.notetakers.base import ProviderError, Recording


async def verify(key: str, http: httpx.AsyncClient) -> str:
    raise ProviderError("phone recordings have no account to connect")


async def list_recordings(key: str, start: datetime, end: datetime, http: httpx.AsyncClient) -> list[Recording]:
    return []


async def get_recording(key: str, external_id: str, http: httpx.AsyncClient, listed: Recording | None = None) -> Recording:
    raise ProviderError("phone recordings are read from VALR's own store")


async def stored_meeting(connection, external_id: str) -> dict | None:
    from app import phone_recordings
    await phone_recordings.setup(connection)
    return await phone_recordings.sealed_meeting(connection, external_id)


def media_answer(external_id: str) -> dict:
    """What the player asks for: VALR's own audio endpoint (same origin, Range requests)."""
    return {"status": "completed", "url": f"/valr/api/recording/audio?id=phone:{external_id}",
            "content_type": "audio/mp4"}
