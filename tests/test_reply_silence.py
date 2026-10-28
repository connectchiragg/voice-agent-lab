"""The typical reply silence (app/reply_silence.py), kept from the removed Processes timeline."""
from datetime import datetime, timedelta, timezone

from app import reply_silence

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def touch(days_ago, kind="email", hint=""):
    return {"kind": kind, "at": (NOW - timedelta(days=days_ago)).isoformat(), "title": "t", "stage_hint": hint}


def lead(*touches, stage="", manual=False, **extra):
    return {"id": "x", "company": "Aster", "stage": stage, "stage_manual": manual,
            "timeline": list(reversed(touches)), **extra}


def test_the_longest_gap_counts_only_when_the_process_came_back():
    assert reply_silence.longest_silence(lead(touch(30), touch(20), touch(5)), NOW) == 15
    assert reply_silence.longest_silence(lead(touch(50), touch(22)), NOW) is None  # Fizzled: 22 quiet days.
    rejected = lead(touch(50), touch(30), touch(25, hint="closed-lost"))
    assert reply_silence.longest_silence(rejected, NOW) == 20  # Ended, not fizzled.
    booked = lead(touch(50), touch(30), {"kind": "calendar", "at": (NOW + timedelta(days=2)).isoformat()})
    assert reply_silence.longest_silence(booked, NOW) == 20  # Something is still scheduled.
    assert reply_silence.longest_silence(lead(touch(10), touch(5), stage="ghosted", manual=True), NOW) is None
    assert reply_silence.longest_silence(lead(touch(10), touch(5), agency_only=True), NOW) is None
    assert reply_silence.longest_silence(lead(touch(3)), NOW) is None  # One touch: no gap.
    assert reply_silence.longest_silence(lead({"kind": "email", "at": "not a date"}, touch(4), touch(1)), NOW) == 3


def test_the_typical_silence_is_a_median_and_says_when_there_is_not_enough_data():
    raw = {"leads": [lead(touch(20), touch(20 - d), touch(1)) for d in (2, 4, 6, 8)] + [lead(touch(60))]}
    out = reply_silence.typical_silence(raw, NOW)
    # Gaps: max(2, 17), max(4, 15), max(6, 13), max(8, 11) -> 17, 15, 13, 11.
    assert out == {"enough": True, "n": 4, "median_days": 14.0, "longest_days": 17.0, "fizzle_days": 21}
    thin = reply_silence.typical_silence({"leads": raw["leads"][:2]}, NOW)
    assert not thin["enough"] and thin["median_days"] is None and thin["n"] == 2
    assert reply_silence.typical_silence(None, NOW)["n"] == 0
