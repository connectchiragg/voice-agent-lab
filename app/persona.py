"""The assistant's name: displayed as ASSISTANT_NAME ("VALR"), spoken as SPOKEN_NAME.

Aura-2 (odysseus) was checked on 2026-10-16 by synthesizing "Hi <owner>, it's <name>"
and transcribing it back with Deepgram nova-3: "Valor" came back as "Valor" every
time (0.48 s, the shortest and clearest); "Valer" was heard as "Fowler"; "Valar" and
"Vahl-er" as "Valar". So prompts and captions say VALR, and `app.deepgram.speakable`
swaps in SPOKEN_NAME just before text goes to Aura.
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("uvicorn.error")

# Words respelled for Aura only (captions and history keep the real spelling), e.g. a
# company name written as one word that a listener expects as two. The map is private
# data (it names real companies), so it is read from VALR_PRONOUNCE_PATH (default
# /data/pronounce.json, the read-only private data dir): a JSON object of
# {"Written": "Spoken"}, matched as whole words and case-sensitively. A missing or
# invalid file means no respellings. Checked on 2026-10-16 by synthesizing each spelling
# three times and transcribing it back with nova-3, plus a listening sample set: a
# two-word respelling puts the word boundary where a listener expects it, while
# hyphenated syllables were heard as separate words. Aura-2's inline IPA control was
# read aloud as JSON, so it is unused.
PRONOUNCE_PATH = "/data/pronounce.json"
EXAMPLE_PRONOUNCE = {"Acmecorp": "Acme Corp"}  # The file's shape; not loaded.


def load_pronounce(path: str | os.PathLike | None = None) -> dict[str, str]:
    """The respelling map from `path` (else VALR_PRONOUNCE_PATH, else PRONOUNCE_PATH)."""
    source = Path(path or os.getenv("VALR_PRONOUNCE_PATH") or PRONOUNCE_PATH).expanduser()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        logger.warning("VALR pronounce map unreadable: %s", type(error).__name__)
        return {}
    if not isinstance(data, dict):
        logger.warning("VALR pronounce map is not a JSON object: ignored")
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and k.strip()}


def use_pronounce(mapping: dict[str, str]) -> None:
    """Sets the respellings `spoken` applies."""
    global PRONOUNCE, PRONOUNCE_PATTERN
    PRONOUNCE = dict(mapping)
    PRONOUNCE_PATTERN = (re.compile(r"\b(" + "|".join(map(re.escape, sorted(PRONOUNCE, key=len, reverse=True)))
                                    + r")\b") if PRONOUNCE else None)


PRONOUNCE: dict[str, str] = {}
PRONOUNCE_PATTERN: re.Pattern[str] | None = None
use_pronounce(load_pronounce())

ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "VALR")
SPOKEN_NAME = os.getenv("ASSISTANT_SPOKEN_NAME", "Valor")
NAME_PATTERN = re.compile(rf"\b{re.escape(ASSISTANT_NAME)}\b")


_owner_pattern: tuple[str, str, re.Pattern[str] | None] = ("", "", None)


def owner_respelling() -> tuple[str, re.Pattern[str]] | None:
    """The owner's first name and its respelling from the Profile page ("How to pronounce
    your name", app/owner.py), as (respelling, pattern); None when none is set. Compiled once
    per change: this runs on the realtime path."""
    global _owner_pattern
    from app import owner
    say, name = owner.pronounce(), owner.first_name("")
    if not say or not name:
        return None
    if _owner_pattern[:2] != (name, say):
        _owner_pattern = (name, say, re.compile(rf"\b{re.escape(name)}\b"))
    return say, _owner_pattern[2]  # type: ignore[return-value]


def spoken(text: str) -> str:
    """Text for the TTS: the displayed name replaced by its phonetic spelling, the owner's
    name by his respelling, and the respellings in PRONOUNCE."""
    text = NAME_PATTERN.sub(SPOKEN_NAME, text) if ASSISTANT_NAME != SPOKEN_NAME else text
    mine = owner_respelling()
    if mine is not None:
        text = mine[1].sub(mine[0], text)
    if PRONOUNCE_PATTERN is None:
        return text
    return PRONOUNCE_PATTERN.sub(lambda match: PRONOUNCE[match.group(0)], text)
