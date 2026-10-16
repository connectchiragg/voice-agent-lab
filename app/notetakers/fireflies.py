"""Fireflies.ai (https://docs.fireflies.ai): GraphQL at https://api.fireflies.ai/graphql,
`Authorization: Bearer <API key>`.

- Verify: `user { name }` (the key's own user).
- List: `transcripts(fromDate, toDate, limit, skip)` with id, title, date (epoch ms),
  duration (minutes), transcript_url, meeting_attendees and the summary; pages of 50.
- One recording: `transcript(id)` with sentences; normalisation reuses the profile
  importer's `normalize_fireflies` (names only; emails are used here only to match a
  calendar event and are never stored).
- Recording: `transcript_url` (app.fireflies.ai/view/...) opens the meeting. `video_url`
  and `audio_url` (Pro and up) are download links that expire after 24 hours, and
  Fireflies documents no embed player or timestamp parameter, so the dashboard links out.
- No webhook: the owner no longer records with Fireflies, so it is a one-time history
  source ("Check for new recordings" imports the past meetings once connected).
"""
from datetime import datetime
from typing import Any

import httpx

from app.notetakers.base import KeyRefused, ProviderError, Recording, check, safe_link, utc
from app.profile.recordings import normalize_fireflies

NAME = "fireflies"
API = "https://api.fireflies.ai/graphql"
PAGE = 50
MAX_PAGES = 20

VERIFY = "query { user { name } }"
LIST = """
query Transcripts($fromDate: DateTime, $toDate: DateTime, $limit: Int, $skip: Int) {
  transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip) {
    id title date duration transcript_url
    meeting_attendees { displayName name email }
  }
}"""
ONE = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id title date duration transcript_url
    speakers { name }
    meeting_attendees { displayName name email }
    summary { overview short_summary keywords }
    sentences { speaker_name text start_time }
  }
}"""


async def graphql(http: httpx.AsyncClient, key: str, query: str, variables: dict | None = None) -> dict:
    response = check(await http.post(API, json={"query": query, "variables": variables or {}},
                                     headers={"Authorization": f"Bearer {key}"}), "Fireflies")
    data = response.json()
    if data.get("errors"):
        codes = sorted({str((e.get("extensions") or {}).get("code") or "error") for e in data["errors"]})
        if any(c in ("auth_failed", "invalid_api_key", "UNAUTHENTICATED", "forbidden") for c in codes):
            raise KeyRefused("Fireflies refused the key")
        raise ProviderError(f"Fireflies GraphQL error: {', '.join(codes)}")
    return data.get("data") or {}


def timestamp_url(transcript_url: str, seconds: int) -> str:
    """Fireflies documents no timestamp parameter for transcript_url: moments show the time only."""
    return ""


def recording(item: dict[str, Any], with_meeting: bool = False) -> Recording:
    start = utc(item.get("date"))
    end = None
    if start and isinstance(item.get("duration"), (int, float)):
        from datetime import timedelta
        end = utc((datetime.fromisoformat(start.replace("Z", "+00:00"))
                   + timedelta(minutes=float(item["duration"]))).isoformat())
    emails = [str(a.get("email") or "").lower() for a in item.get("meeting_attendees") or []
              if isinstance(a, dict) and a.get("email")]
    return Recording(provider=NAME, external_id=str(item.get("id") or ""), title=str(item.get("title") or ""),
                     start=start, end=end, share_url=safe_link(str(item.get("transcript_url") or "")),
                     embed_url="", attendee_emails=emails,
                     meeting=normalize_fireflies(item) if with_meeting else None)


async def verify(key: str, http: httpx.AsyncClient) -> str:
    user = (await graphql(http, key, VERIFY)).get("user") or {}
    return str(user.get("name") or "")


async def list_recordings(key: str, start: datetime, end: datetime, http: httpx.AsyncClient,
                          with_meeting: bool = False) -> list[Recording]:
    out: list[Recording] = []
    for page in range(MAX_PAGES):
        batch = (await graphql(http, key, LIST, {"fromDate": utc(start.isoformat()), "toDate": utc(end.isoformat()),
                                                 "limit": PAGE, "skip": page * PAGE})).get("transcripts") or []
        out += [recording(item) for item in batch if isinstance(item, dict)]
        if len(batch) < PAGE:
            break
    return out


async def get_recording(key: str, external_id: str, http: httpx.AsyncClient,
                        listed: Recording | None = None) -> Recording:
    item = (await graphql(http, key, ONE, {"id": external_id})).get("transcript")
    if not item:
        raise ProviderError("Fireflies has no such transcript")
    return recording(item, with_meeting=True)
