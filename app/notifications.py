"""Notifications: what changed in the job search, in the app's bell and as phone pushes.

Where they come from (all in the research worker):

- a KEPT job-search email (app/lead_worker.py, `process`): `from_email` decides the kind from
  the classifier's decision. Dropped mail, newsletters, job-board alerts, his own sent mail and
  automatic "application received" receipts never notify. Only incremental passes notify
  (never the first backfill), and only for mail under VALR_NOTIFY_MAX_AGE_HOURS (48) old;
- every kept inbound job email that is still UNREAD in Gmail (`sync_unread`, after each Gmail
  sync): older mail too, up to 30 days, so the bell shows all his unread job mail. These items
  are created silently (push "skipped"): only genuinely new mail pings the phone.
  The unread set is one messages.list ("is:unread newer_than:30d") per sync, plus the UNREAD
  label changes in the Gmail history. Reading the mail in Gmail reads its item; marking it
  unread again in Gmail brings the dot back. Reading an item in VALR does not touch Gmail
  (that needs the gmail.modify scope; VALR only has gmail.readonly);
- a processed call recording (`from_recording`);
- a calendar change (`from_calendar`, called by the calendar sync in app/interview_worker.py):
  an interview-like event added, moved (its time changed) or cancelled. The first sync (no
  stored snapshots yet) only seeds them, silently, like the email backfill.

Only concrete new events notify (the owner, 2026-10-17: "only new events, not what the LLM
thinks needs attention"). Nothing inferred from a lead rebuild (stage changes, "your move",
gone quiet, new lead) and no pinned action items: `after_rebuild` only links email
notifications to their lead. Rows of those REMOVED kinds stored before are marked read at
`setup` and left out of every list, count, push and digest.

Kinds and push routing (the in-app bell always lists every item):

    immediate (one push each, within ~1 minute): offer, interview (invite, reschedule,
        cancel), reply_needed (a recruiter asks him for something), take_home, recording,
        review (a coach review is ready), briefing (the 30-minute heads-up), account
        (Google needs reconnecting), calendar (when the interview is within 48 hours)
    digest (ONE push at 09:00, only when there is something): rejection, reply (no answer
        needed, or a first email from a recruiter), update, calendar (later)

Quiet hours 23:00-08:00 (the owner's time zone, app/owner.py): immediate pushes wait until
the end, except one tied to an interview in the next VALR_NOTIFY_URGENT_HOURS (12). Settings
(`notify_settings`) holds the three switches (immediate alerts, daily digest, quiet hours),
the digest time and the quiet hours; VALR_NOTIFY_IMMEDIATE / _DIGEST / _QUIET_HOURS (all 1),
VALR_NOTIFY_DIGEST_AT (09:00) and VALR_NOTIFY_QUIET (23:00-08:00) are only the defaults.

Max age: a new email pushes only under VALR_NOTIFY_MAX_AGE_HOURS (48) old; an unread one
stays in the bell up to 30 days (KEEP_FOR), dated by when it arrived.

Dedupe: one notification per Gmail thread within 10 minutes (a later, more important email in
the same thread upgrades it instead); every source has a unique `source_id`, so a re-run never
repeats one. Items older than 30 days drop off the list.

Phone push (app/webpush.py, the Home Screen app): each immediate item is pushed once, unread,
within two days, to every subscribed device, with the "Action needed" title and Urgency: high;
digest items wait for the one digest push. With no device subscribed nothing is pushed: the
items stay in the bell, and Settings says phone notifications are off. Rows from before
phone push existed have push_state NULL and are never pushed. (The tg_state column is left
from the Telegram days and no longer read.)

Push payloads never carry pay figures, phone numbers or email addresses (`scrub`), and
nothing here logs a title, a company or a body.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("valr")

DEDUPE = timedelta(minutes=10)
KEEP_FOR = timedelta(days=30)
CALENDAR_SOON = timedelta(hours=48)  # A calendar change this close is immediate.
LOCK = 7_202_610_31

IMMEDIATE = ("offer", "interview", "reply_needed", "take_home", "recording", "review", "briefing", "account")
DIGEST = ("rejection", "reply", "update")
KINDS = IMMEDIATE + DIGEST + ("calendar",)  # calendar: immediate within 48 hours, else digest.
# Inferred, not events: never created now; stored ones are hidden (`hide_removed`, `VISIBLE`).
REMOVED = ("stage", "waiting", "quiet", "new_lead")
VISIBLE = "NOT (kind = ANY(%s))"
LABELS = {"offer": "Offer", "interview": "Interview invite", "reply_needed": "Asked for something",
          "take_home": "Take-home", "rejection": "Not moving forward", "reply": "Reply", "update": "Update",
          "recording": "Call recording", "calendar": "Calendar", "review": "Coach review",
          "briefing": "Briefing", "account": "Account"}
RANK = {"immediate": 2, "digest": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id           bigserial PRIMARY KEY,
    source_id    text UNIQUE,                     -- gmail:<message>, calendar:<event>:<change>, ...
    kind         text NOT NULL,
    importance   text NOT NULL,                   -- immediate | digest
    title        text NOT NULL,
    body         text NOT NULL DEFAULT '',
    company      text NOT NULL DEFAULT '',
    link         text NOT NULL DEFAULT '',        -- the lead or interview page in VALR
    gmail_url    text NOT NULL DEFAULT '',        -- the Gmail thread, when it came from an email
    thread_id    text,
    lead_id      text,
    action       text NOT NULL DEFAULT '',        -- what the email asks, a few words
    interview_at timestamptz,                     -- an interview this is about, when known
    created_at   timestamptz NOT NULL DEFAULT now(),
    read_at      timestamptz,
    tg_state     text NOT NULL DEFAULT 'pending', -- pending | sent | digested | skipped | failed
    tg_sent_at   timestamptz
);
CREATE INDEX IF NOT EXISTS notifications_by_created ON notifications (created_at DESC);
CREATE INDEX IF NOT EXISTS notifications_by_thread ON notifications (thread_id, created_at);
CREATE TABLE IF NOT EXISTS notify_settings (
    id              int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    immediate       boolean,
    digest          boolean,
    quiet           boolean,
    last_digest_on  date,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- Phone push (app/webpush.py). Existing rows keep push_state NULL: old news is never pushed.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'notifications' AND column_name = 'push_state') THEN
    ALTER TABLE notifications ADD COLUMN push_state text;  -- pending | sent | skipped | failed
    ALTER TABLE notifications ALTER COLUMN push_state SET DEFAULT 'pending';
    ALTER TABLE notifications ADD COLUMN push_sent_at timestamptz;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'notify_settings' AND column_name = 'delivery') THEN
    ALTER TABLE notify_settings ADD COLUMN delivery text;  -- Unused since Telegram went.
  END IF;
END $$;
ALTER TABLE notify_settings ADD COLUMN IF NOT EXISTS digest_at text;     -- "09:00", local
ALTER TABLE notify_settings ADD COLUMN IF NOT EXISTS quiet_start text;   -- "23:00"
ALTER TABLE notify_settings ADD COLUMN IF NOT EXISTS quiet_end text;     -- "08:00"
"""
FIELDS = ("id", "source_id", "kind", "importance", "title", "body", "company", "link", "gmail_url", "thread_id",
          "lead_id", "action", "interview_at", "created_at", "read_at", "tg_state", "tg_sent_at",
          "push_state", "push_sent_at")


# Settings.

def flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip() == "1"


def zone() -> ZoneInfo:
    from app import owner
    return owner.zone()


def clock_of(text: str, default: time) -> time:
    try:
        hours, _, minutes = text.strip().partition(":")
        return time(int(hours), int(minutes or 0))
    except ValueError:
        return default


def quiet_window() -> tuple[time, time]:
    """VALR_NOTIFY_QUIET "23:00-08:00" -> (start, end), local time: the default."""
    start, _, end = os.getenv("VALR_NOTIFY_QUIET", "23:00-08:00").partition("-")
    return clock_of(start, time(23)), clock_of(end, time(8))


def digest_at() -> time:
    """VALR_NOTIFY_DIGEST_AT: the default digest time."""
    return clock_of(os.getenv("VALR_NOTIFY_DIGEST_AT", "09:00"), time(9))


HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def hhmm(at: time) -> str:
    return f"{at.hour:02d}:{at.minute:02d}"


def urgent_within() -> timedelta:
    return timedelta(hours=float(os.getenv("VALR_NOTIFY_URGENT_HOURS", "12")))


def max_age() -> timedelta:
    return timedelta(hours=float(os.getenv("VALR_NOTIFY_MAX_AGE_HOURS", "48")))


def host() -> str:
    return os.getenv("VALR_HOST", "valr.haciensus.com").strip().lower()


@dataclass
class Switches:
    immediate: bool = True
    digest: bool = True
    quiet: bool = True
    last_digest_on: date | None = None
    digest_time: time = time(9)
    quiet_start: time = time(23)
    quiet_end: time = time(8)

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> "Switches":
        row = row or {}
        pick = lambda key, env: row.get(key) if row.get(key) is not None else flag(env)  # noqa: E731
        start, end = quiet_window()
        at = lambda key, default: clock_of(row[key], default) if row.get(key) else default  # noqa: E731
        return cls(pick("immediate", "VALR_NOTIFY_IMMEDIATE"), pick("digest", "VALR_NOTIFY_DIGEST"),
                   pick("quiet", "VALR_NOTIFY_QUIET_HOURS"), row.get("last_digest_on"),
                   at("digest_at", digest_at()), at("quiet_start", start), at("quiet_end", end))

    def view(self) -> dict[str, Any]:
        return {"immediate": self.immediate, "digest": self.digest, "quiet": self.quiet,
                "digest_at": hhmm(self.digest_time), "quiet_start": hhmm(self.quiet_start),
                "quiet_end": hhmm(self.quiet_end)}


# What an email means (pure).

AUTO_RECEIPT = re.compile(r"\b(application (received|submitted)|thank(s| you) for (applying|your application|your interest)|"
                          r"we('ve| have) received your application|received your (application|resume|cv))", re.I)
RESCHEDULE = re.compile(r"\b(reschedul|cancel|postpon|moved? (the|your) (interview|call))", re.I)


def email_kind(category: str, action_requested: bool | None, stage_hint: str, new_thread: bool,
               subject: str = "", summary: str = "") -> str | None:
    """The kind of a kept inbound email, or None when it must not notify."""
    text = f"{subject} {summary}"
    if category in ("newsletter", "job_board_alert", "own_reply"):
        return None
    if category == "offer":
        return "offer"
    if category == "rejection":
        return "rejection"
    if category == "scheduling" or RESCHEDULE.search(text):
        return "interview"
    if category == "assessment":
        return "take_home"
    if action_requested:
        return "reply_needed"
    if category == "application_update":
        # "We received your application": the lead updates silently. Any other update from a
        # company is still worth a dot (the owner: "any emails should show red dot").
        return None if AUTO_RECEIPT.search(text) else "update"
    return "reply"  # A first email from a recruiter too: the email is the event, not a "new lead".


def importance(kind: str) -> str:
    return "immediate" if kind in IMMEDIATE else "digest"


def name_of(company: str) -> str:
    from app import company_names
    shown = company_names.display(company or "")
    return shown if shown and shown != company_names.NOT_IDENTIFIED else ""


def email_title(kind: str, company: str, action: str = "", new_thread: bool = False) -> str:
    who = name_of(company) or "A recruiter"
    if kind == "reply" and new_thread:
        return f"{who} emailed you"
    return {"offer": f"{who} sent an offer", "interview": f"{who} wants to schedule an interview",
            "reply_needed": f"{who} asked you to {plain_action(action)}" if action else f"{who} asked you for something",
            "take_home": f"{who} sent a take-home", "rejection": f"{who}: not moving forward",
            "reply": f"{who} replied", "update": f"{who} sent an update"}[kind]


LINKEDIN_URL = "https://www.linkedin.com/"


def linkedin_title(touch: Any) -> str:
    """"Priya (Acme) messaged you on LinkedIn" for a LinkedIn message touchpoint (the sender's
    first name and company only), else "". Same kinds and routing as any email."""
    if not str(getattr(touch, "url", "") or "").startswith(LINKEDIN_URL) \
            or not str(getattr(touch, "title", "") or "").startswith("LinkedIn message"):
        return ""
    people = getattr(touch, "people", None) or []
    first = next((p.name.split()[0] for p in people if getattr(p, "name", "").split()), "")
    company = name_of(getattr(touch, "company", "") or "")
    who = f"{first} ({company})" if first and company else first or (f"Someone at {company}" if company else "Someone")
    return f"{who} messaged you on LinkedIn"[:140]


def plain_action(action: str) -> str:
    text = " ".join((action or "").split()).rstrip(".")
    text = re.sub(r"^(please|kindly)\s+", "", text, flags=re.I)
    return (text[:1].lower() + text[1:])[:80] if text else "reply"


# Text (pure). No pay figures, phone numbers or email addresses.

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
MONEY = re.compile(r"(?:₹|\$|€|£|rs\.?|inr|usd)\s*[\d,.]+\s*(?:k|l|lpa|lakhs?|crores?|cr|m|mn|million)?|"
                   r"\b[\d,.]+\s*(?:k|lpa|lakhs?|crores?|cr|lacs?|million|per annum|ctc)\b", re.I)
PAY_WORDS = re.compile(r"\b(ctc|salary|compensation|package|stipend|base pay)\b[^.;,]*", re.I)
URL = re.compile(r"https?://\S+")


def scrub(text: str) -> str:
    text = URL.sub("", text or "")
    text = EMAIL.sub("", text)
    text = MONEY.sub("", text)
    text = PAY_WORDS.sub("pay details", text)
    text = PHONE.sub("", text)
    return " ".join(text.split()).strip(" ,;:-")


DIGEST_PHRASE = {"rejection": "said no", "reply": "replied", "recording": "call recording is ready", "offer": "sent an offer", "interview": "wants an interview",
                 "reply_needed": "asked you for something", "take_home": "sent a take-home", "update": "sent an update",
                 "review": "coach review is ready"}


def digest_line(note: dict[str, Any]) -> str:
    who = scrub(name_of(note.get("company") or "")) or "A recruiter"
    if note["kind"] == "calendar":
        return scrub(note.get("title") or "") or "A calendar change"
    return f"{who} {DIGEST_PHRASE.get(note['kind'], 'has an update')}"


def digest_message(notes: list[dict[str, Any]], unread: int | None = None, most: int = 4) -> dict[str, Any]:
    """The one digest push: "VALR · 5 updates" and "Acme replied · Kite said no · +3 more"."""
    from app import webpush
    lines = list(dict.fromkeys(digest_line(n) for n in notes))
    body = " · ".join(lines[:most]) + (f" · +{len(lines) - most} more" if len(lines) > most else "")
    count = len(notes)
    return webpush.payload(f"VALR · {count} update{'s' if count != 1 else ''}", body, "/valr", "valr-digest",
                           unread, False)


def absolute(link: str) -> str:
    return link if link.startswith("https://") else f"https://{host()}{link if link.startswith('/') else '/' + link}"


def lead_link(lead_id: str) -> str:
    return f"/valr/leads?lead={lead_id}" if lead_id else "/valr/leads"


# Timing (pure).

def local(now: datetime) -> datetime:
    return now.astimezone(zone())


def in_quiet_hours(now: datetime, switches: "Switches | None" = None) -> bool:
    start, end = (switches.quiet_start, switches.quiet_end) if switches else quiet_window()
    at = local(now).time()
    return (start <= at or at < end) if start > end else (start <= at < end)


def urgent(note: dict[str, Any], now: datetime) -> bool:
    """Tied to an interview in the next VALR_NOTIFY_URGENT_HOURS."""
    when = note.get("interview_at")
    return bool(when) and now - timedelta(minutes=30) <= when <= now + urgent_within()


def may_send_now(note: dict[str, Any], now: datetime, switches: Switches) -> bool:
    return not (switches.quiet and in_quiet_hours(now, switches)) or urgent(note, now)


def digest_due(now: datetime, switches: Switches) -> bool:
    today = local(now)
    return switches.digest and today.time() >= switches.digest_time and switches.last_digest_on != today.date()


# The store (Postgres). Tests use an in-memory stand-in with the same methods.

class Store:
    def __init__(self, connection: Any) -> None:
        self.c = connection

    async def setup(self) -> None:
        await self.c.execute(SCHEMA)
        await self.hide_removed()

    async def hide_removed(self) -> None:
        """Stored items of the REMOVED kinds: read, and never sent anywhere."""
        await self.c.execute(
            "UPDATE notifications SET read_at = coalesce(read_at, now()), "
            "push_state = CASE WHEN push_state = 'pending' THEN 'skipped' ELSE push_state END "
            "WHERE kind = ANY(%s) AND (read_at IS NULL OR push_state = 'pending')",
            (list(REMOVED),))

    async def recent_for_thread(self, thread_id: str, since: datetime) -> dict[str, Any] | None:
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE thread_id = %s AND created_at >= %s "
            "ORDER BY created_at DESC LIMIT 1", (thread_id, since))
        row = await cursor.fetchone()
        return dict(zip(FIELDS, row)) if row else None

    async def recent_for_lead(self, lead_id: str, since: datetime) -> list[dict[str, Any]]:
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE lead_id = %s AND created_at >= %s",
            (lead_id, since))
        return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]

    async def insert(self, note: dict[str, Any]) -> int | None:
        """None when the source_id is already there."""
        keys = [k for k in note if k in FIELDS and k != "id"]
        cursor = await self.c.execute(
            f"INSERT INTO notifications ({', '.join(keys)}) VALUES ({', '.join(['%s'] * len(keys))}) "
            "ON CONFLICT (source_id) DO NOTHING RETURNING id", tuple(note[k] for k in keys))
        row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def update(self, note_id: int, **values: Any) -> None:
        sets = ", ".join(f"{k} = %s" for k in values)
        await self.c.execute(f"UPDATE notifications SET {sets} WHERE id = %s", (*values.values(), note_id))

    async def unlinked(self) -> list[dict[str, Any]]:
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE lead_id IS NULL AND thread_id IS NOT NULL "
            "AND created_at >= %s", (datetime.now(timezone.utc) - KEEP_FOR,))
        return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]

    async def listing(self, unread_only: bool, since: datetime, limit: int = 100) -> list[dict[str, Any]]:
        where = f"created_at >= %s AND {VISIBLE}" + (" AND read_at IS NULL" if unread_only else "")
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE {where} ORDER BY created_at DESC LIMIT %s",
            (since, list(REMOVED), limit))
        return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]

    async def unread_count(self, since: datetime) -> int:
        cursor = await self.c.execute(
            f"SELECT count(*) FROM notifications WHERE read_at IS NULL AND created_at >= %s AND {VISIBLE}",
            (since, list(REMOVED)))
        return int((await cursor.fetchone())[0])

    async def mark_read(self, ids: list[int] | None, now: datetime) -> int:
        if ids is None:
            cursor = await self.c.execute("UPDATE notifications SET read_at = %s WHERE read_at IS NULL", (now,))
        else:
            cursor = await self.c.execute(
                "UPDATE notifications SET read_at = %s WHERE read_at IS NULL AND id = ANY(%s)", (now, ids))
        return int(getattr(cursor, "rowcount", 0) or 0)

    async def unread_by_lead(self, since: datetime) -> dict[str, int]:
        """Unread notifications that came from an email, per lead (the red dots on Leads)."""
        cursor = await self.c.execute(
            "SELECT lead_id, count(*) FROM notifications WHERE read_at IS NULL AND created_at >= %s "
            "AND lead_id IS NOT NULL AND source_id LIKE 'gmail:%%' GROUP BY lead_id", (since,))
        return {str(r[0]): int(r[1]) for r in await cursor.fetchall()}

    async def mark_lead_read(self, lead_id: str, now: datetime) -> int:
        """Opening a lead's card reads everything about it (its emails included)."""
        cursor = await self.c.execute(
            "UPDATE notifications SET read_at = %s WHERE read_at IS NULL AND lead_id = %s", (now, lead_id))
        return int(getattr(cursor, "rowcount", 0) or 0)

    async def email_items(self, since: datetime) -> dict[str, dict[str, Any]]:
        """{gmail message id: {id, read_at}} for the items made from an email since `since`."""
        cursor = await self.c.execute(
            "SELECT source_id, id, read_at FROM notifications WHERE source_id LIKE 'gmail:%%' AND created_at >= %s",
            (since,))
        return {str(r[0]).removeprefix("gmail:"): {"id": int(r[1]), "read_at": r[2]} for r in await cursor.fetchall()}

    async def mark_unread(self, ids: list[int]) -> int:
        cursor = await self.c.execute(
            "UPDATE notifications SET read_at = NULL WHERE read_at IS NOT NULL AND id = ANY(%s)", (ids,))
        return int(getattr(cursor, "rowcount", 0) or 0)

    async def purge(self, before: datetime) -> None:
        await self.c.execute("DELETE FROM notifications WHERE created_at < %s", (before,))

    async def push_pending(self) -> list[dict[str, Any]]:
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE push_state = 'pending' AND {VISIBLE} "
            "ORDER BY created_at", (list(REMOVED),))
        return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]

    async def switches(self) -> Switches:
        keys = ("immediate", "digest", "quiet", "last_digest_on", "digest_at", "quiet_start", "quiet_end")
        cursor = await self.c.execute(f"SELECT {', '.join(keys)} FROM notify_settings WHERE id = 1")
        row = await cursor.fetchone()
        return Switches.from_row(dict(zip(keys, row)) if row else None)

    async def save_switches(self, **values: Any) -> None:
        keys = list(values)
        await self.c.execute(
            f"INSERT INTO notify_settings (id, {', '.join(keys)}) VALUES (1, {', '.join(['%s'] * len(keys))}) "
            f"ON CONFLICT (id) DO UPDATE SET {', '.join(f'{k} = excluded.{k}' for k in keys)}, updated_at = now()",
            tuple(values.values()))

    async def try_lock(self) -> bool:
        cursor = await self.c.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,))
        return bool((await cursor.fetchone())[0])

    async def unlock(self) -> None:
        await self.c.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))


# Creating notifications.

async def add(store: Any, note: dict[str, Any], now: datetime) -> int | None:
    """Insert one, with the 10-minute per-thread dedupe: a later email in the same thread only
    upgrades the earlier item when it is more important (an offer after a plain reply)."""
    note = {"importance": importance(note["kind"]), "created_at": now, "push_state": "pending", **note}
    thread = note.get("thread_id")
    if thread:
        earlier = await store.recent_for_thread(thread, now - DEDUPE)
        if earlier is not None:
            if RANK[note["importance"]] > RANK[earlier["importance"]]:
                fields = {k: note[k] for k in ("kind", "importance", "title", "body", "action") if k in note}
                if earlier.get("push_state") is not None:
                    fields["push_state"] = "pending"  # Pushed again with the same tag: the phone replaces it.
                await store.update(earlier["id"], read_at=None, **fields)
                return earlier["id"]
            if earlier.get("read_at") is not None:
                await store.update(earlier["id"], read_at=None)  # A new email: the dot comes back.
            return None
    return await store.insert(note)


async def from_recording(store: Any, provider: str, external_id: str, company: str, other: str,
                         now: datetime) -> int | None:
    """A processed call recording -> one notification linking to its past-interview page."""
    who = name_of(company) or (other or "").strip()[:60]
    note = {"source_id": f"{provider}:{external_id}", "kind": "recording",
            "title": f"Call with {who} is ready" if who else "Your call recording is ready",
            "body": "Transcript and coach review are in Past interviews.", "company": (company or "")[:120],
            "link": f"/valr/interviews/past/{provider}-{external_id}", "gmail_url": "", "thread_id": None,
            "action": ""}
    return await add(store, note, now)


async def from_email(store: Any, email: Any, decision: Any, touch: Any, new_thread: bool, now: datetime) -> int | None:
    """A kept email -> a notification (or nothing). Inbound mail only."""
    if not decision.keep or touch.direction != "in" or now - email.at > max_age():
        return None
    kind = email_kind(decision.category, decision.action_requested, decision.stage_hint, new_thread,
                      email.subject, decision.summary)
    if kind is None:
        return None
    note = {"source_id": f"gmail:{email.id}", "kind": kind,
            "title": linkedin_title(touch) or email_title(kind, touch.company, decision.action, new_thread),
            "body": (decision.summary or "")[:240], "company": (touch.company or "")[:120],
            "link": lead_link(""), "gmail_url": touch.url or "", "thread_id": getattr(touch, "thread", "") or email.thread_id,
            "action": (decision.action or "")[:120]}
    return await add(store, note, now)


# Unread mail (app/lead_worker.py, after each Gmail sync).

QUIET_KINDS = ("newsletter", "job_board_alert", "own_reply")  # Kept for context, never bell items.


def unread_kind(touch: Any, new_thread: bool) -> str | None:
    """The kind of a kept inbound email shown because it is unread: its usual kind, else
    "update" (an automatic "application received" is not news, but it is unread job mail)."""
    if (getattr(touch, "category", "") or "") in QUIET_KINDS:
        return None
    return email_kind(touch.category or "", touch.action_requested, touch.stage_hint or "", new_thread,
                      touch.title or "", touch.summary or "") or "update"


def silent_note(touch: Any, new_thread: bool) -> dict[str, Any] | None:
    """A bell item for a kept inbound email touchpoint, dated when the mail arrived, that is
    never pushed."""
    if touch.kind != "email" or touch.direction != "in" or not touch.id.startswith("gmail:"):
        return None
    kind = unread_kind(touch, new_thread)
    if kind is None:
        return None
    lead_id = getattr(touch, "lead_id", "") or ""
    return {"source_id": touch.id, "kind": kind, "importance": importance(kind),
            "title": linkedin_title(touch) or email_title(kind, touch.company, touch.action or "", new_thread),
            "body": (touch.summary or "")[:240], "company": (touch.company or "")[:120],
            "link": lead_link(lead_id), "gmail_url": touch.url or "", "thread_id": touch.thread or None,
            "lead_id": lead_id or None, "action": (touch.action or "")[:120], "created_at": touch.at,
            "push_state": "skipped"}


async def sync_unread(store: Any, kept: list[Any], unread: set[str] | None, complete: bool,
                      changes: dict[str, bool], now: datetime, first_in_thread: set[str] = frozenset()) -> dict[str, int]:
    """Makes the bell match Gmail's unread job mail.

    `kept`: the kept email touchpoints of the last 30 days; `unread`: Gmail's unread message
    ids of the last 30 days (None when the list could not be fetched), `complete` when that
    list was not cut short; `changes`: {message id: True (marked unread) | False (read)} from
    the Gmail history; `first_in_thread`: touch ids that started their thread.

    - an unread kept inbound email without an item gets one, silently;
    - an unread item whose mail is read in Gmail is read (only when the list is complete, or
      the history says so: a cut list never reads anything by mistake);
    - an item whose mail was marked unread again in Gmail (history) is unread again.
    Reading an item in VALR is kept: a mail that stays unread in Gmail does not undo it."""
    out = {"added": 0, "read": 0, "unread": 0}
    since = now - KEEP_FOR
    items = await store.email_items(since)
    # The list is fetched after the history, so it wins; without it, the history alone.
    unread_now = set(unread) if unread is not None else {i for i, v in changes.items() if v}
    for touch in kept:
        message_id = touch.id.removeprefix("gmail:")
        if message_id not in unread_now or message_id in items or touch.at < since:
            continue
        note = silent_note(touch, touch.id in first_in_thread)
        if note is not None and await store.insert(note) is not None:
            out["added"] += 1
    if unread is not None and complete:
        read = {i for i in items if i not in unread}
    else:
        read = {i for i, v in changes.items() if not v and i not in unread_now}
    gone = [items[i]["id"] for i in read if i in items and items[i]["read_at"] is None]
    if gone:
        out["read"] = await store.mark_read(gone, now)
    back = [items[i]["id"] for i, v in changes.items()
            if v and i in unread_now and i in items and items[i]["read_at"] is not None]
    if back:
        out["unread"] = await store.mark_unread(back)
    return out


async def after_rebuild(store: Any, leads: list[Any], now: datetime) -> int:
    """After `leads_refresh`: link email notifications to their lead (the red dots on Leads).
    Creates nothing: stage changes, "your move" and gone-quiet are not notifications. -> 0."""
    by_thread = {t.thread: lead for lead in leads for t in lead.touches if getattr(t, "thread", "")}
    for note in await store.unlinked():
        lead = by_thread.get(note["thread_id"])
        if lead is None:
            continue
        soon = lead.next_step_at if lead.next_step_at and lead.next_step_at > now else None
        await store.update(note["id"], lead_id=lead.id, link=lead_link(lead.id),
                           interview_at=note.get("interview_at") or soon)
    return 0


# Calendar changes (app/interview_worker.py, after each calendar sync).

CALENDAR_CHANGES = ("added", "moved", "cancelled")


def when_text(start_utc: str) -> str:
    """"Mon 19 Oct, 6:00 PM" in the owner's time zone."""
    at = local(datetime.fromisoformat(start_utc))
    return f"{at:%a} {at.day} {at:%b}, {at.strftime('%I:%M %p').lstrip('0')}"


def calendar_title(change: str, company: str, start_utc: str, before_utc: str = "") -> str:
    who = f"Interview with {name_of(company)}" if name_of(company) else "Interview"
    if change == "added":
        return f"{who} added: {when_text(start_utc)}"
    if change == "moved":
        return f"{who} moved to {when_text(start_utc)}"
    return f"{who} cancelled: {when_text(before_utc or start_utc)}"


async def from_calendar(store: Any, event: Any, change: str, now: datetime, before_utc: str = "") -> int | None:
    """One calendar change (`change`: added, moved or cancelled) of an interview-like event ->
    one notification. Deduped per event, change and (for a move) the new time. The caller
    decides what is interview-like (app/interview_types.looks_like_interview) and skips the
    first, seeding sync."""
    from app.interview_types import slot_workflow_id
    if change not in CALENDAR_CHANGES:
        return None
    start = datetime.fromisoformat(event.start_utc if change != "cancelled" else (before_utc or event.start_utc))
    if start < now:
        return None  # Already over (or in progress): not news.
    source = f"calendar:{event.key}:{change}" + (f":{event.start_utc}" if change == "moved" else "")
    note = {"source_id": source, "kind": "calendar",
            "importance": "immediate" if start - now <= CALENDAR_SOON else "digest",
            "title": calendar_title(change, event.company, event.start_utc, before_utc),
            "body": f"Was {when_text(before_utc)}." if change == "moved" and before_utc else "",
            "company": (event.company or "")[:120],
            "link": "/valr/interviews" if change == "cancelled" else f"/valr/interviews/{slot_workflow_id(event.key)}",
            "gmail_url": "", "thread_id": None, "action": change, "interview_at": start}
    return await add(store, note, now)


# Phone push (pure text, then the pass).

PUSH_PHRASE = {**DIGEST_PHRASE, "offer": "sent you an offer", "interview": "wants to schedule (or move) an interview",
               "take_home": "sent a take-home assignment", "reply_needed": "asked you for something"}


OWN_WORDS = ("briefing", "review", "account")


def push_message(note: dict[str, Any], unread: int | None = None) -> dict[str, Any]:
    """{title, body, url, tag, badge, urgent} for static/sw.js. Immediate kinds: "Action needed"."""
    from app import webpush
    who = scrub(name_of(note.get("company") or "")) or "A recruiter"
    kind = note["kind"]
    urgent = note.get("importance") == "immediate"
    if kind == "reply_needed" and plain_action(note.get("action") or "") != "reply":
        body = f"{who} asked you to {plain_action(note.get('action') or '')}."
    elif kind == "calendar":
        body = (scrub(note.get("title") or "") or "A calendar change") + "."
    else:
        body = f"{who} {PUSH_PHRASE.get(kind, 'has an update')}."
    if kind in OWN_WORDS:  # VALR's own messages (heads-up, review ready, reconnect): as written.
        title, body = note.get("title") or LABELS[kind], note.get("body") or ""
    elif kind == "calendar":
        title = "Calendar" + (f" · {scrub(name_of(note.get('company') or ''))}" if name_of(note.get("company") or "") else "")
    else:
        title = f"Action needed · {who}" if urgent else f"{LABELS.get(kind, 'Update')} · {who}"
    link = note.get("link") or "/valr"
    return webpush.payload(title, body, link if link.startswith("/") else "/valr",
                           webpush.tag_for(note.get("thread_id"), note.get("lead_id"), note.get("source_id")),
                           unread, urgent)


async def dispatch(store: Any, now: datetime, sender: Any = None) -> dict[str, int]:
    """One pass over the pending items: immediate ones are pushed now (held in quiet hours
    unless urgent), digest ones wait for the ONE digest push at the digest time. Skipped (left
    in the bell only) when nobody is subscribed, it was read already or it is over two days old."""
    switches = await store.switches()
    out = {"pushed": 0, "held": 0, "digest": 0}
    pending = await store.push_pending()
    devices = await sender.subscribers() if sender is not None else 0
    unread: int | None = None
    digest = []
    for note in pending:
        if not devices or note.get("read_at") or note["created_at"] < now - timedelta(days=2):
            await store.update(note["id"], push_state="skipped")
            continue
        if note["importance"] == "digest" or not switches.immediate:
            digest.append(note)
            continue
        if not may_send_now(note, now, switches):
            out["held"] += 1
            continue
        if unread is None:
            unread = await store.unread_count(now - KEEP_FOR)
        result = await sender.send(push_message(note, unread), now)
        await store.update(note["id"], push_state="sent" if result.delivered else "failed", push_sent_at=now)
        out["pushed"] += bool(result.delivered)
    if digest_due(now, switches):
        if digest:
            if unread is None:
                unread = await store.unread_count(now - KEEP_FOR)
            result = await sender.send(digest_message(digest, unread), now)
            for note in digest:
                await store.update(note["id"], push_state="digested" if result.delivered else "failed",
                                   push_sent_at=now)
            out["digest"] = len(digest) if result.delivered else 0
        await store.save_switches(last_digest_on=local(now).date())
    elif not switches.digest:
        for note in digest:
            await store.update(note["id"], push_state="skipped")
    return out


async def run_dispatch(connection: Any, now: datetime | None = None) -> dict[str, int]:
    """One dispatch pass on a real connection (one pass at a time: an advisory lock)."""
    from app import webpush
    now = now or datetime.now(timezone.utc)
    store = Store(connection)
    await store.setup()
    if not await store.try_lock():
        return {"pushed": 0, "held": 0, "digest": 0}
    try:
        result = await dispatch(store, now, await webpush.sender(connection))
        await store.purge(now - KEEP_FOR)
    finally:
        await store.unlock()
    if result["pushed"] or result["digest"]:
        logger.info("VALR notifications: %d pushed, %d in the digest, %d held for quiet hours",
                    result["pushed"], result["digest"], result["held"])
    return result


async def notify_now(connection: Any, note: dict[str, Any], now: datetime | None = None) -> str:
    """Add one item to the bell and push it at once (the heads-up, a ready review): 'sent',
    'no_device' (it stays in the bell), 'failed' or 'duplicate' (its source_id is known)."""
    from app import webpush
    now = now or datetime.now(timezone.utc)
    store = Store(connection)
    await store.setup()
    note_id = await add(store, note, now)
    if note_id is None:
        return "duplicate"
    sender = await webpush.sender(connection)
    if sender is None or not await sender.subscribers():
        await store.update(note_id, push_state="skipped")
        return "no_device"
    unread = await store.unread_count(now - KEEP_FOR)
    result = await sender.send(push_message({**note, "importance": importance(note["kind"])}, unread), now)
    await store.update(note_id, push_state="sent" if result.delivered else "failed", push_sent_at=now)
    return "sent" if result.delivered else "failed"


async def loop(connect: Any, stop: Any = None) -> None:
    """The research worker's background task: a dispatch every VALR_NOTIFY_LOOP_S (60) seconds,
    so held items leave when quiet hours end and the digest goes at its time."""
    import asyncio
    stop = stop or asyncio.Event()
    every = float(os.getenv("VALR_NOTIFY_LOOP_S", "60"))
    while not stop.is_set():
        try:
            async with await connect() as connection:
                await run_dispatch(connection)
        except Exception as error:  # noqa: BLE001 - keep the worker alive
            logger.warning("VALR notifications loop: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass


# The API's view.

def view(note: dict[str, Any], now: datetime) -> dict[str, Any]:
    created = note["created_at"]
    return {"id": note["id"], "kind": note["kind"], "label": LABELS.get(note["kind"], "Update"),
            "importance": note["importance"], "title": note["title"], "body": note["body"],
            "company": name_of(note.get("company") or ""), "link": note.get("link") or "/valr/leads",
            "gmail_url": note.get("gmail_url") if str(note.get("gmail_url") or "").startswith("https://") else "",
            "created_at": created.astimezone(timezone.utc).isoformat(),
            "day": day_label(created, now), "read": note.get("read_at") is not None,
            "lead_id": note.get("lead_id") or "", "email": str(note.get("source_id") or "").startswith("gmail:")}


def day_label(moment: datetime, now: datetime) -> str:
    days = (local(now).date() - local(moment).date()).days
    return "Today" if days <= 0 else "Yesterday" if days == 1 else "Earlier"
