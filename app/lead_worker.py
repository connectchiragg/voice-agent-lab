"""Lead tracker activities for the research worker (task queue `voice-research`).

- `gmail_sync`: the messages added since the stored historyId (or, until the backfill is done,
  the last VALR_LEADS_BACKFILL_DAYS days, at most VALR_LEADS_BACKFILL_MAX messages, oldest
  first), plus up to DEFERRED_PER_PASS earlier "deferred" ones. At most
  VALR_LEADS_FETCH_PER_RUN (150) messages a run, in batches of BATCH, each recorded before the
  next, so a crash or a rate limit resumes where it stopped (summary.more: the workflow runs
  again). Gmail calls are paced (gmail_api, VALR_GMAIL_RPS); a rate limit that outlasts the
  retries ends the run successfully as mode "paused-ratelimit" (last_error "rate limited,
  resuming") and the workflow resumes it ~2 minutes later. While the backfill is not done, a
  push only records last_push_at. Each new message: heuristics, then Claude Haiku
  within the day's budget (VALR_LEADS_LLM_PER_DAY) and the research month cap; the decision
  is logged without content; a kept one becomes a touchpoint. A historyId that is too old
  (404) falls back to a 3-day search. One pass at a time (a Postgres advisory lock).
- `leads_refresh`: calendar interviews (calendar_events), Fathom recordings (FATHOM_API_KEY)
  and scored interviews (performance.json) become touchpoints; then every lead is rebuilt
  (app/leads.build) and saved.
- `gmail_watch`: users.watch on VALR_GMAIL_TOPIC, daily.

Settings: VALR_LEADS=1 turns the schedules on; VALR_GMAIL_TOPIC; VALR_LEADS_BACKFILL_DAYS (30),
VALR_LEADS_BACKFILL_MAX (500), VALR_LEADS_LLM_PER_DAY (300), VALR_LEADS_POLL_MINUTES (30),
VALR_LEADS_FETCH_PER_RUN (150), VALR_GMAIL_RPS (4).
Nothing here logs an address, a subject or an email's text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import psycopg
from temporalio import activity

from app import company_names, company_resolver, gmail_api, google_oauth, lead_classify, leads, leads_store, research_jobs
from app.lead_classify import Decision, Email
from app.lead_workflow import GmailSyncInput, GmailSyncSummary, RefreshSummary, WatchSummary
from app.leads import Contact, Touch

logger = logging.getLogger("valr")
SYNC_LOCK = 7_202_610_18
DEFERRED_PER_PASS = 40
RECOVERY_QUERY = "newer_than:3d -in:chats"
CONCURRENCY = 4
BATCH = 25  # Messages fetched, decided and recorded together.
PAUSED = "paused-ratelimit"
RATE_LIMITED = "rate limited, resuming"
FATHOM_MEETINGS = "https://api.fathom.ai/external/v1/meetings"
DETAIL_URL = "/valr/performance"  # When no recording is known.


def detail_url(interview_id: str) -> str:
    """The past interview's page for an id like "fathom:1000": /valr/interviews/past/fathom-1000
    (the slug of app/valr_ui/attention.past_slug); /valr/performance when there is none."""
    interview_id = str(interview_id or "")
    if ":" not in interview_id:
        return DETAIL_URL
    return "/valr/interviews/past/" + "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in interview_id)[:80]


def enabled() -> bool:
    return os.getenv("VALR_LEADS", "0") == "1"


def topic() -> str:
    return os.getenv("VALR_GMAIL_TOPIC", "").strip()


def llm_per_day() -> int:
    return int(os.getenv("VALR_LEADS_LLM_PER_DAY", "300"))


def per_run() -> int:
    """Gmail messages fetched at most per gmail_sync run (VALR_LEADS_FETCH_PER_RUN, 150)."""
    return max(1, int(os.getenv("VALR_LEADS_FETCH_PER_RUN", "150")))


def unique_ids(refs: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(ref["id"] for ref in refs if ref.get("id")))


def owner_emails() -> set[str]:
    from app.interview_worker import owner_emails as owners
    return owners()


async def connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True)


def snippet_cipher(email: Email) -> str | None:
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    text = " ".join(email.snippet.split())[:200]
    return google_oauth.encrypt(key, text) if key and text else None


def email_touch(email: Email, decision: Decision, owners: set[str]) -> Touch:
    outbound = email.from_email.lower() in owners
    people = [Contact(a, n, r) for a, n, r in lead_classify.touch_people(email, decision, owners)]
    domain = "" if outbound else lead_classify.sender_domain(email)
    if outbound and people:
        domain = leads.domain_of(people[0].email)
    found = company_names.resolve(attendees=[p.email for p in people] if outbound else [],
                                  sender_email="" if outbound else email.from_email, sender_name=email.from_name,
                                  title=email.subject, stated=decision.company, owner_emails=owners)
    return Touch(id=f"gmail:{email.id}", kind="email", at=email.at, company=found.name,
                 stated=decision.company[:80], company_how=company_resolver.HOW.get(found.source, "").format(
                     domain=found.domain),
                 domain=domain, people=people, title=email.subject[:140],
                 summary=decision.summary, direction="out" if outbound else "in",
                 sentiment=None if outbound else decision.sentiment, cue=decision.cue,
                 stage_hint=decision.stage_hint, next_step=decision.next_step,
                 url=gmail_api.thread_url(email.thread_id), role=decision.role, thread=email.thread_id)


def beat() -> None:
    try:
        activity.heartbeat()
    except RuntimeError:
        pass  # Not in an activity (tests, the CLI).


class Budget:
    """LLM calls left today, and the research month cap."""

    def __init__(self, left: int, month_ok: bool) -> None:
        self.left, self.month_ok = left, month_ok

    def take(self) -> bool:
        if self.left <= 0 or not self.month_ok:
            return False
        self.left -= 1
        return True


class LeadActivities:
    def __init__(self, http: httpx.AsyncClient | None = None,
                 classify: Callable[[Email, set[str]], Decision] | None = None, bot: Any = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._classify = classify
        self._bot = bot

    def all(self) -> list[Any]:
        return [self.gmail_sync, self.leads_refresh, self.gmail_watch]

    def classify(self, email: Email, owners: set[str]) -> Decision:
        if self._classify is not None:
            return self._classify(email, owners)
        from app import llm_gateway
        return lead_classify.classify(email, owners, llm_gateway.shared().client(timeout=60))

    # Gmail.

    @activity.defn(name="gmail_sync")
    async def gmail_sync(self, request: GmailSyncInput) -> GmailSyncSummary:
        async with await connect() as connection:
            await leads_store.setup(connection)
            await research_jobs.setup(connection)
            await connection.execute("SELECT pg_advisory_lock(%s)", (SYNC_LOCK,))
            try:
                return await self.sync(connection, request, datetime.now(timezone.utc))
            except Exception as error:
                await leads_store.save_state(connection, last_error=type(error).__name__)
                raise
            finally:
                await connection.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK,))

    async def sync(self, connection: Any, request: GmailSyncInput, now: datetime) -> GmailSyncSummary:
        from app.interview_worker import gmail_access
        summary = GmailSyncSummary()
        state = await leads_store.load_state(connection)
        if request.reason == "push":
            await leads_store.save_state(connection, last_push_at=now)
            if not state.get("backfill_done_at"):
                summary.mode = "skipped"  # The backfill covers it: its historyId baseline came first.
                return summary
        if request.history_id and state.get("history_id") and request.history_id <= state["history_id"]:
            summary.mode = "skipped"  # A late or repeated push: already processed.
            return summary
        token, status = await gmail_access(connection, self.http)
        if not token:
            summary.error = f"Gmail {status}"
            await leads_store.save_state(connection, last_error=summary.error)
            logger.info("VALR leads: Gmail sync skipped (%s)", status)
            if status == "reconnect needed":
                await self.alert_reconnect(connection, state)
            return summary
        try:
            if not state.get("backfill_done_at"):
                await self.backfill(connection, token, state, now, summary)
            else:
                await self.incremental(connection, token, state, now, summary)
        except gmail_api.GmailRateLimited as error:
            # Decisions so far are saved (per batch); the workflow resumes in a few minutes.
            summary.mode, summary.more, summary.error = PAUSED, True, RATE_LIMITED
            await leads_store.save_state(connection, last_sync_at=now, last_error=RATE_LIMITED)
            logger.warning("VALR leads: Gmail rate limited (%s); paused, resuming later", error.reason)
        logger.info("VALR leads: Gmail %s (%s): %d new, %d kept, %d dropped, %d deferred, %d model calls, "
                    "$%.4f, %d left", summary.mode, request.reason, summary.fetched, summary.kept,
                    summary.dropped, summary.deferred, summary.llm_calls, summary.usd, summary.left)
        return summary

    async def backfill(self, connection: Any, token: str, state: dict[str, Any], now: datetime,
                       summary: GmailSyncSummary) -> None:
        """The first VALR_LEADS_BACKFILL_DAYS days, oldest first, at most per_run() messages a
        run; done (backfill_done_at) once every listed message has been looked at."""
        summary.mode = "backfill"
        started = state.get("backfill_started_at") or now
        if not state.get("history_id"):
            profile = await gmail_api.profile(self.http, token)
            # Saved before listing, so the history passes after the backfill miss nothing.
            await leads_store.save_state(connection, account=str(profile.get("emailAddress", "")).lower(),
                                         history_id=int(profile.get("historyId") or 0) or None,
                                         backfill_started_at=started)
        elif not state.get("backfill_started_at"):
            await leads_store.save_state(connection, backfill_started_at=started)
        days = int(os.getenv("VALR_LEADS_BACKFILL_DAYS", "30"))
        cap = int(os.getenv("VALR_LEADS_BACKFILL_MAX", "500"))
        since = int((started - timedelta(days=days)).timestamp())  # Fixed, so resumed runs list the same mail.
        refs = await gmail_api.search(self.http, token, f"after:{since} -in:chats", cap)
        ids = list(reversed(unique_ids(refs)))  # Oldest first: a message before the replies to it.
        done = await leads_store.recorded(connection, ids)
        todo = [i for i in ids if i not in done]
        work = todo[:per_run()]
        try:
            await self.process_batches(connection, token, work, summary)
        finally:
            await leads_store.save_state(connection, backfill_count=len(await leads_store.recorded(connection, ids)))
        summary.left = len(todo) - len(work)
        values: dict[str, Any] = {"last_sync_at": now, "last_error": None}
        if summary.left:
            summary.more = True
        else:
            values.update(backfill_done_at=datetime.now(timezone.utc))
        await leads_store.save_state(connection, **values)

    async def incremental(self, connection: Any, token: str, state: dict[str, Any], now: datetime,
                          summary: GmailSyncSummary) -> None:
        """Messages added since the stored historyId, plus earlier deferred ones. The historyId
        moves on only when every new message fits in this run."""
        summary.mode = "history"
        try:
            refs, new_history = await gmail_api.history(self.http, token, int(state["history_id"]))
        except gmail_api.HistoryGone:
            summary.mode = "recovery"
            logger.warning("VALR leads: Gmail historyId too old; searching the last 3 days")
            new_history = int((await gmail_api.profile(self.http, token)).get("historyId") or 0)
            refs = list(reversed(await gmail_api.search(self.http, token, RECOVERY_QUERY, 300)))
        ids = unique_ids(refs)
        done = await leads_store.seen(connection, ids)
        fresh = [i for i in ids if i not in done]
        limit = per_run()
        work = fresh[:limit]
        work += [i for i in await leads_store.deferred(connection, DEFERRED_PER_PASS)
                 if i not in fresh][:max(0, limit - len(work))]
        await self.process_batches(connection, token, work, summary)
        summary.left = max(0, len(fresh) - limit)
        values: dict[str, Any] = {"last_sync_at": now, "last_error": None}
        if summary.left:
            summary.more = True
        elif new_history:
            values["history_id"] = max(new_history, int(state.get("history_id") or 0))
        await leads_store.save_state(connection, **values)

    async def process_batches(self, connection: Any, token: str, ids: list[str],
                              summary: GmailSyncSummary) -> None:
        """BATCH messages at a time, each fetched, decided and recorded before the next: a
        rate limit or a crash loses at most the batch in flight."""
        for start in range(0, len(ids), BATCH):
            await self.process(connection, token, ids[start:start + BATCH], summary)

    async def process(self, connection: Any, token: str, ids: list[str], summary: GmailSyncSummary) -> None:
        """Fetches, decides and records `ids`. On GmailRateLimited the messages fetched so far
        are still decided and recorded, then it is raised."""
        owners = owner_emails()
        limits = research_jobs.Limits.interview_from_env()
        budget = Budget(max(0, llm_per_day() - await leads_store.llm_calls_today(connection)),
                        await research_jobs.month_spend(connection) < limits.month_usd)
        emails: list[Email] = []
        limited: gmail_api.GmailRateLimited | None = None
        for message_id in ids:
            try:
                raw = await gmail_api.message(self.http, token, message_id)
            except gmail_api.GmailRateLimited as error:
                limited = error
                break
            beat()
            if raw is not None:
                emails.append(gmail_api.parse(raw))
        summary.fetched += len(emails)
        known = await leads_store.kept_threads(connection, [e.thread_id for e in emails])
        gate = asyncio.Semaphore(CONCURRENCY)

        async def decide(email: Email) -> Decision:
            mine = lead_classify.own_mail(email, owners, email.thread_id in known)
            if mine is not None:
                return mine
            early = lead_classify.prefilter(email, email.thread_id in known)
            if early is not None:
                return early
            if not budget.take():
                return Decision(False, "budget", 0.0, "deferred")
            async with gate:
                try:
                    return await asyncio.to_thread(self.classify, email, owners)
                except Exception as error:  # noqa: BLE001 - retried by the next pass
                    logger.warning("VALR leads: classifier failed (%s); deferred", type(error).__name__)
                    return Decision(False, "error", 0.0, "deferred")

        work = asyncio.ensure_future(asyncio.gather(*(decide(email) for email in sorted(emails, key=lambda e: e.at))))
        while not work.done():
            beat()
            await asyncio.wait([work], timeout=10)
        decisions = await work
        ordered = sorted(emails, key=lambda e: e.at)
        kept_now = {e.thread_id for e, d in zip(ordered, decisions) if d.keep}
        decisions = [lead_classify.own_mail(e, owners, True) if d.reason == "own_mail" and e.thread_id in kept_now
                     else d for e, d in zip(ordered, decisions)]  # His reply to a message kept in this pass.
        rows = []
        for email, decision in zip(sorted(emails, key=lambda e: e.at), decisions):
            usd = 0.0
            if decision.usage:
                summary.llm_calls += 1
                row = {"part": "leads", "kind": "classify", "model": lead_classify.MODEL, **decision.usage}
                usd = research_jobs.price(lead_classify.MODEL, row["input_tokens"], row["output_tokens"],
                                          row["cache_read"], row["cache_write"])
                rows.append(row)
            verdict = "deferred" if decision.method == "deferred" else "keep" if decision.keep else "drop"
            await leads_store.record_email(connection, email.id, email.thread_id, email.at,
                                           lead_classify.sender_domain(email), verdict, decision.method,
                                           decision.reason, decision.category, decision.confidence, usd)
            logger.info("VALR lead email: %s by %s (%s, %.2f)", verdict, decision.method,
                        decision.category if decision.method == "llm" else decision.reason, decision.confidence)
            if verdict == "keep":
                summary.kept += 1
                await leads_store.upsert_touch(connection, email_touch(email, decision, owners),
                                               snippet_cipher(email))
            elif verdict == "drop":
                summary.dropped += 1
            else:
                summary.deferred += 1
        if rows:
            summary.usd += await research_jobs.record_spend(
                connection, f"leads-{datetime.now(timezone.utc):%Y-%m-%d}", rows)
        if limited is not None:
            raise limited

    async def alert_reconnect(self, connection: Any, state: dict[str, Any]) -> bool:
        """Telegram, once per expiry (keyed by the token's connected_at), when the refresh
        token was refused (invalid_grant: Testing mode's 7 days, or revoked)."""
        from app import telegram_api
        info = await google_oauth.status(connection)
        expired = info.get("connected_at")
        if info.get("state") != "reconnect_needed" or expired is None \
                or state.get("reconnect_alerted_for") == expired:
            return False
        bot = self._bot or telegram_api.bot()
        if bot is None:
            logger.warning("VALR leads: Gmail needs reconnecting; Telegram is not set up to say so")
            return False
        await telegram_api.setup(connection)
        chat = await telegram_api.owner_chat(connection)
        if chat is None:
            logger.warning("VALR leads: Gmail needs reconnecting; no linked Telegram chat")
            return False
        host = os.getenv("VALR_HOST", "valr.haciensus.com").strip()
        try:
            await bot.send(chat, f"Gmail disconnected — reconnect: https://{host}/oauth/google/start")
        except telegram_api.TelegramError as error:
            logger.warning("VALR leads: reconnect alert not sent: %s", error)
            return False
        await leads_store.save_state(connection, reconnect_alerted_for=expired)
        logger.info("VALR leads: Gmail reconnect alert sent on Telegram")
        return True

    @activity.defn(name="gmail_watch")
    async def gmail_watch(self, reason: str) -> WatchSummary:
        from app.interview_worker import gmail_access
        name = topic()
        if not name:
            return WatchSummary(error="VALR_GMAIL_TOPIC is not set")
        async with await connect() as connection:
            await leads_store.setup(connection)
            token, status = await gmail_access(connection, self.http)
            if not token:
                return WatchSummary(error=f"Gmail {status}")
            body = await gmail_api.watch(self.http, token, name)
            expires = datetime.fromtimestamp(int(body.get("expiration", 0)) / 1000, timezone.utc)
            history = int(body.get("historyId") or 0)
            state = await leads_store.load_state(connection)
            values: dict[str, Any] = {"watch_expires": expires, "watch_renewed_at": datetime.now(timezone.utc)}
            await leads_store.save_state(connection, **values)
            if not state.get("backfill_done_at"):
                logger.info("VALR leads: watch on; the backfill runs from the poll")
        logger.info("VALR leads: Gmail watch renewed (%s) until %s", reason, expires.isoformat())
        return WatchSummary(expires.isoformat(), history)

    # Leads.

    @activity.defn(name="leads_refresh")
    async def leads_refresh(self, reason: str) -> RefreshSummary:
        now = datetime.now(timezone.utc)
        summary = RefreshSummary()
        async with await connect() as connection:
            await leads_store.setup(connection)
            sources = {"calendar": await self.calendar_touches(connection, now),
                       "recording": await self.fathom_touches(now),
                       "evaluation": performance_touches(read_performance())}
            for kind, touches in sources.items():
                summary.sources[kind] = len(touches)
                for touch in touches:
                    await leads_store.upsert_touch(connection, touch)
            for key in await cancelled_calendar_keys(connection):
                await connection.execute("DELETE FROM lead_touchpoints WHERE id = %s", (f"cal:{key}",))
            everything = await leads_store.touches(connection)
            try:  # Rules, then the cached or capped Haiku review for the unclear ones.
                await resolve_touches(connection, everything, company_resolver.default_resolver(owner_emails()),
                                      limit=int(os.getenv("VALR_COMPANY_LLM_PER_RUN", "10")))
            except Exception as error:  # noqa: BLE001 - the rebuild still runs on the rules' names.
                logger.warning("VALR leads: company review skipped (%s)", type(error).__name__)
            built = leads.build(everything, now, await leads_store.overrides(connection))
            await leads_store.save_leads(connection, built, everything, now)
        summary.leads, summary.touches = len(built), len(everything)
        summary.active = sum(1 for lead in built if lead.status == "active")
        summary.expired = summary.leads - summary.active
        logger.info("VALR leads refreshed (%s): %d leads, %d active, %d expired, %d touchpoints; sources %s",
                    reason, summary.leads, summary.active, summary.expired, summary.touches,
                    json.dumps(summary.sources, sort_keys=True))
        return summary

    async def calendar_touches(self, connection: Any, now: datetime) -> list[Touch]:
        from app import calendar_sync
        await calendar_sync.setup(connection)
        events = await calendar_sync.upcoming(connection, now - timedelta(days=60), now + timedelta(days=60))
        known = {p.email for t in await leads_store.touches(connection) if t.kind == "email" for p in t.people if p.email}
        return calendar_to_touches(events, owner_emails(), known)

    async def fathom_touches(self, now: datetime) -> list[Touch]:
        key = os.getenv("FATHOM_API_KEY", "").strip()
        if not key:
            return []
        try:
            response = await self.http.get(
                FATHOM_MEETINGS, headers={"X-Api-Key": key, "Accept": "application/json"},
                params={"include_summary": "true", "limit": 50,
                        "created_after": (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")})
            response.raise_for_status()
            items = response.json().get("items") or []
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("VALR leads: Fathom unavailable (%s)", type(error).__name__)
            return []
        return fathom_to_touches(items, owner_emails())


# Source -> touchpoints (pure, tested).

INTRO = re.compile(r"\bintro|introduct|recruit|\bscreen", re.I)
SCREEN_ONLY = re.compile(r"\bscreen", re.I)


def calendar_to_touches(events: list[Any], owners: set[str], known: set[str] = frozenset()) -> list[Touch]:
    """Calendar events that belong to the job search:
    - interviews (title says interview, round, technical, coding...) with a company or an
      outside attendee;
    - recruiter intro chats: the title says intro, recruiter or screen, with an outside attendee;
    - any meeting where the organizer or an attendee is already a contact on a Gmail lead
      (`known`: their email addresses)."""
    from app.interview_types import INTERVIEW, Person, attendee_role, event_noun
    out = []
    for event in events:
        if event.all_day or event.declined or event.status == "cancelled":
            continue
        everyone = list(event.attendees)
        if event.organizer_email and all(p.email != event.organizer_email for p in everyone):
            everyone.append(Person(event.organizer_email, event.organizer_name, organizer=True))
        people = []
        for person in everyone:
            role = attendee_role(person, owners)
            if role in ("candidate", "recruiter"):
                people.append(Contact(person.email.lower(), person.name,
                                      "interviewer" if role == "candidate" else "recruiter"))
        outside = bool(people)
        interview = bool(INTERVIEW.search(event.title)) and event_noun(event.title) == "interview"
        intro = bool(INTRO.search(event.title)) and outside
        contact = any(p.email.lower() in known for p in everyone if p.email)
        if not ((interview and (event.company or outside)) or intro or contact):
            continue
        screen_only = bool(SCREEN_ONLY.search(event.title)) and not INTERVIEW.search(SCREEN_ONLY.sub("", event.title))
        hint = "interviewing" if interview and not screen_only else "screening"
        kind = "Interview" if hint == "interviewing" else "Recruiter chat"
        found = company_names.resolve(attendees=[p.email for p in people], title=event.title,
                                      stated=event.company, owner_emails=owners)
        out.append(Touch(id=f"cal:{event.key}", kind="calendar",
                         at=datetime.fromisoformat(event.start_utc), company=found.name, stated=event.company[:80],
                         company_how=company_resolver.HOW.get(found.source, "").format(domain=found.domain),
                         domain=found.domain or event.company_domain, people=people, title=event.title[:140],
                         summary=f"{kind} on the calendar" + (f" with {len(people)} people" if people else ""),
                         stage_hint=hint))
    return out


async def cancelled_calendar_keys(connection: Any) -> list[str]:
    cursor = await connection.execute("SELECT key FROM calendar_events WHERE status = 'cancelled'")
    return [row[0] for row in await cursor.fetchall()]


def fathom_to_touches(items: list[dict[str, Any]], owners: set[str]) -> list[Touch]:
    out = []
    for item in items:
        recording = str(item.get("recording_id") or item.get("id") or "")
        when = item.get("recording_start_time") or item.get("created_at")
        if not recording or not when:
            continue
        title = str(item.get("meeting_title") or item.get("title") or "")
        people = [Contact(str(p.get("email", "")).lower(), str(p.get("name", "")), "interviewer")
                  for p in item.get("calendar_invitees") or []
                  if p.get("email") and str(p.get("email", "")).lower() not in owners
                  and p.get("is_external") is not False]
        domains = [leads.company_domain(leads.domain_of(p.email)) for p in people]
        summary = ((item.get("default_summary") or {}).get("markdown_formatted") or "")
        summary = re.sub(r"\s+", " ", re.sub(r"[#*_\[\]()>]", " ", summary)).strip()
        recorder = item.get("recorded_by") if isinstance(item.get("recorded_by"), dict) else {}
        mine = [str(recorder.get("name") or "")] + [str(p.get("name") or "") for p in item.get("calendar_invitees") or []
                                                    if p.get("is_external") is False]
        found = company_names.resolve(attendees=[p.email for p in people], title=title, owner_emails=owners,
                                      owner_names=[m for m in mine if m])
        url = str(item.get("share_url") or item.get("url") or "")
        out.append(Touch(id=f"fathom:{recording}", kind="recording",
                         at=datetime.fromisoformat(str(when).replace("Z", "+00:00")),
                         company=found.name, company_how=company_resolver.HOW.get(found.source, "").format(
                             domain=found.domain),
                         domain=found.domain or next((d for d in domains if d), ""), people=people,
                         title=f"Recording: {title[:120]}", summary=" ".join(summary.split()[:30]),
                         url=url if url.startswith("https://") else "", detail_url=detail_url(f"fathom:{recording}")))
    return out


def read_performance() -> dict[str, Any] | None:
    path = Path(os.getenv("VALR_PERFORMANCE_PATH") or "/data/performance.json").expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def performance_touches(perf: dict[str, Any] | None) -> list[Touch]:
    out = []
    for item in (perf or {}).get("interviews") or []:
        if not isinstance(item, dict) or not item.get("company") or not item.get("date"):
            continue
        try:
            at = datetime.fromisoformat(str(item["date"])[:10]).replace(hour=12, tzinfo=timezone.utc)
            score = float(item["overall_score"]) if item.get("overall_score") is not None else None
        except (TypeError, ValueError):
            continue
        outcome = item.get("outcome")
        round_type = str(item.get("round_type") or "interview").replace("_", " ")
        out.append(Touch(id=f"perf:{item.get('interview_id') or item['date'] + ':' + item['company']}",
                         kind="evaluation", at=at, company=company_names.clean(str(item["company"]))[:80],
                         stated=str(item["company"])[:80],
                         title=f"{round_type.capitalize()} scored" + (f" {score:.1f}/10" if score is not None else ""),
                         summary={"advanced": "Outcome: advanced.", "rejected": "Outcome: rejected."}.get(outcome, ""),
                         stage_hint={"rejected": "closed-lost"}.get(outcome, "interviewing"),
                         cue={"advanced": "positive", "rejected": "negative"}.get(outcome, "none"),
                         score=score, detail_url=detail_url(str(item.get("interview_id") or ""))))
    return out


# Company names: re-derive them with the one resolver (rules, cache, Haiku review, one web
# search), store what changed. Idempotent: the same evidence gives the same name.

def touch_evidence(touch: Touch, owners: set[str], snippet: str = "") -> company_resolver.Evidence:
    people = [(p.name, p.email) for p in touch.people if p.email.lower() not in owners]
    inbound = touch.kind == "email" and touch.direction == "in" and people
    return company_resolver.Evidence(
        kind=touch.kind, title=touch.title.removeprefix("Recording: "),
        people=people[1:] if inbound else people, domains=[touch.domain] if touch.domain else [],
        sender_name=people[0][0] if inbound else "", sender_email=people[0][1] if inbound else "",
        snippet=snippet, stated=touch.stated or touch.company, role=touch.role)


def names_clean(name: str) -> str:
    return company_names.clean(name) or name


async def snippets(connection: Any) -> dict[str, str]:
    """Stored email snippets, decrypted in memory only (first 300 characters)."""
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    if not key:
        return {}
    cursor = await connection.execute("SELECT id, snippet_enc FROM lead_touchpoints WHERE snippet_enc IS NOT NULL")
    out = {}
    for touch_id, sealed in await cursor.fetchall():
        try:
            out[touch_id] = google_oauth.decrypt(key, sealed)[:300]
        except google_oauth.ReconnectNeeded:
            continue
    return out


async def resolve_touches(connection: Any, touches: list[Touch], resolver: company_resolver.Resolver,
                          limit: int | None = None, dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Each email/calendar/recording touch's company, re-derived; changed ones are stored (in
    place on `touches` too). `limit`: model resolutions this pass. -> [(touch id, before, after)]."""
    await company_resolver.setup(connection)
    owners = resolver.owner_emails
    resolver.agencies |= company_resolver.learn_agencies(touches)
    texts = await snippets(connection)
    changes = []
    for touch in touches:
        if touch.kind not in ("email", "calendar", "recording"):
            continue
        if limit is not None and resolver.calls >= limit:
            resolver.client = None  # Rules and cache only for the rest of this pass.
        beat()
        if not touch.stated and touch.company:
            touch.stated = touch.company  # The original name: strong evidence, kept for good.
        ev = touch_evidence(touch, owners, texts.get(touch.id, ""))
        decision = await resolver.resolve(connection, ev)
        before = touch.company
        baseline = touch.stated or touch.company  # Repairs compare against the original name.
        if not company_resolver.keep_or_change(baseline, decision, resolver.agencies):
            resolver.kept += 1
            decision = company_resolver.Decision(names_clean(baseline), "", company_resolver.EXISTING, "existing",
                                                 "kept the earlier name")
        if (decision.company, decision.how) != (touch.company, touch.company_how):
            changes.append((touch.id, before, decision.company))
            touch.company, touch.company_how = decision.company, decision.how
            if decision.domain:
                touch.domain = decision.domain
            if not dry_run:
                await leads_store.upsert_touch(connection, touch)
    return changes


async def repair_recordings(connection: Any, resolver: company_resolver.Resolver,
                            dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Stored notetaker recordings' company, re-derived (their invitee domain, titles, names)."""
    from app.notetakers import store
    await store.setup(connection)
    cursor = await connection.execute(
        "SELECT provider, external_id, title, event_title, company, company_domain, event_company, interviewers "
        "FROM notetaker_recordings")
    changes = []
    for provider, external_id, title, event_title, company, domain, event_company, people in await cursor.fetchall():
        beat()
        ev = company_resolver.Evidence(
            kind="recording", title=event_title or title or "", domains=[domain] if domain else [],
            people=[(n, "") for n in store.names({"interviewers": people})], stated=company or event_company or "")
        decision = await resolver.resolve(connection, ev, entity_id=f"interview:{provider}:{external_id}")
        old = company or event_company or ""
        if decision.source != "override" and not company_resolver.keep_or_change(old, decision, resolver.agencies):
            resolver.kept += 1
            continue
        if decision.company != (company or ""):
            changes.append((f"{provider}:{external_id}", company or event_company or "", decision.company))
            if not dry_run:
                await connection.execute(
                    "UPDATE notetaker_recordings SET company = %s, company_how = %s "
                    "WHERE provider = %s AND external_id = %s",
                    (decision.company, decision.how[:200], provider, external_id))
    return changes


async def repair_companies(dry_run: bool = False) -> dict[str, Any]:
    """`python -m app.lead_worker repair-companies [--dry-run]`: names only, before -> after.
    The full pipeline (rules -> Haiku -> one web search), with the stability rule: a stored name
    changes only for a more confident one that is not an agency or platform. A dry run changes
    no names (model answers are still cached, so the real run costs nothing more)."""
    now = datetime.now(timezone.utc)
    resolver = company_resolver.default_resolver(owner_emails())
    async with await connect() as connection:
        await leads_store.setup(connection)
        everything = await leads_store.touches(connection)
        before = leads.build([leads_store.touch_from_row(t.id, t.kind, t.at, t.lead_id, leads_store.touch_data(t))
                              for t in everything], now, await leads_store.overrides(connection))
        touches = await resolve_touches(connection, everything, resolver, dry_run=dry_run)
        recordings = await repair_recordings(connection, resolver, dry_run=dry_run)
        after = leads.build(everything, now, await leads_store.overrides(connection))
        if not dry_run:
            await leads_store.save_leads(connection, after, everything, now)
    return {"dry_run": dry_run, "model_calls": resolver.calls, "usd": round(resolver.usd, 4),
            "kept_by_stability_rule": resolver.kept, "agency_domains_learned": len(resolver.agencies),
            "leads_before": len(before), "leads_after": len(after),
            "not_identified_before": sum(1 for l in before if l.company == company_names.NOT_IDENTIFIED),
            "not_identified_after": sum(1 for l in after if l.company == company_names.NOT_IDENTIFIED),
            "lead_names_before": sorted({l.company for l in before}), "lead_names_after": sorted({l.company for l in after}),
            "touches": [f"{a or '-'} -> {b or company_names.NOT_IDENTIFIED}" for _, a, b in touches],
            "recordings": [f"{a or '-'} -> {b or company_names.NOT_IDENTIFIED}" for _, a, b in recordings]}


# Schedules (the research worker calls this at start) and a small CLI.

async def ensure_lead_schedules(client: Any) -> None:
    """With VALR_LEADS=1: the 30-minute safety poll (its first run backfills) and the daily
    watch renewal, both started at once. Without it: nothing (existing schedules stay)."""
    from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError,
                                   ScheduleIntervalSpec, ScheduleOverlapPolicy, SchedulePolicy, ScheduleSpec)
    from app.lead_workflow import (POLL_SCHEDULE, TASK_QUEUE, WATCH_SCHEDULE, GmailSyncWorkflow,
                                   GmailWatchWorkflow)
    if not enabled():
        logger.info("VALR leads are off (VALR_LEADS=0)")
        return
    minutes = float(os.getenv("VALR_LEADS_POLL_MINUTES", "30"))
    plans = [(POLL_SCHEDULE, ScheduleActionStartWorkflow(GmailSyncWorkflow.run, GmailSyncInput("poll"),
                                                         id=f"{POLL_SCHEDULE}-run", task_queue=TASK_QUEUE),
              timedelta(minutes=minutes))]
    if topic():
        plans.append((WATCH_SCHEDULE, ScheduleActionStartWorkflow(GmailWatchWorkflow.run, "schedule",
                                                                  id=f"{WATCH_SCHEDULE}-run", task_queue=TASK_QUEUE),
                      timedelta(hours=24)))
    else:
        logger.warning("VALR leads: VALR_GMAIL_TOPIC is not set; no push, polling only")
    for name, action, every in plans:
        try:
            await client.create_schedule(name, Schedule(
                action=action, spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP)), trigger_immediately=True)
            logger.info("VALR leads: schedule %s every %s", name, every)
        except ScheduleAlreadyRunningError:
            pass


async def cli(command: str) -> None:
    """`python -m app.lead_worker sync|watch|stats|repair-companies [--dry-run]`: counts and
    company names only, no content."""
    from dataclasses import asdict
    from temporalio.client import Client
    from app.lead_workflow import SYNC_WORKFLOW_ID, TASK_QUEUE, GmailSyncWorkflow, GmailWatchWorkflow
    if command.startswith("repair-companies"):
        print(json.dumps(await repair_companies(dry_run=command.endswith("--dry-run")), indent=2))
        return
    if command == "stats":
        async with await connect() as connection:
            await leads_store.setup(connection)
            state = await leads_store.load_state(connection)
            counts = await leads_store.decision_counts(connection)
            cursor = await connection.execute("SELECT status, stage, count(*) FROM leads GROUP BY 1, 2 ORDER BY 1, 2")
            rows = await cursor.fetchall()
        print(json.dumps({"history_id": state.get("history_id"), "watch_expires": str(state.get("watch_expires")),
                          "last_sync_at": str(state.get("last_sync_at")), "backfill_count": state.get("backfill_count"),
                          "backfill_done_at": str(state.get("backfill_done_at")),
                          "last_error": state.get("last_error"), "decisions_30d": counts,
                          "leads": [f"{r[0]} {r[1]}: {r[2]}" for r in rows]}, indent=2))
        return
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
                                  namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
    if command == "watch":
        result = await client.execute_workflow(GmailWatchWorkflow.run, "manual", id="valr-gmail-watch-manual",
                                               task_queue=TASK_QUEUE)
    else:
        from temporalio.common import WorkflowIDConflictPolicy
        handle = await client.start_workflow(GmailSyncWorkflow.run, GmailSyncInput("manual"), id=SYNC_WORKFLOW_ID,
                                             task_queue=TASK_QUEUE, start_signal="poke",
                                             id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        result = await handle.result()
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    if sys.argv[1:2] not in (["sync"], ["watch"], ["stats"], ["repair-companies"]):
        sys.exit("usage: python -m app.lead_worker sync|watch|stats|repair-companies [--dry-run]")
    asyncio.run(cli(" ".join(sys.argv[1:3])))
