"""The provider contract, and the pure parts shared by every notetaker.

A provider (app/notetakers/fathom.py, fireflies.py, granola.py) offers:

- `verify(key, http) -> account label`: one cheap authenticated call; raises
  `KeyRefused` on 401/403 so the dashboard can say "that key was refused".
- `list_recordings(key, start, end, http) -> [Recording]`: metadata in a date range.
- `get_recording(key, external_id, http) -> Recording`: with `meeting` filled in,
  the normalised meeting of the profile importer (app/profile/recordings.py:
  {source, id, title, date, duration, participants, turns, vendor_summary}).

A Recording also carries the links the dashboard needs: `share_url` (a deep link
that opens the recording at the provider; the video itself plays from the provider's
official download API, app/notetakers/media.py), and `attendee_emails`, used only in
memory to match a recording to a calendar event; they are never stored or shown.
`company`, `company_domain`, `interviewers` and `invitee_hashes` come from the external
invitees (`invitee_details`) and are stored, so a past interview keeps its company and
people after the calendar forgets the event. `embed_url` is kept empty (the old iframe).
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx

TIMEOUT_S = 20
MAX_MOMENTS = 8


class KeyRefused(Exception):
    """The provider answered 401/403 to this key."""


class ProviderError(Exception):
    """Any other provider failure; the message never contains the key."""


@dataclass
class Recording:
    provider: str
    external_id: str
    title: str = ""
    start: str | None = None  # ISO 8601 UTC
    end: str | None = None
    share_url: str = ""
    embed_url: str = ""
    attendee_emails: list[str] = field(default_factory=list)
    meeting: dict[str, Any] | None = None  # The importer's normalised meeting, when fetched.
    company: str = ""
    company_domain: str = ""
    interviewers: list[str] = field(default_factory=list)
    invitee_hashes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        """What may be stored and shown: no emails, no transcript."""
        return {"provider": self.provider, "external_id": self.external_id, "title": self.title,
                "start": self.start, "end": self.end, "share_url": self.share_url,
                "embed_url": self.embed_url, "company": self.company, "company_domain": self.company_domain,
                "interviewers": self.interviewers, "invitee_hashes": self.invitee_hashes}


def check(response: httpx.Response, provider: str) -> httpx.Response:
    if response.status_code in (401, 403):
        raise KeyRefused(f"{provider} refused the key (HTTP {response.status_code})")
    if response.status_code >= 400:
        raise ProviderError(f"{provider} returned HTTP {response.status_code}")
    return response


def utc(value: Any) -> str | None:
    """Any ISO 8601 text or epoch milliseconds -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def with_query(url: str, **params: Any) -> str:
    """`url` with `params` set in its query string (existing keys replaced)."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query)) | {k: str(v) for k, v in params.items()}
    return urlunsplit(parts._replace(query=urlencode(query)))


# The company and people of a recording, from its calendar invitees.

FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
            "yahoo.co.in", "icloud.com", "me.com", "proton.me", "protonmail.com", "aol.com", "zoho.com",
            "rediffmail.com", "gmx.com", "mail.com", "yandex.com", "msn.com"}
TOOL_DOMAINS = ("calendar.google.com", "google.com", "fathom.video", "fireflies.ai", "granola.ai",
                "zoom.us", "calendly.com", "resource.calendar.google.com")
SUFFIXES = {"com", "co", "in", "io", "ai", "net", "org", "dev", "tech", "app", "us", "uk", "de", "so",
            "xyz", "inc", "global", "group", "careers", "jobs", "hr", "mail", "team", "security", "example"}
# Words that are never a company on their own: round labels and title filler.
NOT_COMPANY = {"me", "you", "round", "interview", "interviews", "call", "meeting", "min", "mins", "and", "with",
               "screen", "screening", "intro", "chat", "sync", "level", "technical", "tech", "hr", "final",
               "onsite", "loop", "panel", "discussion", "for", "the", "of", "at", "x", "vs", "catch", "up",
               "followup", "follow", "first", "second", "third", "system", "design", "coding", "hiring",
               "manager", "culture", "fit", "behavioral", "behavioural", "test", "unknown", "fathom",
               "fireflies", "granola", "zoom", "google", "teams", "meet"}
ROUND_TOKEN = re.compile(r"^(?:l|r|round|level|lvl|stage)\d{1,2}$|^\d{1,2}$")


def email_hash(email: str) -> str:
    """A short, one-way tag of an address: matches a lead's contact without storing the email."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def owner_words() -> set[str]:
    """The owner's own name words (VALR_OWNER_NAME / VALR_CANDIDATE_NAME / the profile's name)."""
    import os
    raw = " ".join(os.getenv(k, "") for k in ("VALR_OWNER_NAME", "VALR_CANDIDATE_NAME"))
    try:
        from app.profile.config import settings
        raw += " " + (settings().candidate_name or "")
    except Exception:  # noqa: BLE001 - no profile config here: env only.
        pass
    return {w for w in re.findall(r"[a-z0-9]+", raw.lower()) if len(w) > 1}


def is_company(name: str, owner: set[str] | None = None) -> bool:
    """False for "", a round label ("L2", "Round 2", "Interview") or the owner's own name."""
    owner = owner_words() if owner is None else owner
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    rest = [w for w in words if w not in NOT_COMPANY and w not in owner and not ROUND_TOKEN.match(w)]
    return bool(rest)


def company_from_domain(domain: str) -> str:
    """"talent.quillworks.co.in" -> "Quillworks"; "" for a free-mail or tool domain."""
    domain = (domain or "").lower().strip(".")
    if not domain or domain in FREEMAIL or domain.endswith(TOOL_DOMAINS):
        return ""
    parts = domain.split(".")
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    core = parts[-1] if parts else ""
    return core.replace("-", " ").title() if core and core not in SUFFIXES else ""


def invitee_details(invitees: list[dict[str, Any]], owner_emails: set[str] | None = None) -> dict[str, Any]:
    """Fathom's `calendar_invitees` -> {company, company_domain, interviewers, invitee_hashes}.

    The external invitees (`is_external`, else not the owner's address or domain) are the
    interviewers; the most common non-free-mail domain among them names the company."""
    owner_emails = {e.lower() for e in (owner_emails or set())}
    owner_domains = {e.rpartition("@")[2] for e in owner_emails} - FREEMAIL
    people, domains = [], []
    for invitee in invitees or []:
        if not isinstance(invitee, dict):
            continue
        email = str(invitee.get("email") or "").strip().lower()
        domain = email.rpartition("@")[2]
        if email in owner_emails or (domain and domain in owner_domains):
            continue
        if invitee.get("is_external") is False:  # The recorder's own side.
            continue
        if domain.endswith(TOOL_DOMAINS):
            continue
        people.append((str(invitee.get("name") or "").strip(), email))
        if domain and domain not in FREEMAIL:
            domains.append(domain)
    domain = max(set(domains), key=domains.count) if domains else ""
    names = [n for n, email in people if n and "@" not in n]
    return {"company": company_from_domain(domain), "company_domain": domain,
            "interviewers": list(dict.fromkeys(names))[:8],
            "invitee_hashes": [email_hash(e) for _, e in people if e][:12]}


def safe_link(url: str) -> str:
    return url if (url or "").startswith("https://") else ""


# Matching a recording to a calendar event: time window + attendees.

MATCH_SLACK = timedelta(minutes=20)


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def match_score(recording: Recording, start: str, end: str, emails: set[str], title: str = "") -> float:
    """0 when it cannot be this event; otherwise higher is better.

    The recording must overlap the event's [start - 20 min, end + 20 min]; overlap share,
    shared attendee emails (the strongest signal) and a shared title word add up.
    """
    r_start, r_end = _dt(recording.start), _dt(recording.end) or _dt(recording.start)
    e_start, e_end = _dt(start), _dt(end)
    if not (r_start and e_start and e_end):
        return 0.0
    r_end = max(r_end or r_start, r_start + timedelta(minutes=1))
    lo, hi = max(r_start, e_start - MATCH_SLACK), min(r_end, e_end + MATCH_SLACK)
    if hi <= lo:
        return 0.0
    event_len = max((e_end - e_start).total_seconds(), 60)
    overlap = min((min(r_end, e_end) - max(r_start, e_start)).total_seconds(), event_len) / event_len
    score = 1.0 + max(overlap, 0.0)
    shared = {e.lower() for e in recording.attendee_emails} & {e.lower() for e in emails}
    score += 2.0 * len(shared)
    words = {w for w in re.findall(r"[a-z0-9]{4,}", title.lower())}
    if words & set(re.findall(r"[a-z0-9]{4,}", recording.title.lower())):
        score += 0.5
    return score


def best_match(recordings: list[Recording], start: str, end: str, emails: set[str],
               title: str = "") -> Recording | None:
    scored = [(match_score(r, start, end, emails, title), r) for r in recordings]
    scored = [(s, r) for s, r in scored if s > 0]
    return max(scored, key=lambda pair: pair[0])[1] if scored else None


# Key moments: timestamp jump links from the vendor summary or the transcript.

SUMMARY_LINK = re.compile(r"\[([^\]]{3,160})\]\((https://[^)\s]+)\)")
STAMP = re.compile(r"[?&](?:timestamp|t|time)=(\d+(?:\.\d+)?)")


def moments_from_summary(markdown: str | None) -> list[dict[str, Any]]:
    """Markdown links that carry a timestamp (Fathom's summary links each point to its moment)."""
    out: list[dict[str, Any]] = []
    for label, url in SUMMARY_LINK.findall(markdown or ""):
        stamp = STAMP.search(url)
        if stamp:
            out.append({"label": _clean(label), "seconds": int(float(stamp.group(1))), "url": url})
    return _spread(out)


def moments_from_turns(turns: list[dict[str, Any]], owner_names: set[str] = frozenset()) -> list[dict[str, Any]]:
    """Questions put to the owner: turns by someone else that end in "?" (at least 25 characters)."""
    owners = {n.lower() for n in owner_names}
    out = []
    for turn in turns or []:
        text, start = str(turn.get("text") or "").strip(), turn.get("start")
        if start is None or not text.endswith("?") or len(text) < 25:
            continue
        if str(turn.get("speaker") or "").lower() in owners:
            continue
        out.append({"label": _clean(text), "seconds": int(start), "url": ""})
    return _spread(out)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= 120 else text[:117].rstrip() + "…"


def _spread(moments: list[dict[str, Any]], gap_s: int = 60) -> list[dict[str, Any]]:
    """At most MAX_MOMENTS, at least a minute apart, in time order."""
    kept: list[dict[str, Any]] = []
    for moment in sorted(moments, key=lambda m: m["seconds"]):
        if not kept or moment["seconds"] - kept[-1]["seconds"] >= gap_s:
            kept.append(moment)
    if len(kept) > MAX_MOMENTS:
        step = len(kept) / MAX_MOMENTS
        kept = [kept[int(i * step)] for i in range(MAX_MOMENTS)]
    return kept


def clock(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
