"""Lead tracker activities for the research worker (task queue `voice-research`).

- `gmail_sync`: the messages added since the stored historyId (or, the first time, the last
  VALR_LEADS_BACKFILL_DAYS days, at most VALR_LEADS_BACKFILL_MAX messages), plus up to
  DEFERRED_PER_PASS earlier "deferred" ones. Each new message: heuristics, then Claude Haiku
  within the day's budget (VALR_LEADS_LLM_PER_DAY) and the research month cap; the decision
  is logged without content; a kept one becomes a touchpoint. A historyId that is too old
  (404) falls back to a 3-day search. One pass at a time (a Postgres advisory lock).
- `leads_refresh`: calendar interviews (calendar_events), Fathom recordings (FATHOM_API_KEY)
  and scored interviews (performance.json) become touchpoints; then every lead is rebuilt
  (app/leads.build) and saved.
- `gmail_watch`: users.watch on VALR_GMAIL_TOPIC, daily.

Settings: VALR_LEADS=1 turns the schedules on; VALR_GMAIL_TOPIC; VALR_LEADS_BACKFILL_DAYS (30),
VALR_LEADS_BACKFILL_MAX (500), VALR_LEADS_LLM_PER_DAY (300), VALR_LEADS_POLL_MINUTES (30).
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

from app import gmail_api, google_oauth, lead_classify, leads, leads_store, research_jobs
from app.lead_classify import Decision, Email
from app.lead_workflow import GmailSyncInput, GmailSyncSummary, RefreshSummary, WatchSummary
from app.leads import Contact, Touch

logger = logging.getLogger("valr")
SYNC_LOCK = 7_202_610_18
DEFERRED_PER_PASS = 40
RECOVERY_QUERY = "newer_than:3d -in:chats"
CONCURRENCY = 4
FATHOM_MEETINGS = "https://api.fathom.ai/external/v1/meetings"
DETAIL_URL = "/valr#performance"


def enabled() -> bool:
    return os.getenv("VALR_LEADS", "0") == "1"


def topic() -> str:
    return os.getenv("VALR_GMAIL_TOPIC", "").strip()


def llm_per_day() -> int:
    return int(os.getenv("VALR_LEADS_LLM_PER_DAY", "300"))


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
    return Touch(id=f"gmail:{email.id}", kind="email", at=email.at, company=decision.company,
                 domain=domain, people=people, title=email.subject[:140],
                 summary=decision.summary, direction="out" if outbound else "in",
                 sentiment=None if outbound else decision.sentiment, cue=decision.cue,
                 stage_hint=decision.stage_hint, next_step=decision.next_step,
                 url=gmail_api.thread_url(email.thread_id), role=decision.role)


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
                 classify: Callable[[Email, set[str]], Decision] | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._classify = classify

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
        if request.history_id and state.get("history_id") and request.history_id <= state["history_id"]:
            summary.mode = "skipped"  # A late or repeated push: already processed.
            return summary
        token, status = await gmail_access(connection, self.http)
        if not token:
            summary.error = f"Gmail {status}"
            await leads_store.save_state(connection, last_error=summary.error)
            logger.info("VALR leads: Gmail sync skipped (%s)", status)
            return summary
        refs: list[dict[str, Any]]
        new_history: int | None = None
        if not state.get("history_id"):
            summary.mode = "backfill"
            profile = await gmail_api.profile(self.http, token)
            new_history = int(profile.get("historyId") or 0)  # Before listing: nothing is missed.
            days = int(os.getenv("VALR_LEADS_BACKFILL_DAYS", "30"))
            cap = int(os.getenv("VALR_LEADS_BACKFILL_MAX", "500"))
            await leads_store.save_state(connection, account=str(profile.get("emailAddress", "")).lower(),
                                         backfill_started_at=now)
            refs = await gmail_api.search(self.http, token, f"newer_than:{days}d -in:chats", cap)
        else:
            summary.mode = "history"
            try:
                refs, new_history = await gmail_api.history(self.http, token, int(state["history_id"]))
            except gmail_api.HistoryGone:
                summary.mode = "recovery"
                logger.warning("VALR leads: Gmail historyId too old; searching the last 3 days")
                new_history = int((await gmail_api.profile(self.http, token)).get("historyId") or 0)
                refs = await gmail_api.search(self.http, token, RECOVERY_QUERY, 300)
        ids = list(dict.fromkeys(ref["id"] for ref in refs if ref.get("id")))
        done = await leads_store.seen(connection, ids)
        todo = [i for i in ids if i not in done]
        todo += [i for i in await leads_store.deferred(connection, DEFERRED_PER_PASS) if i not in todo]
        await self.process(connection, token, todo, summary)
        values: dict[str, Any] = {"last_sync_at": now, "last_error": None}
        if new_history:
            values["history_id"] = max(new_history, int(state.get("history_id") or 0))
        if summary.mode == "backfill":
            values.update(backfill_done_at=datetime.now(timezone.utc), backfill_count=len(ids))
        await leads_store.save_state(connection, **values)
        logger.info("VALR leads: Gmail %s (%s): %d new, %d kept, %d dropped, %d deferred, %d model calls, $%.4f",
                    summary.mode, request.reason, summary.fetched, summary.kept, summary.dropped,
                    summary.deferred, summary.llm_calls, summary.usd)
        return summary

    async def process(self, connection: Any, token: str, ids: list[str], summary: GmailSyncSummary) -> None:
        owners = owner_emails()
        limits = research_jobs.Limits.interview_from_env()
        budget = Budget(max(0, llm_per_day() - await leads_store.llm_calls_today(connection)),
                        await research_jobs.month_spend(connection) < limits.month_usd)
        emails: list[Email] = []
        for message_id in ids:
            raw = await gmail_api.message(self.http, token, message_id)
            beat()
            if raw is not None:
                emails.append(gmail_api.parse(raw))
        summary.fetched = len(emails)
        known = await leads_store.kept_threads(connection, [e.thread_id for e in emails])
        gate = asyncio.Semaphore(CONCURRENCY)

        async def decide(email: Email) -> Decision:
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
            summary.usd = await research_jobs.record_spend(
                connection, f"leads-{datetime.now(timezone.utc):%Y-%m-%d}", rows)

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
            if not state.get("history_id"):
                logger.info("VALR leads: watch on; the first sync backfills")
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
            built = leads.build(everything, now)
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
        return calendar_to_touches(events, owner_emails())

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

def calendar_to_touches(events: list[Any], owners: set[str]) -> list[Touch]:
    from app.interview_types import INTERVIEW, attendee_role, event_noun
    out = []
    for event in events:
        if event.all_day or event.declined or event.status == "cancelled":
            continue
        interview = bool(INTERVIEW.search(event.title))
        if not (event.company and (interview or event.company_domain)):
            continue
        people = []
        everyone = list(event.attendees)
        if event.organizer_email and all(p.email != event.organizer_email for p in everyone):
            from app.interview_types import Person
            everyone.append(Person(event.organizer_email, event.organizer_name, organizer=True))
        for person in everyone:
            role = attendee_role(person, owners)
            if role in ("candidate", "recruiter"):
                people.append(Contact(person.email.lower(), person.name,
                                      "interviewer" if role == "candidate" else "recruiter"))
        noun = event_noun(event.title)
        out.append(Touch(id=f"cal:{event.key}", kind="calendar",
                         at=datetime.fromisoformat(event.start_utc), company=event.company,
                         domain=event.company_domain, people=people, title=event.title[:140],
                         summary=f"{noun.capitalize()} on the calendar" + (f" with {len(people)} people" if people else ""),
                         stage_hint="interviewing" if noun == "interview" else ""))
    return out


async def cancelled_calendar_keys(connection: Any) -> list[str]:
    cursor = await connection.execute("SELECT key FROM calendar_events WHERE status = 'cancelled'")
    return [row[0] for row in await cursor.fetchall()]


TITLE_COMPANY = re.compile(r"(?:with|@|at|x|<>|/|-|:)\s*(?P<c>[A-Z][\w&.\- ]{1,40}?)\s*(?:\(|$|-|\||:|interview|round)", re.I)


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
        match = TITLE_COMPANY.search(title)
        url = str(item.get("share_url") or item.get("url") or "")
        out.append(Touch(id=f"fathom:{recording}", kind="recording",
                         at=datetime.fromisoformat(str(when).replace("Z", "+00:00")),
                         company=match.group("c").strip() if match else "",
                         domain=next((d for d in domains if d), ""), people=people,
                         title=f"Recording: {title[:120]}", summary=" ".join(summary.split()[:30]),
                         url=url if url.startswith("https://") else "", detail_url=DETAIL_URL))
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
                         kind="evaluation", at=at, company=str(item["company"])[:80],
                         title=f"{round_type.capitalize()} scored" + (f" {score:.1f}/10" if score is not None else ""),
                         summary={"advanced": "Outcome: advanced.", "rejected": "Outcome: rejected."}.get(outcome, ""),
                         stage_hint={"rejected": "closed-lost"}.get(outcome, "interviewing"),
                         cue={"advanced": "positive", "rejected": "negative"}.get(outcome, "none"),
                         score=score, detail_url=DETAIL_URL))
    return out


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
    """`python -m app.lead_worker sync|watch|stats`: counts only, no content."""
    from dataclasses import asdict
    from temporalio.client import Client
    from app.lead_workflow import SYNC_WORKFLOW_ID, TASK_QUEUE, GmailSyncWorkflow, GmailWatchWorkflow
    if command == "stats":
        async with await connect() as connection:
            await leads_store.setup(connection)
            state = await leads_store.load_state(connection)
            counts = await leads_store.decision_counts(connection)
            cursor = await connection.execute("SELECT status, stage, count(*) FROM leads GROUP BY 1, 2 ORDER BY 1, 2")
            rows = await cursor.fetchall()
        print(json.dumps({"history_id": state.get("history_id"), "watch_expires": str(state.get("watch_expires")),
                          "last_sync_at": str(state.get("last_sync_at")), "backfill_count": state.get("backfill_count"),
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
    if sys.argv[1:2] not in (["sync"], ["watch"], ["stats"]):
        sys.exit("usage: python -m app.lead_worker sync|watch|stats")
    asyncio.run(cli(sys.argv[1]))
