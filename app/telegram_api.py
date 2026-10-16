"""VALR on Telegram, the shared part: the Bot API client, the owner link, the heads-up.

Used by the research worker (the T-30 heads-up activity) and the app (the webhook,
app/telegram_bot.py), so it imports only the standard library, httpx and the pure
store and type modules.

The owner design (2026-10-16): 30 minutes before each interview block VALR sends one
Telegram message (the interviews' calendar titles and times, and a one- or two-line
teaser from public research only, see `public_teaser`) with three buttons: [📞 Call me now] [⏰ In 10 min] [Skip]. No
reply means no call; the phone call is on demand only.

Secrets: TELEGRAM_BOT_TOKEN (Bot API) and TELEGRAM_WEBHOOK_SECRET (Telegram sends it
back in X-Telegram-Bot-Api-Secret-Token). The token is part of every Bot API URL, so
httpx's request log is silenced here and errors are logged by class name only.

The owner's chat: captured by `/start <code>`, the one-time code the dashboard shows
(stored as an HMAC, 15 minutes, 5 wrong tries), or TELEGRAM_OWNER_CHAT_ID if set.
Nothing here logs a chat id, a name or message text.
"""
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.interview_types import SlotEvent

logger = logging.getLogger("valr")
logging.getLogger("httpx").setLevel(logging.WARNING)  # Its INFO line prints the URL: the token.
API = "https://api.telegram.org"
TIMEOUT_S = 15
CODE_TTL = timedelta(minutes=15)
CODE_TRIES = 5
LATER = timedelta(minutes=10)  # "In 10 min".
MIN_LEFT = timedelta(minutes=5)  # A call must start at least this long before the interview.
TEASER_CHARS = 220

SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_link (
    id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    chat_id       bigint,
    linked_at     timestamptz,
    code_mac      text,
    code_expires  timestamptz,
    code_tries    int NOT NULL DEFAULT 0
);
ALTER TABLE telegram_link ADD COLUMN IF NOT EXISTS username text;
CREATE TABLE IF NOT EXISTS telegram_heads_up (
    block_id      text PRIMARY KEY,
    message_id    bigint,
    sent_at       timestamptz NOT NULL DEFAULT now(),
    outcome       text NOT NULL DEFAULT 'sent',   -- sent, called, later, skipped
    outcome_at    timestamptz,
    call_at       timestamptz,                    -- called / later: when the dial is due
    call_workflow text
);
"""


# Settings.

def bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def webhook_secret() -> str:
    return os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()


def configured() -> bool:
    return bool(bot_token() and webhook_secret())


def auto_dial() -> bool:
    """VALR_AUTO_DIAL=1 brings back the automatic T-30 call instead of the heads-up."""
    return os.getenv("VALR_AUTO_DIAL", "0").strip() == "1"


def zone() -> ZoneInfo:
    return ZoneInfo(os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata"))


def clock(moment: datetime) -> str:
    """ "4:00 PM IST" in the owner's zone."""
    local = moment.astimezone(zone())
    return f"{local.strftime('%I:%M %p').lstrip('0')} {local.strftime('%Z')}"


# The Bot API.

class TelegramError(Exception):
    """A Bot API call failed; the message never contains the token."""


@dataclass
class Bot:
    token: str
    http: httpx.AsyncClient | None = None

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        client = self.http or httpx.AsyncClient(timeout=TIMEOUT_S)
        try:
            response = await client.post(f"{API}/bot{self.token}/{method}", json=payload)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise TelegramError(f"{method}: {type(error).__name__}") from None
        finally:
            if self.http is None:
                await client.aclose()
        if not body.get("ok"):
            raise TelegramError(f"{method}: {response.status_code} {str(body.get('description', ''))[:120]}")
        return body.get("result")

    async def send(self, chat_id: int, text: str, keyboard: list[list[dict]] | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "link_preview_options": {"is_disabled": True}}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = await self.call("sendMessage", payload)
        return int(result.get("message_id", 0)) if isinstance(result, dict) else 0

    async def edit(self, chat_id: int, message_id: int, text: str,
                   keyboard: list[list[dict]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                                   "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                                   "reply_markup": {"inline_keyboard": keyboard or []}}
        try:
            await self.call("editMessageText", payload)
        except TelegramError as error:
            if "not modified" not in str(error):
                raise

    async def answer(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})


def bot() -> Bot | None:
    token = bot_token()
    return Bot(token) if token else None


_bot_username: str | None = None
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
START_PAYLOAD = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # What t.me/<bot>?start= accepts.


async def bot_username(client: Bot | None = None) -> str | None:
    """The bot's @username for t.me deep links: TELEGRAM_BOT_USERNAME, else getMe (cached)."""
    global _bot_username
    explicit = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    if USERNAME.match(explicit):
        return explicit
    if _bot_username:
        return _bot_username
    client = client or bot()
    if client is None:
        return None
    try:
        me = await client.call("getMe", {})
    except TelegramError as error:
        logger.warning("Telegram getMe failed: %s", type(error).__name__)
        return None
    name = str((me or {}).get("username") or "") if isinstance(me, dict) else ""
    _bot_username = name if USERNAME.match(name) else None
    return _bot_username


def deep_link(username: str, code: str) -> str:
    """https://t.me/<bot>?start=<code>: one tap opens the chat and sends `/start <code>`."""
    if not USERNAME.match(username) or not START_PAYLOAD.match(code):
        raise ValueError("Not a valid bot username or start payload.")
    return f"https://t.me/{username}?start={code}"


async def username(client: Bot | None = None) -> str:
    """The bot's @username, "" if unknown (the notetakers branch's name for bot_username)."""
    return await bot_username(client) or ""


def start_link(name: str, code: str) -> str:
    """deep_link, or "" for a bad username or payload (the notetakers branch's name)."""
    try:
        return deep_link(name, code)
    except ValueError:
        return ""


# The owner link (Postgres).

async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


def code_mac(code: str) -> str:
    key = (webhook_secret() or bot_token() or "valr").encode()
    return hmac.new(key, f"valr-telegram-start|{code}".encode(), hashlib.sha256).hexdigest()


async def owner_chat(connection: Any) -> int | None:
    explicit = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
    if explicit.lstrip("-").isdigit():
        return int(explicit)
    cursor = await connection.execute("SELECT chat_id FROM telegram_link WHERE id = 1")
    row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def link_status(connection: Any) -> dict[str, Any]:
    cursor = await connection.execute(
        "SELECT chat_id IS NOT NULL, linked_at, code_expires, username FROM telegram_link WHERE id = 1")
    row = await cursor.fetchone()
    linked = bool(row and row[0]) or bool(os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip())
    username = str(row[3]) if row and len(row) > 3 and row[3] and USERNAME.match(str(row[3])) else None
    return {"linked": linked, "linked_at": row[1] if row else None, "username": username,
            "code_pending": bool(row and row[2] and row[2] > datetime.now(timezone.utc))}


async def new_code(connection: Any, now: datetime | None = None) -> str:
    """A fresh six-digit /start code for the dashboard; replaces any earlier one."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (now or datetime.now(timezone.utc)) + CODE_TTL
    await connection.execute(
        "INSERT INTO telegram_link (id, code_mac, code_expires, code_tries) VALUES (1, %s, %s, 0) "
        "ON CONFLICT (id) DO UPDATE SET code_mac = excluded.code_mac, "
        "code_expires = excluded.code_expires, code_tries = 0", (code_mac(code), expires))
    return code


async def redeem(connection: Any, chat_id: int, code: str, now: datetime | None = None,
                 username: str | None = None) -> bool:
    """`/start <code>` from `chat_id` (typed, or from a t.me/<bot>?start=<code> deep link, which
    arrives as the same text): link it if the code is current; count a wrong try. `username`:
    the owner's Telegram @username, kept only to show "Connected as @username"."""
    now = now or datetime.now(timezone.utc)
    cursor = await connection.execute(
        "SELECT code_mac, code_expires, code_tries FROM telegram_link WHERE id = 1")
    row = await cursor.fetchone()
    if not row or not row[0] or row[1] is None or row[1] <= now or row[2] >= CODE_TRIES:
        return False
    if not (code.isdigit() and hmac.compare_digest(row[0], code_mac(code))):
        await connection.execute("UPDATE telegram_link SET code_tries = code_tries + 1 WHERE id = 1")
        return False
    await connection.execute(
        "UPDATE telegram_link SET chat_id = %s, linked_at = %s, code_mac = NULL, code_expires = NULL, "
        "code_tries = 0, username = %s WHERE id = 1",
        (chat_id, now, username if username and USERNAME.match(username) else None))
    return True


# Heads-up records (per block).

async def record_heads_up(connection: Any, block_id: str, message_id: int) -> None:
    await connection.execute(
        "INSERT INTO telegram_heads_up (block_id, message_id) VALUES (%s, %s) ON CONFLICT (block_id) "
        "DO UPDATE SET message_id = excluded.message_id, sent_at = now(), outcome = 'sent', "
        "outcome_at = NULL, call_at = NULL, call_workflow = NULL", (block_id, message_id))


async def heads_up(connection: Any, block_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        "SELECT block_id, message_id, sent_at, outcome, outcome_at, call_at, call_workflow "
        "FROM telegram_heads_up WHERE block_id = %s", (block_id,))
    row = await cursor.fetchone()
    names = ("block_id", "message_id", "sent_at", "outcome", "outcome_at", "call_at", "call_workflow")
    return dict(zip(names, row)) if row else None


async def heads_ups(connection: Any, block_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not block_ids:
        return {}
    cursor = await connection.execute(
        "SELECT block_id, sent_at, outcome, outcome_at, call_at FROM telegram_heads_up "
        "WHERE block_id = ANY(%s)", (block_ids,))
    return {row[0]: dict(zip(("block_id", "sent_at", "outcome", "outcome_at", "call_at"), row))
            for row in await cursor.fetchall()}


async def set_outcome(connection: Any, block_id: str, outcome: str, call_at: datetime | None = None,
                      call_workflow: str | None = None) -> None:
    await connection.execute(
        "INSERT INTO telegram_heads_up (block_id, outcome, outcome_at, call_at, call_workflow) "
        "VALUES (%s, %s, now(), %s, %s) ON CONFLICT (block_id) DO UPDATE SET outcome = excluded.outcome, "
        "outcome_at = now(), call_at = excluded.call_at, call_workflow = excluded.call_workflow",
        (block_id, outcome, call_at, call_workflow))


# Messages.

def e(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=False)


def keyboard(block_id: str, later: bool = True) -> list[list[dict]]:
    row = [{"text": "📞 Call me now", "callback_data": f"c:{block_id}"}]
    if later:
        row.append({"text": "⏰ In 10 min", "callback_data": f"l:{block_id}"})
    row.append({"text": "Skip", "callback_data": f"s:{block_id}"})
    return [row]


def event_label(event: SlotEvent) -> str:
    """The calendar's own title (never a guess), with the company if the title lacks it."""
    title = " ".join(event.title.split())[:80] or event.company or "Interview"
    if event.company and event.company.lower() not in title.lower():
        title = f"{event.company}: {title}"
    return title


def teaser(spoken: str | None, limit: int = TEASER_CHARS) -> str:
    """The brief's first one or two sentences, at most `limit` characters."""
    text = " ".join((spoken or "").split())
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = sentences[0]
    if len(sentences) > 1 and len(out) + 1 + len(sentences[1]) <= limit:
        out += " " + sentences[1]
    return out if len(out) <= limit else out[:limit - 1].rsplit(" ", 1)[0] + "…"


# The teaser uses public research only (owner's decision, 2026-10-16): the company
# researcher's summary (R3), else the round researcher's (R2); never the spoken brief or
# the private part (R1: his email and recordings). Sentences that mention pay, an email
# address or a phone number are dropped; nothing usable leaves just the title and time.
PRIVATE_HINT = re.compile(r"[₹$€£]|\bLPA\b|\blakhs?\b|\bcrores?\b|\bCTC\b|salar|compensation|"
                          r"\bpay\b|\bbonus|\bequity\b|\bESOPs?\b|\bRSUs?\b|\bOTE\b|@|"
                          r"\+?\d[\d\s().-]{7,}\d|\b(?:e-?mail|inbox|recruiter said|you wrote)\b", re.I)


def public_teaser(slot: dict[str, Any] | None, company: dict[str, Any] | None = None) -> str:
    """One or two public sentences about the company or the round, or ""."""
    parts = (slot or {}).get("parts") or {}
    for part in (parts.get("company") or company, parts.get("round")):
        if not isinstance(part, dict) or not part.get("ok"):
            continue
        found = part.get("findings")
        summary = found.get("summary") if isinstance(found, dict) else None
        if not isinstance(summary, str):
            continue
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", " ".join(summary.split()))
                     if x and not PRIVATE_HINT.search(x)]
        if sentences:
            return teaser(" ".join(sentences))
    return ""


def heads_up_text(events: list[SlotEvent], teasers: dict[str, str], ready: set[str],
                  now: datetime) -> str:
    """ "<b>Kestrel screening call</b> at 4:00 PM IST, in 30 min." plus public teasers.

    `teasers`: event key -> a public teaser (may be empty); `ready`: keys with a brief."""
    first = datetime.fromisoformat(events[0].start_utc)
    minutes = max(round((first - now).total_seconds() / 60), 0)
    lines = []
    for index, event in enumerate(events):
        when = clock(datetime.fromisoformat(event.start_utc))
        lead = f", in {minutes} min" if index == 0 else ""
        lines.append(f"<b>{e(event_label(event))}</b> at {e(when)}{lead}.")
    done = [event for event in events if event.key in ready]
    if len(done) == len(events):
        lines.append("Your briefing is ready.")
    elif done:
        lines.append("Part of your briefing is ready.")
    else:
        lines.append("Your briefing is still being prepared.")
    shown = [teasers.get(event.key, "") for event in events]
    shown = [line for line in shown if line]
    if shown:
        lines.append("")
        lines += [f"<i>{e(line)}</i>" for line in shown[:2]]
    lines.append("")
    lines.append("Want a call? No reply means no call.")
    return "\n".join(lines)


def outcome_line(outcome: str, at: datetime | None) -> str:
    when = clock(at) if at else ""
    return {"called": f"📞 Called at {when}.", "calling": f"📞 Calling you by {when} (finishing the research first).",
            "later": f"⏰ Calling you at {when}.", "skipped": "Skipped: no call.",
            "too_late": "This interview has started: no call."}.get(outcome, outcome)
