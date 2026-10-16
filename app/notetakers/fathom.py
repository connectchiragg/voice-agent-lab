"""Fathom (https://developers.fathom.ai): REST, `X-Api-Key` header, cursor pagination.

- List: `GET /external/v1/meetings?created_after=&created_before=&cursor=` (items carry
  `recording_id`, `url`, `share_url`, `title`/`meeting_title`, the scheduled and
  recording times and `calendar_invitees` with emails). `include_transcript` and
  `include_summary` add the transcript and `default_summary.markdown_formatted`.
- One recording: the list filtered to it is not offered, so the transcript and summary
  come from `GET /external/v1/recordings/{id}/transcript` and `/summary`.
- Normalisation reuses the profile importer's `normalize_fathom` (names, no emails).
- Links: `url` (https://fathom.video/calls/<id>, the owner's own view) is the deep link;
  timestamps use `?timestamp=<seconds>` as in the API's `recording_playback_url`.
- Video: the official download API (developers.fathom.ai, "Request a download" and "Get
  download status"): `POST /recordings/{id}/download` -> 202 RecordingDownload
  {download_id, recording_id, status: processing|completed|failed|expired, video?|audio?:
  {url, content_type, file_size_bytes, expires_at}, failure_reason?}; then
  `GET /recordings/{id}/downloads/{download_id}`. Only the API client that made a
  download can read it. Errors: 403 (limited-access share), 404, 422 (no downloadable
  media), 429. Video is generated in the background; audio-only completes at once.
  Measured 2026-10-16: video from streamkit.storage.googleapis.com, audio-only from
  fathom-recording-data.storage.googleapis.com, both `Accept-Ranges: bytes`, signed for
  24 h. The old `fathom.video/embed/<token>` iframe (undocumented) is gone: Fathom's
  pages send `frame-ancestors 'self'`. Caching and the player: app/notetakers/media.py.
- Company and people: `calendar_invitees` ({name, email, is_external}) give the
  interviewers and the company's domain (base.invitee_details).
- Webhook: Standard Webhooks signature; the payload is the same Meeting object.
"""
import os
from datetime import datetime
from typing import Any

import httpx

from app.notetakers.base import Recording, check, invitee_details, safe_link, utc, with_query
from app.profile.recordings import normalize_fathom

NAME = "fathom"
API = "https://api.fathom.ai/external/v1"
MAX_PAGES = 15  # Fathom pages are 10 meetings each.


def headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "Accept": "application/json"}


def timestamp_url(share_url: str, seconds: int) -> str:
    return with_query(share_url, timestamp=int(seconds)) if share_url else ""


def recording(item: dict[str, Any], with_meeting: bool = False) -> Recording:
    share = safe_link(str(item.get("share_url") or ""))
    start = item.get("recording_start_time") or item.get("scheduled_start_time") or item.get("created_at")
    end = item.get("recording_end_time") or item.get("scheduled_end_time")
    invitees = [i for i in item.get("calendar_invitees") or [] if isinstance(i, dict)]
    emails = [str(i.get("email") or "").lower() for i in invitees if i.get("email")]
    recorder = item.get("recorded_by") if isinstance(item.get("recorded_by"), dict) else {}
    details = invitee_details(invitees, owner_emails() | ({str(recorder.get("email")).lower()} if recorder.get("email") else set()),
                              str(item.get("meeting_title") or item.get("title") or ""), str(recorder.get("name") or ""))
    return Recording(
        provider=NAME, external_id=str(item.get("recording_id") or ""),
        title=str(item.get("title") or item.get("meeting_title") or ""),
        start=utc(start), end=utc(end),
        share_url=safe_link(str(item.get("url") or "")) or share,
        attendee_emails=emails, meeting=normalize_fathom(item) if with_meeting else None, **details)


def owner_emails() -> set[str]:
    """The owner's addresses (as app.interview_worker.owner_emails, without importing Temporal)."""
    return {e.strip().lower() for e in os.getenv("VALR_OWNER_EMAILS", "").split(",") if e.strip()} \
        | ({os.getenv("VALR_GMAIL_ACCOUNT", "").strip().lower()} - {""})


async def verify(key: str, http: httpx.AsyncClient) -> str:
    check(await http.get(f"{API}/meetings", headers=headers(key), params={"limit": 1}), "Fathom")
    return ""  # The meetings list names no account; the dashboard says "Connected".


async def list_recordings(key: str, start: datetime, end: datetime, http: httpx.AsyncClient,
                          with_meeting: bool = False) -> list[Recording]:
    params: dict[str, Any] = {"created_after": utc(start.isoformat()), "created_before": utc(end.isoformat())}
    if with_meeting:
        params |= {"include_transcript": "true", "include_summary": "true"}
    out: list[Recording] = []
    for _ in range(MAX_PAGES):
        data = check(await http.get(f"{API}/meetings", headers=headers(key), params=params), "Fathom").json()
        out += [recording(item, with_meeting) for item in data.get("items") or [] if isinstance(item, dict)]
        cursor = data.get("next_cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    return out


async def get_recording(key: str, external_id: str, http: httpx.AsyncClient,
                        listed: Recording | None = None) -> Recording:
    """Transcript and summary for one recording; `listed` supplies the links and times."""
    transcript = check(await http.get(f"{API}/recordings/{external_id}/transcript", headers=headers(key)),
                       "Fathom").json()
    summary = check(await http.get(f"{API}/recordings/{external_id}/summary", headers=headers(key)),
                    "Fathom").json()
    item = {"recording_id": external_id, "transcript": transcript.get("transcript") or [],
            "default_summary": summary.get("summary") or summary.get("default_summary") or {}}
    base = listed or Recording(NAME, external_id)
    item |= {"title": base.title, "recording_start_time": base.start, "recording_end_time": base.end}
    base.meeting = normalize_fathom(item)
    return base


def payload_recording(payload: dict[str, Any]) -> Recording:
    """A webhook body (the same Meeting object as the list) -> Recording with its meeting."""
    return recording(payload, with_meeting=bool(payload.get("transcript") or payload.get("default_summary")))


# Downloads (the official API). Each returns the raw response: app/notetakers/media.py maps
# the status codes to what the owner reads.

async def request_download(key: str, external_id: str, http: httpx.AsyncClient) -> httpx.Response:
    return await http.post(f"{API}/recordings/{external_id}/download", headers=headers(key), json={})


async def download_status(key: str, external_id: str, download_id: str, http: httpx.AsyncClient) -> httpx.Response:
    return await http.get(f"{API}/recordings/{external_id}/downloads/{download_id}", headers=headers(key))
