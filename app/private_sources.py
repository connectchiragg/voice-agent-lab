"""R1's private sources: Gmail (read-only), Fathom recordings, the derived interview
history and pushed context. Only metadata and short relevant excerpts come out.

Synchronous (the researcher's tools run in a thread); the activity fetches the Gmail
access token, the pushed context and the history beforehand. Every function returns
(text for the researcher, source URLs) and never raises for a missing source: a
source that is not configured says so in one line.

- Gmail: `threads.list` with a search query, then each thread's messages (format
  "full"): subject, sender name and domain (never the address), date, and the lines
  that mention the interview's essentials (round, schedule, JD, prep, salary...),
  quoted replies dropped. At most MAX_THREADS threads, EXCERPT_CHARS each.
- Fathom (the key pasted on the dashboard, decrypted by the activity, else
  FATHOM_API_KEY; the ongoing recordings source): meetings whose title or invitees
  mention the company or an interviewer: title, date, and the start of Fathom's summary.
- Granola (the dashboard key, else GRANOLA_API_KEY; notes only): notes of the last 180
  days whose title mentions the company or an interviewer: title, date, the start of
  Granola's summary.
- History: the profile import's one-time Fireflies/Fathom export, as analysed in
  data/evaluation.json (VALR_EVALUATION_PATH): earlier rounds with this company,
  questions asked, weak spots. Fireflies itself is not called.
- Pushed context: what a Mac-side job POSTed to /valr/context for this event.
"""
import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
FATHOM_MEETINGS = "https://api.fathom.ai/external/v1/meetings"
MAX_THREADS = 5
MAX_MESSAGES = 4  # Per thread, newest.
EXCERPT_CHARS = 700
TOTAL_CHARS = 3500
RELEVANT = re.compile(
    r"interview|round|schedul|slot|calendar|invite|panel|onsite|on-site|virtual|zoom|meet\b|"
    r"teams|job description|\bjd\b|role|position|responsibilit|requirement|prepar|prep\b|"
    r"topic|system design|coding|dsa|leetcode|hackerrank|assignment|take-home|take home|"
    r"behaviou?ral|hiring manager|recruiter|feedback|next step|offer|salary|compensation|ctc|"
    r"\blpa\b|budget|range|notice period|joining|equity|esop|stock|bonus|location|hybrid|remote",
    re.I)
QUOTE_START = re.compile(r"^(on .+ wrote:|-{2,} ?original message|from: .+@)", re.I)


def gmail_url(thread_id: str) -> str:
    return f"https://mail.google.com/mail/#all/{thread_id}"


def sender(value: str) -> str:
    """'Asha Rao <asha@acme.com>' -> 'Asha Rao (acme.com)': no address."""
    match = re.match(r'\s*"?([^"<]*)"?\s*<([^>]+)>', value or "")
    if match:
        name, address = match.group(1).strip(), match.group(2)
    else:
        name, address = "", (value or "").strip()
    domain = address.rpartition("@")[2]
    return f"{name or 'someone'} ({domain})" if domain else (name or "someone")


def body_text(payload: dict[str, Any]) -> str:
    """The first text/plain part (else text/html stripped), decoded."""
    stack, html = [payload], ""
    while stack:
        part = stack.pop(0)
        data = (part.get("body") or {}).get("data")
        mime = part.get("mimeType", "")
        if data and mime == "text/plain":
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        if data and mime == "text/html" and not html:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            html = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))
        stack.extend(part.get("parts") or [])
    return html


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Relevant lines of a message, before any quoted reply; signatures' first lines kept
    (a sender's title often sits there, which the HR guard uses)."""
    lines: list[str] = []
    for line in text.replace("\r", "").split("\n"):
        stripped = line.strip()
        if QUOTE_START.match(stripped):
            break
        if not stripped or stripped.startswith(">"):
            continue
        if RELEVANT.search(stripped) or len(lines) < 1:
            lines.append(stripped)
    out = " / ".join(lines)
    return out[:limit] + ("..." if len(out) > limit else "")


def header(message: dict[str, Any], name: str) -> str:
    for item in (message.get("payload") or {}).get("headers") or []:
        if item.get("name", "").lower() == name.lower():
            return str(item.get("value", ""))
    return ""


def gmail_search(http: httpx.Client, access_token: str | None, query: str,
                 status: str = "") -> tuple[str, list[str]]:
    if not access_token:
        return f"Gmail: not available ({status or 'not connected'}).", []
    auth = {"Authorization": f"Bearer {access_token}"}
    listing = http.get(f"{GMAIL}/threads", params={"q": f"{query} newer_than:180d",
                                                   "maxResults": MAX_THREADS}, headers=auth)
    if listing.status_code in (401, 403):
        return "Gmail: access was refused (reconnect needed).", []
    listing.raise_for_status()
    threads = listing.json().get("threads") or []
    if not threads:
        return f"Gmail search '{query}': no threads.", []
    blocks, urls, total = [], [], 0
    for item in threads[:MAX_THREADS]:
        thread = http.get(f"{GMAIL}/threads/{item['id']}", params={"format": "full"}, headers=auth)
        if thread.status_code != 200:
            continue
        messages = (thread.json().get("messages") or [])[-MAX_MESSAGES:]
        if not messages:
            continue
        url = gmail_url(item["id"])
        lines = [f"Thread [source_url: {url}] subject: {header(messages[0], 'Subject')[:120]}"]
        for message in messages:
            text = excerpt(body_text(message.get("payload") or {}))
            lines.append(f"- {header(message, 'Date')[:31]} from {sender(header(message, 'From'))}: "
                         f"{text or message.get('snippet', '')[:200]}")
        block = "\n".join(lines)
        if total + len(block) > TOTAL_CHARS:
            break
        blocks.append(block)
        urls.append(url)
        total += len(block)
    return f"Gmail search '{query}':\n" + "\n\n".join(blocks), urls


def matches(text: str, terms: list[str]) -> bool:
    low = (text or "").lower()
    return any(term and term.lower() in low for term in terms)


def fathom_search(http: httpx.Client, terms: list[str], key: str | None = None) -> tuple[str, list[str]]:
    key = (key or os.getenv("FATHOM_API_KEY", "")).strip()
    if not key:
        return "Fathom: not configured.", []
    response = http.get(FATHOM_MEETINGS, params={"include_summary": "true", "limit": 50},
                        headers={"X-Api-Key": key, "Accept": "application/json"})
    if response.status_code in (401, 403):
        return "Fathom: the API key was refused.", []
    response.raise_for_status()
    lines, urls = [], []
    for item in response.json().get("items") or []:
        invitees = " ".join(f"{i.get('name', '')} {i.get('email_domain', '')}"
                            for i in item.get("calendar_invitees") or [])
        title = item.get("meeting_title") or item.get("title") or ""
        if not matches(f"{title} {invitees}", terms):
            continue
        url = item.get("share_url") or item.get("url") or f"https://fathom.video/calls/{item.get('recording_id')}"
        summary = ((item.get("default_summary") or {}).get("markdown_formatted") or "")
        summary = re.sub(r"\s+", " ", re.sub(r"[#*_\[\]()]", " ", summary))[:600]
        date = (item.get("recording_start_time") or item.get("created_at") or "")[:10]
        lines.append(f"- {date} '{title[:80]}' [source_url: {url}]: {summary}")
        urls.append(url)
        if len(lines) >= 4:
            break
    return ("Fathom recordings:\n" + "\n".join(lines)) if lines else "Fathom: no matching recordings.", urls


GRANOLA_NOTES = "https://public-api.granola.ai/v1/notes"
GRANOLA_DAYS = 180


def granola_search(http: httpx.Client, terms: list[str], key: str | None = None,
                   now: datetime | None = None) -> tuple[str, list[str]]:
    """Granola notes about this company or its interviewers (titles matched, then up to 3 read)."""
    key = (key or os.getenv("GRANOLA_API_KEY", "")).strip()
    if not key:
        return "", []  # Optional source: silent when not connected.
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    since = (now or datetime.now(timezone.utc)) - timedelta(days=GRANOLA_DAYS)
    params: dict[str, Any] = {"created_after": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "page_size": 30}
    found: list[dict] = []
    for _ in range(5):
        response = http.get(GRANOLA_NOTES, params=params, headers=headers)
        if response.status_code in (401, 403):
            return "Granola: the API key was refused.", []
        response.raise_for_status()
        data = response.json()
        found += [n for n in data.get("notes") or data.get("data") or [] if isinstance(n, dict)
                  and matches(str(n.get("title") or ""), terms)]
        if len(found) >= 3 or not (data.get("hasMore") and data.get("cursor")):
            break
        params["cursor"] = data["cursor"]
    lines, urls = [], []
    for note in found[:3]:
        detail = http.get(f"{GRANOLA_NOTES}/{note.get('id')}", headers=headers)
        body = detail.json() if detail.status_code == 200 else {}
        summary = str(body.get("summary_text") or body.get("summary_markdown") or "")
        summary = re.sub(r"\s+", " ", re.sub(r"[#*_\[\]()]", " ", summary))[:600]
        url = f"valr://granola/{note.get('id')}"
        lines.append(f"- {str(note.get('created_at') or '')[:10]} '{str(note.get('title') or '')[:80]}' "
                     f"[source_url: {url}]: {summary}")
        urls.append(url)
    return ("Granola notes:\n" + "\n".join(lines)) if lines else "Granola: no matching notes.", urls


def history_path() -> Path:
    path = Path(os.getenv("VALR_EVALUATION_PATH") or "data/evaluation.json").expanduser()
    return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path


def history(company: str, terms: list[str], path: Path | None = None) -> tuple[str, list[str]]:
    """Earlier recorded rounds with this company, from the derived evaluation file."""
    try:
        data = json.loads((path or history_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "Interview history: not available.", []
    lines, urls = [], []
    for item in data.get("interviews") or []:
        if not matches(f"{item.get('company') or ''} {item.get('title') or ''}", [company] + terms):
            continue
        url = f"valr://history/{item.get('source')}/{item.get('id')}"
        weak = "; ".join(str(w.get("topic", ""))[:60] for w in item.get("weak_answers") or [])[:240]
        asked = "; ".join(str(q)[:80] for q in (item.get("questions_asked") or [])[:5])
        people = ", ".join(str(p.get("name") if isinstance(p, dict) else p)[:40]
                           for p in item.get("interviewers") or [])[:160]
        lines.append(f"- {str(item.get('date', ''))[:10]} {item.get('company')}: "
                     f"{item.get('round_type')} [source_url: {url}]; interviewers: {people or '-'}; "
                     f"asked: {asked or '-'}; his weak spots: {weak or '-'}")
        urls.append(url)
    return ("Earlier recorded rounds:\n" + "\n".join(lines[:4])) if lines else \
        "Interview history: no earlier recorded rounds with this company.", urls[:4]


def pushed(texts: list[str]) -> tuple[str, list[str]]:
    """Decrypted pushes (JSON objects with free-text fields), as excerpts."""
    lines, urls = [], []
    for index, text in enumerate(texts[:3]):
        try:
            data = json.loads(text)
        except ValueError:
            continue
        url = f"valr://pushed/{data.get('id') or index}"
        body = " ".join(str(data.get(k, "")) for k in ("subject", "summary", "excerpt", "notes"))
        lines.append(f"- [source_url: {url}] {re.sub(r'\\s+', ' ', body)[:EXCERPT_CHARS]}")
        urls.append(url)
    return ("Pushed email context:\n" + "\n".join(lines)) if lines else "", urls
