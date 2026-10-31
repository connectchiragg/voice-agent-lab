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
    return any(term and re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", low)
               for term in terms)


def fathom_search(http: httpx.Client, terms: list[str], key: str | None = None) -> tuple[str, list[str]]:
    from app import workspaces
    key = (key or (os.getenv("FATHOM_API_KEY", "") if workspaces.is_owner() else "")).strip()
    if not key:
        return "Fathom: not configured.", []
    response = http.get(FATHOM_MEETINGS, params={"include_summary": "true", "limit": 50},
                        headers={"X-Api-Key": key, "Accept": "application/json"})
    if response.status_code in (401, 403):
        return "Fathom: the API key was refused.", []
    response.raise_for_status()
    lines, urls = [], []
    for item in response.json().get("items") or []:
        invitees = " ".join(f"{i.get('name', '')} {i.get('email_domain', '')} {i.get('email', '').rpartition('@')[2]}"
                            for i in item.get("calendar_invitees") or [])
        title = item.get("meeting_title") or item.get("title") or ""
        if not matches(f"{title} {invitees}", terms):
            continue
        url = item.get("share_url") or item.get("url") or f"https://fathom.video/calls/{item.get('recording_id')}"
        summary = ((item.get("default_summary") or {}).get("markdown_formatted") or "")
        summary = re.sub(r"\s+", " ", re.sub(r"[#*_\[\]()]", " ", summary))[:900]
        # A summary/listing alone often omits the actual questions. Read at most two
        # matching transcripts using the same official endpoint as notetakers/fathom.py.
        turns = []
        if item.get("recording_id") and len(lines) < 2:
            try:
                detail = http.get(f"https://api.fathom.ai/external/v1/recordings/{item['recording_id']}/transcript",
                                  headers={"X-Api-Key": key, "Accept": "application/json"})
                if detail.status_code == 200:
                    turns = [{"speaker": (t.get("speaker") or {}).get("display_name", "Speaker"),
                              "text": t.get("text", "")} for t in detail.json().get("transcript") or []]
            except httpx.HTTPError:
                pass  # Keep the summary; do not invent missing transcript content.
        if turns:
            summary += "\nTranscript excerpts:\n" + transcript_excerpt(turns, limit=1200)
        date = (item.get("recording_start_time") or item.get("created_at") or "")[:10]
        lines.append(f"- {date} '{title[:80]}' [source_url: {url}]: {summary}")
        urls.append(url)
        if len(lines) >= 4:
            break
    return ("Fathom recordings (summary unless marked transcript excerpts):\n" + "\n".join(lines)) if lines else "Fathom: no matching recordings.", urls


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
    """Earlier recorded rounds with this company, from the derived evaluation file (the owner's:
    nothing for a guest)."""
    from app import workspaces
    if path is None and not workspaces.is_owner():
        return "", []
    paths = [path] if path else [history_path()]
    if path is None:
        from app.profile.config import settings
        paths.insert(0, settings().performance_path)  # Updated after every evaluated round.
    interviews = []
    for source in paths:
        try:
            interviews.extend(json.loads(source.read_text(encoding="utf-8")).get("interviews") or [])
        except (OSError, ValueError):
            continue
    if not interviews:
        return "Interview history: not available.", []
    lines, urls, seen = [], [], set()
    for item in sorted(interviews, key=lambda i: str(i.get("date") or ""), reverse=True):
        if not matches(f"{item.get('company') or ''} {item.get('title') or ''}", [company] if company else terms):
            continue
        identity = item.get("interview_id") or f"{item.get('source')}:{item.get('id')}"
        if identity in seen:
            continue
        seen.add(identity)
        url = f"valr://history/{item.get('source')}/{item.get('id')}"
        weak = "; ".join(str(w.get("topic", ""))[:60] for w in item.get("weak_answers") or [])[:240]
        questions = item.get("questions_asked") or [q.get("question", "") for q in item.get("question_feedback") or []]
        asked = "; ".join(str(q)[:120] for q in questions[:4])
        people = ", ".join(str(p.get("name") if isinstance(p, dict) else p)[:40]
                           for p in item.get("interviewers") or [])[:160]
        feedback = "; ".join(str(q.get("improve") or "")[:100] for q in (item.get("question_feedback") or [])[:2])
        lines.append(f"- {str(item.get('date', ''))[:10]} {item.get('company')}: "
                     f"{item.get('round_type')} [source_url: {url}]; interviewers: {people or '-'}; "
                     f"asked: {asked or '-'}; review suggestions (not interviewer statements): {feedback or weak or '-'}")
        urls.append(url)
    return ("Earlier recorded rounds:\n" + "\n".join(lines[:4])) if lines else \
        "Interview history: no earlier recorded rounds with this company.", urls[:4]


def transcript_excerpt(turns: list[dict], limit: int = 1800) -> str:
    """Questions and adjacent answers, not just the start of a long recording.

    This is source material, not an assessment of interviewer sentiment or a pass/fail.
    Preserve order and speaker labels; bound the text sent to the researcher.
    """
    selected: set[int] = set()
    for index, turn in enumerate(turns):
        text = str(turn.get("text") or "")
        if "?" in text or RELEVANT.search(text):
            selected.update(range(index, min(index + 2, len(turns))))
    lines, size = [], 0
    for index in sorted(selected):
        turn = turns[index]
        text = re.sub(r"\s+", " ", str(turn.get("text") or "")).strip()
        text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b", "[email hidden]", text)
        line = f"{str(turn.get('speaker') or 'Speaker')[:60]}: {text[:450]}"
        if size + len(line) > limit:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def same_company_rows(rows: list[tuple], company: str, domain: str) -> list[tuple]:
    """Rows (..., company_domain, company, event_company) of this company: its domain, else (no
    domain on the recording: a phone call, an older Fathom row) the same company by name
    (app/company_names.same: "Acme" ~ "Acme Robotics", suffixes ignored). -> the first six columns."""
    from app import company_names
    name = company or (company_names.from_domain(domain) if domain else "")
    out = []
    for row in rows:
        head, (row_domain, row_company, row_event_company) = row[:6], row[6:9]
        if row_domain:
            if domain and row_domain.lower() == domain.lower():
                out.append(head)
        elif name and any(c and company_names.same(c, name) for c in (row_company, row_event_company)):
            out.append(head)
    return out


async def stored_rounds(connection: Any, event: Any) -> tuple[str, list[str]]:
    """Read previous rounds of this exact company from the live recording store.

    No provider calls, notifications or transcript writes. Domain wins over ambiguous
    company names. Hidden recordings and the current/future round are excluded.
    """
    from app import google_oauth
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    if not key or not (event.company_domain or event.company):
        return "", []
    cursor = await connection.execute(
        "SELECT r.provider, r.external_id, r.title, r.started_at, r.share_url, t.sealed, "
        "r.company_domain, r.company, r.event_company "
        "FROM notetaker_recordings r JOIN recording_transcripts t "
        "ON t.provider = r.provider AND t.external_id = r.external_id "
        "WHERE r.started_at < %s AND (r.event_key IS NULL OR r.event_key <> %s) "
        "AND NOT EXISTS (SELECT 1 FROM notetaker_hidden h "
        "WHERE h.provider = r.provider AND h.external_id = r.external_id) "
        "AND ((%s <> '' AND lower(r.company_domain) = lower(%s)) "
        "OR ((r.company_domain IS NULL OR r.company_domain = '') "
        "AND (coalesce(r.company, '') <> '' OR coalesce(r.event_company, '') <> ''))) "
        "ORDER BY r.started_at DESC LIMIT 40",
        (event.start_utc, event.key, event.company_domain, event.company_domain))
    lines, urls = [], []
    for provider, external_id, title, started, share_url, sealed in same_company_rows(
            await cursor.fetchall(), event.company, event.company_domain)[:3]:
        try:
            data = json.loads(google_oauth.decrypt(key, sealed))
        except (google_oauth.ReconnectNeeded, ValueError):
            continue
        excerpt_text = transcript_excerpt(data.get("turns") or [], limit=1200)
        if not excerpt_text:
            continue
        url = share_url or f"valr://recording/{provider}/{external_id}"
        lines.append(f"Earlier round {str(started)[:10]} '{title[:120]}' "
                     f"[source_url: {url}] (transcript excerpts):\n{excerpt_text}")
        urls.append(url)
    return "\n\n".join(lines), urls


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
