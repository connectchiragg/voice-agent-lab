"""Interview transcripts, kept for the owner: encrypted at rest, shown only to him.

Table `recording_transcripts`, one row per recording (Fathom, Fireflies, Granola, phone):
the speaker-labelled turns with their start times ({speaker, text, start}) and the title,
as Fernet ciphertext (VALR_TOKEN_KEY, app/google_oauth.py, the same key and helpers as the
notetaker keys). Without VALR_TOKEN_KEY nothing is stored. Kept until the owner deletes the
recording ("Delete recording", phone uploads); "Hide" keeps it but the page and API stop
showing it. Nothing here logs transcript text, speakers or titles: counts only.

Written wherever VALR holds a recording's meeting: the notetaker intake (a webhook or export
that carried it), the worker's post-call evaluation (every new, phone and history recording)
and the transcript API's on-demand fetch from the provider. Read by
`GET /valr/api/recording/transcript` (owner only, app/notetakers/routes.py) for the past
interview's "Transcript" section and its .txt download.

Size: a 45-60 minute interview is about 8-12k words, 50-70 KB of JSON; Fernet adds a third
(base64), so about 70-100 KB a recording: 100 interviews is under 10 MB.
"""
import json
import logging
import os
from typing import Any

from app import google_oauth

logger = logging.getLogger("valr")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recording_transcripts (
    provider      text NOT NULL,
    external_id   text NOT NULL,
    sealed        text NOT NULL,            -- Fernet ciphertext (VALR_TOKEN_KEY) of {title, turns}
    turns         integer NOT NULL DEFAULT 0,
    chars         integer NOT NULL DEFAULT 0,
    stored_at     timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, external_id)
);
"""
MAX_TURNS = 5000


def token_key() -> str:
    return os.getenv("VALR_TOKEN_KEY", "").strip()


async def setup(connection: Any) -> None:
    await connection.execute(SCHEMA)


def turns_of(meeting: dict | None) -> list[dict[str, Any]]:
    """The normalised meeting's turns as {speaker, text, start (seconds or None)}."""
    out = []
    for turn in (meeting or {}).get("turns") or []:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        start = turn.get("start")
        out.append({"speaker": str(turn.get("speaker") or "Unknown")[:80], "text": text,
                    "start": float(start) if isinstance(start, (int, float)) else None})
    return out[:MAX_TURNS]


async def save(connection: Any, provider: str, external_id: str, meeting: dict | None,
               key: str | None = None) -> bool:
    """Store (or replace) one recording's transcript. False without turns or VALR_TOKEN_KEY."""
    key = key if key is not None else token_key()
    turns = turns_of(meeting)
    if not key or not turns:
        return False
    payload = json.dumps({"title": str((meeting or {}).get("title") or "")[:300], "turns": turns},
                         ensure_ascii=False)
    await setup(connection)
    await connection.execute(
        "INSERT INTO recording_transcripts (provider, external_id, sealed, turns, chars) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (provider, external_id) DO UPDATE SET sealed = excluded.sealed, turns = excluded.turns, "
        "chars = excluded.chars, updated_at = now()",
        (provider, str(external_id), google_oauth.encrypt(key, payload), len(turns),
         sum(len(t["text"]) for t in turns)))
    logger.info("VALR transcript stored (%s, %d turns)", provider, len(turns))
    return True


async def save_quietly(connection: Any, provider: str, external_id: str, meeting: dict | None) -> bool:
    """`save` that never raises (the caller's work goes on); logs the error class only."""
    try:
        return await save(connection, provider, external_id, meeting)
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR transcript not stored: %s", type(error).__name__)
        return False


async def load(connection: Any, provider: str, external_id: str, key: str | None = None) -> dict | None:
    """{title, turns} or None (none stored, or VALR_TOKEN_KEY changed)."""
    key = key if key is not None else token_key()
    if not key:
        return None
    await setup(connection)
    cursor = await connection.execute(
        "SELECT sealed FROM recording_transcripts WHERE provider = %s AND external_id = %s",
        (provider, str(external_id)))
    row = await cursor.fetchone()
    if not row:
        return None
    try:
        return json.loads(google_oauth.decrypt(key, row[0]))
    except (google_oauth.ReconnectNeeded, ValueError):
        return None


async def delete(connection: Any, provider: str, external_id: str) -> None:
    await setup(connection)
    await connection.execute("DELETE FROM recording_transcripts WHERE provider = %s AND external_id = %s",
                             (provider, str(external_id)))


# Shown to the owner.

def owner_names() -> set[str]:
    names = {os.getenv(n, "").strip().lower() for n in ("VALR_CANDIDATE_NAME", "VALR_OWNER_NAME")} - {""}
    return names | {n.split()[0] for n in names}


def is_owner(speaker: str, owners: set[str]) -> bool:
    name = speaker.strip().lower()
    return bool(name) and (name in owners or (name.split() or [""])[0] in owners)


def clock(seconds: float | None) -> str:
    if seconds is None:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def view(data: dict, owners: set[str] | None = None) -> list[dict[str, Any]]:
    """Turns for the page: {who ("You" or the speaker's name), seconds, clock, text}."""
    owners = owner_names() if owners is None else owners
    return [{"who": "You" if is_owner(t["speaker"], owners) else t["speaker"], "seconds": t.get("start"),
             "clock": clock(t.get("start")), "text": t["text"]} for t in data.get("turns") or []]


def as_text(data: dict, owners: set[str] | None = None) -> str:
    """The .txt download: a title line, then "[m:ss] Who: text" per turn."""
    lines = [str(data.get("title") or "Interview transcript"), ""]
    for turn in view(data, owners):
        stamp = f"[{turn['clock']}] " if turn["clock"] else ""
        lines.append(f"{stamp}{turn['who']}: {turn['text']}")
    return "\n".join(lines) + "\n"
