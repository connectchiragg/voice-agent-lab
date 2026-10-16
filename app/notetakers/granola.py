"""Granola (https://docs.granola.ai): REST at https://public-api.granola.ai/v1,
`Authorization: Bearer grn_...`. API keys need a Business or Enterprise plan; there is
no OAuth for the REST API (the official MCP server, https://mcp.granola.ai/mcp, uses
browser OAuth and is meant for assistants, not servers).

- Verify: `GET /v1/notes?page_size=1`.
- List: `GET /v1/notes?created_after=&created_before=&cursor=&page_size=30`.
- One note: `GET /v1/notes/{id}?include=transcript` (summary_markdown, attendees,
  calendar_event, web_url, transcript with absolute ISO start times); a 413
  TRANSCRIPT_TOO_LARGE falls back to the paged `GET /v1/notes/{id}/transcript`.
- Granola keeps no audio or video: there is nothing to embed; `web_url` opens the note.
- Without an API key (Free / Pro plans): `parse_export` reads the CSV export Granola
  emails from Settings (title, summary, transcript), a JSON export (the API's or the
  MCP's note objects) or one Markdown note.
"""
import csv
import hashlib
import io
import json
import re
from datetime import datetime
from typing import Any

import httpx

from app.notetakers.base import ProviderError, Recording, check, safe_link, utc
from app.profile.recordings import _names

NAME = "granola"
API = "https://public-api.granola.ai/v1"
PAGE = 30
MAX_PAGES = 10
TRANSCRIPT_PAGES = 40


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _offset(start: Any, origin: datetime | None) -> float | None:
    if isinstance(start, (int, float)):
        return float(start)
    when = utc(start)
    if not (when and origin):
        return None
    return max((datetime.fromisoformat(when.replace("Z", "+00:00")) - origin).total_seconds(), 0.0)


def normalize(note: dict[str, Any]) -> dict[str, Any]:
    """A Granola note -> the profile importer's meeting shape."""
    event = note.get("calendar_event") or {}
    start = utc(event.get("scheduled_start_time") or note.get("created_at"))
    end = utc(event.get("scheduled_end_time"))
    origin = None
    transcript = note.get("transcript") or []
    first = next((utc(t.get("start_time")) for t in transcript if isinstance(t, dict) and utc(t.get("start_time"))), None)
    if first or start:
        origin = datetime.fromisoformat((first or start).replace("Z", "+00:00"))
    turns = []
    for t in transcript:
        if not isinstance(t, dict):
            continue
        speaker = t.get("speaker") or {}
        name = speaker.get("name") if isinstance(speaker, dict) else str(speaker)
        if not name and isinstance(speaker, dict):
            name = "Me" if speaker.get("attribution") == "me" else speaker.get("diarization_label") or "Them"
        turns.append({"speaker": name or "Unknown", "text": str(t.get("text") or ""),
                      "start": _offset(t.get("start_time"), origin)})
    duration = None
    if start and end:
        duration = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                    - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    attendees = [a.get("name") for a in note.get("attendees") or [] if isinstance(a, dict)]
    return {"source": NAME, "id": str(note.get("id") or ""),
            "title": str(note.get("title") or event.get("event_title") or ""),
            "date": start, "duration": duration, "participants": _names(attendees + [t["speaker"] for t in turns]),
            "turns": turns, "vendor_summary": note.get("summary_markdown") or note.get("summary_text")}


def recording(note: dict[str, Any], with_meeting: bool = False) -> Recording:
    event = note.get("calendar_event") or {}
    emails = [str(a.get("email") or "").lower() for a in (note.get("attendees") or [])
              + (event.get("invitees") or []) if isinstance(a, dict) and a.get("email")]
    return Recording(provider=NAME, external_id=str(note.get("id") or ""),
                     title=str(note.get("title") or event.get("event_title") or ""),
                     start=utc(event.get("scheduled_start_time") or note.get("created_at")),
                     end=utc(event.get("scheduled_end_time")),
                     share_url=safe_link(str(note.get("web_url") or "")), embed_url="",
                     attendee_emails=sorted(set(emails)),
                     meeting=normalize(note) if with_meeting else None)


def timestamp_url(share_url: str, seconds: int) -> str:
    return ""  # Notes have no player.


async def verify(key: str, http: httpx.AsyncClient) -> str:
    check(await http.get(f"{API}/notes", headers=headers(key), params={"page_size": 1}), "Granola")
    return ""


async def list_recordings(key: str, start: datetime, end: datetime, http: httpx.AsyncClient,
                          with_meeting: bool = False) -> list[Recording]:
    params: dict[str, Any] = {"created_after": utc(start.isoformat()), "created_before": utc(end.isoformat()),
                              "page_size": PAGE}
    out: list[Recording] = []
    for _ in range(MAX_PAGES):
        data = check(await http.get(f"{API}/notes", headers=headers(key), params=params), "Granola").json()
        out += [recording(n) for n in data.get("notes") or data.get("data") or data.get("items") or []
                if isinstance(n, dict)]
        cursor = data.get("cursor") or data.get("next_cursor")
        if not (cursor and data.get("hasMore", True)):
            break
        params["cursor"] = cursor
    return out


async def get_recording(key: str, external_id: str, http: httpx.AsyncClient,
                        listed: Recording | None = None) -> Recording:
    response = await http.get(f"{API}/notes/{external_id}", headers=headers(key), params={"include": "transcript"})
    if response.status_code == 413:
        note = check(await http.get(f"{API}/notes/{external_id}", headers=headers(key)), "Granola").json()
        note["transcript"] = await _paged_transcript(key, external_id, http)
    else:
        note = check(response, "Granola").json()
    if not isinstance(note, dict) or not note.get("id"):
        raise ProviderError("Granola returned no note")
    return recording(note, with_meeting=True)


async def _paged_transcript(key: str, external_id: str, http: httpx.AsyncClient) -> list[dict]:
    params: dict[str, Any] = {"page_size": 100}
    out: list[dict] = []
    for _ in range(TRANSCRIPT_PAGES):
        data = check(await http.get(f"{API}/notes/{external_id}/transcript", headers=headers(key),
                                    params=params), "Granola").json()
        out += data.get("transcript") or []
        if not (data.get("hasMore") and data.get("cursor")):
            break
        params["cursor"] = data["cursor"]
    return out


# Exports (no API key needed).

def _export_id(*parts: str) -> str:
    return "export-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def parse_export(text: str, filename: str = "") -> list[Recording]:
    """A Granola export -> recordings with their meetings. Raises ValueError if unreadable."""
    stripped = text.lstrip("﻿").strip()
    if stripped.startswith(("{", "[")):
        data = json.loads(stripped)
        notes = data if isinstance(data, list) else data.get("notes") or data.get("data") or [data]
        out = []
        for note in notes:
            if not isinstance(note, dict):
                continue
            note = dict(note)
            note.setdefault("id", _export_id(str(note.get("title")), str(note.get("created_at"))))
            if isinstance(note.get("transcript"), str):
                note["transcript"] = _text_transcript(note["transcript"])
            out.append(recording(note, with_meeting=True))
        return out
    if filename.lower().endswith(".csv") or _looks_csv(stripped):
        return _from_csv(stripped)
    return [_from_markdown(stripped, filename)]


def _looks_csv(text: str) -> bool:
    head = text.splitlines()[0].lower() if text else ""
    return "title" in head and "," in head


def _text_transcript(text: str) -> list[dict]:
    """"Me: ..." / "Them: ..." lines -> transcript items without times."""
    out = []
    for line in text.splitlines():
        match = re.match(r"^\s*([^:]{1,40}):\s+(.+)$", line)
        if match:
            out.append({"speaker": {"name": match.group(1).strip()}, "text": match.group(2).strip()})
        elif line.strip() and out:
            out[-1]["text"] += " " + line.strip()
    return out


def _from_csv(text: str) -> list[Recording]:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("empty CSV")
    out = []
    for row in rows:
        row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
        title = row.get("title", "")
        created = row.get("created_at") or row.get("date") or row.get("created") or ""
        note = {"id": row.get("id") or _export_id(title, created), "title": title, "created_at": created,
                "summary_markdown": row.get("summary") or row.get("notes") or "",
                "transcript": _text_transcript(row.get("transcript", ""))}
        out.append(recording(note, with_meeting=True))
    return out


def _from_markdown(text: str, filename: str) -> Recording:
    lines = text.splitlines()
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), filename.rsplit(".", 1)[0])
    split = re.split(r"(?im)^#+\s*transcript\s*$", text, maxsplit=1)
    note = {"id": _export_id(title, text[:200]), "title": title, "summary_markdown": split[0].strip(),
            "transcript": _text_transcript(split[1]) if len(split) > 1 else []}
    return recording(note, with_meeting=True)
