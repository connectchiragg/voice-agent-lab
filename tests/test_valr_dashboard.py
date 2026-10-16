"""The VALR cockpit: Access on every route, owner + CSRF on the actions, the pause flag,
and nothing private (attendee emails, phone numbers) on the page or in the JSON."""
import html
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import (calendar_sync, callbacks, interview_store, interviews, research, valr_dashboard,
                 valr_view)
from app.interview_types import Person, SlotEvent, slot_workflow_id
from tests.valr_sample import sample_view

OWNER = "owner@example.com"
GOOD = {"Cf-Access-Jwt-Assertion": "owner"}


class Verifier:
    async def email(self, token):
        return {"owner": OWNER, "guest": "guest@example.com"}.get(token)


class Pool:
    def connection(self):
        class Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False
        return Context()


@pytest.fixture
def client(monkeypatch):
    from app.main import app
    flags: dict[str, bool] = {}

    async def collect(pool, now=None):
        return sample_view(paused=flags.get(interview_store.DIALS_PAUSED, False))

    async def setup(connection):
        return None

    async def set_flag(connection, name, enabled):
        flags[name] = enabled

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(interview_store, "setup", setup)
    monkeypatch.setattr(interview_store, "set_flag", set_flag)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")  # Cloudflare Access only (app/auth).
    test_client = TestClient(app)
    test_client.flags = flags
    return test_client


def first_ref(client) -> str:
    page = client.get("/valr/interviews", headers=GOOD).text
    return page.split('href="/valr/interviews/slot-', 1)[1].split('"', 1)[0]


def detail(client) -> str:
    return client.get(f"/valr/interviews/slot-{first_ref(client)}", headers=GOOD).text


def token(client) -> str:
    page = client.get("/valr", headers=GOOD).text
    return page.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]


def test_page_and_state_need_a_verified_access_login(client):
    for path in ("/valr", "/valr/api/state"):
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 403
        response = client.get(path, headers=GOOD)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    state = client.get("/valr/api/state", headers=GOOD).json()
    assert {"blocks", "spend", "dials", "connections", "performance", "jobs"} <= set(state)


def test_attendee_emails_are_shown_to_the_owner_and_phone_numbers_are_not(client):
    upcoming = client.get("/valr/interviews", headers=GOOD).text
    for text in (detail(client) + upcoming, client.get("/valr/api/state", headers=GOOD).text):
        assert "maya.lin@juniper.example" in text and "talent@juniper.example" in text  # Attendees.
        assert "greenhouse" not in text.lower() and "555 0134" not in text  # Tools; phone numbers.
        assert "[phone hidden]" in text
        assert "[email hidden]" in text and "priya@juniper.example" not in text  # Free text stays scrubbed.
    page = detail(client)
    assert "Maya Lin" in page and "Recruiter, not researched" in page
    assert 'href="mailto:maya.lin@juniper.example"' in page
    assert "45–60 LPA" in page  # India pay stays visible to the owner.
    assert "+1 228 888 8007" in client.get("/valr", headers=GOOD).text  # VALR's own number, to call it.


def test_plain_language_and_ist_times(client):
    import re
    page = "".join(client.get(path, headers=GOOD).text for path in (
        "/valr", "/valr/interviews", "/valr/performance", "/valr/settings", "/valr/leads",
        "/valr/interviews/past/fathom-1005")) + detail(client)
    for heading in ("Who you're meeting", "What to expect in this round", "Likely questions",
                    "Pay snapshot", "Prep checklist", "From your emails &amp; past rounds", "This week's focus", "Where you stand",
                    "How the interviewer reacted"):
        assert heading in page, heading
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<[^>]*=\"[^\"]*\"", " ", page, flags=re.S))
    for jargon in ("R1", "R2", "R3", "slot", "workflow", "cache hit", " block"):
        assert jargon not in visible, jargon
    times = re.findall(r"\b\d{1,2}:\d{2} (?:AM|PM) IST\b", page)
    assert times and not re.search(r">\d{2}:\d{2}<", page)  # 12-hour IST, never bare 24-hour times.
    assert "Times in IST" in page


def test_pause_flag_round_trip_needs_owner_and_csrf(client):
    csrf = token(client)
    body = json.dumps({"paused": True})
    json_headers = {**GOOD, "Content-Type": "application/json"}
    assert client.post("/valr/api/dials", content=body, headers=json_headers).status_code == 403
    assert client.post("/valr/api/dials", content=body,
                       headers={**json_headers, "X-CSRF-Token": csrf + "0"}).status_code == 403
    assert client.post("/valr/api/dials", content=body, headers={
        **json_headers, "X-CSRF-Token": csrf, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/valr/api/dials", content=body, headers={
        **json_headers, "X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/valr/api/dials", content="paused=true", headers={
        **GOOD, "X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    guest = {"Cf-Access-Jwt-Assertion": "guest", "Content-Type": "application/json", "X-CSRF-Token": csrf}
    assert client.post("/valr/api/dials", content=body, headers=guest).status_code == 403
    assert client.flags == {}
    ok = client.post("/valr/api/dials", content=body, headers={
        **json_headers, "X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert ok.status_code == 200 and ok.json()["paused"] is True
    assert client.flags == {interview_store.DIALS_PAUSED: True}
    page = client.get("/valr", headers=GOOD).text
    # The default is a Telegram heads-up (no automatic call), so it is the heads-ups that pause.
    assert "Paused" in page and "Resume heads-ups" in page and "Heads-ups are paused" in page
    assert "Paused: no heads-up" in client.get("/valr/interviews", headers=GOOD).text
    assert client.get("/valr/api/state", headers=GOOD).json()["next_call"] is None
    client.post("/valr/api/dials", content=json.dumps({"paused": False}),
                headers={**json_headers, "X-CSRF-Token": csrf})
    assert client.flags[interview_store.DIALS_PAUSED] is False


def test_sync_now_and_slot_refresh(client, monkeypatch):
    csrf = token(client)
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}
    reasons, refreshed = [], []

    async def sync_now(reason):
        reasons.append(reason)
        return True

    start = datetime.now(timezone.utc) + timedelta(hours=3)
    event = SlotEvent("ev-1", "Interview", start.isoformat(), (start + timedelta(hours=1)).isoformat(),
                      attendees=[Person("x@acme.example", "X")], company="Acme")

    async def upcoming(connection, begin, end):
        return [event]

    async def setup(connection):
        return None

    async def temporal():
        return object()

    async def start_slot_now(client_, event_, owner, block_id, refresh):
        refreshed.append((event_.key, block_id, refresh))

    monkeypatch.setattr(interviews, "sync_now", sync_now)
    monkeypatch.setattr(calendar_sync, "upcoming", upcoming)
    monkeypatch.setattr(calendar_sync, "setup", setup)
    monkeypatch.setattr(callbacks, "temporal", temporal)
    monkeypatch.setattr(interviews, "start_slot_now", start_slot_now)
    assert client.post("/valr/api/sync", content="{}", headers=headers).status_code == 200
    assert reasons == ["dashboard"]
    assert client.post("/valr/api/slots/slot-unknown/refresh", content="{}",
                       headers=headers).status_code == 404
    assert client.post(f"/valr/api/slots/{slot_workflow_id('ev-1')}/refresh", content="{}",
                       headers={**GOOD, "Content-Type": "application/json"}).status_code == 403
    done = client.post(f"/valr/api/slots/{slot_workflow_id('ev-1')}/refresh", content="{}", headers=headers)
    assert done.status_code == 200 and refreshed == [("ev-1", refreshed[0][1], True)]
    assert refreshed[0][1].startswith("briefing-")


def test_csrf_tokens_expire_and_bind_to_the_viewer():
    now = time.time()
    issued = valr_dashboard.csrf_token(OWNER, now)
    assert valr_dashboard.csrf_ok(OWNER, issued, now + 60)
    assert not valr_dashboard.csrf_ok("someone@example.com", issued, now + 60)
    assert not valr_dashboard.csrf_ok(OWNER, issued, now + valr_dashboard.CSRF_TTL_S + 1)
    assert not valr_dashboard.csrf_ok(OWNER, "", now) and not valr_dashboard.csrf_ok(OWNER, "x.y", now)


def test_builders():
    assert valr_view.scrub("mail a.b@c.io or +91 98765 43210 or (415) 555-0134") == \
        "mail [email hidden] or [phone hidden] or [phone hidden]"
    assert valr_view.scrub("$180,000-$220,000 and 1,20,00,000 INR") == "$180,000-$220,000 and 1,20,00,000 INR"
    start = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    event = SlotEvent("k", "t", start.isoformat(), (start + timedelta(hours=1)).isoformat(), attendees=[
        Person("me@x.com", "Me", is_self=True), Person("a@acme.example", "Ana"),
        Person("b@acme.example", "Ben"), Person("hr@acme.example", "Hana Recruiter"),
        Person("c@acme.example", "", response="declined"), Person("room@resource.calendar.google.com", "Room", resource=True)])
    rows = valr_view.interviewer_rows(event, {"a@acme.example"}, True, {"me@x.com"})
    assert [(r["name"], r["email"], r["status"]) for r in rows] == [
        ("Ana", "a@acme.example", "researched"), ("Ben", "b@acme.example", "skipped_classifier"),
        ("Hana Recruiter", "hr@acme.example", "skipped_hr"), ("C", "c@acme.example", "declined")]
    assert "me@x.com" not in json.dumps(rows) and "resource.calendar" not in json.dumps(rows)
    blocks = [{"id": "b", "start": start.isoformat(), "state": "waiting", "label": "L"}]
    nxt = valr_view.next_call(blocks, start - timedelta(hours=1), paused=False)
    assert nxt["at"] == (start - timedelta(minutes=30)).isoformat() and nxt["attempt"] == 1
    late = start - timedelta(minutes=12)  # Past T-30: as soon as research is ready.
    assert valr_view.next_call(blocks, late, False)["at"] == late.isoformat()
    assert valr_view.next_call(blocks, start - timedelta(minutes=4), False) is None
    assert valr_view.next_call(blocks, start - timedelta(hours=1), paused=True) is None
    assert valr_view.cache_hits([{"status": "done", "detail": "cache: company"},
                                 {"status": "cached", "detail": "slot research reused"}]) == \
        ["company", "whole slot"]


def test_dev_preview_is_loopback_only(monkeypatch):
    from app.main import app
    monkeypatch.setattr(valr_dashboard, "verifier", None)
    monkeypatch.setattr(valr_dashboard, "dev_preview", False)
    monkeypatch.setattr(valr_view, "collect", lambda pool, now=None: _view())
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.delenv("VALR_DEV_PREVIEW", raising=False)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")
    with pytest.raises(RuntimeError):
        valr_dashboard.enable_dev_preview("127.0.0.1")  # No explicit opt-in.
    monkeypatch.setenv("VALR_DEV_PREVIEW", "1")
    for host in ("0.0.0.0", "::", "localhost", "10.0.0.5"):
        with pytest.raises(RuntimeError):
            valr_dashboard.enable_dev_preview(host)
    local = TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 50000))
    assert local.get("/valr").status_code == 403  # The env var alone (as in a container) opens nothing.
    valr_dashboard.enable_dev_preview("127.0.0.1")
    assert local.get("/valr").status_code == 200
    remote = TestClient(app, base_url="http://127.0.0.1:8090", client=("10.0.0.9", 50000))
    assert remote.get("/valr").status_code == 403
    proxied = TestClient(app, base_url="https://valr.haciensus.com", client=("127.0.0.1", 50000))
    assert proxied.get("/valr").status_code == 403  # A tunnel connects from loopback, for another host.
    monkeypatch.setenv("VALR_DEV_PREVIEW", "0")
    assert local.get("/valr").status_code == 403


async def _view():
    return sample_view()


@pytest.mark.parametrize("scenario,expected", [
    ("empty", {"/valr": ["Google isn't connected", "No phone number on file", "None yet"],
               "/valr/interviews": ["Connect your Google Calendar"],
               "/valr/performance": ["Your scores appear after"],
               "/valr/settings": ["Connect Google", "Missing"]}),
    ("error", {"/valr/interviews": ["We couldn't load your interviews just now"],
               "/valr": ["Google access expired", "Reconnect Google", "The last calendar sync failed"]}),
])
def test_first_run_and_error_states(client, monkeypatch, scenario, expected):
    async def collect(pool, now=None):
        return sample_view(scenario=scenario)
    monkeypatch.setattr(valr_view, "collect", collect)
    for path, texts in expected.items():
        page = html.unescape(client.get(path, headers=GOOD).text)
        for text in texts:
            assert text in page, (path, text)


def test_a_database_outage_is_a_page_not_a_traceback(client, monkeypatch):
    async def collect(pool, now=None):
        raise OSError("connection refused")
    monkeypatch.setattr(valr_view, "collect", collect)
    response = client.get("/valr", headers=GOOD)
    assert response.status_code == 503 and "can't read its data" in response.text
    assert "connection refused" not in response.text


def test_performance_reads_schema_2_and_falls_back_only_when_missing(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from tests.valr_sample import performance_file
    data = performance_file(datetime(2026, 10, 16, tzinfo=timezone.utc))
    path = tmp_path / "performance.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("VALR_PERFORMANCE_PATH", str(path))
    monkeypatch.setenv("VALR_SCORES_PATH", str(tmp_path / "interview_scores.csv"))
    monkeypatch.setenv("VALR_EVALUATION_PATH", str(tmp_path / "evaluation.json"))
    monkeypatch.setenv("VALR_PROFILE_PATH", str(tmp_path / "profile.json"))
    perf, target = valr_view.profile_files()
    view = valr_view.performance(perf)
    assert target is None and len(view["interviews"]) == 7 and view["available"]
    assert [d["label"] for d in view["dimensions"]][:2] == ["Technical", "Communication"]
    assert view["most_improved"]["label"] == "Answering first"
    assert view["biggest_risk"]["label"] == "Handling pushback"
    assert [f["action"] for f in view["focus_this_week"]] == data["interviews"][-1]["do_next_time"]
    assert view["interviews"][0]["dimensions"]["negotiation"] is not None  # A screen tests pay talk.
    assert view["interviews"][1]["dimensions"]["negotiation"] is None  # Not exercised: n/a, not 0.
    assert view["series"]["communication.clarity"]["n"] == 7
    # The old 1-5 files are used only when performance.json is missing.
    (tmp_path / "interview_scores.json").write_text(json.dumps({"rows": [
        {"date": f"2026-0{m}-01", "company": "A", "dimension": "structure", "score": 3} for m in (7, 8, 9)]}))
    assert valr_view.profile_files()[0]["schema_version"] == 3  # schema 3 is read as is
    path.write_text(json.dumps(dict(data, schema_version=2)))
    assert valr_view.profile_files()[0]["schema_version"] == 2  # and schema 2 still is
    path.unlink()
    old, _ = valr_view.profile_files()
    assert valr_view.performance(old)["interviews"][0]["dimensions"] == {"communication": 6.0}
    (tmp_path / "interview_scores.json").unlink()
    assert valr_view.performance(valr_view.profile_files()[0])["available"] is False


def test_telegram_mode_tile_statuses_and_start_code(client, monkeypatch):
    from app import telegram_api

    async def collect(pool, now=None):
        view = sample_view()
        view["mode"] = "telegram"
        view["connections"]["telegram"] = {"configured": True, "linked": False, "linked_at": None}
        from app.valr_ui import attention
        attention.next_slot(view)[0]["heads_up"] = {"outcome": "later", "sent_at": view["generated_at"],
                                         "outcome_at": view["generated_at"],
                                         "call_at": "2026-10-16T10:40:00+00:00"}
        return view

    codes = []

    async def none(connection):
        return None

    async def new_code(connection, now=None):
        codes.append("246810")
        return "246810"

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(telegram_api, "setup", none)
    monkeypatch.setattr(telegram_api, "new_code", new_code)
    home = client.get("/valr", headers=GOOD).text
    assert "Next Telegram heads-up" in home and "A Telegram message 30 minutes" in home
    assert "Pause heads-ups" in home and "Telegram isn't connected" in home and "tries again" not in home
    upcoming = client.get("/valr/interviews", headers=GOOD).text
    assert "Telegram heads-up at" in upcoming and "Calling at 4:10 PM" in upcoming
    settings = client.get("/valr/settings", headers=GOOD).text
    assert "Open Telegram &amp; connect" in settings and 'data-action="tglink"' in settings
    assert "TELEGRAM_" not in settings and "FATHOM_API_KEY" not in settings  # No server talk.
    assert "connect it in Settings" in client.get("/valr/system", headers=GOOD).text
    csrf = token(client)
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert client.post("/valr/api/telegram/code", content="{}", headers=headers).status_code == 503
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "y" * 40)
    assert client.post("/valr/api/telegram/code", content="{}", headers=GOOD).status_code in (403, 415)
    guest = {**headers, "Cf-Access-Jwt-Assertion": "guest"}
    assert client.post("/valr/api/telegram/code", content="{}", headers=guest).status_code == 403
    assert codes == []
    async def username(client_=None):
        return "valr_sample_bot"
    monkeypatch.setattr(telegram_api, "bot_username", username)
    ok = client.post("/valr/api/telegram/code", content="{}", headers=headers)
    assert ok.status_code == 200 and ok.json()["command"] == "/start 246810"
    assert ok.json()["link"] == "https://t.me/valr_sample_bot?start=246810"  # One tap: /start 246810.
