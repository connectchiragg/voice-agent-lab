"""The block VALR's interview-briefing prompt includes: profile brief + evaluation brief.

Read once when a briefing call starts (outside the realtime audio path) and put
into the system prompt. Missing files are fine: the block just gets shorter.
Compensation is included because the briefing coaches negotiation; never log
this block.
"""
import json
from pathlib import Path

from app.profile.config import settings

HEADER = ("Private candidate context for this prep call (do not read figures aloud unless "
          "the candidate asks about compensation):")


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prompt_block(profile_path: Path | None = None, evaluation_path: Path | None = None) -> str:
    cfg = settings()
    profile = _read(profile_path or cfg.profile_path)
    evaluation = _read(evaluation_path or cfg.evaluation_path)
    parts = []
    if profile and profile.get("brief"):
        parts.append("## Profile\n" + profile["brief"])
    if evaluation and evaluation.get("brief_text"):
        parts.append("## Self-evaluation from recent interviews\n" + evaluation["brief_text"])
    return HEADER + "\n" + "\n\n".join(parts) if parts else ""
