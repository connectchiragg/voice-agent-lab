"""The owner's profile: one place to read who VALR works for, editable in the app.

Table `owner_profile` (one row, id = 1), edited on /valr/profile and Settings:

    display_name        the greeting on calls and the app ("Hi Chirag")
    recording_name      how the owner appears in transcripts (telling him apart from the
                        interviewer); VALR_CANDIDATE_NAME before
    name_pronunciation  a respelling the voice uses for the display name ("Chi-rag")
    timezone            IANA zone for reminders, quiet hours, the digest and "4:00 PM IST"
    photo, photo_type,  the avatar (a square WebP/JPEG/PNG the browser cropped), its ETag
    photo_etag
    phone_e164          the owner's phone; NULL means "the server's VALR_OWNER_PHONE"
    phone_removed       true after Remove: the env number no longer applies either
    phone_updated_at
    legacy_keys         the caller keys the owner had before the stable key (see below)
    migrated_at         when caller memory was moved to the stable key

Every value falls back to the environment when unset, so an existing deploy behaves as
before: VALR_OWNER_NAME, VALR_CANDIDATE_NAME, ASSISTANT_TIMEZONE, VALR_OWNER_PHONE.

The stable owner key. Caller memory, research limits and callback workflows used to be
keyed by "tel:<owner's phone>", so changing the number would have orphaned them. The owner
is now always `KEY` ("owner"): `canonical` maps his current phone, his sign-in emails and
the key itself to it. At the first setup the memory rows of the old keys are merged into
"owner", and the old keys are kept in `legacy_keys`, only so that workflows started before
(a briefing or callback holding "tel:<old>") still dial the owner's CURRENT number
(`is_owner_key`). An inbound call from an old number is NOT the owner.

Reads are synchronous from an in-process cache (the realtime call path never waits on the
database). The app loads it at start-up and updates it on every save; the workers refresh
it every minute (`keep_fresh`) and before a dial (`refresh`). Nothing here logs a name, a
number or a photo.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

logger = logging.getLogger("valr")

KEY = "owner"
DEFAULT_ZONE = "Asia/Kolkata"
FRESH_S = 60.0
NAME_MAX = 60
PHOTO_MAX = 2 * 1024 * 1024
PHOTO_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_profile (
    id                  int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    display_name        text,
    recording_name      text,
    name_pronunciation  text,
    timezone            text,
    photo               bytea,
    photo_type          text,
    photo_etag          text,
    phone_e164          text,
    phone_removed       boolean NOT NULL DEFAULT false,
    phone_updated_at    timestamptz,
    legacy_keys         text[] NOT NULL DEFAULT '{}',
    migrated_at         timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO owner_profile (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""
FIELDS = ("display_name", "recording_name", "name_pronunciation", "timezone", "photo_type", "photo_etag",
          "phone_e164", "phone_removed", "phone_updated_at", "legacy_keys", "migrated_at", "updated_at")

_row: dict[str, Any] = {}
_loaded_at = 0.0


# The cache.

def apply(row: dict[str, Any] | None) -> None:
    """Replace the cached row (after a load or a save)."""
    global _row, _loaded_at
    _row = dict(row or {})
    _loaded_at = time.monotonic()


def reset() -> None:
    """Forget the cached row (tests)."""
    global _row, _loaded_at
    _row, _loaded_at = {}, 0.0


def stale() -> bool:
    return not _loaded_at or time.monotonic() - _loaded_at > FRESH_S


def cached(field: str) -> Any:
    return _row.get(field)


def env(name: str) -> str:
    return os.getenv(name, "").strip()


# What the rest of VALR reads.

def name() -> str:
    """The full display name ("" if unknown)."""
    return (cached("display_name") or env("VALR_OWNER_NAME") or env("VALR_CANDIDATE_NAME")).strip()


def first_name(default: str = "there") -> str:
    full = name()
    return full.split()[0] if full else default


def recording_name() -> str:
    """How the owner shows up in transcripts ("" if unknown)."""
    return (cached("recording_name") or env("VALR_CANDIDATE_NAME") or cached("display_name")
            or env("VALR_OWNER_NAME")).strip()


def names() -> set[str]:
    """Every name of the owner, lower case (transcripts: who is the candidate)."""
    return {n.strip().lower() for n in (name(), recording_name(), env("VALR_OWNER_NAME"),
                                         env("VALR_CANDIDATE_NAME")) if n and n.strip()}


def pronounce() -> str:
    return (cached("name_pronunciation") or "").strip()


def zone_name() -> str:
    candidate = (cached("timezone") or env("ASSISTANT_TIMEZONE") or DEFAULT_ZONE).strip()
    return candidate if valid_zone(candidate) else DEFAULT_ZONE


def zone() -> ZoneInfo:
    return ZoneInfo(zone_name())


def phone() -> str:
    """The owner's phone in E.164, or "" when there is none (removed, or never set)."""
    if cached("phone_e164"):
        return str(cached("phone_e164"))
    if cached("phone_removed"):
        return ""
    return env("VALR_OWNER_PHONE")


def phone_source() -> str:
    """db (set in the app) | env (from VALR_OWNER_PHONE) | none."""
    if cached("phone_e164"):
        return "db"
    if not cached("phone_removed") and env("VALR_OWNER_PHONE"):
        return "env"
    return "none"


def photo_etag() -> str:
    return str(cached("photo_etag") or "")


def key() -> str:
    return KEY


def emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv("VALR_OWNER_EMAILS", "").split(",") if e.strip()}


def legacy_keys() -> set[str]:
    return {str(k) for k in (cached("legacy_keys") or [])}


def canonical(caller_key: str | None) -> str | None:
    """The owner's current phone, his sign-in emails and KEY -> KEY; anyone else unchanged.
    An old number of the owner (one he changed or removed) is not the owner any more."""
    if not caller_key:
        return caller_key
    lowered = caller_key.lower()
    current = phone()
    if lowered == KEY or (current and caller_key == f"tel:{current}") \
            or (lowered.startswith("email:") and lowered.removeprefix("email:") in emails()):
        return KEY
    return caller_key


def is_owner_key(caller_key: str | None) -> bool:
    """A key that means the owner when DIALLING (a workflow's input): KEY or a key from before
    the stable key. Inbound recognition uses `canonical`, never this."""
    return bool(caller_key) and (caller_key == KEY or caller_key in legacy_keys())


def dial_number(caller_key: str | None) -> str | None:
    """The number to ring for this caller key: the owner's current phone for an owner key
    (None when he has none), else the key's own "tel:" number."""
    if is_owner_key(caller_key):
        return phone() or None
    if caller_key and caller_key.startswith("tel:"):
        return caller_key.removeprefix("tel:")
    return None


# Validation (pure).

class InvalidInput(ValueError):
    """A message for the owner, safe to show."""


def valid_zone(text: str) -> bool:
    if not text or len(text) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", text):
        return False
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def zones() -> list[str]:
    """IANA zones for the picker: Region/City only, sorted."""
    return sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))


def clean_name(text: str, label: str = "Name") -> str:
    value = " ".join((text or "").split())
    if not value:
        raise InvalidInput(f"{label} can't be empty.")
    if len(value) > NAME_MAX:
        raise InvalidInput(f"{label} is at most {NAME_MAX} characters.")
    if re.search(r"[<>{}\[\]\\@/:;=_|#$%^*~`\d]", value):
        raise InvalidInput(f"{label} can only have letters, spaces, hyphens, dots and apostrophes.")
    return value


def clean_pronunciation(text: str) -> str:
    value = " ".join((text or "").split())
    if not value:
        return ""
    if len(value) > NAME_MAX:
        raise InvalidInput(f"Pronunciation is at most {NAME_MAX} characters.")
    if not re.fullmatch(r"[^\W\d_]+(?:[ '\-.][^\W\d_]+)*\.?", value):
        raise InvalidInput("Pronunciation can only have letters, spaces and hyphens, like “Chi-rag”.")
    return value


# Phone numbers (pure; no libphonenumber: India strictly, others by length and a few rules).

class PhoneError(InvalidInput):
    pass


TWO_DIGIT_CC = {"20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47",
                "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65",
                "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95", "98"}
US_NOT_PERSONAL = {"800", "833", "844", "855", "866", "877", "888", "900", "976"}  # Toll-free, premium.


def country_code(e164: str) -> str:
    digits = e164.lstrip("+")
    if digits[:1] in ("1", "7"):
        return digits[:1]
    if digits[:2] in TWO_DIGIT_CC:
        return digits[:2]
    return digits[:3]


def valr_numbers() -> set[str]:
    return {re.sub(r"[^\d+]", "", os.getenv(n, "")) for n in ("VALR_PUBLIC_NUMBER", "TELNYX_FROM_NUMBER")} - {""}


def normalize_phone(raw: str) -> str:
    """The owner's input -> E.164, or PhoneError with what to fix. A bare 10-digit number
    starting 6-9 is Indian; a leading 0 before an Indian mobile is dropped."""
    text = re.sub(r"[\s().\-]", "", raw or "")
    if not text:
        raise PhoneError("Enter your mobile number.")
    if text.startswith("00"):
        text = "+" + text[2:]
    if not text.startswith("+"):
        national = text[1:] if len(text) == 11 and text.startswith("0") else text
        if re.fullmatch(r"[6-9]\d{9}", national):
            text = "+91" + national
        else:
            raise PhoneError("Add the country code, like +91 98765 43210.")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", text):
        raise PhoneError("That doesn't look like a phone number: + and the country code, then 8 to 15 digits.")
    cc = country_code(text)
    national = text[1 + len(cc):]
    if cc == "91":
        national = national[1:] if len(national) == 11 and national.startswith("0") else national
        if not re.fullmatch(r"\d{10}", national):
            raise PhoneError("An Indian mobile number has 10 digits after +91.")
        if national[0] not in "6789":
            raise PhoneError("Use your mobile number: VALR calls you on the go.")
        text = "+91" + national
    elif cc == "1":
        if not re.fullmatch(r"[2-9]\d{2}[2-9]\d{6}", national):
            raise PhoneError("A US or Canadian number has 10 digits after +1.")
        if national[:3] in US_NOT_PERSONAL:
            raise PhoneError("Use your own mobile number, not a toll-free or premium one.")
    elif cc == "44":
        if not re.fullmatch(r"\d{10}", national):
            raise PhoneError("A UK number has 10 digits after +44 (drop the leading 0).")
        if national[0] in "89":
            raise PhoneError("Use your own mobile number, not a freephone, shared-cost or premium one.")
    elif len(national) < 6:
        raise PhoneError("That number is too short.")
    if text in valr_numbers():
        raise PhoneError("That's VALR's own number. Enter yours.")
    return text


def mask(e164: str) -> str:
    """+919876543210 -> "+91 ••••• •3210": the country code and the last 4 digits only."""
    if not e164 or not e164.startswith("+") or len(e164) < 8:
        return ""
    cc = country_code(e164)
    national = e164[1 + len(cc):]
    hidden = max(len(national) - 4, 1)
    return f"+{cc} {'•' * (hidden - 1)} •{national[-4:]}".replace("  ", " ")


# Photos (pure checks; the browser crops to a square and re-encodes, which drops EXIF).

def photo_type(data: bytes) -> str | None:
    for magic, kind in PHOTO_TYPES.items():
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def etag_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# The store.

async def setup(connection: Any) -> None:
    """The table and its row; once, the move of caller memory to the stable key."""
    await connection.execute(SCHEMA)
    cursor = await connection.execute("SELECT migrated_at FROM owner_profile WHERE id = 1")
    row = await cursor.fetchone()
    if row and row[0] is None:
        await migrate(connection)


def old_keys() -> list[str]:
    keys = [f"email:{e}" for e in sorted(emails())]
    if env("VALR_OWNER_PHONE"):
        keys.insert(0, f"tel:{env('VALR_OWNER_PHONE')}")
    return keys


async def migrate(connection: Any) -> None:
    """Merge the caller memory of the owner's old keys into KEY (one transaction; a second
    process finds migrated_at set and does nothing)."""
    keys = old_keys()
    async with connection.transaction():
        cursor = await connection.execute(
            "SELECT migrated_at FROM owner_profile WHERE id = 1 FOR UPDATE")
        row = await cursor.fetchone()
        if not row or row[0] is not None:
            return
        cursor = await connection.execute("SELECT to_regclass('callers') IS NOT NULL, "
                                          "to_regclass('call_summaries') IS NOT NULL")
        has_callers, has_summaries = await cursor.fetchone()
        if keys and has_summaries:
            await connection.execute("UPDATE call_summaries SET caller_key = %s WHERE caller_key = ANY(%s)",
                                     (KEY, keys))
        if keys and has_callers:
            await connection.execute(
                "INSERT INTO callers (caller_key, display_name, first_seen, last_seen, call_count, profile, open_items) "
                "SELECT %s, (array_agg(display_name ORDER BY last_seen DESC) FILTER (WHERE display_name IS NOT NULL))[1], "
                "min(first_seen), max(last_seen), "
                "sum(call_count), (array_agg(profile ORDER BY last_seen DESC))[1], "
                "(array_agg(open_items ORDER BY last_seen DESC))[1] FROM callers WHERE caller_key = ANY(%s) "
                "HAVING count(*) > 0 ON CONFLICT (caller_key) DO NOTHING", (KEY, keys))
            await connection.execute("DELETE FROM callers WHERE caller_key = ANY(%s)", (keys,))
        await connection.execute(
            "UPDATE owner_profile SET legacy_keys = %s, migrated_at = now() WHERE id = 1", (keys,))
    logger.info("VALR owner key: caller memory moved to the stable key (%d old keys)", len(keys))


async def load(connection: Any) -> dict[str, Any]:
    cursor = await connection.execute(f"SELECT {', '.join(FIELDS)} FROM owner_profile WHERE id = 1")
    row = await cursor.fetchone()
    data = dict(zip(FIELDS, row)) if row else {}
    apply(data)
    return data


async def refresh(connection: Any = None, force: bool = False) -> None:
    """Reload the cache when it is older than a minute (or `force`). Never raises: on a
    database problem the last values (or the environment) stay."""
    if not force and not stale():
        return
    try:
        if connection is not None:
            await setup(connection)
            await load(connection)
            return
        from app import research
        if research.pool is not None:
            async with research.pool.connection() as pooled:
                await setup(pooled)
                await load(pooled)
            return
        import psycopg
        from app import research_jobs
        async with await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True,
                                                         connect_timeout=5) as own:
            await setup(own)
            await load(own)
    except Exception as error:  # noqa: BLE001 - keep the last values
        logger.warning("VALR owner profile unavailable: %s", type(error).__name__)


async def keep_fresh(stop: asyncio.Event | None = None, every: float = FRESH_S) -> None:
    """A worker's background task: reload the owner's profile every minute."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        await refresh(force=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass


async def save(connection: Any, **values: Any) -> dict[str, Any]:
    """Update columns of the one row, then the cache. -> the new row."""
    allowed = {"display_name", "recording_name", "name_pronunciation", "timezone", "photo", "photo_type",
               "photo_etag", "phone_e164", "phone_removed", "phone_updated_at"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown owner_profile columns: {sorted(unknown)}")
    await setup(connection)
    if values:
        sets = ", ".join(f"{k} = %s" for k in values)
        await connection.execute(f"UPDATE owner_profile SET {sets}, updated_at = now() WHERE id = 1",
                                 tuple(values.values()))
    return await load(connection)


async def set_phone(connection: Any, e164: str) -> dict[str, Any]:
    return await save(connection, phone_e164=e164, phone_removed=False, phone_updated_at=datetime.now(timezone.utc))


async def remove_phone(connection: Any) -> dict[str, Any]:
    return await save(connection, phone_e164=None, phone_removed=True, phone_updated_at=datetime.now(timezone.utc))


async def photo(connection: Any) -> tuple[bytes, str, str] | None:
    """(bytes, content type, etag) of the uploaded photo, or None."""
    cursor = await connection.execute("SELECT photo, photo_type, photo_etag FROM owner_profile WHERE id = 1")
    row = await cursor.fetchone()
    if not row or row[0] is None:
        return None
    return bytes(row[0]), str(row[1] or "image/webp"), str(row[2] or "")


def view() -> dict[str, Any]:
    """The profile as the API and the pages show it (never the full phone number)."""
    number = phone()
    return {"display_name": name(), "recording_name": recording_name(), "name_pronunciation": pronounce(),
            "timezone": zone_name(), "photo": bool(photo_etag()), "photo_etag": photo_etag(),
            "phone": {"masked": mask(number), "set": bool(number), "source": phone_source(),
                      "country": ("+" + country_code(number)) if number else ""},
            "defaults": {"display_name": bool(cached("display_name")), "timezone": bool(cached("timezone"))}}
