"""Post-call reports on Telegram: queue, process (in the research worker) and deliver.

How a recording gets here:
- the notetaker webhook (valr-notetakers: /hooks/fathom -> app.notetakers.pipeline) calls
  `app.profile.notetaker_hook.evaluate_recording`, which only queues it (`enqueue`): the
  web process never scores;
- fallback: the research worker's `loop` checks Fathom for recordings of the last day
  every VALR_POST_CALL_CHECK_MIN minutes (default 30) and queues the new ones.

The worker's `loop` then takes queued recordings one at a time (`run_queue` ->
`on_new_interview`): app/profile/post_call.process in a thread (Claude through the
gateway), then `deliver`: sent to the owner's linked Telegram chat, or kept as
"pending_telegram" (the dashboard says so) and sent once Telegram is linked
(`send_pending`).

Table `post_call_reports`, one row per recording: status queued | processing | sent |
pending_telegram | not_interview | no_transcript | failed. `sealed` holds the normalised
meeting (transcript) as Fernet ciphertext (VALR_TOKEN_KEY) only until it is processed;
without that key nothing is sealed and the worker re-fetches the transcript from Fathom.
`text` is the rendered report (no pay figures, emails or phone numbers). Nothing here
logs report text, quotes or names.

History (`python -m app.notetakers history`, app/notetakers/history.py): old recordings are
queued with `history = true`. The loop scores them one at a time, at most one every
HISTORY_GAP_S (30 s), after the new recordings, while the research month cap
(VALR_MONTH_USD, research_spend) leaves room for one more; their spend is recorded in
research_spend (job "history-<date>"). A history item ends as status `history` (scored, in
performance.json) or not_interview / no_transcript / failed: no report is written, nothing
goes to Telegram or the phone, and no notification is created. The coach read is refreshed
once, with the last item.

Re-analysis (mode `reanalyse`): an interview already in performance.json whose interviewer
impression is only "derived" (its transcript was not kept) is queued the same way; the worker
fetches the transcript from the provider again and re-runs the rubric and the impression cues
on it (app/profile/post_call.reanalyse). `requeue` queues it (the history import, and the
page's "Try again").

Every meeting the worker evaluates is also kept for the owner, encrypted
(app/notetakers/transcripts.py).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app import telegram_api

logger = logging.getLogger("valr")

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_call_reports (
    provider      text NOT NULL,
    external_id   text NOT NULL,
    status        text NOT NULL DEFAULT 'queued',
    interview_id  text,
    title         text NOT NULL DEFAULT '',
    recording_url text NOT NULL DEFAULT '',
    sealed        text,
    text          text,
    detail        text,
    cost_usd      numeric,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    sent_at       timestamptz,
    message_id    bigint,
    PRIMARY KEY (provider, external_id)
);
ALTER TABLE post_call_reports ADD COLUMN IF NOT EXISTS history boolean NOT NULL DEFAULT false;
ALTER TABLE post_call_reports ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'score';
"""
DONE = ("sent", "history", "not_interview", "no_transcript")
HISTORY_GAP_S = 30.0
MAX_TRIES_DETAIL = "failed"


def host() -> str:
    return os.getenv("VALR_HOST", "valr.haciensus.com").strip().lower()


def token_key() -> str:
    return os.getenv("VALR_TOKEN_KEY", "").strip()


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


async def enqueue(connection: Any, provider: str, external_id: str, meeting: dict | None = None,
                  title: str = "", recording_url: str = "", history: bool = False) -> bool:
    """Queue one recording; True when it is new. Never re-queues a known one. `history`: an old
    recording from the backfill (scored quietly, see the module doc)."""
    from app import google_oauth
    await setup(connection)
    sealed = (google_oauth.encrypt(token_key(), json.dumps(meeting))
              if meeting and token_key() else None)
    cursor = await connection.execute(
        "INSERT INTO post_call_reports (provider, external_id, title, recording_url, sealed, history) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (provider, external_id) DO NOTHING RETURNING 1",
        (provider, str(external_id), (title or "")[:300], recording_url or "", sealed, bool(history)))
    return await cursor.fetchone() is not None


async def set_status(connection: Any, provider: str, external_id: str, status: str, **fields: Any) -> None:
    columns = ["status = %s", "updated_at = now()"] + [f"{k} = %s" for k in fields]
    await connection.execute(
        f"UPDATE post_call_reports SET {', '.join(columns)} WHERE provider = %s AND external_id = %s",
        (status, *fields.values(), provider, str(external_id)))


async def statuses(connection: Any) -> dict[tuple[str, str], str]:
    """Every recording's report status, for Past interviews' "Review pending / not available"."""
    await setup(connection)
    cursor = await connection.execute("SELECT provider, external_id, status FROM post_call_reports")
    return {(p, x): st for p, x, st in await cursor.fetchall()}


async def queued(connection: Any, limit: int = 5, history: bool = False) -> list[dict[str, Any]]:
    """Queued recordings, oldest first: the new ones, or (`history`) the backfill's."""
    cursor = await connection.execute(
        "SELECT provider, external_id, sealed, recording_url, mode FROM post_call_reports "
        "WHERE status = 'queued' AND history = %s ORDER BY created_at LIMIT %s", (history, limit))
    return [dict(zip(("provider", "external_id", "sealed", "recording_url", "mode"), row))
            for row in await cursor.fetchall()]


async def requeue(connection: Any, provider: str, external_id: str, title: str = "",
                  recording_url: str = "") -> bool:
    """Queue a scored interview for re-analysis (history, mode reanalyse); True when queued.
    A row already queued or being processed is left alone."""
    await setup(connection)
    cursor = await connection.execute(
        "INSERT INTO post_call_reports (provider, external_id, title, recording_url, history, mode) "
        "VALUES (%s, %s, %s, %s, true, 'reanalyse') ON CONFLICT (provider, external_id) DO UPDATE SET "
        "status = 'queued', history = true, mode = 'reanalyse', detail = NULL, updated_at = now() "
        "WHERE post_call_reports.status NOT IN ('queued', 'processing') RETURNING 1",
        (provider, str(external_id), (title or "")[:300], recording_url or ""))
    return await cursor.fetchone() is not None


async def history_backlog(connection: Any) -> int:
    cursor = await connection.execute(
        "SELECT count(*) FROM post_call_reports WHERE status = 'queued' AND history")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def overview(connection: Any, limit: int = 5) -> dict[str, Any]:
    """For the dashboard: reports waiting for Telegram, and the latest ones."""
    await setup(connection)
    cursor = await connection.execute(
        "SELECT status, count(*) FROM post_call_reports GROUP BY status")
    counts = {row[0]: int(row[1]) for row in await cursor.fetchall()}
    cursor = await connection.execute(
        "SELECT title, status, updated_at, interview_id FROM post_call_reports "
        "WHERE status IN ('sent', 'pending_telegram', 'failed', 'queued', 'processing') "
        "ORDER BY updated_at DESC LIMIT %s", (limit,))
    recent = [dict(zip(("title", "status", "updated_at", "interview_id"), row))
              for row in await cursor.fetchall()]
    return {"pending": counts.get("pending_telegram", 0), "queued": counts.get("queued", 0)
            + counts.get("processing", 0), "failed": counts.get("failed", 0), "recent": recent}


# Delivery.

async def deliver(connection: Any, provider: str, external_id: str, text: str,
                  bot: telegram_api.Bot | None = None) -> str:
    """Send to the owner's chat, or keep it as pending_telegram. -> the status."""
    bot = bot or telegram_api.bot()
    chat = await telegram_api.owner_chat(connection) if bot else None
    if bot is None or chat is None:
        await set_status(connection, provider, external_id, "pending_telegram", text=text,
                         detail="Telegram is not linked: the report waits on the dashboard.")
        return "pending_telegram"
    try:
        message_id = await bot.send(chat, text)
    except telegram_api.TelegramError as error:
        logger.warning("VALR post-call report not sent: %s", type(error).__name__)
        await set_status(connection, provider, external_id, "pending_telegram", text=text,
                         detail="Telegram refused the message; retrying later.")
        return "pending_telegram"
    await set_status(connection, provider, external_id, "sent", text=text, message_id=message_id,
                     sent_at=datetime.now(timezone.utc), detail="")
    return "sent"


async def send_pending(connection: Any, bot: telegram_api.Bot | None = None) -> int:
    """Reports kept while Telegram was unlinked: send them now if it is linked."""
    bot = bot or telegram_api.bot()
    if bot is None or await telegram_api.owner_chat(connection) is None:
        return 0
    cursor = await connection.execute(
        "SELECT provider, external_id, text FROM post_call_reports WHERE status = 'pending_telegram' "
        "AND text IS NOT NULL ORDER BY updated_at LIMIT 5")
    sent = 0
    for provider, external_id, text in await cursor.fetchall():
        sent += await deliver(connection, provider, external_id, text, bot) == "sent"
    return sent


# Processing (research worker only: it imports the Claude client).

Process = Callable[[dict], dict]


def default_process(meeting: dict, history: bool = False, refresh_coach: bool = True,
                    reanalyse: bool = False) -> dict:
    """Score one meeting (Claude). `history`: no report is composed; the coach read is
    refreshed only with `refresh_coach`; `reanalyse`: re-read an interview already in
    performance.json. Adds cost_usd and the per-model token rows."""
    from app.profile import importer, post_call
    from app.profile.config import settings
    from app.profile.llm import AnthropicJsonLLM
    cfg = settings()
    llm = AnthropicJsonLLM()
    try:
        if reanalyse:
            out = post_call.reanalyse(meeting, cfg, llm, importer.load_comp(cfg.profile_path),
                                      refresh_coach=refresh_coach)
        elif history:
            out = post_call.process(meeting, cfg, llm, importer.load_comp(cfg.profile_path),
                                    refresh_coach=refresh_coach, compose_report=False)
        else:
            out = post_call.process(meeting, cfg, llm, importer.load_comp(cfg.profile_path))
    finally:
        spend = spend_rows(llm.ledger)
    out["cost_usd"] = llm.ledger.cost_usd()
    out["spend_rows"] = spend
    return out


def spend_rows(ledger: Any) -> list[dict[str, Any]]:
    """The ledger's per-model totals as research_spend rows."""
    return [{"part": "post_call_history", "kind": "history", "model": model,
             "input_tokens": int(row["input_tokens"]), "output_tokens": int(row["output_tokens"])}
            for model, row in (getattr(ledger, "calls", None) or {}).items()]


async def on_new_interview(meeting: dict, *, connection: Any, recording_url: str = "",
                           process: Process | None = None, bot: telegram_api.Bot | None = None,
                           history: bool = False) -> str:
    """The entry point for one new recording (normalised meeting, transcript included):
    evaluate it, then deliver the report. -> the final status.

    `history` (the backfill): evaluate only; status `history`, nothing is delivered."""
    provider, external_id = meeting["source"], str(meeting["id"])
    await enqueue(connection, provider, external_id, None, meeting.get("title", ""), recording_url, history)
    await set_status(connection, provider, external_id, "processing", sealed=None)
    from app.notetakers import transcripts  # The owner's copy (encrypted); never logged.
    await transcripts.save_quietly(connection, provider, external_id, meeting)
    try:
        out = await asyncio.to_thread(process or default_process, meeting)
    except Exception as error:  # noqa: BLE001 - recorded; the dashboard shows it.
        logger.warning("VALR post-call report failed: %s", type(error).__name__)
        await set_status(connection, provider, external_id, "failed", detail=type(error).__name__)
        return "failed"
    if out["status"] != "ready":
        await set_status(connection, provider, external_id, out["status"], cost_usd=out.get("cost_usd"))
        return out["status"]
    if history:  # Scored into performance.json: no report, no Telegram, no notification.
        await set_status(connection, provider, external_id, "history", interview_id=out["interview_id"],
                         cost_usd=out.get("cost_usd"), detail=out.get("performance_note", ""))
        return "history"
    from app.profile import post_call
    text = post_call.render(out["report"], host(), recording_url)
    await set_status(connection, provider, external_id, "processing", interview_id=out["interview_id"],
                     cost_usd=out.get("cost_usd"), detail=out.get("performance_note", ""))
    return await deliver(connection, provider, external_id, text, bot)


Fetch = Callable[[str, str], dict | None]


def fathom_fetch(provider: str, external_id: str) -> dict | None:
    """A queued recording's meeting when nothing was sealed: Fathom's last 3 days."""
    if provider != "fathom" or not os.getenv("FATHOM_API_KEY"):
        return None
    from app.profile import recordings
    items = recordings.fetch_fathom(os.environ["FATHOM_API_KEY"],
                                    datetime.now(timezone.utc) - timedelta(days=3),
                                    recordings.Http(min_interval=2.0), None)
    item = next((i for i in items if str(i.get("recording_id")) == str(external_id)), None)
    return recordings.normalize_fathom(item) if item else None


async def run_queue(connection: Any, fetch: Fetch = fathom_fetch, process: Process | None = None,
                    bot: telegram_api.Bot | None = None) -> int:
    from app import google_oauth
    done = 0
    for row in await queued(connection):
        meeting = None
        if row["sealed"] and token_key():
            try:
                meeting = json.loads(google_oauth.decrypt(token_key(), row["sealed"]))
            except Exception:  # noqa: BLE001 - re-fetch instead
                meeting = None
        if meeting is None and row["provider"] == "phone":  # Kept by VALR (app/phone_recordings.py).
            from app import phone_recordings
            meeting = await phone_recordings.sealed_meeting(connection, row["external_id"])
        if meeting is None:
            meeting = await asyncio.to_thread(fetch, row["provider"], row["external_id"])
        if meeting is None:
            await set_status(connection, row["provider"], row["external_id"], "failed",
                             detail="Transcript not available from the provider.")
            continue
        await on_new_interview(meeting, connection=connection, recording_url=row["recording_url"],
                               process=process, bot=bot)
        done += 1
    return done


# History (the backfill): one at a time, paced, within the research month cap.

FetchAsync = Callable[[Any, str, str], Any]


async def month_room(connection: Any, need_usd: float) -> bool:
    """True while the research month cap leaves room for `need_usd` more."""
    from app import research_jobs
    try:
        await research_jobs.setup(connection)
        spent = await research_jobs.month_spend(connection)
    except Exception as error:  # noqa: BLE001 - unknown spend: wait rather than overspend.
        logger.warning("VALR history: month spend unavailable (%s)", type(error).__name__)
        return False
    return spent + need_usd <= research_jobs.Limits.from_env().month_usd


async def record_history_spend(connection: Any, rows: list[dict[str, Any]], now: datetime | None = None) -> float:
    from app import research_jobs
    if not rows:
        return 0.0
    try:
        return await research_jobs.record_spend(
            connection, f"history-{(now or datetime.now(timezone.utc)):%Y-%m-%d}", rows)
    except Exception as error:  # noqa: BLE001 - the item is scored; only the ledger row is lost.
        logger.warning("VALR history spend not recorded: %s", type(error).__name__)
        return 0.0


async def provider_fetch(connection: Any, provider: str, external_id: str) -> dict | None:
    """An old recording's meeting, by id, from its provider (the stored or server key)."""
    from app.notetakers import history
    return await history.fetch_meeting(connection, provider, external_id)


async def run_history(connection: Any, fetch: FetchAsync = provider_fetch, process: Any = None,
                      room: Callable[[Any, float], Any] = month_room) -> str | None:
    """Score the oldest queued history item. -> its status, "month_cap", or None (none queued)."""
    from app import google_oauth
    from app.notetakers import history
    rows = await queued(connection, 1, history=True)
    if not rows:
        return None
    row = rows[0]
    if not await room(connection, history.estimate(1)["usd"]):
        logger.info("VALR history paused: the research month cap is reached")
        return "month_cap"
    meeting = None
    if row["sealed"] and token_key():
        try:
            meeting = json.loads(google_oauth.decrypt(token_key(), row["sealed"]))
        except Exception:  # noqa: BLE001 - fetch it instead
            meeting = None
    if meeting is None:
        try:
            meeting = await fetch(connection, row["provider"], row["external_id"])
        except Exception as error:  # noqa: BLE001 - recorded; never the message (could quote).
            logger.warning("VALR history fetch failed: %s", type(error).__name__)
            meeting = None
    if meeting is None:
        label = {"fathom": "Fathom", "fireflies": "Fireflies"}.get(row["provider"], "the notetaker")
        await set_status(connection, row["provider"], row["external_id"], "failed",
                         detail=f"Couldn't read this recording's transcript from {label}.")
        return "failed"
    last = await history_backlog(connection) <= 1
    box: dict[str, Any] = {}
    again = row.get("mode") == "reanalyse"

    def work(m: dict) -> dict:
        out = (process or default_process)(m, history=True, refresh_coach=last, reanalyse=again)
        box["rows"] = out.get("spend_rows") or []
        return out
    status = await on_new_interview(meeting, connection=connection, recording_url=row["recording_url"],
                                    process=work, history=True)
    await record_history_spend(connection, box.get("rows") or [])
    logger.info("VALR history: one recording %s", status)
    return status


async def check_recent(connection: Any, now: datetime | None = None, days: float = 1.0) -> int:
    """Fallback for a missed webhook: queue Fathom recordings of the last day. -> new count."""
    key = os.getenv("FATHOM_API_KEY", "").strip()
    if not key:
        return 0
    from app.profile import recordings
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    items = await asyncio.to_thread(recordings.fetch_fathom, key, since, recordings.Http(min_interval=2.0), None)
    new = 0
    for item in items:
        if not item.get("transcript"):
            continue  # still processing at Fathom: the next check picks it up
        meeting = recordings.normalize_fathom(item)
        new += await enqueue(connection, "fathom", meeting["id"], meeting, meeting.get("title", ""),
                             item.get("share_url") or item.get("url") or "")
    return new


async def loop(connect: Callable[[], Any], stop: asyncio.Event | None = None) -> None:
    """The research worker's background task: queue, fallback check, pending sends."""
    stop = stop or asyncio.Event()
    check_every = timedelta(minutes=float(os.getenv("VALR_POST_CALL_CHECK_MIN", "30")))
    last_check = datetime.min.replace(tzinfo=timezone.utc)
    while not stop.is_set():
        wait = 120.0
        try:
            async with await connect() as connection:
                await setup(connection)
                now = datetime.now(timezone.utc)
                if now - last_check >= check_every:
                    last_check = now
                    queued_new = await check_recent(connection, now)
                    if queued_new:
                        logger.info("VALR post-call: %d new recording(s) queued", queued_new)
                await run_queue(connection)
                await send_pending(connection)
                if await run_history(connection) not in (None, "month_cap"):
                    wait = HISTORY_GAP_S  # More history: the next one in 30 s.
        except Exception as error:  # noqa: BLE001 - keep the worker alive
            logger.warning("VALR post-call loop: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def enabled() -> bool:
    return os.getenv("VALR_POST_CALL", "1") == "1"
