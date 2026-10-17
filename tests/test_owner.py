"""The owner's profile (app/owner.py): env fallbacks, the stable key, phone rules, masking,
and (with VALR_TEST_POSTGRES) the one-time move of caller memory to the stable key."""
import asyncio
import os

import pytest

from app import callbacks, interviews, memory, owner, persona

PHONE = "+919800000001"  # Made up (a test range), never a real number.
NEW = "+919800000002"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("VALR_OWNER_PHONE", "VALR_OWNER_NAME", "VALR_CANDIDATE_NAME", "ASSISTANT_TIMEZONE",
                 "VALR_OWNER_KEYS", "VALR_PUBLIC_NUMBER", "TELNYX_FROM_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VALR_OWNER_EMAILS", "owner@example.com")
    owner.reset()
    yield
    owner.reset()


def test_env_fallbacks_then_the_profile_wins(monkeypatch):
    monkeypatch.setenv("VALR_OWNER_NAME", "Sam Rivera")
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Europe/London")
    monkeypatch.setenv("VALR_OWNER_PHONE", PHONE)
    assert (owner.name(), owner.first_name(), owner.zone_name(), owner.phone()) == \
        ("Sam Rivera", "Sam", "Europe/London", PHONE)
    assert owner.phone_source() == "env"
    owner.apply({"display_name": "Alex Kim", "timezone": "Asia/Kolkata", "phone_e164": NEW,
                 "recording_name": "Alex K"})
    assert (owner.first_name(), owner.zone_name(), owner.phone(), owner.recording_name()) == \
        ("Alex", "Asia/Kolkata", NEW, "Alex K")
    assert owner.phone_source() == "db"
    assert interviews.owner_name() == "Alex"


def test_removed_phone_ignores_the_env(monkeypatch):
    monkeypatch.setenv("VALR_OWNER_PHONE", PHONE)
    owner.apply({"phone_removed": True})
    assert owner.phone() == "" and owner.phone_source() == "none"
    assert callbacks.phone_of(owner.KEY) is None


def test_a_bad_zone_falls_back(monkeypatch):
    monkeypatch.setenv("ASSISTANT_TIMEZONE", "Mars/Olympus")
    assert owner.zone_name() == owner.DEFAULT_ZONE


def test_stable_key_survives_a_number_change(monkeypatch):
    monkeypatch.setenv("VALR_OWNER_PHONE", PHONE)
    assert memory.call_caller_key(PHONE) == owner.KEY
    assert owner.canonical("email:Owner@Example.com") == owner.KEY
    owner.apply({"phone_e164": NEW, "legacy_keys": [f"tel:{PHONE}"]})
    # Inbound: only the current number is the owner.
    assert memory.call_caller_key(NEW) == owner.KEY
    assert memory.call_caller_key(PHONE) == f"tel:{PHONE}"
    assert interviews.is_owner(owner.KEY) and not interviews.is_owner(f"tel:{PHONE}")
    # Dialling: the stable key and a key a workflow held from before ring the new number.
    assert owner.dial_number(owner.KEY) == NEW
    assert owner.dial_number(f"tel:{PHONE}") == NEW
    assert callbacks.phone_of(owner.KEY) == NEW
    assert callbacks.phone_of("tel:+15550100") == "+15550100"  # Anyone else: their own number.


@pytest.mark.parametrize("raw, want", [
    ("98000 00001", PHONE), ("098000 00001", PHONE), ("+91 98000-00001", PHONE), ("0091 9800000001", PHONE),
    ("+91 09800000001", PHONE), ("+1 (415) 555-0123", "+14155550123"), ("+44 7700 900123", "+447700900123"),
    ("+49 1512 3456789", "+4915123456789"),
])
def test_phone_numbers_accepted(raw, want):
    assert owner.normalize_phone(raw) == want


@pytest.mark.parametrize("raw, why", [
    ("", "Enter"), ("12345", "country code"), ("+91 2223456789", "mobile"), ("+91 98000 0000", "10 digits"),
    ("+1 800 555 0123", "toll-free"), ("+44 9090 123456", "premium"), ("+12", "doesn't look"),
    ("call me", "country code"),
])
def test_phone_numbers_refused(raw, why):
    with pytest.raises(owner.PhoneError, match=why):
        owner.normalize_phone(raw)


def test_valr_own_number_is_refused(monkeypatch):
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+14155550100")
    with pytest.raises(owner.PhoneError, match="VALR's own"):
        owner.normalize_phone("+1 415 555 0100")


def test_mask_shows_country_code_and_last_four():
    assert owner.mask(PHONE) == "+91 ••••• •0001"
    assert owner.mask("+14155550123") == "+1 ••••• •0123"
    assert owner.mask("+447700900123") == "+44 ••••• •0123"
    assert "98000" not in owner.mask(PHONE) and owner.mask("") == ""


def test_view_never_carries_the_full_number(monkeypatch):
    owner.apply({"phone_e164": PHONE})
    view = owner.view()
    assert PHONE not in str(view) and view["phone"]["masked"].endswith("0001")
    assert view["phone"]["country"] == "+91"


def test_names_and_pronunciation_rules():
    assert owner.clean_name("  Alex   Kim ") == "Alex Kim"
    assert owner.clean_name("Anne-Marie O'Neil") == "Anne-Marie O'Neil"
    for bad in ("", "x" * 61, "<b>Alex</b>", "Alex2"):
        with pytest.raises(owner.InvalidInput):
            owner.clean_name(bad)
    assert owner.clean_pronunciation("Chi-rag") == "Chi-rag" and owner.clean_pronunciation("") == ""
    with pytest.raises(owner.InvalidInput):
        owner.clean_pronunciation("say <this>")


def test_voice_uses_the_pronunciation():
    owner.apply({"display_name": "Alex Kim", "name_pronunciation": "Al-ex"})
    assert persona.spoken("Hi Alex, it's VALR.").startswith("Hi Al-ex,")
    owner.apply({"display_name": "Alex Kim"})
    assert persona.spoken("Hi Alex.") == "Hi Alex."


def test_photo_types():
    assert owner.photo_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert owner.photo_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert owner.photo_type(b"<svg onload=alert(1)>") is None


POSTGRES = os.getenv("VALR_TEST_POSTGRES")


@pytest.mark.skipif(not POSTGRES, reason="set VALR_TEST_POSTGRES to a scratch database's conninfo")
def test_migration_moves_caller_memory_once(monkeypatch):
    import psycopg
    monkeypatch.setenv("VALR_OWNER_PHONE", PHONE)

    async def go():
        async with await psycopg.AsyncConnection.connect(POSTGRES, autocommit=True) as c:
            await c.execute("DROP TABLE IF EXISTS owner_profile, callers, call_summaries")
            await c.execute(memory.SCHEMA)
            await c.execute("INSERT INTO callers (caller_key, display_name, first_seen, last_seen, call_count) "
                            "VALUES (%s, 'Sam', now() - interval '9 days', now() - interval '2 days', 3), "
                            "('email:owner@example.com', NULL, now() - interval '3 days', now() - interval '1 day', 2), "
                            "('tel:+15550100', 'Other', now(), now(), 1)", (f"tel:{PHONE}",))
            await c.execute("INSERT INTO call_summaries (call_id, caller_key, started_at, ended_at, summary) VALUES "
                            "('a', %s, now(), now(), 'phone'), ('b', 'email:owner@example.com', now(), now(), 'web')",
                            (f"tel:{PHONE}",))
            await owner.setup(c)
            await owner.setup(c)  # Twice: nothing more happens.
            cur = await c.execute("SELECT caller_key, call_count, display_name FROM callers ORDER BY caller_key")
            rows = await cur.fetchall()
            cur = await c.execute("SELECT DISTINCT caller_key FROM call_summaries")
            summaries = {r[0] for r in await cur.fetchall()}
            data = await owner.load(c)
            saved = await owner.set_phone(c, NEW)
            removed = await owner.remove_phone(c)
            await c.execute("DROP TABLE owner_profile, callers, call_summaries")
        return rows, summaries, data, saved, removed

    rows, summaries, data, saved, removed = asyncio.run(go())
    assert rows == [("owner", 5, "Sam"), ("tel:+15550100", 1, "Other")]
    assert summaries == {"owner"}
    assert set(data["legacy_keys"]) == {f"tel:{PHONE}", "email:owner@example.com"}
    assert saved["phone_e164"] == NEW and removed["phone_e164"] is None and removed["phone_removed"]
    assert owner.phone() == ""  # The cache follows the saves.
