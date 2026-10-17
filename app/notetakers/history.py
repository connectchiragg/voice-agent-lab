"""History: every interview of the last N days, imported once and scored quietly.

`python -m app.notetakers history --days 90 [--dry-run] [--yes] [--no-calendar]`, run in the
research worker container (it opens its own Postgres connection; no web process needed).

1. Recordings: pages through every Fathom meeting (and Fireflies transcript, when a key is
   stored or set) in the window, paced for the providers' rate limits (Fathom: 60 requests a
   minute, 429s wait for its reset). The interview-like ones (`interview_like`: the title
   rules of the calendar and of the classifier, external invitees, the company resolver's
   answer from the invitees' domains) are stored in `notetaker_recordings` with their company,
   interviewers and share link (the video plays from Fathom's download API when opened).
2. Evaluation: each new one without a scorecard is queued in `post_call_reports` with
   `history = true`. The worker's post-call loop scores them one every 30 s within the
   research month cap, and sends nothing (app/post_call.py, module doc). The estimated
   cost is printed first; over ESTIMATE_GATE_USD the real run needs `--yes`.
3. Re-analysis: interviews already in performance.json whose interviewer impression is only
   "derived" (or missing) because their transcript was not kept are queued once in mode
   `reanalyse` (app/post_call.requeue): the worker fetches the transcript from Fathom or
   Fireflies again, keeps it encrypted for the owner and re-runs the rubric and the cues.
4. Calendar (optional): the Google Calendar API (read-only, the stored token) lists the
   window's interview-like events; those with no recording are kept in `calendar_history`
   and show in Past interviews as "No recording".

Idempotent: a re-run stores and queues nothing new. Prints counts only (no titles, names,
emails or transcripts).
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.notetakers import base, store
from app.notetakers.base import KeyRefused, ProviderError, Recording

logger = logging.getLogger("valr")

HISTORY_PROVIDERS = ("fathom", "fireflies")
MAX_PAGES = 200  # Fathom: 10 a page, Fireflies: 50 a page; far beyond 90 days of meetings.
PACE_S = {"fathom": 1.1, "fireflies": 2.0}  # Between list pages: under 60 a minute.
MAX_PARTICIPANTS = 8  # As app/profile/classify.py: a bigger meeting is not an interview.
ESTIMATE_GATE_USD = 5.0

# What one history evaluation costs (app/profile/post_call.process, compose_report=False):
# the classifier (Haiku: title, summary and a 3,000-character excerpt) and two Sonnet reads
# of the whole transcript (the rubric, max 12k output; the interviewer's cues, max 4k). A
# 45-60 minute interview is about 10-14k transcript tokens; the prompts add about 3k each.
# The coach read is refreshed once at the end (COACH). Estimates, not measurements.
PER_INTERVIEW = {"claude-haiku-5-5": (3_000, 300), "claude-sonnet-5-5": (34_000, 8_000)}
COACH = {"claude-sonnet-5-5": (40_000, 5_000)}
SECONDS_PER_INTERVIEW = 30 + 90  # The 30 s gap plus about 1.5 minutes of model time.


# The interview filter.

def _patterns():
    from app.interview_types import INTERVIEW, NOT_INTERVIEW
    from app.profile.classify import NEGATIVE, POSITIVE
    return POSITIVE, NEGATIVE, INTERVIEW, NOT_INTERVIEW


def interview_like(recording: Recording) -> tuple[bool, str]:
    """(is it an interview?, why) from what the list gives: the title, the invitees and the
    company the resolver found. The same rules as the app: the calendar's (interview, round,
    screen, technical... but not "sync" or "catch-up") and the classifier's title heuristic,
    with an outside person or a company; more than 8 people is a meeting, not an interview."""
    positive, negative, interview, not_interview = _patterns()
    title = recording.title or ""
    outside = bool(recording.interviewers or recording.invitee_hashes)
    company = bool(recording.company and base.is_company(recording.company))
    if len(recording.attendee_emails) > MAX_PARTICIPANTS:
        return False, "large meeting"
    says_interview = bool(interview.search(title))
    if (negative.search(title) or not_interview.search(title)) and not says_interview:
        return False, "internal title"
    if not (outside or company):
        return False, "no outside invitee or company"
    if says_interview or positive.search(title):
        return True, "title"
    if company and recording.company_domain:
        return True, "outside company"
    return False, "no interview signal"


# Cost.

def estimate(count: int, coach_refresh: bool = True) -> dict[str, Any]:
    """The evaluation's estimated cost for `count` interviews: {usd, tokens: {model: (in, out)}}."""
    from app import research_jobs
    tokens: dict[str, list[int]] = {}
    for model, (i, o) in PER_INTERVIEW.items():
        tokens.setdefault(model, [0, 0])
        tokens[model][0] += i * count
        tokens[model][1] += o * count
    if coach_refresh and count:
        for model, (i, o) in COACH.items():
            tokens.setdefault(model, [0, 0])
            tokens[model][0] += i
            tokens[model][1] += o
    usd = sum(research_jobs.price(m, i, o) for m, (i, o) in tokens.items())
    return {"usd": round(usd, 4), "tokens": {m: tuple(v) for m, v in tokens.items()},
            "minutes": round(count * SECONDS_PER_INTERVIEW / 60)}


def estimate_text(count: int) -> str:
    est = estimate(count)
    per = estimate(1, coach_refresh=False)
    models = ", ".join(f"{m} {i:,} in / {o:,} out" for m, (i, o) in per["tokens"].items())
    return (f"Estimated evaluation: {count} interview(s) x ~${per['usd']:.2f} ({models} each) "
            f"+ one coach refresh = ~${est['usd']:.2f}, about {est['minutes']} minutes in the worker.")


# Recordings.

@dataclass
class Counts:
    total: int = 0
    interview_like: int = 0
    already_stored: int = 0
    new: int = 0
    queued: int = 0
    already_scored: int = 0
    hidden: int = 0
    reanalyse: int = 0
    providers: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


Lister = Callable[[str, str, datetime, datetime, httpx.AsyncClient], Awaitable[list[Recording]]]


async def list_provider(provider: str, key: str, start: datetime, end: datetime,
                        http: httpx.AsyncClient) -> list[Recording]:
    from app import notetakers
    return await notetakers.PROVIDERS[provider].list_recordings(
        key, start, end, http, max_pages=MAX_PAGES, pace_s=PACE_S.get(provider, 1.0))


def scored_ids() -> set[str]:
    """The interview ids performance.json already holds ("fathom:<id>"): never rescored."""
    return set(performance_interviews())


def performance_interviews() -> dict[str, dict]:
    """performance.json's interviews by id (the worker's live copy, else its seed)."""
    try:
        from app.profile.config import settings
        cfg = settings()
    except Exception:  # noqa: BLE001
        return {}
    for path in (cfg.performance_path, cfg.performance_fallback):
        if path is None or not Path(path).exists():
            continue
        try:
            perf = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return {str(iv.get("interview_id") or iv.get("id")): iv for iv in perf.get("interviews") or []
                if isinstance(iv, dict) and (iv.get("interview_id") or iv.get("id"))}
    return {}


def needs_cues(iv: dict) -> bool:
    """Its interviewer impression is only derived from the stored analysis, or missing."""
    return (iv.get("impression") or {}).get("coverage") in (None, "derived", "none")


async def reanalysed(connection: Any) -> set[tuple[str, str]]:
    """Interviews already queued for re-analysis once (any outcome): the import never repeats
    them; the page's "Try again" does."""
    from app import post_call
    await post_call.setup(connection)
    cursor = await connection.execute("SELECT provider, external_id FROM post_call_reports WHERE mode = 'reanalyse'")
    return {(p, x) for p, x in await cursor.fetchall()}


async def queue_reanalyse(connection: Any, provider: str, external_id: str, title: str, url: str) -> bool:
    from app import post_call
    return await post_call.requeue(connection, provider, external_id, title, url)


async def queue_history(connection: Any, recording: Recording) -> bool:
    from app import post_call
    await post_call.setup(connection)
    return await post_call.enqueue(connection, recording.provider, recording.external_id, None,
                                   recording.title, base.safe_link(recording.share_url), history=True)


async def import_recordings(connection: Any, http: httpx.AsyncClient, days: int = 90, dry_run: bool = False,
                            now: datetime | None = None, keys: dict[str, str] | None = None,
                            lister: Lister = list_provider, scored: set[str] | None = None,
                            match: Callable[[Recording], Awaitable[dict | None]] | None = None,
                            queue: Callable[[Any, Recording], Awaitable[bool]] = queue_history,
                            interviews: dict[str, dict] | None = None,
                            requeue: Callable[..., Awaitable[bool]] = queue_reanalyse) -> Counts:
    """List, filter, store and queue (new interviews, and derived-only ones for re-analysis).
    `dry_run` writes nothing. -> the counts."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    await store.setup(connection)
    if keys is None:
        from app import notetakers
        keys = {p: k for p in HISTORY_PROVIDERS if (k := await notetakers.api_key(connection, p))}
    interviews = performance_interviews() if interviews is None else interviews
    scored = set(interviews) if scored is None else scored
    gone = await store.hidden(connection)
    counts = Counts()
    candidates: list[Recording] = []
    for provider in HISTORY_PROVIDERS:
        if not keys.get(provider):
            continue
        try:
            found = await lister(provider, keys[provider], start, now, http)
        except (KeyRefused, ProviderError, httpx.HTTPError) as error:
            counts.errors.append(f"{provider}: {type(error).__name__}")
            continue
        counts.providers[provider] = len(found)
        counts.total += len(found)
        candidates += [r for r in found if r.external_id and interview_like(r)[0]]
    counts.interview_like = len(candidates)
    stored = await store.many(connection, [(r.provider, r.external_id) for r in candidates])
    for recording in candidates:
        pair = (recording.provider, recording.external_id)
        if pair in gone:
            counts.hidden += 1
            continue
        if pair in stored:
            counts.already_stored += 1
            continue
        counts.new += 1
        has_card = f"{recording.provider}:{recording.external_id}" in scored
        counts.already_scored += has_card
        if dry_run:
            counts.queued += not has_card
            continue
        await store.upsert_recording(connection, recording.public(), "history")
        event = await match(recording) if match else None
        if event:
            await store.set_event(connection, recording.provider, recording.external_id, event["key"],
                                  event.get("title", ""), event.get("company", ""), event.get("start"))
        if has_card:
            await store.set_eval(connection, recording.provider, recording.external_id, "handed_off",
                                 "Already scored (performance.json).")
            await keep_transcript(connection, recording, keys.get(recording.provider) or "", http)
            continue
        queued = await queue(connection, recording)
        counts.queued += queued
        await store.set_eval(connection, recording.provider, recording.external_id, "handed_off",
                             "queued for the history evaluation" if queued else "already queued")
    await reanalyse_derived(connection, counts, interviews, keys, dry_run, requeue)
    return counts


async def reanalyse_derived(connection: Any, counts: Counts, interviews: dict[str, dict], keys: dict[str, str],
                            dry_run: bool, requeue: Callable[..., Awaitable[bool]]) -> None:
    """Scored interviews of a connected provider with only derived cues -> one re-analysis each."""
    done = await reanalysed(connection)
    gone = await store.hidden(connection)
    for iid, iv in interviews.items():
        provider, _, external_id = iid.partition(":")
        if provider not in HISTORY_PROVIDERS or not keys.get(provider) or not external_id or not needs_cues(iv):
            continue
        if (provider, external_id) in done or (provider, external_id) in gone:
            continue
        if dry_run:
            counts.reanalyse += 1
            continue
        row = await store.recording(connection, provider, external_id) or {}
        counts.reanalyse += await requeue(connection, provider, external_id, str(iv.get("title") or row.get("title") or ""),
                                          base.safe_link(row.get("share_url") or iv.get("recording_url") or ""))


async def keep_transcript(connection: Any, recording: Recording, key: str, http: httpx.AsyncClient) -> bool:
    """The owner's copy of an already-scored interview's transcript (nothing to evaluate).
    Best effort: a failure leaves it to the page's on-demand fetch."""
    from app import notetakers
    from app.notetakers import transcripts
    if not key or not transcripts.token_key():
        return False
    if await transcripts.load(connection, recording.provider, recording.external_id) is not None:
        return False
    try:
        await asyncio.sleep(PACE_S.get(recording.provider, 1.0))
        got = await notetakers.PROVIDERS[recording.provider].get_recording(key, recording.external_id, http,
                                                                            listed=recording)
    except (KeyRefused, ProviderError, httpx.HTTPError) as error:
        logger.warning("VALR history: transcript not kept (%s)", type(error).__name__)
        return False
    return await transcripts.save_quietly(connection, recording.provider, recording.external_id, got.meeting)


async def fetch_meeting(connection: Any, provider: str, external_id: str,
                        http: httpx.AsyncClient | None = None) -> dict | None:
    """One recording's normalised meeting (transcript included), from its provider by id. For
    the worker's history items (any age; the post-call fallback looks back 3 days only)."""
    from app import notetakers
    if provider not in HISTORY_PROVIDERS:
        return None
    key = await notetakers.api_key(connection, provider)
    if not key:
        return None
    await store.setup(connection)
    row = await store.recording(connection, provider, external_id) or {}
    listed = Recording(provider, external_id, row.get("title") or "", notetakers._iso(row.get("started_at")),
                       notetakers._iso(row.get("ended_at")), row.get("share_url") or "")
    if http is not None:
        return (await notetakers.PROVIDERS[provider].get_recording(key, external_id, http, listed=listed)).meeting
    async with notetakers.http_client() as client:
        return (await notetakers.PROVIDERS[provider].get_recording(key, external_id, client, listed=listed)).meeting


# Calendar: past interviews that have no recording.

CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_history (
    event_key     text PRIMARY KEY,
    title         text NOT NULL DEFAULT '',
    company       text NOT NULL DEFAULT '',
    start_utc     timestamptz NOT NULL,
    end_utc       timestamptz,
    interviewers  text NOT NULL DEFAULT '[]',   -- JSON list of first names (no emails)
    found_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS calendar_history_by_start ON calendar_history (start_utc);
"""
CALENDAR_FIELDS = ("event_key", "title", "company", "start_utc", "end_utc", "interviewers")
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


async def calendar_setup(connection: Any) -> None:
    await connection.execute(CALENDAR_SCHEMA)


async def list_past_events(http: httpx.AsyncClient, access_token: str, start: datetime, end: datetime) -> list[Any]:
    """The primary calendar's events in [start, end) as SlotEvents (read-only, paged)."""
    from app import calendar_sync
    params: dict[str, Any] = {"singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
                              "timeMin": start.isoformat(), "timeMax": end.isoformat()}
    items: list[dict[str, Any]] = []
    for _ in range(calendar_sync.MAX_PAGES):
        response = await http.get(CALENDAR_API, params=params, headers=calendar_sync.headers(access_token))
        response.raise_for_status()
        body = response.json()
        items += body.get("items") or []
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    events, _ = calendar_sync.from_google_items(items)
    return events


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def scrub(text: object) -> str:
    """As app/valr_view.scrub (emails and phone numbers hidden), without its imports."""
    value = "" if text is None else str(text)
    return PHONE.sub("[phone hidden]", EMAIL.sub("[email hidden]", value))


def _first_names(event: Any, owners: set[str]) -> list[str]:
    from app.interview_types import people
    names = [str(p.name or "").split()[0] for p in people(event, owners) if p.name and "@" not in p.name]
    return list(dict.fromkeys(n for n in names if n))[:6]


async def import_calendar(connection: Any, events: list[Any], owners: set[str], dry_run: bool = False,
                          now: datetime | None = None) -> dict[str, int]:
    """Past interview-like events with no stored recording -> `calendar_history`.
    -> {events, interviews, with_recording, no_recording, new}."""
    from app.interview_types import looks_like_interview
    now = now or datetime.now(timezone.utc)
    await store.setup(connection)
    await calendar_setup(connection)
    out = {"events": len(events), "interviews": 0, "with_recording": 0, "no_recording": 0, "new": 0}
    for event in events:
        if event.status == "cancelled" or event.declined or not looks_like_interview(event, owners):
            continue
        if base._dt(event.start_utc) is None or base._dt(event.start_utc) > now:
            continue
        out["interviews"] += 1
        if await store.by_event(connection, event.key) or await recording_near(connection, event):
            out["with_recording"] += 1
            continue
        out["no_recording"] += 1
        if dry_run:
            continue
        cursor = await connection.execute(
            "INSERT INTO calendar_history (event_key, title, company, start_utc, end_utc, interviewers) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (event_key) DO UPDATE SET title = excluded.title, "
            "company = excluded.company, start_utc = excluded.start_utc, end_utc = excluded.end_utc, "
            "interviewers = excluded.interviewers RETURNING (xmax = 0)",
            (event.key, scrub(event.title)[:300], scrub(event.company or "")[:120], base._dt(event.start_utc),
             base._dt(event.end_utc), json.dumps(_first_names(event, owners))))
        row = await cursor.fetchone()
        out["new"] += bool(row and row[0])
    return out


async def recording_near(connection: Any, event: Any) -> bool:
    """A stored recording that started within this event's time (+/- 20 minutes)."""
    start, end = base._dt(event.start_utc), base._dt(event.end_utc)
    if not start:
        return False
    rows = await store.between(connection, start - base.MATCH_SLACK, (end or start) + base.MATCH_SLACK, limit=5)
    return bool(rows)


async def calendar_entries(connection: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Stored "No recording" interviews in [start, end), in the shape Past interviews reads,
    minus any that have a recording by now."""
    await calendar_setup(connection)
    cursor = await connection.execute(
        f"SELECT {', '.join(CALENDAR_FIELDS)} FROM calendar_history WHERE start_utc >= %s AND start_utc < %s "
        "AND NOT EXISTS (SELECT 1 FROM notetaker_recordings r WHERE r.event_key = calendar_history.event_key) "
        "ORDER BY start_utc", (start, end))
    out = []
    for row in await cursor.fetchall():
        item = dict(zip(CALENDAR_FIELDS, row))
        try:
            names = [str(n) for n in json.loads(item["interviewers"] or "[]")][:6]
        except ValueError:
            names = []
        out.append({"id": f"calendar:{item['event_key']}", "recording_only": True, "no_recording": True,
                    "date": item["start_utc"].astimezone(timezone.utc).isoformat(), "title": item["title"],
                    "company": item["company"], "round": "", "interviewers": names, "provider": "calendar",
                    "review": "no_recording", "overall": None})
    return out


async def calendar_history(connection: Any, http: httpx.AsyncClient, days: int, dry_run: bool = False,
                           now: datetime | None = None) -> dict[str, Any]:
    from app import google_oauth
    now = now or datetime.now(timezone.utc)
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None:
        return {"skipped": "Google is not configured"}
    try:
        await google_oauth.setup(connection)
        token = await google_oauth.access_token(connection, settings, http)
    except (google_oauth.ReconnectNeeded, google_oauth.NotConfigured) as error:
        return {"skipped": f"Google not connected ({type(error).__name__})"}
    events = await list_past_events(http, token, now - timedelta(days=days), now)
    return await import_calendar(connection, events, owner_emails(), dry_run, now)


def owner_emails() -> set[str]:
    from app.notetakers import fathom
    return fathom.owner_emails()


# The command.

async def run(connection: Any, http: httpx.AsyncClient, days: int, dry_run: bool, yes: bool,
              calendar: bool = True, out: Callable[[str], None] = print,
              match: Callable[[Recording], Awaitable[dict | None]] | None = None) -> int:
    """The `history` command. -> exit code (2: the estimate needs --yes)."""
    preview = await import_recordings(connection, http, days, dry_run=True, match=match)
    out(f"Last {days} days: {preview.total} meeting(s) "
        f"({', '.join(f'{p} {n}' for p, n in preview.providers.items()) or 'no notetaker connected'}), "
        f"{preview.interview_like} interview-like, {preview.already_stored + preview.hidden} already stored, "
        f"{preview.new} new ({preview.already_scored} of them already scored).")
    for error in preview.errors:
        out(f"  could not list {error}")
    out(f"Interviews to read again (interviewer cues missing): {preview.reanalyse}.")
    work = preview.queued + preview.reanalyse
    out(estimate_text(work))
    if not dry_run and estimate(work)["usd"] > ESTIMATE_GATE_USD and not yes:
        out(f"Over ${ESTIMATE_GATE_USD:.0f}: run again with --yes to import and queue them.")
        return 2
    if not dry_run:
        done = await import_recordings(connection, http, days, match=match)
        out(f"Imported {done.new} recording(s); queued {done.queued} for the evaluation and "
            f"{done.reanalyse} to read again (the worker does one every 30 s; no Telegram, push or "
            "notifications).")
    if calendar:
        result = await calendar_history(connection, http, days, dry_run)
        out("Calendar: " + ", ".join(f"{k} {v}" for k, v in result.items()))
    return 0


async def main(argv: list[str]) -> int:
    import argparse
    import psycopg
    from app import research_jobs
    parser = argparse.ArgumentParser(prog="python -m app.notetakers history")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-calendar", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366:
        print("--days must be 1 to 366.")
        return 1
    logging.basicConfig(level=logging.WARNING)
    async with await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True) as connection:
        from app import notetakers

        async def match(recording: Recording) -> dict | None:
            try:
                return await notetakers.match_event(connection, recording)
            except Exception:  # noqa: BLE001 - no calendar: no event link
                return None
        async with httpx.AsyncClient(timeout=base.TIMEOUT_S) as http:
            return await run(connection, http, args.days, args.dry_run, args.yes, not args.no_calendar, match=match)


def cli(argv: list[str]) -> int:
    return asyncio.run(main(argv))
