"""How long a hiring process usually goes quiet and still comes back: the typical reply
silence, from the leads' touchpoints (app/leads.graph). Pure, no I/O; kept from the removed
Processes timeline for a "Nudge now" list (a lead quiet for longer than this is worth a nudge).

For each lead (an agency with no employer yet is skipped), the longest gap between two of its
past touches counts when the process did not fizzle out: it ended (offer, rejection or a closed
stage), something is still scheduled, or it had word within FIZZLE_DAYS (the same 21 days the
lead tracker calls "ghosted"). The typical silence is the median of those gaps; below
MIN_SAMPLE leads it says there is not enough data.
"""
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

FIZZLE_DAYS = 21
MIN_SAMPLE = 3
CLOSING_HINTS = ("closed-lost", "closed-won", "offer")


def when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def fizzled(lead: dict[str, Any], past: list[datetime], scheduled: bool, now: datetime) -> bool:
    """Gone quiet for good: not closed, nothing scheduled, no word for FIZZLE_DAYS (or he set
    "Gone quiet" himself)."""
    if lead.get("stage") == "ghosted" and lead.get("stage_manual"):
        return True
    closed = lead.get("stage") in ("closed-won", "closed-lost") or any(
        str(t.get("stage_hint") or "") in CLOSING_HINTS for t in lead.get("timeline") or [] if isinstance(t, dict))
    return not closed and not scheduled and now - past[-1] > timedelta(days=FIZZLE_DAYS)


def longest_silence(lead: dict[str, Any], now: datetime) -> float | None:
    """The longest quiet spell (days) in a process that came back afterwards; None otherwise."""
    if lead.get("agency_only"):
        return None
    touches = [(when(t.get("at")), t) for t in lead.get("timeline") or [] if isinstance(t, dict)]
    past = sorted(at for at, _ in touches if at is not None and at <= now)
    scheduled = any(at is not None and at > now and t.get("kind") == "calendar" for at, t in touches)
    if len(past) < 2 or fizzled(lead, past, scheduled, now):
        return None
    return max((b - a).total_seconds() / 86400 for a, b in zip(past, past[1:]))


def typical_silence(raw: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """{"enough", "n", "median_days", "longest_days", "fizzle_days"} over the leads JSON."""
    values = [v for lead in (raw or {}).get("leads") or [] if isinstance(lead, dict)
              if (v := longest_silence(lead, now)) is not None]
    enough = len(values) >= MIN_SAMPLE
    return {"enough": enough, "n": len(values), "median_days": round(median(values), 1) if enough else None,
            "longest_days": round(max(values), 1) if values else None, "fizzle_days": FIZZLE_DAYS}
