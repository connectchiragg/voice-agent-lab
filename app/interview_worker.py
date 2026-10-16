"""Interview briefing activities for the research worker (task queue `voice-research`).

- `calendar_sync`: pull the calendar (Google incremental with the stored syncToken, a
  full resync after a 410; the ICS URL when OAuth is not connected), diff each event
  against its snapshot, signal the slot workflows, recompute the blocks and start,
  update or cancel their call workflows, and renew the push channel when due.
  One sync at a time (a Postgres advisory lock).
- `slot_plan`: the cache decisions and the job booking (interview limits).
- `interview_part`: one researcher (R1 private, R2 round, R2 interviewers, R3 company);
  interviewers go through the HR guard and the classifier first.
- `slot_synthesize`: merge the new parts into the slot's stored context, write the brief.
- `slot_finish`, `slots_ready`, `block_record`: bookkeeping.
- `dials_paused`: the dashboard's pause switch for automatic briefing calls (and, since
  the Telegram design, for the heads-ups too).
- `briefing_auto_dial`: the Settings choice (stored in valr_flags), else VALR_AUTO_DIAL=1 (default off):
  dial at T-30 instead of a heads-up.
- `telegram_heads_up`: the T-30 Telegram message with [Call me now] [In 10 min] [Skip].

Nothing here logs an email address, a phone number, an email's text or a pay figure.
"""
import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from app import (calendar_sync, google_oauth, interview_store, interview_types, research_jobs,
                 telegram_api)
from app.interview_types import (Person, SlotEvent, attendee_role, company_key, group_blocks,
                                 people, slot_workflow_id)
from app.interview_workflow import (TASK_QUEUE, BlockCallWorkflow, BlockInput, BlockRecord,
                                    HeadsUpInput, InterviewPartInput, InterviewPartResult, InterviewSlotWorkflow,
                                    PlanInput, PlanResult, SlotFinish, SlotInput, SlotPlan,
                                    SlotSynthInput, SlotSynthResult, SlotUpdate, SyncInput,
                                    SyncSummary, RESEARCH_LEAD)

logger = logging.getLogger("valr")
HORIZON = timedelta(days=7)
SYNC_LOCK = 7_202_610_17
HEARTBEAT_S = 5


def owner_emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv("VALR_OWNER_EMAILS", "").split(",") if e.strip()} \
        | ({os.getenv("VALR_GMAIL_ACCOUNT", "").strip().lower()} - {""})


def owner_phone() -> str:
    return os.getenv("VALR_OWNER_PHONE", "").strip()


def owner_key() -> str:
    return f"tel:{owner_phone()}" if owner_phone() else ""


def zone() -> ZoneInfo:
    return ZoneInfo(os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata"))


def clock(moment: datetime) -> str:
    local = moment.astimezone(zone())
    return f"{local.strftime('%I').lstrip('0')}:{local:%M %p}"


def block_label(events: list[SlotEvent]) -> str:
    first = datetime.fromisoformat(events[0].start_utc)
    names = [e.company or e.title[:40] for e in events]
    if len(events) == 1:
        return f"your {clock(first)} {interview_types.event_noun(events[0].title)} with {names[0]}"
    nouns = {interview_types.event_noun(event.title) for event in events}
    noun = "interviews" if nouns == {"interview"} else "meetings"
    return f"your {clock(first)} {noun}: {', then '.join(names)}"


def fingerprint(event: SlotEvent) -> str:
    return interview_types.fingerprint(event, owner_emails())


async def connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True)


async def gmail_access(connection: Any, http: httpx.AsyncClient) -> tuple[str | None, str]:
    """(access token, status) for Gmail and Calendar; never raises."""
    settings = google_oauth.OAuthSettings.from_env()
    if settings is None:
        return None, "not configured"
    try:
        await google_oauth.setup(connection)
        return await google_oauth.access_token(connection, settings, http), "connected"
    except google_oauth.ReconnectNeeded:
        return None, "reconnect needed"
    except google_oauth.NotConfigured:
        return None, "not connected"
    except httpx.HTTPError as error:
        return None, f"unavailable ({type(error).__name__})"


class InterviewActivities:
    def __init__(self, client: Client, http: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self.http = http or httpx.AsyncClient(timeout=20, follow_redirects=True)

    # Calendar.

    @activity.defn(name="calendar_sync")
    async def calendar_sync(self, request: SyncInput) -> SyncSummary:
        async with await connect() as connection:
            await calendar_sync.setup(connection)
            await interview_store.setup(connection)
            await connection.execute("SELECT pg_advisory_lock(%s)", (SYNC_LOCK,))
            try:
                return await self.sync(connection, request, datetime.now(timezone.utc))
            finally:
                await connection.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK,))

    async def sync(self, connection: Any, request: SyncInput, now: datetime) -> SyncSummary:
        summary = SyncSummary()
        owners = owner_emails()
        state = await calendar_sync.load_state(connection)
        token, status = await gmail_access(connection, self.http)
        events: list[SlotEvent] = []
        cancelled: list[str] = []
        full = request.full
        next_token = None
        if token:
            sync_token = None if request.full else state.get("sync_token")
            try:
                items, next_token = await calendar_sync.list_events(self.http, token, sync_token)
            except calendar_sync.SyncTokenExpired:
                logger.info("VALR calendar: sync token expired; full resync")
                items, next_token = await calendar_sync.list_events(self.http, token, None)
                sync_token = None
            full = sync_token is None
            events, cancelled = calendar_sync.from_google_items(items)
            summary.source = "google"
        else:
            url = os.getenv("VALR_CALENDAR_ICS_URL", "").strip()
            if not url:
                summary.error = f"no calendar source (Google {status}, no ICS URL)"
                return summary
            response = await self.http.get(url)
            response.raise_for_status()
            events = calendar_sync.from_ics_text(response.text, owners)
            full, summary.source = True, f"ics (Google {status})"
        pairs = await calendar_sync.changes(connection, events, cancelled, full, now, owners)
        await calendar_sync.save_state(
            connection, source=summary.source, last_sync_at=now, last_error=None,
            **({"sync_token": next_token} if token else {}),
            **({"last_full_sync_at": now} if full else {}))
        for event, change in pairs:
            for kind in change.kinds:
                summary.changes[kind] = summary.changes.get(kind, 0) + 1
            await self.signal_slot(event, change)
        await self.reconcile(connection, now, summary)
        if token:
            summary.channel = await self.renew_channel(connection, token, state, now)
        logger.info("VALR calendar sync (%s, %s): changes %s, %d slots and %d blocks started, "
                    "%d blocks cancelled%s", request.reason, summary.source,
                    json.dumps(summary.changes, sort_keys=True), summary.slots_started,
                    summary.blocks_started, summary.blocks_cancelled,
                    f", channel {summary.channel}" if summary.channel else "")
        return summary

    async def signal_slot(self, event: SlotEvent, change: Any) -> None:
        handle = self.client.get_workflow_handle(slot_workflow_id(event.key))
        try:
            if "cancelled" in change.kinds:
                await handle.signal(InterviewSlotWorkflow.cancel)
            elif "new" not in change.kinds:
                await handle.signal(InterviewSlotWorkflow.event_updated, SlotUpdate(event, change))
        except RPCError:
            pass  # Not running: reconcile starts it if it is upcoming.

    async def reconcile(self, connection: Any, now: datetime, summary: SyncSummary) -> None:
        """Blocks from the stored snapshots: start or update slots and block calls; cancel
        the block calls of blocks that disappeared or moved."""
        upcoming = await calendar_sync.upcoming(connection, now - timedelta(hours=1), now + HORIZON)
        by_key = {event.key: event for event in upcoming}
        blocks = [b for b in group_blocks(upcoming) if datetime.fromisoformat(b.start_utc) > now]
        previous = {b["block_id"]: b for b in await interview_store.blocks_between(
            connection, now, now + HORIZON + timedelta(days=1))}
        wanted = set()
        for block in blocks:
            wanted.add(block.block_id)
            start = datetime.fromisoformat(block.start_utc)
            due = (start - RESEARCH_LEAD).isoformat()
            for key in block.slot_keys:
                summary.slots_started += await self.ensure_slot(by_key[key], block.block_id, due)
            if not owner_phone():
                continue
            events = [by_key[key] for key in block.slot_keys]
            old = previous.get(block.block_id)
            if old is None or old.get("state") in ("cancelled",):
                started = await self.start_block(block, block_label(events))
                summary.blocks_started += started
            elif list(old["slot_keys"]) != block.slot_keys:
                try:
                    await self.client.get_workflow_handle(block.block_id).signal(
                        BlockCallWorkflow.update_slots, block.slot_keys)
                except RPCError:
                    pass
                await interview_store.record_block(connection, block.block_id, block.start_utc,
                                                   block.slot_keys, block_label(events), old["state"])
        for block_id, old in previous.items():
            if block_id in wanted or old.get("state") not in ("waiting",):
                continue
            if old["start_utc"] <= now:
                continue
            try:
                await self.client.get_workflow_handle(block_id).signal(BlockCallWorkflow.cancel)
            except RPCError:
                pass
            await interview_store.record_block(connection, block_id, old["start_utc"].isoformat(),
                                               list(old["slot_keys"]), old["label"], "cancelled")
            summary.blocks_cancelled += 1

    async def ensure_slot(self, event: SlotEvent, block_id: str, due: str) -> int:
        """Start the slot's workflow if it is not running; else tell it its block. -> started?"""
        workflow_id = slot_workflow_id(event.key)
        handle = self.client.get_workflow_handle(workflow_id)
        try:
            description = await handle.describe()
            running = description.status is not None and description.status.name == "RUNNING"
        except RPCError:
            running = False
        if running:
            await handle.signal(InterviewSlotWorkflow.set_plan, SlotPlan(block_id, due))
            return 0
        limits = research_jobs.Limits.interview_from_env()
        await self.client.start_workflow(
            InterviewSlotWorkflow.run, SlotInput(event, owner_key(), block_id, due, limits.job_usd),
            id=workflow_id, task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        return 1

    async def start_block(self, block: Any, label: str) -> int:
        request = BlockInput(block.block_id, block.start_utc, block.slot_keys, owner_key(),
                             owner_phone(), label,
                             dials_per_day=int(os.getenv("VALR_BRIEFING_DIALS_PER_DAY", 12)),
                             dial=os.getenv("VALR_BRIEFING_DIAL", "1") == "1")
        try:
            await self.client.start_workflow(
                BlockCallWorkflow.run, request, id=block.block_id, task_queue=TASK_QUEUE,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
        except Exception as error:  # noqa: BLE001 - an already finished block (briefed) stays done
            logger.info("VALR block %s not started: %s", block.block_id, type(error).__name__)
            return 0
        return 1

    async def renew_channel(self, connection: Any, token: str, state: dict[str, Any],
                            now: datetime) -> str:
        address = os.getenv("VALR_GCAL_WEBHOOK", "https://valr.haciensus.com/hooks/gcal")
        if os.getenv("VALR_GCAL_PUSH", "1") != "1":
            return "push off"
        if not calendar_sync.needs_renewal(state, now):
            return "ok"
        try:
            channel = await calendar_sync.watch(self.http, token, address, now)
        except httpx.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", "")
            logger.warning("VALR calendar watch failed: %s %s", type(error).__name__, status)
            return f"watch failed {status}"
        if state.get("channel_id") and state.get("resource_id"):
            try:
                await calendar_sync.stop(self.http, token, state["channel_id"], state["resource_id"])
            except httpx.HTTPError:
                pass
        await calendar_sync.save_state(connection, channel_id=channel["channel_id"],
                                       resource_id=channel["resource_id"],
                                       channel_token=channel["channel_token"],
                                       channel_expires=channel["expires"])
        return f"renewed until {channel['expires']:%Y-%m-%d %H:%M} UTC"

    # Research.

    @activity.defn(name="slot_plan")
    async def slot_plan(self, plan: PlanInput) -> PlanResult:
        event = plan.event
        parts = [p for p in plan.parts if p != "resynth"]
        hits: list[str] = []
        async with await connect() as connection:
            await research_jobs.setup(connection)
            await interview_store.setup(connection)
            existing = await interview_store.load_slot(connection, event.key)
            same_inputs = bool(existing and existing["parts"].get("_fingerprint") == fingerprint(event))
            if (not plan.refresh and same_inputs and existing["status"] in ("done", "partial")
                    and set(parts) >= {"private", "round", "company"}):
                await research_jobs.record_cache_hit(connection, "cache-" + uuid.uuid4().hex[:11],
                                                     plan.owner_key, event.title[:120], event.key,
                                                     "slot research reused (inputs unchanged)")
                logger.info("VALR slot cache hit (inputs unchanged)")
                return PlanResult("", [], ["slot"])
            if "company" in parts and not plan.refresh and \
                    await interview_store.company(connection, company_key(event)) is not None:
                parts.remove("company")
                hits.append("company")
            job_id = "iv-" + uuid.uuid4().hex[:13]
            limits = research_jobs.Limits.interview_from_env()
            refusal = await research_jobs.reserve(connection, research_jobs.Booking(
                job_id, plan.owner_key, event.title[:120], 0, "interview", event.key), limits)
            if refusal:
                logger.info("VALR slot research refused: %s", refusal)
                return PlanResult("", [], hits, refusal)
            if hits:
                await research_jobs.set_detail(connection, job_id, "cache: " + ", ".join(hits))
        return PlanResult(job_id, parts, hits)

    @activity.defn(name="interview_part")
    async def interview_part(self, task: InterviewPartInput) -> InterviewPartResult:
        from app import interview_agents, research_agents  # Heavy: loaded on first use.
        from app.research_worker import heartbeat_while, save_spend
        event = task.event
        meter = research_agents.Meter(task.part)
        private = interview_agents.PrivateInputs()
        interviewers = list(task.interviewers)
        skipped = 0
        async with await connect() as connection:
            await research_jobs.mark_started(connection, task.job_id)
            if task.part == "private":
                private.gmail_token, private.gmail_status = await gmail_access(connection, self.http)
                await interview_store.setup(connection)
                private.pushed_texts = await decrypt_pushes(connection, event)
                private.fathom_key, private.granola_key = await notetaker_keys(connection)
            if task.part in ("private", "round", "interviewers"):
                candidates = interviewers or people(event, owner_emails())
                if task.part == "interviewers":
                    stored = await interview_store.load_slot(connection, event.key)
                    evidence = ((stored or {}).get("parts", {}).get("private") or {}).get("evidence", "")
                    roles = await asyncio.to_thread(interview_agents.classify_people, candidates,
                                                    evidence, meter, event.company_domain)
                    interviewers = [p for p in candidates if roles.get(p.email.lower()) == "interviewer"]
                    skipped = len(candidates) - len(interviewers)
                    if skipped:
                        logger.info("VALR interviewer research: %d of %d people skipped "
                                    "(recruiting/HR/unknown)", skipped, len(candidates))
                else:
                    interviewers = candidates
        if task.part == "interviewers" and not interviewers:
            await save_spend(task.job_id, meter.rows)
            return InterviewPartResult(task.part, skipped=skipped, usd=meter.usd())
        if task.part == "private" and not (private.gmail_token or private.fathom_key or private.granola_key
                                           or private.pushed_texts or history_available()):
            return InterviewPartResult(task.part, error=f"no private sources (Gmail {private.gmail_status})")
        box: list = []
        coaching = "\n".join(owner_context()) if task.part == "round" else ""
        work = asyncio.ensure_future(asyncio.to_thread(
            interview_agents.run_interview_part, task.part, event, interviewers, task.seconds,
            task.budget_usd, meter, private, box, coaching))
        try:
            await heartbeat_while(work)
            outcome = await work
        except asyncio.CancelledError:
            for ctx in box:
                ctx.killed = True
            await save_spend(task.job_id, meter.rows)
            raise
        usd = await save_spend(task.job_id, meter.rows)
        result = InterviewPartResult(
            task.part, outcome.ok, json.dumps(outcome.findings) if outcome.findings else "",
            outcome.evidence, outcome.seen_urls, usd, outcome.stopped, dict(meter.cap_hits),
            outcome.error, [p.email.lower() for p in interviewers] if task.part == "interviewers" else [],
            skipped)
        if task.part == "company" and (outcome.ok or outcome.evidence):
            async with await connect() as connection:
                await interview_store.setup(connection)
                cached = await interview_store.company(connection, company_key(event))
                if outcome.ok or not (cached or {}).get("ok"):  # Never replace good with worse.
                    await interview_store.save_company(connection, company_key(event),
                                                       event.company, part_record(result))
        logger.info("VALR %s researcher: %s, $%.4f%s", task.part, "ok" if outcome.ok else "no findings",
                    usd, f", stopped ({outcome.stopped})" if outcome.stopped else "")
        return result

    @activity.defn(name="slot_synthesize")
    async def slot_synthesize(self, task: SlotSynthInput) -> SlotSynthResult:
        from app import interview_agents, research_agents
        from app.research_worker import heartbeat_while, save_spend
        event = task.event
        async with await connect() as connection:
            await interview_store.setup(connection)
            stored = await interview_store.load_slot(connection, event.key)
            parts = merge_parts((stored or {}).get("parts") or {}, task.parts, task.removed)
            cached_company = await interview_store.company(connection, company_key(event))
        if "company" not in parts and cached_company:
            parts["company"] = cached_company
        parts["_fingerprint"] = fingerprint(event)
        research = {k: v for k, v in parts.items() if not k.startswith("_")}
        known = {url for part in research.values() if isinstance(part, dict) and "seen_urls" in part
                 for url in part["seen_urls"]}
        known |= {url for part in (parts.get("interviewers_by_email") or {}).values()
                  for url in part.get("seen_urls", [])}
        usable = any(isinstance(p, dict) and (p.get("ok") or p.get("evidence")) for p in research.values()) \
            or any(p.get("ok") for p in (parts.get("interviewers_by_email") or {}).values())
        current = [p for p in people(event, owner_emails())
                   if p.email.lower() in interviewed(parts)]
        meter = research_agents.Meter("synth")
        profile_text, evaluation_text = owner_context()
        status, spoken, notes, sources = "apologized", (
            f"I couldn't find anything reliable about {event.company or event.title} for this one, "
            "so go with what you already know."), [], []
        if usable:
            work = asyncio.ensure_future(asyncio.to_thread(
                interview_agents.synthesize_slot, event, current, research, known, profile_text,
                evaluation_text, task.seconds, meter))
            try:
                await heartbeat_while(work)
                outcome = await work
                spoken, notes, sources = outcome.spoken, outcome.notes, outcome.sources
                status = "partial" if outcome.partial else "done"
            finally:
                usd = await save_spend(task.job_id, meter.rows)
        else:
            usd = 0.0
        async with await connect() as connection:
            await interview_store.save_parts(connection, event.key, company_key(event), event.title,
                                             event.start_utc, parts)
            await interview_store.save_brief(connection, event.key, spoken, notes, sources, status)
        logger.info("VALR slot brief: %s, %d words, %d notes, $%.4f", status, len(spoken.split()),
                    len(notes), usd)
        return SlotSynthResult(status, usd, len(spoken.split()), dict(meter.cap_hits))

    @activity.defn(name="slot_finish")
    async def slot_finish(self, task: SlotFinish) -> None:
        async with await connect() as connection:
            await research_jobs.finish(connection, task.job_id, task.status, task.duration_s, "",
                                       [], [], task.cap_hits, task.detail)

    @activity.defn(name="slots_ready")
    async def slots_ready(self, keys: list[str]) -> bool:
        async with await connect() as connection:
            await interview_store.setup(connection)
            return await interview_store.ready(connection, keys)

    @activity.defn(name="block_record")
    async def block_record(self, record: BlockRecord) -> None:
        async with await connect() as connection:
            await interview_store.setup(connection)
            await interview_store.record_block(connection, record.block_id, record.start_utc,
                                               record.slot_keys, record.label, record.state)

    @activity.defn(name="dials_paused")
    async def dials_paused(self) -> bool:
        async with await connect() as connection:
            await interview_store.setup(connection)
            return (await interview_store.flag(connection, interview_store.DIALS_PAUSED))["enabled"]

    @activity.defn(name="briefing_auto_dial")
    async def briefing_auto_dial(self) -> bool:
        """The owner's Settings choice (heads-up vs automatic call), else VALR_AUTO_DIAL."""
        try:
            async with await connect() as connection:
                await interview_store.setup(connection)
                return await interview_store.auto_dial(connection)
        except Exception as error:  # noqa: BLE001 - the database is down: the server default.
            activity.logger.warning("Auto-dial choice unavailable: %s", type(error).__name__)
            return telegram_api.auto_dial()

    @activity.defn(name="telegram_heads_up")
    async def telegram_heads_up(self, request: HeadsUpInput) -> str:
        """Send the block's heads-up; 'sent', or why not (no secrets, no personal data)."""
        bot = telegram_api.bot()
        if bot is None:
            return "Telegram is not configured (TELEGRAM_BOT_TOKEN)"
        async with await connect() as connection:
            await telegram_api.setup(connection)
            await interview_store.setup(connection)
            await calendar_sync.setup(connection)
            chat = await telegram_api.owner_chat(connection)
            if chat is None:
                return "Telegram is not linked yet (send the dashboard's /start code)"
            snapshots = await calendar_sync.snapshots(connection, list(request.slot_keys))
            events = [snapshots[key] for key in request.slot_keys
                      if key in snapshots and interview_types.briefable(snapshots[key])]
            slots = await interview_store.load_slots(connection, [e.key for e in events])
            companies = {}
            for key in {slot.get("company_key") for slot in slots} - {None, ""}:
                companies[key] = await interview_store.company(connection, key)
        if not events:
            return "no interviews left in this block"
        # Public research only in Telegram: no spoken brief, no private (email) part, no pay.
        ready = {slot["key"] for slot in slots if slot.get("status") in ("done", "partial")}
        teasers = {slot["key"]: telegram_api.public_teaser(slot, companies.get(slot.get("company_key")))
                   for slot in slots}
        text = telegram_api.heads_up_text(events, teasers, ready, datetime.now(timezone.utc))
        try:
            message_id = await bot.send(chat, text, telegram_api.keyboard(request.block_id))
        except telegram_api.TelegramError as error:
            logger.warning("VALR Telegram heads-up failed: %s", error)
            return f"failed: {error}"[:200]
        async with await connect() as connection:
            await telegram_api.setup(connection)
            await telegram_api.record_heads_up(connection, request.block_id, message_id)
        logger.info("VALR Telegram heads-up sent for %s", request.block_id)
        return "sent"

    def all(self) -> list[Any]:
        return [self.calendar_sync, self.slot_plan, self.interview_part, self.slot_synthesize,
                self.slot_finish, self.slots_ready, self.block_record, self.dials_paused,
                self.briefing_auto_dial, self.telegram_heads_up]


def part_record(result: InterviewPartResult) -> dict[str, Any]:
    return {"ok": result.ok, "findings": json.loads(result.findings) if result.findings else None,
            "evidence": result.evidence, "seen_urls": result.seen_urls}


def merge_parts(stored: dict[str, Any], results: list[InterviewPartResult],
                removed: list[str]) -> dict[str, Any]:
    """Overlay this run's parts on the stored context; drop removed interviewers' notes.

    A part that failed this time keeps its earlier result. Interviewer findings are
    stored per research batch (key: the researched emails); a batch is dropped once
    none of its people is still on the invite.
    """
    parts = {k: v for k, v in stored.items()}
    batches = dict(parts.get("interviewers_by_email") or {})
    gone = {email.lower() for email in removed}
    for result in results:
        if not (result.ok or result.evidence):
            continue
        if result.part == "interviewers":
            if result.researched:
                batches[",".join(sorted(result.researched))] = part_record(result)
        elif result.ok or not (parts.get(result.part) or {}).get("ok"):
            parts[result.part] = part_record(result)
    if gone:
        batches = {key: value for key, value in batches.items()
                   if set(key.split(",")) - gone}
    parts["interviewers_by_email"] = batches
    parts["_removed"] = sorted(set(parts.get("_removed", [])) | gone)
    return parts


def interviewed(parts: dict[str, Any]) -> set[str]:
    gone = set(parts.get("_removed", []))
    return {email for key in (parts.get("interviewers_by_email") or {}) for email in key.split(",")} - gone


def history_available() -> bool:
    from app.private_sources import history_path
    return history_path().exists()


async def decrypt_pushes(connection: Any, event: SlotEvent) -> list[str]:
    key = os.getenv("VALR_TOKEN_KEY", "").strip()
    if not key:
        return []
    out = []
    for sealed in await interview_store.pushed_context(connection, event.key, event.company):
        try:
            out.append(google_oauth.decrypt(key, sealed))
        except google_oauth.ReconnectNeeded:
            continue
    return out


async def notetaker_keys(connection: Any) -> tuple[str | None, str | None]:
    """R1's Fathom and Granola keys: pasted on the dashboard (decrypted here), else the env."""
    from app import notetakers
    try:
        return (await notetakers.api_key(connection, "fathom"), await notetakers.api_key(connection, "granola"))
    except Exception as error:  # noqa: BLE001 - a store problem must not stop R1's other sources.
        logger.warning("VALR notetaker keys unavailable: %s", type(error).__name__)
        return (os.getenv("FATHOM_API_KEY", "").strip() or None, os.getenv("GRANOLA_API_KEY", "").strip() or None)


def owner_context() -> tuple[str, str]:
    """The profile brief with pay REDACTED (it goes to Claude) and the evaluation brief."""
    try:
        from app.profile.config import settings as profile_settings
        from app.profile.profile import load_profile, redacted_summary
        cfg = profile_settings()
        profile = redacted_summary(load_profile(cfg.profile_path))
    except Exception:  # noqa: BLE001
        profile = ""
    try:
        from app.profile.config import settings as profile_settings
        evaluation = json.loads(profile_settings().evaluation_path.read_text(encoding="utf-8"))
        brief = evaluation.get("brief_text") or ""
    except Exception:  # noqa: BLE001
        brief = ""
    return profile, brief


async def guard_report(events: list[SlotEvent]) -> dict[str, int]:
    """Counts of attendee roles (for the dry run): no names or emails."""
    counts: dict[str, int] = {}
    for event in events:
        for person in event.attendees:
            role = attendee_role(person, owner_emails())
            counts[role] = counts.get(role, 0) + 1
    return counts


__all__ = ["InterviewActivities", "Person", "asdict"]
