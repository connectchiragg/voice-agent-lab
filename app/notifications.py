"""Notifications: what changed in the job search, in the app's bell and as phone pushes.

Where they come from (all in the research worker):

- a KEPT job-search email (app/lead_worker.py, `process`): `from_email` decides the kind from
  the classifier's decision. Dropped mail, newsletters, job-board alerts, his own sent mail and
  automatic "application received" receipts never notify. Incremental passes notify, and a
  backfill only for mail that arrived after it started (old mail is not news), and only for
  mail under VALR_NOTIFY_MAX_AGE_HOURS (48) old;
- every kept inbound job email that is still UNREAD in Gmail (`sync_unread`, after each Gmail
  sync): older mail too, up to 30 days, so the bell shows all his unread job mail. These items
  are created silently (push "skipped"): only genuinely new mail pings the phone.
  A safety net: an unread, recent (48 h) kept email of a notifying kind that arrived after the
  backfill began and still has no item (nothing in its thread either) was missed, so it is
  added as a normal, pushed item (`missed`, "caught_up").
  The unread set is one messages.list ("is:unread newer_than:30d") per sync, plus the UNREAD
  label changes in the Gmail history. Reading the mail in Gmail reads its item; marking it
  unread again in Gmail brings the dot back. Reading an item in VALR does not touch Gmail
  (that needs the gmail.modify scope; VALR only has gmail.readonly);
- a processed call recording (`from_recording`);
- a calendar change (`from_calendar`, called by the calendar sync in app/interview_worker.py):
  an interview-like event added, moved (its time changed) or cancelled. The first sync (no
  stored snapshots yet) only seeds them, silently, like the email backfill.

Not stateless (the owner, 2026-10-19: "a reminder of an event is not a new event"): before a
kept email becomes an item, app/notify_context.py judges it against what VALR already knows
about the company and thread (touchpoints, earlier bell items, calendar events): reminders,
duplicates and confirmations of something already known make no item at all (its touchpoint
carries `novelty`, so the unread pass skips it too). A calendar interview added (or moved to a
time) that an email in the bell already announced for that company is not notified again
(`from_calendar`); an email's interview item carries the time it names (`interview_at`).

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

Latency: Gmail's Pub/Sub push starts a sync within seconds (app/leads_web.py); the worker's
fast check (app/lead_worker.GmailCheck, every 30 s) catches a push that never came; the item is
stored as soon as its batch is classified, and pushed right then (`dispatch_now`), not at the
dispatch loop's next tick. Calendar changes and processed recordings push the same way.
A phone call recording is in the bell and pushed when its upload finishes (`from_call_upload`,
called by app/phone_recordings.announce); processing makes the same item "ready" silently
(`from_recording`), and the coach review's push takes the call's push tag (`push_tag`), so the
phone replaces the first notification: two pushes per call at most.

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

from app import schema

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
INCOMING = "incoming"  # A new email shown before it is classified (`incoming`), then settled.
LABELS = {INCOMING: "New email", "offer": "Offer", "interview": "Interview invite", "reply_needed": "Asked for something",
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
                 WHERE table_name = 'notifications' AND column_name = 'arrived_at') THEN
    ALTER TABLE notifications ADD COLUMN arrived_at timestamptz;  -- Gmail's internalDate: the latency SLA
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'notifications' AND column_name = 'ready_at') THEN
    ALTER TABLE notifications ADD COLUMN ready_at timestamptz;  -- A call recording's item: processed
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
          "push_state", "push_sent_at", "arrived_at", "ready_at")


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


PEOPLE = ("recruiter", "hiring_manager", "interviewer", "coordinator")
WARM = 0.3  # The classifier's sentiment (-1..1) from which a reply counts as positive.


def warm_reply(kind: str, sender_role: str, sentiment: float | None, cue: str, bulk: bool) -> bool:
    """A reply or update that is pushed at once, not in the digest (the owner, 2026-10-17:
    "non promotional positive replies from actual third party/recruiters"): from a person at
    the company (not an alert or a mailing list) and positive."""
    return (kind in ("reply", "update") and not bulk and sender_role in PEOPLE
            and (cue == "positive" or (sentiment is not None and sentiment >= WARM)))


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
        """The table, once per process (app/schema.py); stored items of removed kinds, every time
        (a plain UPDATE, no DDL lock)."""
        await schema.once(self.c, "notifications", lambda: self.c.execute(SCHEMA))
        await self.hide_removed()

    async def hide_removed(self) -> None:
        """Stored items of the REMOVED kinds: read, and never sent anywhere."""
        await self.c.execute(
            "UPDATE notifications SET read_at = coalesce(read_at, now()), "
            "push_state = CASE WHEN push_state = 'pending' THEN 'skipped' ELSE push_state END "
            "WHERE kind = ANY(%s) AND (read_at IS NULL OR push_state = 'pending')",
            (list(REMOVED),))

    async def recent_for_thread(self, thread_id: str, since: datetime) -> dict[str, Any] | None:
        """The newest settled item of the thread (an unclassified `incoming` one does not count)."""
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE thread_id = %s AND created_at >= %s "
            "AND kind <> %s ORDER BY created_at DESC LIMIT 1", (thread_id, since, INCOMING))
        row = await cursor.fetchone()
        return dict(zip(FIELDS, row)) if row else None

    async def recent(self, since: datetime) -> list[dict[str, Any]]:
        """Settled items since `since` (app/notify_context.py)."""
        cursor = await self.c.execute(
            f"SELECT {', '.join(FIELDS)} FROM notifications WHERE created_at >= %s AND kind <> %s "
            "ORDER BY created_at DESC LIMIT 500", (since, INCOMING))
        return [dict(zip(FIELDS, row)) for row in await cursor.fetchall()]

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
            "ON CONFLICT (owner_id, source_id) DO NOTHING RETURNING id", tuple(note[k] for k in keys))
        row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def get_source(self, source_id: str) -> dict[str, Any] | None:
        cursor = await self.c.execute(f"SELECT {', '.join(FIELDS)} FROM notifications WHERE source_id = %s",
                                      (source_id,))
        row = await cursor.fetchone()
        return dict(zip(FIELDS, row)) if row else None

    async def delete(self, note_id: int) -> None:
        await self.c.execute("DELETE FROM notifications WHERE id = %s", (note_id,))

    async def latencies(self, since: datetime, prefix: str = "gmail:") -> list[tuple[float, float | None, float | None]]:
        """(seconds from arrival to the bell item, to its push or None, to "ready" or None) for the
        live items whose source_id starts with `prefix` created since `since`: emails ("gmail:",
        arrival = Gmail's internalDate; silent unread-mail items carry no arrived_at) or phone
        calls ("phone:", arrival = the upload finishing; ready = transcribed and in Past)."""
        cursor = await self.c.execute(
            "SELECT extract(epoch FROM created_at - arrived_at), extract(epoch FROM push_sent_at - arrived_at), "
            "extract(epoch FROM ready_at - arrived_at) FROM notifications WHERE arrived_at IS NOT NULL "
            "AND created_at >= %s AND source_id LIKE %s", (since, prefix + "%"))
        return [(float(r[0]), float(r[1]) if r[1] is not None else None, float(r[2]) if r[2] is not None else None)
                for r in await cursor.fetchall()]

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
            f"ON CONFLICT (owner_id, id) DO UPDATE SET {', '.join(f'{k} = excluded.{k}' for k in keys)}, updated_at = now()",
            tuple(values.values()))

    async def try_lock(self) -> bool:
        """One dispatch at a time per workspace (app/workspaces.lock_key: the owner's key as before)."""
        from app import workspaces
        cursor = await self.c.execute("SELECT pg_try_advisory_lock(%s)", (workspaces.lock_key(LOCK),))
        return bool((await cursor.fetchone())[0])

    async def unlock(self) -> None:
        from app import workspaces
        await self.c.execute("SELECT pg_advisory_unlock(%s)", (workspaces.lock_key(LOCK),))


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


def call_party(other: str) -> str:
    """The other party's name for a title or a push: never a phone number (scrub)."""
    return scrub(" ".join((other or "").split()))[:60]


def past_link(provider: str, external_id: str) -> str:
    return f"/valr/interviews/past/{provider}-{external_id}"


def SILENT(now: datetime) -> dict[str, Any]:  # noqa: N802 - a constant-like helper
    """A call's own items: in the bell as history, read and never pushed; the call's one
    notification is its coach review (app/post_call.deliver)."""
    return {"push_state": "skipped", "read_at": now}


async def from_call_upload(store: Any, upload_id: str, other: str, now: datetime) -> int | None:
    """A phone call recording just uploaded -> its bell item at once ("Call with Riya recorded"),
    silent: read, never pushed (the owner, 2026-10-17: one notification per call, the coach
    review once the pipeline is done, and only for an interview). The same item becomes "... is
    ready" when it is processed (`from_recording`). The upload finishing is its arrival (the
    latency figures)."""
    from app.phone_recordings import PROVIDER
    who = call_party(other)
    note = {"source_id": f"{PROVIDER}:{upload_id}", "kind": "recording", "importance": "immediate",
            "title": f"Call with {who} recorded" if who else "New call recording",
            "body": "Transcribing; the coach review follows.", "company": "",
            "link": past_link(PROVIDER, upload_id), "gmail_url": "", "thread_id": None, "action": "uploaded",
            "arrived_at": now, **SILENT(now)}
    return await add(store, note, now)


async def from_recording(store: Any, provider: str, external_id: str, company: str, other: str,
                         now: datetime) -> int | None:
    """A processed call recording -> its bell item linking to its past-interview page, silent (read,
    not pushed): a phone call's item from the upload (`from_call_upload`) is updated in place.
    The one notification a call gets is "Your coach review is ready" (app/post_call.deliver),
    only for an interview. -> the id of a NEW item, else None."""
    who = name_of(company) or call_party(other)
    note = {"source_id": f"{provider}:{external_id}", "kind": "recording",
            "title": f"Call with {who} is ready" if who else "Your call recording is ready",
            "body": "Transcript and coach review are in Past interviews.", "company": (company or "")[:120],
            "link": past_link(provider, external_id), "gmail_url": "", "thread_id": None,
            "action": "ready"}
    found = await store.get_source(note["source_id"])
    if found is None:
        return await add(store, {**note, "ready_at": now, **SILENT(now)}, now)
    if found.get("action") == "ready":
        return None  # A re-run.
    note["body"] = "Transcript is in Past interviews; the coach review follows."
    await store.update(found["id"], ready_at=now, **{k: note[k] for k in ("title", "body", "company", "link", "action")})
    if found.get("arrived_at"):
        logger.info("VALR notification latency: call ready %.1f s after its upload",
                    (now - found["arrived_at"]).total_seconds())
    return None


async def call_failed(store: Any, provider: str, external_id: str, other: str, now: datetime) -> bool:
    """A call recording that could not be processed (after its last try): its item says so. Not
    pushed again. False when there is no item."""
    found = await store.get_source(f"{provider}:{external_id}")
    if found is None or found.get("action") == "ready":
        return False
    who = call_party(other)
    await store.update(found["id"], title=f"Call with {who}: transcription failed" if who
                       else "A call recording could not be transcribed",
                       body="Send it again from the Mac app or Settings; nothing else is needed.",
                       action="failed", link="/valr/interviews/past")
    return True


def stale(novelty: str | None) -> bool:
    """Not news: a reminder, duplicate or confirmation of something already known."""
    from app.notify_context import SUPPRESSED
    return (novelty or "") in SUPPRESSED


async def from_email(store: Any, email: Any, decision: Any, touch: Any, new_thread: bool, now: datetime) -> int | None:
    """A kept email -> a notification (or nothing). Inbound mail only. Its `incoming` item, when
    there is one, is replaced (keeping when it appeared) or, when it does not notify, retracted.
    Not news (`touch.novelty`: a reminder, duplicate or confirmation, app/notify_context.py):
    nothing."""
    incoming = await store.get_source(f"gmail:{email.id}")
    if incoming is not None and incoming["kind"] != INCOMING:
        return None  # Settled already (a re-run).
    kind = None
    if decision.keep and touch.direction == "in" and now - email.at <= max_age() \
            and not stale(getattr(touch, "novelty", "")):
        kind = email_kind(decision.category, decision.action_requested, decision.stage_hint, new_thread,
                          email.subject, decision.summary)
    if incoming is not None:
        await store.delete(incoming["id"])
    if kind is None:
        return None
    note = {"source_id": f"gmail:{email.id}", "kind": kind,
            "title": linkedin_title(touch) or email_title(kind, touch.company, decision.action, new_thread),
            "body": (decision.summary or "")[:240], "company": (touch.company or "")[:120],
            "link": lead_link(""), "gmail_url": touch.url or "", "thread_id": getattr(touch, "thread", "") or email.thread_id,
            "action": (decision.action or "")[:120], "arrived_at": email.at}
    if kind == "interview":
        from app.notify_context import parse_when
        when = parse_when(getattr(decision, "event_at", "") or "")
        if when is not None and when > now - timedelta(hours=1):
            note["interview_at"] = when  # The calendar invite for it is then not news (`from_calendar`).
    from app import lead_classify  # A LinkedIn message is a person's, though LinkedIn relays it as bulk mail.
    bulk = lead_classify.is_bulk(email) and lead_classify.linkedin_message(email) is None
    if warm_reply(kind, decision.sender_role, decision.sentiment, decision.cue, bulk):
        note["importance"] = "immediate"
    if incoming is not None:
        note["created_at"] = incoming["created_at"]  # In the bell since then: the SLA counts from it.
        note["read_at"] = incoming.get("read_at")  # Read in Gmail (or the bell) already: no push.
    return await add(store, note, now)


async def from_incoming(store: Any, email: Any, now: datetime) -> int | None:
    """A new inbound email that the model is about to classify -> an `incoming` bell item at
    once, from its headers ("Priya Raman: Interview availability"), so it is in the app within
    seconds of arriving. Never pushed (push_state "hold"): `from_email` then
    turns it into the real item (pushed) or retracts it (not job mail). Only new mail (under
    VALR_NOTIFY_MAX_AGE_HOURS); once per message."""
    if now - email.at > max_age():
        return None
    who = " ".join((email.from_name or email.from_email.split("@")[-1] or "Someone").split())[:60]
    subject = " ".join((email.subject or "(no subject)").split())[:120]
    note = {"source_id": f"gmail:{email.id}", "kind": INCOMING, "importance": "digest",
            "title": f"{who}: {subject}"[:140], "body": "Checking whether it is about your job search…",
            "link": "/valr/leads", "gmail_url": gmail_link(email.thread_id), "thread_id": email.thread_id or None,
            "created_at": now, "arrived_at": email.at, "push_state": "hold"}
    created = await store.insert(note)
    if created is not None:
        logger.info("VALR notification latency: email in the bell %.1f s after it arrived",
                    (now - email.at).total_seconds())
    return created


def gmail_link(thread_id: str) -> str:
    from app import gmail_api
    return gmail_api.thread_url(thread_id) if thread_id else ""


async def retract_incoming(store: Any, email_id: str) -> bool:
    """A dropped email: its `incoming` item goes (it was not job mail)."""
    found = await store.get_source(f"gmail:{email_id}")
    if found is None or found["kind"] != INCOMING:
        return False
    await store.delete(found["id"])
    return True


SLA_P95_S = 10.0  # The owner, 2026-10-17: a new email in the app within 5-10 s (p95 10 s, p50 5 s).
SLA_P50_S = 5.0
PHONE_SLA_S = 10.0  # The owner, 2026-10-17: a call in the bell and on the phone ~10 s after its upload.


def percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(-(-share * len(ordered) // 1)) - 1))]


def rnd(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


async def latency_report(store: Any, now: datetime, window: timedelta = timedelta(hours=24)) -> dict[str, Any]:
    """{count, p50_s, p95_s, max_s, push_p50_s, push_p95_s, sla_p50_s, sla_p95_s, met, phone} for the
    email items of the last `window` (Gmail's arrival time -> the bell item, and -> the phone push);
    `phone`: the call recordings' series (the upload finishing -> the bell item, -> its push, ->
    "ready": transcribed and in Past interviews), against PHONE_SLA_S for the bell."""
    rows = await store.latencies(now - window, "gmail:")
    shown = [max(0.0, a) for a, _, _ in rows]
    pushed = [max(0.0, b) for _, b, _ in rows if b is not None]
    p95 = percentile(shown, 0.95)
    calls = await store.latencies(now - window, "phone:")
    bell = [max(0.0, a) for a, _, _ in calls]
    ready = [max(0.0, c) for _, _, c in calls if c is not None]
    call_pushed = [max(0.0, b) for _, b, _ in calls if b is not None]
    phone_p95 = percentile(bell, 0.95)
    return {"window_h": window.total_seconds() / 3600, "count": len(shown), "p50_s": rnd(percentile(shown, 0.5)),
            "p95_s": rnd(p95), "max_s": rnd(max(shown) if shown else None),
            "push_count": len(pushed), "push_p50_s": rnd(percentile(pushed, 0.5)),
            "push_p95_s": rnd(percentile(pushed, 0.95)), "sla_p50_s": SLA_P50_S, "sla_p95_s": SLA_P95_S,
            "met": None if p95 is None else p95 <= SLA_P95_S,
            "phone": {"count": len(bell), "bell_p50_s": rnd(percentile(bell, 0.5)), "bell_p95_s": rnd(phone_p95),
                      "push_p50_s": rnd(percentile(call_pushed, 0.5)), "push_p95_s": rnd(percentile(call_pushed, 0.95)),
                      "ready_count": len(ready), "ready_p50_s": rnd(percentile(ready, 0.5)),
                      "ready_p95_s": rnd(percentile(ready, 0.95)), "sla_s": PHONE_SLA_S,
                      "met": None if phone_p95 is None else phone_p95 <= PHONE_SLA_S}}


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
    if touch.kind != "email" or touch.direction != "in" or not touch.id.startswith("gmail:") \
            or stale(getattr(touch, "novelty", "")):
        return None  # Not mail to him, or not news (a reminder, a duplicate, a confirmation).
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


async def missed(store: Any, touch: Any, new_thread: bool, live_since: datetime | None, now: datetime) -> bool:
    """A kept inbound email that `from_email` would have pushed but that has no item: new since
    `live_since` (not old mail from a backfill), recent, of a notifying kind, and no item for
    its thread within the dedupe window (that one already covers it)."""
    if live_since is None or touch.at < live_since or now - touch.at > max_age():
        return False
    if email_kind(touch.category or "", touch.action_requested, touch.stage_hint or "", new_thread,
                  touch.title or "", touch.summary or "") is None:
        return False
    return not (touch.thread and await store.recent_for_thread(touch.thread, touch.at - DEDUPE))


async def sync_unread(store: Any, kept: list[Any], unread: set[str] | None, complete: bool,
                      changes: dict[str, bool], now: datetime, first_in_thread: set[str] = frozenset(),
                      live_since: datetime | None = None) -> dict[str, int]:
    """Makes the bell match Gmail's unread job mail.

    `kept`: the kept email touchpoints of the last 30 days; `unread`: Gmail's unread message
    ids of the last 30 days (None when the list could not be fetched), `complete` when that
    list was not cut short; `changes`: {message id: True (marked unread) | False (read)} from
    the Gmail history; `first_in_thread`: touch ids that started their thread.

    - an unread kept inbound email without an item gets one, silently, unless it is a missed
      new event (`missed`: it arrived after `live_since`, under VALR_NOTIFY_MAX_AGE_HOURS ago,
      is of a kind `from_email` notifies, and nothing in its thread is in the bell): then it
      is pushed like any new email ("caught_up"), so a failed or skipped `from_email` never
      loses one;
    - an unread item whose mail is read in Gmail is read (only when the list is complete, or
      the history says so: a cut list never reads anything by mistake);
    - an item whose mail was marked unread again in Gmail (history) is unread again.
    Reading an item in VALR is kept: a mail that stays unread in Gmail does not undo it."""
    out = {"added": 0, "read": 0, "unread": 0, "caught_up": 0}
    since = now - KEEP_FOR
    items = await store.email_items(since)
    # The list is fetched after the history, so it wins; without it, the history alone.
    unread_now = set(unread) if unread is not None else {i for i, v in changes.items() if v}
    for touch in kept:
        message_id = touch.id.removeprefix("gmail:")
        if message_id not in unread_now or message_id in items or touch.at < since:
            continue
        note = silent_note(touch, touch.id in first_in_thread)
        if note is None:
            continue
        loud = await missed(store, touch, touch.id in first_in_thread, live_since, now)
        if loud:  # New to him now: dated now, its arrival kept for the latency figures.
            note.update(push_state="pending", created_at=now, arrived_at=touch.at)
        if await store.insert(note) is not None:
            out["caught_up" if loud else "added"] += 1
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


RECRUITER_CALL = re.compile(r"recruit|talent|screen|\bhr\b|\bta\b|intro(ductory)? (call|chat)|phone call|"
                            r"chat with|call with", re.I)
RECRUITING_ADDRESS = re.compile(r"recruit|talent|hiring|careers?|people|^hr$|^ta$", re.I)


def recruiter_call(event: Any) -> bool:
    """A recruiter's call (a screen, an intro chat), by its title or a recruiting organizer: pushed
    at once however far off it is (the owner, 2026-10-17: "recruiter calls notifications should
    be quick")."""
    organizer = (getattr(event, "organizer_email", "") or "").lower().partition("@")[0]
    return bool(RECRUITER_CALL.search(getattr(event, "title", "") or "")
                or (organizer and RECRUITING_ADDRESS.search(organizer)))


async def from_calendar(store: Any, event: Any, change: str, now: datetime, before_utc: str = "") -> int | None:
    """One calendar change (`change`: added, moved or cancelled) of an interview-like event ->
    one notification. Deduped per event, change and (for a move) the new time. The caller
    decides what is interview-like (app/interview_types.looks_like_interview) and skips the
    first, seeding sync. An interview added (or moved to a time) that the bell already announced
    for the same company, from an email or another event, is not notified again."""
    from app import notify_context
    from app.interview_types import slot_workflow_id
    if change not in CALENDAR_CHANGES:
        return None
    start = datetime.fromisoformat(event.start_utc if change != "cancelled" else (before_utc or event.start_utc))
    if start < now:
        return None  # Already over (or in progress): not news.
    source = f"calendar:{event.key}:{change}" + (f":{event.start_utc}" if change == "moved" else "")
    if change in ("added", "moved") and await store.get_source(source) is None:
        known = notify_context.calendar_known(await store.recent(now - notify_context.WINDOW), event.company or "",
                                              start, f"calendar:{event.key}:", change, now)
        if known is not None:
            logger.info("VALR calendar change not notified: %s, already in the bell (%s)", change,
                        "email" if str(known.get("source_id") or "").startswith("gmail:") else "calendar")
            return None
    note = {"source_id": source, "kind": "calendar",
            "importance": "immediate" if start - now <= CALENDAR_SOON or recruiter_call(event) else "digest",
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


def push_tag(note: dict[str, Any]) -> str:
    """The thread's (or lead's, or item's) tag; a coach review ("review:<provider>:<id>") takes its
    recording's tag ("<provider>:<id>"), so its push replaces the call's on the phone."""
    from app import webpush
    source = str(note.get("source_id") or "")
    if source.startswith("review:"):
        source = source.removeprefix("review:")
    return webpush.tag_for(note.get("thread_id"), note.get("lead_id"), source)


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
    elif kind == "recording":  # "Call with Riya recorded" / "... is ready": as written, no numbers.
        title, body = scrub(note.get("title") or "") or LABELS[kind], scrub(note.get("body") or "")
    elif kind == "calendar":
        title = "Calendar" + (f" · {scrub(name_of(note.get('company') or ''))}" if name_of(note.get("company") or "") else "")
    else:
        # A warm reply is pushed at once too, but it asks nothing of him: its own label.
        title = f"Action needed · {who}" if urgent and kind in IMMEDIATE else f"{LABELS.get(kind, 'Update')} · {who}"
    link = note.get("link") or "/valr"
    return webpush.payload(title, body, link if link.startswith("/") else "/valr",
                           push_tag(note),
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
        if result.delivered and note.get("arrived_at"):
            logger.info("VALR notification latency: email pushed %.1f s after it arrived",
                        (now - note["arrived_at"]).total_seconds())
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
        return {"pushed": 0, "held": 0, "digest": 0, "locked": 1}
    try:
        result = await dispatch(store, now, await webpush.sender(connection))
        await store.purge(now - KEEP_FOR)
    finally:
        await store.unlock()
    if result["pushed"] or result["digest"]:
        logger.info("VALR notifications: %d pushed, %d in the digest, %d held for quiet hours",
                    result["pushed"], result["digest"], result["held"])
    return result


async def dispatch_now(connection: Any, tries: int = 5, wait: float = 0.5) -> dict[str, int] | None:
    """Right after a new item is stored (an email, a calendar change, a recording): push it now
    instead of at the loop's next tick (up to VALR_NOTIFY_LOOP_S later). When the loop holds the
    lock it may have listed the pending items before this one was stored, so retry a few times
    (~2 s). None when it could not run (the loop then sends it). Never raises."""
    import asyncio
    for attempt in range(tries):
        try:
            result = await run_dispatch(connection)
        except Exception as error:  # noqa: BLE001 - the loop sends it on its next tick
            logger.warning("VALR notifications: immediate dispatch failed (%s)", type(error).__name__)
            return None
        if not result.get("locked"):
            return result
        if attempt + 1 < tries:
            await asyncio.sleep(wait)
    return None


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
    """The research worker's background task: every VALR_NOTIFY_LOOP_S (60) seconds, a dispatch
    for each workspace (app/workspaces.each: each in its own scope, with its own quiet hours,
    digest and devices; one failing never stops the others), so held items leave when quiet hours
    end and each digest goes at its time; once an hour, the owner's latency figures of the last 24
    hours are logged ("VALR notification SLA ...")."""
    import asyncio
    from app import workspaces
    stop = stop or asyncio.Event()
    every = float(os.getenv("VALR_NOTIFY_LOOP_S", "60"))
    reported: list[datetime | None] = [None]

    async def one(user: str) -> None:
        async with await connect() as connection:
            await run_dispatch(connection)
            now = datetime.now(timezone.utc)
            if workspaces.owner(user) and (reported[0] is None or now - reported[0] >= timedelta(hours=1)):
                reported[0] = now
                await log_latency(Store(connection), now)
    while not stop.is_set():
        try:
            await workspaces.each(one, label="notifications", connect=connect, budget_s=max(every, 30.0))
        except Exception as error:  # noqa: BLE001 - keep the worker alive
            logger.warning("VALR notifications loop: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass


async def log_latency(store: Any, now: datetime) -> None:
    report = await latency_report(store, now)
    if report["count"]:
        logger.info("VALR notification SLA (24 h): %d emails, p50 %s s, p95 %s s, max %s s "
                    "(target p95 <= %g s: %s); push p50 %s s, p95 %s s", report["count"],
                    report["p50_s"], report["p95_s"], report["max_s"], SLA_P95_S,
                    "met" if report["met"] else "MISSED", report["push_p50_s"], report["push_p95_s"])
    calls = report["phone"]
    if calls["count"]:
        logger.info("VALR notification SLA (24 h): %d calls, upload -> bell p50 %s s, p95 %s s "
                    "(target <= %g s: %s); -> ready p50 %s s, p95 %s s", calls["count"],
                    calls["bell_p50_s"], calls["bell_p95_s"], PHONE_SLA_S,
                    "met" if calls["met"] else "MISSED", calls["ready_p50_s"], calls["ready_p95_s"])


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
