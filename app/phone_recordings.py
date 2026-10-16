"""Phone call recordings (Truecaller on the iPhone) -> VALR's past interviews.

The path: Truecaller records the call -> its Share button -> the iOS Shortcut "Send to VALR"
-> `POST /hooks/recording-upload` (app/phone_web.py) -> stored encrypted here -> the research
worker transcribes it with Deepgram (pre-recorded, nova-3, diarized) -> the notetaker pipeline
as provider "phone" (app/notetakers/pipeline.py: Past interviews, the coach's evaluation and
the post-call Telegram report, exactly like a Fathom recording).

Storage (Postgres, so the app container needs no writable disk):
- `phone_uploads`: one row per upload: when, size, the audio kind, the optional title and other
  party's name the Shortcut sent, status (uploaded | processing | done | failed), the
  recording id it became, and when its audio is deleted.
- `phone_audio`: the audio in CHUNK-byte pieces, each a Fernet token (VALR_TOKEN_KEY), so it
  streams in and out (Range requests) without holding a whole file in memory. Deleted
  VALR_PHONE_AUDIO_DAYS (7) days after processing (or after a final failure).
- Re-sends are safe: each upload's SHA-256 is kept (`sha256`), and a file VALR already has
  (waiting, processing or done) is answered {ok, duplicate, id} and not stored again. A failed
  one can be sent again.
- The file name is a hint (`from_filename`): Truecaller names often hold the contact and the
  date. The date and time become `recorded_at`, a name becomes the other party; digits are
  never kept, so a phone number in the name is never stored or shown.
- `phone_upload_token`: the Shortcut's bearer token, as a SHA-256 hash only. VALR_UPLOAD_TOKEN
  is the first seed; Settings can show it once, and rotate it (the new one is shown once).

Evaluation: the worker hands each processed call to the coach (the default hook queues it on
the worker's own connection: the worker image has no groq/fastapi, so nothing here may import
app.research). A hand-off that failed is retried once at each worker start, or by hand:
`python -m app.phone_recordings reevaluate <upload id|--all-unevaluated>` (worker container).

Nothing here logs a name, a title, a token or transcript text.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

import httpx

from app import google_oauth

logger = logging.getLogger("valr")

PROVIDER = "phone"
CHUNK = 1024 * 1024  # Plaintext bytes per stored piece.
MAX_TRIES = 3
KINDS = {"m4a": "audio/mp4", "mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac"}
EXTENSIONS = {".m4a": "m4a", ".mp4": "m4a", ".mp3": "mp3", ".wav": "wav", ".aac": "aac"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_uploads (
    id            text PRIMARY KEY,
    received_at   timestamptz NOT NULL DEFAULT now(),
    recorded_at   timestamptz,                 -- the call's start, when the Shortcut sent it
    size          bigint NOT NULL DEFAULT 0,
    kind          text NOT NULL DEFAULT '',    -- m4a | mp3 | wav | aac
    title         text NOT NULL DEFAULT '',
    other_name    text NOT NULL DEFAULT '',
    status        text NOT NULL DEFAULT 'receiving', -- receiving | uploaded | processing | done | failed
    tries         int NOT NULL DEFAULT 0,
    error         text,
    duration_s    double precision,
    meeting       text,                        -- Fernet ciphertext of the normalised meeting (transcript)
    processed_at  timestamptz,
    delete_after  timestamptz,
    audio_deleted_at timestamptz
);
CREATE TABLE IF NOT EXISTS phone_audio (
    upload_id     text NOT NULL,
    idx           int NOT NULL,
    data          bytea NOT NULL,              -- a Fernet token of CHUNK plaintext bytes (the last: fewer)
    PRIMARY KEY (upload_id, idx)
);
CREATE TABLE IF NOT EXISTS phone_upload_token (
    id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    token_hash    text NOT NULL,
    source        text NOT NULL,               -- env | generated | rotated
    created_at    timestamptz NOT NULL DEFAULT now(),
    revealed_at   timestamptz
);
-- Duplicate detection (re-sends from the batch Shortcut). Older rows keep NULL.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'phone_uploads' AND column_name = 'sha256') THEN
    ALTER TABLE phone_uploads ADD COLUMN sha256 text;
    CREATE UNIQUE INDEX phone_uploads_by_sha256 ON phone_uploads (sha256) WHERE sha256 IS NOT NULL;
  END IF;
END $$;
"""
UPLOAD_FIELDS = ("id", "received_at", "recorded_at", "size", "kind", "title", "other_name", "status", "tries",
                 "error", "duration_s", "meeting", "processed_at", "delete_after", "audio_deleted_at")


# Settings.

def token_key() -> str:
    return os.getenv("VALR_TOKEN_KEY", "").strip()


def max_bytes() -> int:
    return int(float(os.getenv("VALR_PHONE_MAX_MB", "100")) * 1024 * 1024)


def audio_days() -> float:
    return float(os.getenv("VALR_PHONE_AUDIO_DAYS", "7"))


def enabled() -> bool:
    return os.getenv("VALR_PHONE_RECORDINGS", "1") == "1"


@dataclass(frozen=True)
class DeepgramPrerecorded:
    api_key: str
    host: str = "api.deepgram.com"
    model: str = "nova-3"
    language: str = "en-IN"

    @classmethod
    def from_env(cls) -> "DeepgramPrerecorded":
        return cls(os.environ["DEEPGRAM_API_KEY"], os.getenv("VALR_PHONE_DEEPGRAM_HOST", cls.host).strip(),
                   os.getenv("VALR_PHONE_MODEL", cls.model).strip(), os.getenv("VALR_PHONE_LANGUAGE", cls.language).strip())

    def url(self) -> str:
        query = {"model": self.model, "language": self.language, "diarize": "true", "smart_format": "true",
                 "punctuate": "true", "utterances": "true"}
        return f"https://{self.host}/v1/listen?" + "&".join(f"{k}={v}" for k, v in query.items())


# The upload token (hashed).

def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


async def token_row(connection: Any) -> dict[str, Any] | None:
    cursor = await connection.execute(
        "SELECT token_hash, source, created_at, revealed_at FROM phone_upload_token WHERE id = 1")
    row = await cursor.fetchone()
    return dict(zip(("token_hash", "source", "created_at", "revealed_at"), row)) if row else None


async def current_hash(connection: Any) -> str | None:
    row = await token_row(connection)
    if row:
        return row["token_hash"]
    seed = os.getenv("VALR_UPLOAD_TOKEN", "").strip()
    return digest(seed) if seed else None


def token_matches(bearer: str, expected_hash: str | None) -> bool:
    """Constant time; a missing token or no configured token never matches."""
    if not bearer or not expected_hash:
        return False
    return hmac.compare_digest(digest(bearer), expected_hash)


async def save_token(connection: Any, token: str, source: str, revealed: bool) -> None:
    await connection.execute(
        "INSERT INTO phone_upload_token (id, token_hash, source, created_at, revealed_at) "
        "VALUES (1, %s, %s, now(), CASE WHEN %s THEN now() END) ON CONFLICT (id) DO UPDATE SET "
        "token_hash = excluded.token_hash, source = excluded.source, created_at = now(), "
        "revealed_at = excluded.revealed_at", (digest(token), source, revealed))


async def mark_revealed(connection: Any) -> None:
    await connection.execute("UPDATE phone_upload_token SET revealed_at = now() WHERE id = 1")


class AlreadyShown(Exception):
    """The token was shown once already: only a rotation shows a new one."""


async def reveal(connection: Any) -> str:
    """'Show upload token': the env seed once, or a new token when there is none at all."""
    row = await token_row(connection)
    seed = os.getenv("VALR_UPLOAD_TOKEN", "").strip()
    if row is None:
        token = seed or new_token()
        await save_token(connection, token, "env" if seed else "generated", True)
        return token
    if row["revealed_at"] is None and row["source"] == "env" and seed and hmac.compare_digest(digest(seed), row["token_hash"]):
        await mark_revealed(connection)
        return seed
    raise AlreadyShown


async def rotate(connection: Any) -> str:
    token = new_token()
    await save_token(connection, token, "rotated", True)
    return token


# Audio type (pure).

def sniff(head: bytes) -> str | None:
    """m4a | mp3 | wav | aac from the first bytes, or None."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF6 == 0xF0:
        return "aac"  # ADTS
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "mp3"  # An MPEG audio frame
    return None


def kind_of(head: bytes, filename: str = "", content_type: str = "") -> str | None:
    """The sniffed kind; the name and type only have to not contradict it being audio."""
    found = sniff(head)
    if found is None:
        return None
    ext = os.path.splitext(filename.lower())[1]
    if ext and ext not in EXTENSIONS:
        return None
    if content_type and not content_type.lower().startswith(("audio/", "video/mp4", "application/octet-stream")):
        return None
    return found


# Encryption in CHUNK pieces (pure).

def seal_chunk(key: str, data: bytes) -> bytes:
    return google_oauth.fernet(key).encrypt(data)


def open_chunk(key: str, token: bytes) -> bytes:
    return google_oauth.fernet(key).decrypt(bytes(token))


class Chunker:
    """Collects bytes and hands back full CHUNK-size pieces; `rest()` the last one."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer += data
        out = []
        while len(self.buffer) >= CHUNK:
            out.append(bytes(self.buffer[:CHUNK]))
            del self.buffer[:CHUNK]
        return out

    def rest(self) -> bytes:
        out, self.buffer = bytes(self.buffer), bytearray()
        return out


# Multipart, streamed (pure): the form the Shortcut posts, without holding it all in memory.

class MultipartError(ValueError):
    pass


def boundary_of(content_type: str) -> bytes:
    match = re.search(r'boundary="?([^";]{1,200})"?', content_type or "", re.I)
    if not content_type.lower().startswith("multipart/form-data") or not match:
        raise MultipartError("Expected multipart/form-data.")
    return match.group(1).encode()


class MultipartParser:
    """feed(bytes) -> events: ("part", {"name", "filename", "content_type"}), ("data", bytes), ("end", None).
    Bytes may arrive split anywhere; part headers are limited to 8 KB."""

    def __init__(self, boundary: bytes) -> None:
        self.delimiter = b"\r\n--" + boundary
        self.buffer = bytearray(b"\r\n")  # So the first boundary looks like every other one.
        self.state = "preamble"
        self.done = False

    def feed(self, data: bytes) -> list[tuple[str, Any]]:
        self.buffer += data
        events: list[tuple[str, Any]] = []
        while True:
            if self.state in ("preamble", "body"):
                at = self.buffer.find(self.delimiter)
                if at < 0:
                    keep = len(self.delimiter) - 1
                    if self.state == "body" and len(self.buffer) > keep:
                        events.append(("data", bytes(self.buffer[:-keep])))
                        del self.buffer[:-keep]
                    elif self.state == "preamble" and len(self.buffer) > keep:
                        del self.buffer[:-keep]
                    return events
                if self.state == "body":
                    if at:
                        events.append(("data", bytes(self.buffer[:at])))
                    events.append(("end", None))
                del self.buffer[:at + len(self.delimiter)]
                self.state = "after"
            elif self.state == "after":
                if len(self.buffer) < 2:
                    return events
                if self.buffer[:2] == b"--":
                    self.done, self.state = True, "epilogue"
                    self.buffer.clear()
                    return events
                if self.buffer[:2] != b"\r\n":
                    raise MultipartError("Malformed multipart body.")
                del self.buffer[:2]
                self.state = "headers"
            elif self.state == "headers":
                at = self.buffer.find(b"\r\n\r\n")
                if at < 0:
                    if len(self.buffer) > 8192:
                        raise MultipartError("Part headers too long.")
                    return events
                events.append(("part", self.headers(bytes(self.buffer[:at]))))
                del self.buffer[:at + 4]
                self.state = "body"
            else:  # epilogue
                self.buffer.clear()
                return events

    @staticmethod
    def headers(raw: bytes) -> dict[str, str]:
        out = {"name": "", "filename": "", "content_type": ""}
        for line in raw.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key == "content-disposition":
                for name in ("name", "filename"):
                    match = re.search(rf'(?:^|;)\s*{name}="([^"]*)"', value)
                    if match:
                        out[name] = match.group(1)
            elif key == "content-type":
                out["content_type"] = value.strip()
        return out


def clean_text(value: str, limit: int) -> str:
    return " ".join("".join(ch for ch in value if ch.isprintable()).split())[:limit]


PHONE_LIKE = re.compile(r"\+?\d[\d\s().\-_]{5,}\d")
FILLER = re.compile(r"\b(call|calls|recording|recordings|recorded|rec|truecaller|audio|voice|incoming|outgoing|"
                    r"with|from|to|at|on|am|pm|in|out)\b", re.I)
DATE_TIME = re.compile(
    r"(?<![\d+])(?:(?P<y>20\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)|(?P<d2>[0-3]\d)[-_.](?P<m2>[01]\d)[-_.](?P<y2>20\d{2}))"
    r"(?:[\sT_\-.]*(?:at[\s_]*)?(?P<H>[0-2]\d)[-_.:h]?(?P<M>[0-5]\d)(?:[-_.:m]?(?P<S>[0-5]\d))?)?(?!\d)", re.I)


def strip_phones(text: str) -> str:
    """No phone numbers: anything number-like goes."""
    return " ".join(PHONE_LIKE.sub(" ", text or "").split())


def from_filename(filename: str, zone: str = "") -> tuple[str, datetime | None]:
    """'Riya Sen 2026-10-17 14-30-12.m4a' -> ('Riya Sen', 2026-10-17 14:30:12 local). A name made
    only of digits (a phone number) becomes ''. Times are local (ASSISTANT_TIMEZONE)."""
    from zoneinfo import ZoneInfo
    stem = os.path.splitext(os.path.basename(filename or ""))[0][:200]
    when = None
    match = DATE_TIME.search(stem)
    if match:
        g = match.groupdict()
        try:
            local = datetime(int(g["y"] or g["y2"]), int(g["m"] or g["m2"]), int(g["d"] or g["d2"]),
                             int(g["H"] or 0), int(g["M"] or 0), int(g["S"] or 0))
            when = local.replace(tzinfo=ZoneInfo(zone or os.getenv("ASSISTANT_TIMEZONE", "Asia/Kolkata")))
            stem = stem[:match.start()] + " " + stem[match.end():]
        except ValueError:
            when = None
    name = re.sub(r"[\d+]", " ", strip_phones(stem))       # No digits at all survive.
    name = FILLER.sub(" ", re.sub(r"[_\-.()\[\],#]+", " ", name))
    name = " ".join(name.split())
    return (clean_text(name, 80) if re.search(r"[^\W\d_]{2}", name) else ""), when


def parse_when(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# The store.

async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


def new_id() -> str:
    return secrets.token_hex(8)


async def begin_upload(connection: Any, upload_id: str) -> None:
    await connection.execute("INSERT INTO phone_uploads (id, status) VALUES (%s, 'receiving')", (upload_id,))


async def put_chunk(connection: Any, upload_id: str, idx: int, sealed: bytes) -> None:
    await connection.execute("INSERT INTO phone_audio (upload_id, idx, data) VALUES (%s, %s, %s)",
                             (upload_id, idx, sealed))


async def finish_upload(connection: Any, upload_id: str, size: int, kind: str, title: str, other_name: str,
                        recorded_at: datetime | None, sha256: str | None = None) -> None:
    await connection.execute(
        "UPDATE phone_uploads SET status = 'uploaded', size = %s, kind = %s, title = %s, other_name = %s, "
        "recorded_at = %s, sha256 = %s WHERE id = %s", (size, kind, title, other_name, recorded_at, sha256, upload_id))


async def find_duplicate(connection: Any, sha256: str, upload_id: str) -> str | None:
    """An earlier upload of the same bytes that is waiting, processing or done."""
    cursor = await connection.execute(
        "SELECT id FROM phone_uploads WHERE sha256 = %s AND id <> %s AND status IN ('uploaded', 'processing', 'done') "
        "LIMIT 1", (sha256, upload_id))
    row = await cursor.fetchone()
    return row[0] if row else None


async def release_failed(connection: Any, sha256: str) -> None:
    """A failed earlier copy gives up its hash, so this re-send is processed."""
    await connection.execute("UPDATE phone_uploads SET sha256 = NULL WHERE sha256 = %s AND status = 'failed'",
                             (sha256,))


async def drop_upload(connection: Any, upload_id: str) -> None:
    await connection.execute("DELETE FROM phone_audio WHERE upload_id = %s", (upload_id,))
    await connection.execute("DELETE FROM phone_uploads WHERE id = %s", (upload_id,))


async def upload(connection: Any, upload_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(f"SELECT {', '.join(UPLOAD_FIELDS)} FROM phone_uploads WHERE id = %s",
                                      (upload_id,))
    row = await cursor.fetchone()
    return dict(zip(UPLOAD_FIELDS, row)) if row else None


async def chunk(connection: Any, upload_id: str, idx: int) -> bytes | None:
    cursor = await connection.execute("SELECT data FROM phone_audio WHERE upload_id = %s AND idx = %s",
                                      (upload_id, idx))
    row = await cursor.fetchone()
    return bytes(row[0]) if row else None


async def claim(connection: Any) -> dict[str, Any] | None:
    """The oldest upload waiting to be processed, marked processing (one worker at a time)."""
    cursor = await connection.execute(
        "UPDATE phone_uploads SET status = 'processing', tries = tries + 1 WHERE id = ("
        "SELECT id FROM phone_uploads WHERE status = 'uploaded' ORDER BY received_at LIMIT 1 "
        "FOR UPDATE SKIP LOCKED) RETURNING " + ", ".join(UPLOAD_FIELDS))
    row = await cursor.fetchone()
    return dict(zip(UPLOAD_FIELDS, row)) if row else None


async def mark(connection: Any, upload_id: str, **values: Any) -> None:
    sets = ", ".join(f"{k} = %s" for k in values)
    await connection.execute(f"UPDATE phone_uploads SET {sets} WHERE id = %s", (*values.values(), upload_id))


async def stuck(connection: Any, older: datetime) -> None:
    """A worker that died mid-way: processing for over an hour goes back to the queue."""
    await connection.execute("UPDATE phone_uploads SET status = CASE WHEN tries < %s THEN 'uploaded' "
                             "ELSE 'failed' END WHERE status = 'processing' AND received_at < %s", (MAX_TRIES, older))


async def purge(connection: Any, now: datetime) -> int:
    cursor = await connection.execute(
        "UPDATE phone_uploads SET audio_deleted_at = %s WHERE delete_after < %s AND audio_deleted_at IS NULL "
        "RETURNING id", (now, now))
    ids = [r[0] for r in await cursor.fetchall()]
    if ids:
        await connection.execute("DELETE FROM phone_audio WHERE upload_id = ANY(%s)", (ids,))
    # Uploads that never finished (the phone lost signal) after a day.
    cursor = await connection.execute("SELECT id FROM phone_uploads WHERE status = 'receiving' AND received_at < %s",
                                      (now - timedelta(days=1),))
    for (upload_id,) in await cursor.fetchall():
        await drop_upload(connection, upload_id)
    return len(ids)


async def status(connection: Any) -> dict[str, Any]:
    """For the Settings card: is a token set, the last upload and how it went."""
    cursor = await connection.execute(
        "SELECT received_at, status, processed_at FROM phone_uploads WHERE status <> 'receiving' "
        "ORDER BY received_at DESC LIMIT 1")
    last = await cursor.fetchone()
    cursor = await connection.execute("SELECT count(*) FROM phone_uploads WHERE status IN ('uploaded', 'processing')")
    waiting = int((await cursor.fetchone())[0])
    row = await token_row(connection)
    return {"token": bool(row) or bool(os.getenv("VALR_UPLOAD_TOKEN", "").strip()),
            "token_shown": bool(row and row["revealed_at"]),
            "last_upload_at": last[0].astimezone(timezone.utc).isoformat() if last else None,
            "last_status": last[1] if last else None, "waiting": waiting, "audio_days": audio_days()}


async def sealed_meeting(connection: Any, upload_id: str) -> dict | None:
    row = await upload(connection, upload_id)
    if not row or not row.get("meeting") or not token_key():
        return None
    try:
        return json.loads(google_oauth.decrypt(token_key(), row["meeting"]))
    except (google_oauth.ReconnectNeeded, ValueError):
        return None


# Reading the audio back.

def byte_range(header: str, size: int) -> tuple[int, int] | None:
    """'bytes=start-end' -> (start, end inclusive); None for a missing or unusable header."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not match or size <= 0 or match.group(1) == match.group(2) == "":
        return None
    if match.group(1) == "":
        length = int(match.group(2))
        return (max(0, size - length), size - 1) if length else None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable")
    return start, min(end, size - 1)


async def read_range(get_chunk: Callable[[int], Any], key: str, start: int, end: int) -> AsyncIterator[bytes]:
    """The plaintext bytes start..end (inclusive), decrypting only the pieces they touch."""
    for idx in range(start // CHUNK, end // CHUNK + 1):
        sealed = await get_chunk(idx)
        if sealed is None:
            return
        plain = open_chunk(key, sealed)
        lo = start - idx * CHUNK if idx == start // CHUNK else 0
        hi = end - idx * CHUNK + 1 if idx == end // CHUNK else len(plain)
        yield plain[lo:hi]


# Transcription and the meeting (pure parts first).

def first_name(name: str) -> str:
    return (name or "").split()[0].lower() if (name or "").split() else ""


def owner_speaker(utterances: list[dict[str, Any]], candidate: str) -> int | None:
    """Which diarized speaker is the owner: the one who introduces himself by his first name
    ("this is Sam"), else the one the others greet by it ("hi Sam"), else the one who
    talks most (an interview is mostly the candidate talking)."""
    speakers = sorted({int(u.get("speaker") or 0) for u in utterances})
    if not speakers:
        return None
    first = first_name(candidate)
    if first:
        own = re.compile(rf"\b(this is|i am|i'm|my name is|it's|myself)\s+{re.escape(first)}\b", re.I)
        greet = re.compile(rf"\b(hi|hello|hey|thanks|thank you|dear|am i speaking (to|with)|is this)\s*,?\s+{re.escape(first)}\b", re.I)
        for u in utterances:
            if own.search(u.get("transcript") or ""):
                return int(u.get("speaker") or 0)
        for u in utterances:
            if greet.search(u.get("transcript") or "") and len(speakers) == 2:
                return next(s for s in speakers if s != int(u.get("speaker") or 0))
    words: dict[int, int] = {}
    for u in utterances:
        words[int(u.get("speaker") or 0)] = words.get(int(u.get("speaker") or 0), 0) + len((u.get("transcript") or "").split())
    return max(words, key=words.get) if words else speakers[0]


def meeting_from(result: dict[str, Any], upload_id: str, title: str, other_name: str, start: datetime,
                 candidate: str) -> dict[str, Any]:
    """Deepgram's answer -> the profile importer's normalised meeting (source "phone")."""
    utterances = [u for u in ((result.get("results") or {}).get("utterances") or []) if (u.get("transcript") or "").strip()]
    duration = float((result.get("metadata") or {}).get("duration") or (utterances[-1]["end"] if utterances else 0) or 0)
    me = owner_speaker(utterances, candidate)
    others = sorted({int(u.get("speaker") or 0) for u in utterances} - {me})
    label = {}
    for n, speaker in enumerate(others, 1):
        base_name = other_name or "Interviewer"
        label[speaker] = base_name if len(others) == 1 else f"{base_name} ({n})" if other_name else f"Interviewer {n}"
    label[me] = candidate or "Me"
    turns = [{"speaker": label.get(int(u.get("speaker") or 0), "Someone"), "text": " ".join(u["transcript"].split()),
              "start": round(float(u.get("start") or 0), 2)} for u in utterances]
    return {"source": PROVIDER, "id": upload_id, "title": title or (f"Phone call with {other_name}" if other_name else "Phone call"),
            "date": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "duration": duration,
            "scheduled_duration": None, "participants": sorted(set(label.values())), "turns": turns,
            "vendor_summary": None}


async def transcribe(http: httpx.AsyncClient, settings: DeepgramPrerecorded, body: AsyncIterator[bytes], size: int,
                     content_type: str) -> dict[str, Any]:
    """Deepgram pre-recorded: the audio is streamed in, decrypted piece by piece."""
    response = await http.post(settings.url(), content=body, timeout=httpx.Timeout(900, connect=20),
                               headers={"Authorization": f"Token {settings.api_key}", "Content-Type": content_type,
                                        "Content-Length": str(size)})
    if response.status_code >= 400:
        raise RuntimeError(f"Deepgram returned HTTP {response.status_code}")
    return response.json()


# Processing (the research worker).

@dataclass
class Processor:
    """One upload -> a transcript -> a "phone" recording in the notetaker pipeline."""
    http: httpx.AsyncClient | None = None
    deepgram: DeepgramPrerecorded | None = None
    resolver: Any = None          # app/company_resolver.Resolver; None: the worker's default
    hook: Any = None              # The evaluation hook; None: VALR_EVAL_HOOK's
    match: Any = None             # recording -> calendar event ref; None: app.notetakers.match_event
    candidate: str = ""
    done: list[str] = field(default_factory=list)

    async def run_once(self, connection: Any, now: datetime | None = None) -> str | None:
        """Process one waiting upload. -> its id, or None when nothing waits."""
        now = now or datetime.now(timezone.utc)
        await setup(connection)
        await stuck(connection, now - timedelta(hours=1))
        row = await claim(connection)
        if row is None:
            return None
        try:
            await self.process(connection, row, now)
        except Exception as error:  # noqa: BLE001 - retried up to MAX_TRIES; only the class is kept
            final = row["tries"] >= MAX_TRIES
            await mark(connection, row["id"], status="failed" if final else "uploaded", error=type(error).__name__,
                       delete_after=now + timedelta(days=audio_days()) if final else None)
            logger.warning("VALR phone recording failed (%s, try %d)", type(error).__name__, row["tries"])
            return None  # The next pass retries it, not this one.
        self.done.append(row["id"])
        return row["id"]

    async def process(self, connection: Any, row: dict[str, Any], now: datetime) -> None:
        from app.notetakers import pipeline
        from app.notetakers.base import Recording
        key = token_key()
        if not key:
            raise RuntimeError("VALR_TOKEN_KEY is not set")
        settings = self.deepgram or DeepgramPrerecorded.from_env()
        upload_id = row["id"]

        async def get(idx: int) -> bytes | None:
            return await chunk(connection, upload_id, idx)

        own = self.http is None
        http = self.http or httpx.AsyncClient()
        try:
            result = await transcribe(http, settings, read_range(get, key, 0, row["size"] - 1), row["size"],
                                      KINDS.get(row["kind"], "application/octet-stream"))
        finally:
            if own:
                await http.aclose()
        candidate = self.candidate or candidate_name()
        duration = float((result.get("metadata") or {}).get("duration") or 0)
        start = row.get("recorded_at") or (row["received_at"] - timedelta(seconds=duration))
        meeting = meeting_from(result, upload_id, row.get("title") or "", row.get("other_name") or "", start, candidate)
        if not meeting["turns"]:
            raise RuntimeError("Deepgram heard no speech")
        await mark(connection, upload_id, meeting=google_oauth.encrypt(key, json.dumps(meeting)), duration_s=duration)
        recording = Recording(PROVIDER, upload_id, meeting["title"], meeting["date"],
                              (start + timedelta(seconds=duration)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              interviewers=[row["other_name"]] if row.get("other_name") else [], meeting=meeting)
        match = self.match or (lambda r: default_match(connection, r))
        event = await match(recording)
        company, how = await self.company(connection, recording, meeting, event, row.get("other_name") or "",
                                          candidate)
        recording.company = company
        await pipeline.intake(connection, recording, "upload", hook=self.hook or default_hook(connection),
                              match=lambda r: _same(event))
        if how:
            await set_company_how(connection, upload_id, how)
        await mark(connection, upload_id, status="done", processed_at=now, error=None,
                   delete_after=now + timedelta(days=audio_days()))
        logger.info("VALR phone recording processed (%d turns, %.0f s)", len(meeting["turns"]), duration)
        try:
            from app import notifications
            store = notifications.Store(connection)
            await store.setup()
            await notifications.from_recording(store, PROVIDER, upload_id, company, row.get("other_name") or "", now)
        except Exception as error:  # noqa: BLE001 - the recording is processed either way.
            logger.warning("VALR phone recording notification failed: %s", type(error).__name__)

    async def company(self, connection: Any, recording: Any, meeting: dict, event: dict | None,
                      other_name: str, candidate: str = "") -> tuple[str, str]:
        """The calendar event's company, else a lead with this person, else the resolver on the
        call's opening (with the title and the name)."""
        from app import company_names, company_resolver
        if event and event.get("company") and company_names.display(event["company"]):
            return company_names.display(event["company"]), "the calendar event at that time"
        lead = await lead_for_person(connection, other_name, recording.start)
        if lead:
            return lead, "a lead with the same person"
        resolver = self.resolver
        if resolver is None:
            from app.lead_worker import owner_emails
            resolver = company_resolver.default_resolver(owner_emails())
        text, speakers = company_resolver.intro(meeting)
        people = [(n, "") for n in ([other_name] if other_name else []) + [s for s in speakers if s not in (candidate, other_name)]]
        ev = company_resolver.Evidence(kind="recording", title=recording.title, people=people, transcript=text)
        decision = await resolver.resolve(connection, ev, entity_id=f"interview:{PROVIDER}:{recording.external_id}")
        return decision.company, decision.how


def default_hook(connection: Any) -> Any:
    """VALR_EVAL_HOOK's hook; the default one queues on this very connection (the worker has no
    web pool, and no groq/fastapi to import one with)."""
    from app.notetakers import pipeline
    if os.getenv(pipeline.HOOK_ENV, pipeline.DEFAULT_HOOK).strip() != pipeline.DEFAULT_HOOK:
        return None  # pipeline.hand_off resolves the configured one.
    from app.profile import notetaker_hook

    async def hook(provider: str, external_id: str, meeting: dict | None) -> str:
        return await notetaker_hook.evaluate_recording(provider, external_id, meeting, connection=connection)
    return hook


UNEVALUATED = ("pending", "no_hook", "failed")


async def reevaluate(connection: Any, upload_id: str | None = None) -> list[tuple[str, str]]:
    """Hand processed phone recordings to the evaluation again: one (by id, whatever its state;
    a failed post-call report is queued again) or every one whose hand-off never succeeded.
    -> [(id, status)]. Logs ids and statuses only."""
    from app import post_call
    from app.notetakers import pipeline, store
    await store.setup(connection)
    if upload_id:
        ids = [upload_id]
    else:
        cursor = await connection.execute(
            "SELECT external_id FROM notetaker_recordings WHERE provider = %s AND eval_status = ANY(%s) "
            "ORDER BY received_at", (PROVIDER, list(UNEVALUATED)))
        ids = [r[0] for r in await cursor.fetchall()]
    results = []
    for external_id in ids:
        meeting = await sealed_meeting(connection, external_id)
        if meeting is None:
            results.append((external_id, "no_transcript"))
            continue
        if upload_id:
            await post_call.setup(connection)
            await connection.execute("DELETE FROM post_call_reports WHERE provider = %s AND external_id = %s "
                                     "AND status = 'failed'", (PROVIDER, external_id))
        status = await pipeline.hand_off(connection, PROVIDER, external_id, meeting, default_hook(connection))
        results.append((external_id, status))
    if results:
        logger.info("VALR phone recordings re-evaluated: %s", ", ".join(f"{i} {s}" for i, s in results))
    return results


async def set_company_how(connection: Any, upload_id: str, how: str) -> None:
    await connection.execute("UPDATE notetaker_recordings SET company_how = %s WHERE provider = %s "
                             "AND external_id = %s", (how[:200], PROVIDER, upload_id))


async def _same(event: dict | None) -> dict | None:
    return event


async def default_match(connection: Any, recording: Any) -> dict | None:
    from app import notetakers
    try:
        return await notetakers.match_event(connection, recording)
    except Exception as error:  # noqa: BLE001 - no calendar: no match
        logger.warning("VALR phone recording: calendar match skipped (%s)", type(error).__name__)
        return None


async def lead_for_person(connection: Any, name: str, start: str | None) -> str:
    """The company of a live lead whose contact has this name (all its words), if exactly one."""
    words = set((name or "").lower().split())
    if not words:
        return ""
    try:
        cursor = await connection.execute("SELECT company, contacts FROM leads WHERE status <> 'expired'")
        rows = await cursor.fetchall()
    except Exception:  # noqa: BLE001 - no leads table yet
        return ""
    found = set()
    for company, contacts in rows:
        people = contacts if isinstance(contacts, list) else json.loads(contacts or "[]")
        if any(words <= set(str(p.get("name") or "").lower().split()) for p in people if isinstance(p, dict)):
            found.add(company)
    return found.pop() if len(found) == 1 else ""


def candidate_name() -> str:
    try:
        from app.profile.config import settings
        return settings().candidate_name
    except Exception:  # noqa: BLE001
        return os.getenv("VALR_CANDIDATE_NAME", "").strip()


async def loop(connect: Callable[[], Any], stop: asyncio.Event | None = None) -> None:
    """The research worker's background task: one upload at a time, every VALR_PHONE_LOOP_S (30)
    seconds, and the audio clean-up."""
    stop = stop or asyncio.Event()
    every = float(os.getenv("VALR_PHONE_LOOP_S", "30"))
    processor = Processor()
    picked_up = False
    while not stop.is_set():
        try:
            async with await connect() as connection:
                await setup(connection)
                if not picked_up:  # Once per worker start: recordings whose hand-off failed earlier.
                    picked_up = True
                    await reevaluate(connection)
                while await processor.run_once(connection):
                    pass
                await purge(connection, datetime.now(timezone.utc))
        except Exception as error:  # noqa: BLE001 - keep the worker alive
            logger.warning("VALR phone recordings loop: %s", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass


async def _cli(target: str) -> int:
    import psycopg
    from app import research_jobs
    async with await psycopg.AsyncConnection.connect(research_jobs.conninfo(), autocommit=True) as connection:
        await setup(connection)
        results = await reevaluate(connection, None if target == "--all-unevaluated" else target)
    for external_id, status in results:
        print(f"phone:{external_id}  {status}")
    print(f"{len(results)} re-evaluated; the post-call loop scores them within a few minutes.")
    return 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] != ["reevaluate"] or len(sys.argv) != 3:
        sys.exit("usage: python -m app.phone_recordings reevaluate <upload id|--all-unevaluated>  "
                 "(in the research worker container)")
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_cli(sys.argv[2].removeprefix("phone:"))))
