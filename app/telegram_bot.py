"""VALR's Telegram webhook: `POST /hooks/telegram` (the only path that bypasses
Cloudflare Access for Telegram; Telegram cannot log in).

Every request must carry X-Telegram-Bot-Api-Secret-Token equal to TELEGRAM_WEBHOOK_SECRET
(set with setWebhook by deploy/install-telegram.sh; compared in constant time), else 403.
Updates are then acted on only from the owner's private chat (app/telegram_api.py:
linked by `/start <code>` from the dashboard). Anyone else gets one polite line and
nothing is logged about them.

The owner can:
- tap the T-30 heads-up's buttons: [📞 Call me now] starts an on-demand
  BlockCallWorkflow for the block's interviews (its research first if a brief is not
  current, within the five-minute promise); [⏰ In 10 min] the same, dialling in ten
  minutes if that is still five minutes before the interview, else "too close"; [Skip]
  no call (and cancels a pending "In 10 min"). The message is edited to show the outcome.
- send /next, /today (label, time and status as text), /pause, /resume (the dashboard's
  pause switch: no heads-ups and no automatic calls while paused), /help.
"""
import hmac
import logging
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app import calendar_sync, callbacks, interview_store, interviews, research, telegram_api
from app.interview_types import SlotEvent, briefable, group_blocks
from app.interview_workflow import BlockCallWorkflow
from app.telegram_api import LATER, MIN_LEFT, Bot, clock, e, keyboard, outcome_line

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
MAX_BODY = 64 * 1024
BLOCK_ID = re.compile(r"briefing-\d{8}T\d{4}Z")
PRIVATE = "This is a private assistant."
HELP = ("I send you a heads-up 30 minutes before each interview; tap 📞 to get the briefing "
        "call. Commands: /next, /today, /pause (no heads-ups), /resume.")
LOOK_AHEAD = timedelta(days=8)
seen: deque[int] = deque(maxlen=500)  # Recent update ids: Telegram may deliver one twice.
bot_override: Bot | None = None  # Tests.


def same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def current_bot() -> Bot | None:
    return bot_override or telegram_api.bot()


@router.post("/hooks/telegram")
async def webhook(request: Request) -> dict[str, bool]:
    secret = telegram_api.webhook_secret()
    if not secret or not telegram_api.bot_token():
        raise HTTPException(503, "Telegram is not configured.")
    if not same(request.headers.get("x-telegram-bot-api-secret-token"), secret):
        logger.warning("Telegram webhook refused: bad secret token")
        raise HTTPException(403, "Forbidden.")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Too large.")
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}  # Nothing to do; a 4xx would only make Telegram retry.
    if not isinstance(update, dict):
        return {"ok": True}
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        if update_id in seen:
            return {"ok": True}
        seen.append(update_id)
    try:
        await handle(update)
    except Exception as error:  # noqa: BLE001 - answered 200 so Telegram does not resend
        logger.warning("Telegram update failed: %s", type(error).__name__)
    return {"ok": True}


async def handle(update: dict[str, Any]) -> None:
    bot = current_bot()
    if bot is None:
        return
    if isinstance(update.get("callback_query"), dict):
        await on_button(bot, update["callback_query"])
    elif isinstance(update.get("message"), dict):
        await on_message(bot, update["message"])


async def owner_chat() -> int | None:
    if research.pool is None:
        return None
    async with research.pool.connection() as connection:
        await telegram_api.setup(connection)
        return await telegram_api.owner_chat(connection)


# Messages.

async def on_message(bot: Bot, message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
        return  # Groups and channels: ignored.
    chat_id = chat["id"]
    text = str(message.get("text") or "").strip()
    command, _, argument = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    if research.pool is None:
        await bot.send(chat_id, "VALR is unavailable right now. Try again in a minute.")
        return
    owner = await owner_chat()
    if chat_id != owner:
        if command == "/start" and argument.strip():
            async with research.pool.connection() as connection:
                await telegram_api.setup(connection)
                linked = await telegram_api.redeem(connection, chat_id, argument.strip())
            if linked:
                logger.info("Telegram linked to the owner's chat")
                await bot.send(chat_id, "Connected. " + HELP)
                return
        await bot.send(chat_id, PRIVATE)
        return
    if command == "/start":
        await bot.send(chat_id, "You're connected. " + HELP)
    elif command == "/next":
        await bot.send(chat_id, await next_text())
    elif command == "/today":
        await bot.send(chat_id, await today_text())
    elif command in ("/pause", "/resume"):
        paused = command == "/pause"
        async with research.pool.connection() as connection:
            await interview_store.setup(connection)
            await interview_store.set_flag(connection, interview_store.DIALS_PAUSED, paused)
        logger.info("VALR heads-ups %s from Telegram", "paused" if paused else "resumed")
        await bot.send(chat_id, "Paused: no heads-ups and no automatic calls until /resume. "
                                "VALR keeps researching." if paused else
                       "Resumed: you'll get a heads-up 30 minutes before each interview.")
    else:
        await bot.send(chat_id, HELP)


async def upcoming(now: datetime) -> tuple[list[SlotEvent], dict[str, dict[str, Any]]]:
    """Upcoming interviews (not ended) and their stored slots."""
    async with research.pool.connection() as connection:  # type: ignore[union-attr]
        await calendar_sync.setup(connection)
        await interview_store.setup(connection)
        events = [event for event in await calendar_sync.upcoming(connection, now - timedelta(hours=4),
                                                                   now + LOOK_AHEAD)
                  if briefable(event) and datetime.fromisoformat(event.end_utc) > now]
        slots = {slot["key"]: slot for slot in
                 await interview_store.load_slots(connection, [event.key for event in events])}
    return events, slots


def status(event: SlotEvent, slot: dict[str, Any] | None, now: datetime) -> str:
    if datetime.fromisoformat(event.start_utc) <= now:
        return "happening now"
    state = (slot or {}).get("status")
    if state in ("done", "partial"):
        return "briefing ready"
    if state == "apologized":
        return "researched, nothing reliable found"
    if state in ("researching", "pending"):
        return "researching"
    return "research 60 min before"


def line(event: SlotEvent, slot: dict[str, Any] | None, now: datetime) -> str:
    when = clock(datetime.fromisoformat(event.start_utc))
    return f"<b>{e(when)}</b> · {e(telegram_api.event_label(event))} · {e(status(event, slot, now))}"


async def next_text(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    events, slots = await upcoming(now)
    later = [event for event in events if datetime.fromisoformat(event.start_utc) > now]
    if not later:
        return "No interviews in the next 8 days."
    event = later[0]
    start = datetime.fromisoformat(event.start_utc)
    day = start.astimezone(telegram_api.zone()).strftime("%a %d %b")
    return f"Next: {line(event, slots.get(event.key), now)} ({e(day)})"


async def today_text(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(telegram_api.zone()).date()
    events, slots = await upcoming(now)
    mine = [event for event in events
            if datetime.fromisoformat(event.start_utc).astimezone(telegram_api.zone()).date() == today]
    if not mine:
        return "No more interviews today."
    return "Today:\n" + "\n".join(line(event, slots.get(event.key), now) for event in mine)


# Buttons.

async def on_button(bot: Bot, query: dict[str, Any]) -> None:
    query_id = str(query.get("id", ""))
    sender = (query.get("from") or {}).get("id")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if research.pool is None:
        await bot.answer(query_id, "VALR is unavailable right now.")
        return
    owner = await owner_chat()
    if owner is None or sender != owner or chat_id != owner:
        await bot.answer(query_id, PRIVATE)
        return
    action, _, block_id = str(query.get("data", "")).partition(":")
    if action not in ("c", "l", "s") or not BLOCK_ID.fullmatch(block_id):
        await bot.answer(query_id, "That button has expired.")
        return
    message_id = message.get("message_id")
    original = e(original_text(str(message.get("text") or "")))
    reply, outcome, at, buttons = await act(action, block_id, datetime.now(timezone.utc))
    await bot.answer(query_id, reply)
    if outcome and isinstance(message_id, int):
        await bot.edit(owner, message_id, f"{original}\n\n{outcome_line(outcome, at)}", buttons)


OUTCOME_STARTS = ("Want a call?", "📞", "⏰", "Skipped", "This interview")


def original_text(text: str) -> str:
    """The heads-up as sent (plain text from Telegram), without its prompt or an outcome."""
    kept = []
    for row in text.split("\n"):
        if row.startswith(OUTCOME_STARTS):
            break
        kept.append(row)
    return "\n".join(kept).rstrip()


async def block_events(block_id: str, now: datetime) -> list[SlotEvent]:
    """The block's interviews that have not ended (by its recorded slots, else regrouped)."""
    async with research.pool.connection() as connection:  # type: ignore[union-attr]
        await interview_store.setup(connection)
        await calendar_sync.setup(connection)
        record = await interview_store.block(connection, block_id)
        if record is not None:
            snapshots = await calendar_sync.snapshots(connection, list(record["slot_keys"]))
            events = [snapshots[key] for key in record["slot_keys"] if key in snapshots]
        else:
            stored = await calendar_sync.upcoming(connection, now - timedelta(hours=4), now + LOOK_AHEAD)
            by_key = {event.key: event for event in stored}
            plan = next((b for b in group_blocks(stored) if b.block_id == block_id), None)
            events = [by_key[key] for key in plan.slot_keys] if plan else []
    return [event for event in events
            if briefable(event) and datetime.fromisoformat(event.end_utc) > now]


async def act(action: str, block_id: str, now: datetime
              ) -> tuple[str, str, datetime | None, list[list[dict]] | None]:
    """-> (the button's toast, the outcome to show or "", its time, the buttons to keep)."""
    events = await block_events(block_id, now)
    if not events:
        return "This interview is no longer on your calendar.", "", None, None
    start = datetime.fromisoformat(events[0].start_utc)
    async with research.pool.connection() as connection:  # type: ignore[union-attr]
        await telegram_api.setup(connection)
        record = await telegram_api.heads_up(connection, block_id) or {}
    pending = record.get("call_workflow") if record.get("outcome") == "later" else None
    if action == "s":
        await cancel(pending)
        await save(block_id, "skipped")
        logger.info("VALR heads-up %s: skipped", block_id)
        return "Skipped. No call.", "skipped", None, None
    if start <= now:
        return "The interview has started.", "too_late", None, None
    if action == "l":
        due = now + LATER
        if due > start - MIN_LEFT:
            return "Too close to the interview for that: tap 📞 Call me now.", "", None, None
    elif record.get("outcome") in ("called", "calling") and record.get("outcome_at") \
            and now - record["outcome_at"] < timedelta(minutes=10):
        return "Already calling you.", "", None, None
    from app import call_now
    from app.interview_worker import owner_key, owner_phone
    if not owner_phone():
        return "No phone number on file (VALR_OWNER_PHONE).", "", None, None
    client = await callbacks.temporal()
    if client is None:
        return "VALR can't place calls right now. Try again in a minute.", "", None, None
    _, todo = await interviews.split_ready(events)
    await cancel(pending)
    label = interview_label(events)
    dial_at = now + LATER if action == "l" else None
    workflow_id = await call_now.call_block_now(
        client, events, todo, owner_key(), owner_phone(), label,
        dials_per_day=int(os.getenv("VALR_BRIEFING_DIALS_PER_DAY", 12)), dial_at=dial_at,
        rpc_timeout=callbacks.RPC_TIMEOUT)
    if action == "l":
        await save(block_id, "later", dial_at, workflow_id)
        logger.info("VALR heads-up %s: call in 10 minutes", block_id)
        return f"OK, calling you at {clock(dial_at)}.", "later", dial_at, keyboard(block_id, later=False)
    outcome, at = ("called", now) if not todo else ("calling", now + call_now.PROMISE)
    await save(block_id, outcome, at, workflow_id)
    logger.info("VALR heads-up %s: call now (brief %s)", block_id, "ready" if not todo else "researching")
    return ("Calling you now." if not todo else "Researching first; calling within 5 minutes.",
            outcome, at, None)


def interview_label(events: list[SlotEvent]) -> str:
    """For the call's greeting, like the scheduled block's label."""
    names = [interviews.company_label(event) or event.title[:40] for event in events]
    first = datetime.fromisoformat(events[0].start_utc).astimezone(telegram_api.zone())
    when = first.strftime("%I:%M %p").lstrip("0")
    if len(events) == 1:
        return f"your {when} interview with {names[0]}"
    return f"your {when} interviews: {', then '.join(names)}"


async def save(block_id: str, outcome: str, at: datetime | None = None,
               workflow_id: str | None = None) -> None:
    async with research.pool.connection() as connection:  # type: ignore[union-attr]
        await telegram_api.setup(connection)
        await telegram_api.set_outcome(connection, block_id, outcome, at, workflow_id)


async def cancel(workflow_id: str | None) -> None:
    """Cancel a pending "In 10 min" call (it ends without dialling)."""
    if not workflow_id:
        return
    client = await callbacks.temporal()
    if client is None:
        return
    try:
        await client.get_workflow_handle(workflow_id).signal(BlockCallWorkflow.cancel,
                                                             rpc_timeout=callbacks.RPC_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - already finished
        logger.info("VALR pending call not cancelled: %s", type(error).__name__)

