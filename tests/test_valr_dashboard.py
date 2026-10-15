"""The VALR cockpit: Access on every route, owner + CSRF on the actions, the pause flag,
and nothing private (attendee emails, phone numbers) on the page or in the JSON."""
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
    test_client = TestClient(app)
    test_client.flags = flags
    return test_client


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


def test_no_attendee_email_or_phone_number_is_shown(client):
    for text in (client.get("/valr", headers=GOOD).text,
                 client.get("/valr/api/state", headers=GOOD).text):
        assert "juniper.example\"" not in text and "@juniper.example" not in text
        assert "greenhouse" not in text.lower() and "555 0134" not in text
        assert "[phone hidden]" in text and "[email hidden]" in text
    page = client.get("/valr", headers=GOOD).text
    assert "Maya Lin" in page and "skipped: HR / recruiting" in page  # Names and roles, not addresses.
    assert "$180k-$215k" in page  # Comp stays visible to the owner.
    assert "R1" in page and "Interview intel" in page and "Company &amp; pay" in page


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
    assert "Automatic calls are paused" in page and "Paused: no automatic call" in page
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
    assert [(r["name"], r["status"]) for r in rows] == [
        ("Ana", "researched"), ("Ben", "skipped_classifier"), ("Hana Recruiter", "skipped_hr"),
        ("Attendee 4", "declined")]
    assert "@" not in json.dumps(rows)
    blocks = [{"id": "b", "start": start.isoformat(), "state": "waiting", "label": "L"}]
    nxt = valr_view.next_call(blocks, start - timedelta(hours=1), paused=False)
    assert nxt["at"] == (start - timedelta(minutes=15)).isoformat() and nxt["attempt"] == 1
    assert valr_view.next_call(blocks, start - timedelta(minutes=12), False)["attempt"] == 2
    assert valr_view.next_call(blocks, start - timedelta(hours=1), paused=True) is None
    assert valr_view.cache_hits([{"status": "done", "detail": "cache: company"},
                                 {"status": "cached", "detail": "slot research reused"}]) == \
        ["company", "whole slot"]
