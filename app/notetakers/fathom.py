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
- Embed: not in the developer docs. Fathom's release notes (Jan 2025) say recordings can
  be embedded from share links; the player is `https://fathom.video/embed/<token>` for a
  share link `https://fathom.video/share/<token>` (format seen on Iframely, not in
  Fathom's docs). It is expected to play only while the recording's share setting is
  "Anyone with the link"; the dashboard always shows the deep link as well.
- Webhook: Standard Webhooks signature; the payload is the same Meeting object.
"""
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.notetakers.base import Recording, check, safe_link, utc, with_query
from app.profile.recordings import normalize_fathom

NAME = "fathom"
API = "https://api.fathom.ai/external/v1"
MAX_PAGES = 15  # Fathom pages are 10 meetings each.


def headers(key: str) -> dict[str, str]:
    return {"X-Api-Key": key, "Accept": "application/json"}


def embed_url(share_url: str) -> str:
    """https://fathom.video/share/<token> -> https://fathom.video/embed/<token>; else ""."""
    parts = urlsplit(share_url or "")
    path = parts.path.rstrip("/").split("/")
    if parts.scheme == "https" and parts.hostname == "fathom.video" and len(path) == 3 and path[1] == "share":
        return f"https://fathom.video/embed/{path[2]}"
    return ""


def timestamp_url(share_url: str, seconds: int) -> str:
    return with_query(share_url, timestamp=int(seconds)) if share_url else ""


def recording(item: dict[str, Any], with_meeting: bool = False) -> Recording:
    share = safe_link(str(item.get("share_url") or ""))
    start = item.get("recording_start_time") or item.get("scheduled_start_time") or item.get("created_at")
    end = item.get("recording_end_time") or item.get("scheduled_end_time")
    emails = [str(i.get("email") or "").lower() for i in item.get("calendar_invitees") or []
              if isinstance(i, dict) and i.get("email")]
    return Recording(
        provider=NAME, external_id=str(item.get("recording_id") or ""),
        title=str(item.get("title") or item.get("meeting_title") or ""),
        start=utc(start), end=utc(end),
        share_url=safe_link(str(item.get("url") or "")) or share,
        embed_url=embed_url(share), attendee_emails=emails,
        meeting=normalize_fathom(item) if with_meeting else None)


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
