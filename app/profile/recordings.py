"""Meeting-recording importers (Fathom REST, Fireflies GraphQL, or a JSON export) -> one shape.

Normalised meeting:
  {source, id, title, date (ISO 8601 UTC), duration (seconds|None),
   scheduled_duration (seconds|None; Fathom's calendar slot),
   participants: [names], turns: [{speaker, text, start (seconds|None)}], vendor_summary}

Data minimisation: emails are never requested from Fireflies and are dropped from
Fathom/export payloads during normalisation. Raw API pages are cached only in
the private cache directory (see RawCache) and can be purged.
"""
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.profile.config import write_private
from app.profile.resume import EMAIL

log = logging.getLogger(__name__)

FATHOM_MEETINGS = "https://api.fathom.ai/external/v1/meetings"
FIREFLIES_GRAPHQL = "https://api.fireflies.ai/graphql"
FIREFLIES_PAGE = 50  # documented max `limit` per transcripts query


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body)


Transport = Callable[[str, str, dict, bytes | None], HttpResponse]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> HttpResponse:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            return HttpResponse(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, dict(e.headers or {}), e.read())


class ApiError(RuntimeError):
    pass


@dataclass
class Http:
    """Throttled client with 429 handling. Errors never include request headers (keys)."""
    transport: Transport = urllib_transport
    min_interval: float = 0.0
    max_retries: int = 3
    sleep: Callable[[float], None] = time.sleep
    _last: float = field(default=0.0, repr=False)

    def request(self, method: str, url: str, headers: dict, body: bytes | None = None
                ) -> HttpResponse:
        for attempt in range(self.max_retries + 1):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            response = self.transport(method, url, headers, body)
            if response.status != 429 or attempt == self.max_retries:
                break
            retry_after = float(_header(response.headers, "Retry-After") or 2 ** attempt * 5)
            log.info("rate limited by %s; waiting %.0fs", urllib.parse.urlsplit(url).netloc,
                     retry_after)
            self.sleep(retry_after)
        if response.status >= 400:
            raise ApiError(f"{urllib.parse.urlsplit(url).netloc} returned HTTP {response.status}")
        return response


def _header(headers: dict, name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


class RawCache:
    """Raw API pages, private to the owner: directory 0700, files 0600."""

    def __init__(self, root: Path):
        self.root = root

    def save(self, source: str, name: str, payload) -> Path:
        directory = self.root / source
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(directory, 0o700)
        path = directory / f"{name}.json"
        write_private(path, json.dumps(payload))
        return path

    def purge(self) -> bool:
        if not self.root.exists():
            return False
        shutil.rmtree(self.root)
        return True


# ---------- helpers ----------

def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(stamp) -> float | None:
    """'HH:MM:SS' / 'MM:SS' / number -> seconds."""
    if stamp is None or stamp == "":
        return None
    if isinstance(stamp, (int, float)):
        return float(stamp)
    try:
        parts = [float(p) for p in str(stamp).split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def _names(values) -> list[str]:
    """Unique display names, never emails."""
    out: list[str] = []
    for v in values:
        name = (v or "").strip()
        if name and not EMAIL.fullmatch(name) and name not in out:
            out.append(name)
    return out


# ---------- Fathom ----------

def fetch_fathom(api_key: str, since: datetime, http: Http, cache: RawCache | None) -> list[dict]:
    """GET /meetings with transcript + summary inline; cursor pagination."""
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    params = {"created_after": _iso(since), "include_transcript": "true",
              "include_summary": "true"}
    items, cursor, page = [], None, 0
    while True:
        query = params | ({"cursor": cursor} if cursor else {})
        data = http.request("GET", f"{FATHOM_MEETINGS}?{urllib.parse.urlencode(query)}",
                            headers).json()
        if cache:
            cache.save("fathom", f"meetings-{page:03d}", data)
        items += data.get("items") or []
        cursor, page = data.get("next_cursor"), page + 1
        if not cursor:
            return items


def normalize_fathom(item: dict) -> dict:
    start = item.get("recording_start_time") or item.get("scheduled_start_time") \
        or item.get("created_at")
    end = item.get("recording_end_time")
    duration = None
    if start and end:
        duration = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                    - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    turns = [{"speaker": (t.get("speaker") or {}).get("display_name") or "Unknown",
              "text": t.get("text", ""), "start": _seconds(t.get("timestamp"))}
             for t in item.get("transcript") or []]
    invitees = [i.get("name") or i.get("matched_speaker_display_name")
                for i in item.get("calendar_invitees") or []]
    summary = (item.get("default_summary") or {}).get("markdown_formatted")
    scheduled = None  # the calendar slot, to tell an interview that ended early
    if item.get("scheduled_start_time") and item.get("scheduled_end_time"):
        scheduled = (datetime.fromisoformat(item["scheduled_end_time"].replace("Z", "+00:00"))
                     - datetime.fromisoformat(item["scheduled_start_time"].replace("Z", "+00:00"))
                     ).total_seconds()
    return {"source": "fathom", "id": str(item.get("recording_id")),
            "title": item.get("meeting_title") or item.get("title") or "",
            "date": start, "duration": duration, "scheduled_duration": scheduled,
            "participants": _names(invitees + [t["speaker"] for t in turns]),
            "turns": turns, "vendor_summary": summary}


# ---------- Fireflies ----------

FIREFLIES_LIST = """
query Transcripts($fromDate: DateTime, $limit: Int, $skip: Int) {
  transcripts(fromDate: $fromDate, limit: $limit, skip: $skip) {
    id title date duration
    speakers { name }
    meeting_attendees { displayName name }
    summary { overview short_summary keywords }
  }
}"""

FIREFLIES_ONE = """
query Transcript($id: String!) {
  transcript(id: $id) { id sentences { speaker_name text start_time } }
}"""


def _graphql(http: Http, api_key: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    data = http.request("POST", FIREFLIES_GRAPHQL,
                        {"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"}, body).json()
    if data.get("errors"):
        codes = sorted({str((e.get("extensions") or {}).get("code") or "error")
                        for e in data["errors"]})
        raise ApiError(f"Fireflies GraphQL error: {', '.join(codes)}")
    return data["data"]


def fetch_fireflies(api_key: str, since: datetime, http: Http, cache: RawCache | None,
                    want_sentences: Callable[[dict], bool] = lambda m: True) -> list[dict]:
    """List metadata (skip/limit pages of 50), then sentences only for meetings worth reading."""
    meetings, skip = [], 0
    while True:
        page = _graphql(http, api_key, FIREFLIES_LIST,
                        {"fromDate": _iso(since), "limit": FIREFLIES_PAGE, "skip": skip})
        batch = page.get("transcripts") or []
        if cache:
            cache.save("fireflies", f"list-{skip:05d}", page)
        meetings += batch
        if len(batch) < FIREFLIES_PAGE:
            break
        skip += FIREFLIES_PAGE
    for meeting in meetings:
        meeting["sentences"] = None
        if want_sentences(normalize_fireflies(meeting)):
            one = _graphql(http, api_key, FIREFLIES_ONE, {"id": meeting["id"]})["transcript"]
            if cache:
                cache.save("fireflies", f"transcript-{meeting['id']}", one)
            meeting["sentences"] = one.get("sentences") or []
    return meetings


def normalize_fireflies(item: dict) -> dict:
    date = item.get("date")
    iso = (datetime.fromtimestamp(date / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
           if isinstance(date, (int, float)) else item.get("dateString") or date)
    turns = [{"speaker": s.get("speaker_name") or "Unknown", "text": s.get("text", ""),
              "start": _seconds(s.get("start_time"))} for s in item.get("sentences") or []]
    summary = item.get("summary") or {}
    attendees = [a.get("displayName") or a.get("name") for a in item.get("meeting_attendees") or []]
    duration = item.get("duration")
    return {"source": "fireflies", "id": str(item.get("id")), "title": item.get("title") or "",
            "date": iso, "duration": duration * 60 if duration is not None else None,  # minutes
            "participants": _names([s.get("name") for s in item.get("speakers") or []]
                                   + attendees),
            "turns": turns,
            "vendor_summary": summary.get("overview") or summary.get("short_summary")}


# ---------- JSON export (e.g. produced through a ChatGPT/Codex connector) ----------

def load_export(path: Path) -> list[dict]:
    """One export file or every *.json in a directory. A file holds either
    {"source", "meetings": [...]}, a list of meetings, or a single meeting object."""
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    meetings = []
    for f in files:
        data = _parse_lenient(f)
        if data is None:
            log.warning("skipping unreadable export file %s", f.name)
            continue
        if isinstance(data, dict) and "meetings" in data:
            items, source = data["meetings"], data.get("source")
        else:
            items, source = (data if isinstance(data, list) else [data]), None
        meetings += [normalize_export(m, source) for m in items]
    return dedupe(meetings)


def dedupe(meetings: list[dict]) -> list[dict]:
    """One meeting per (source, id); an index entry loses to a file that has the transcript."""
    best: dict[tuple, dict] = {}
    for m in meetings:
        key = (m["source"], m["id"]) if m["id"] else (m["source"], m["title"], m["date"])
        if key not in best or len(m["turns"]) > len(best[key]["turns"]):
            best[key] = m
    return list(best.values())


ESCAPE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])?')


def repair_escapes(text: str) -> str:
    """Walk escapes pairwise; double any backslash that does not start a valid JSON escape."""
    return ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "\\\\", text)


def _parse_lenient(path: Path):
    """Exports written by an LLM agent sometimes carry invalid backslash escapes; repair those."""
    text = path.read_text(encoding="utf-8")
    for candidate in (text, repair_escapes(text)):
        try:
            return json.loads(candidate, strict=False)
        except ValueError:
            continue
    return None


def normalize_export(item: dict, default_source: str | None = None) -> dict:
    duration = item.get("duration_minutes")  # the skills' export also says "duration" (minutes)
    if duration is None and isinstance(item.get("duration"), (int, float)):
        duration = float(item["duration"])
    participants = [p.get("name") if isinstance(p, dict) else p
                    for p in item.get("participants") or []]
    roles = {p["name"]: p.get("role") for p in item.get("participants") or []
             if isinstance(p, dict) and p.get("name") and p.get("role")}
    turns = [{"speaker": t.get("speaker") or "Unknown", "text": t.get("text", ""),
              "start": _seconds(t.get("timestamp", t.get("start")))}
             for t in item.get("transcript") or []]
    meeting = {"source": item.get("source") or default_source or "export",
               "id": str(item.get("id") or ""), "title": item.get("title") or "",
               "date": item.get("date"),
               "duration": float(duration) * 60 if duration is not None else None,
               "participants": _names(participants), "turns": turns,
               "vendor_summary": item.get("vendor_summary") or item.get("summary")}
    if roles:
        meeting["participant_roles"] = {n: r for n, r in roles.items() if not EMAIL.search(r)}
    return meeting


def window_start(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def in_window(meeting: dict, since: datetime) -> bool:
    if not meeting.get("date"):
        return True
    try:
        when = datetime.fromisoformat(str(meeting["date"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when >= since
