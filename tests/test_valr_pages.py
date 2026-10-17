"""The multi-page VALR app (app/valr_ui): Access on every page, the needs-attention model,
the page wording rules, the plug-in contracts, Telegram's one-tap link, and no PII in the
fixtures."""
import asyncio
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi", reason="the pages run in the app's venv")

from fastapi.testclient import TestClient  # noqa: E402

from app import interview_store, research, telegram_api, valr_dashboard, valr_view
from app.valr_ui import attention, contracts, routes, wording
from tests.valr_sample import sample_view
from tests.valr_sample_extras import sample_leads, sample_recording_row

ROOT = Path(__file__).resolve().parent.parent
REAL_LOAD_NOTES, REAL_MARK_LEAD_READ = routes.load_notes, routes.mark_lead_read
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
    box = {"view": lambda: sample_view(extras=True), "leads": lambda: contracts.leads(sample_leads()),
           "notes": [], "read": []}

    async def collect(pool, now=None):
        return box["view"]()

    async def load_leads():
        return box["leads"]()

    async def load_recording(interview):  # fathom:1000 none stored; the others play here.
        if interview["id"] == "fathom:1000":
            return None
        return sample_recording_row(interview["id"])

    async def load_rows(interviews_):  # Fathom's invitee details for two of them (made up).
        return {"fathom:1003": sample_recording_row("fathom:1003", "", "juniper.example", ("Asha Rao",)),
                "fathom:1006": sample_recording_row("fathom:1006", "Nimbus", "", ("Jo Park", "Lee Wu"))}

    async def setup(connection):
        return None

    async def load_notes():  # The unread notifications (app/notifications.py's view shape).
        items = [n for n in box["notes"] if n["id"] not in box["read"]]
        by_lead = {}
        for n in items:
            if n.get("email") and n.get("lead_id"):
                by_lead[n["lead_id"]] = by_lead.get(n["lead_id"], 0) + 1
        return {"items": items, "unread": len(items), "leads": by_lead}

    async def mark_lead_read(lead_id):
        box["read"] += [n["id"] for n in box["notes"] if n.get("lead_id") == lead_id]

    monkeypatch.setattr(routes, "load_notes", load_notes)
    monkeypatch.setattr(routes, "mark_lead_read", mark_lead_read)
    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(routes, "load_leads", load_leads)
    monkeypatch.setattr(routes, "load_recording", load_recording)
    monkeypatch.setattr(routes, "load_rows", load_rows)
    monkeypatch.setattr(interview_store, "setup", setup)
    monkeypatch.setattr(research, "pool", Pool())
    monkeypatch.setattr(valr_dashboard, "verifier", Verifier())
    monkeypatch.setenv("VALR_ADMIN_EMAILS", OWNER)
    monkeypatch.setenv("VALR_AUTH_MODE", "access")  # Cloudflare Access only (app/auth).
    test_client = TestClient(app)
    test_client.box = box
    return test_client


def ref_of(client) -> str:
    page = client.get("/valr/interviews", headers=GOOD).text
    return "slot-" + page.split('href="/valr/interviews/slot-', 1)[1].split('"', 1)[0]


def lead_id(company: str) -> str:
    return next(r["id"] for r in contracts.leads(sample_leads())["leads"] if r["company"] == company)


def pages(client) -> list[str]:
    return ["/valr", "/valr/interviews", f"/valr/interviews/{ref_of(client)}", "/valr/interviews/past",
            "/valr/interviews/past/fathom-1006", "/valr/interviews/past/fathom-1000", "/valr/leads",
            f"/valr/leads?stage=offer&q=x&lead={lead_id('Orbit Labs')}", "/valr/leads?status=expired",
            "/valr/progress", "/valr/settings", "/login"]


# Routes and auth.

def test_every_page_needs_a_verified_access_login(client):
    for path in pages(client):
        if path == "/login":
            continue
        assert client.get(path).status_code == 403, path
        assert client.get(path, headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 403, path
        page = client.get(path, headers=GOOD)
        assert page.status_code == 200, path
        assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
        assert 'name="csrf-token"' in page.text and 'aria-current="page"' in page.text
    for old, new in (("/valr/system", "/valr/settings#advanced"), ("/valr/performance", "/valr/progress")):
        assert client.get(old).status_code == 403
        moved = client.get(old, headers=GOOD, follow_redirects=False)
        assert moved.status_code == 301 and moved.headers["location"] == new
    for path in ("/valr/interviews/slot-gone", "/valr/interviews/past/nope"):
        assert client.get(path).status_code == 403
        assert client.get(path, headers=GOOD).status_code == 404


def test_navigation_and_deep_links(client):
    home = client.get("/valr", headers=GOOD).text
    for href in ("/valr/interviews", "/valr/leads", "/valr/progress", "/valr/settings", "/call"):
        assert f'href="{href}"' in home, href
    assert f'href="/valr/interviews/{ref_of(client)}"' in home  # The next interview, one tap.
    assert 'data-theme="light"' in client.get("/valr?theme=light", headers=GOOD).text
    assert "data-theme=" not in client.get("/valr?theme=<x>", headers=GOOD).text.split("<head>")[0]
    assert re.search(r'/static/valr-ds\.css\?v=[0-9a-f]{10}', home)  # Cache-busted assets.
    assert home.count('data-action="callnow"') == 1  # One call action on the page: "Call me".
    for gone in ("Talk in the browser", "Call VALR", "Times in IST", "only you can see this", OWNER):
        assert gone not in home, gone


def test_a_database_outage_is_a_page_on_every_route(client):
    def down():
        raise OSError("connection refused")
    client.box["view"] = down
    for path in ("/valr", "/valr/interviews", "/valr/settings"):
        response = client.get(path, headers=GOOD)
        assert response.status_code == 503 and "can't read its data" in response.text
        assert "connection refused" not in response.text


def test_actions_stay_post_owner_and_csrf(client, monkeypatch):
    for path in ("/valr/api/telegram/test", "/valr/api/telegram/code", "/valr/api/dials", "/valr/api/call-now"):
        assert client.get(path, headers=GOOD).status_code == 405
    csrf = client.get("/valr", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}
    sent = []

    class Bot:
        async def send(self, chat, text, keyboard=None):
            sent.append((chat, text))
            return 1

    async def none(connection):
        return None

    async def chat(connection):
        return 42

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "y" * 40)
    monkeypatch.setattr(telegram_api, "bot", lambda: Bot())
    monkeypatch.setattr(telegram_api, "setup", none)
    monkeypatch.setattr(telegram_api, "owner_chat", chat)
    assert client.post("/valr/api/telegram/test", content="{}", headers=GOOD).status_code in (403, 415)
    assert client.post("/valr/api/telegram/test", content="{}",
                       headers={**headers, "Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    assert client.post("/valr/api/telegram/test", content="{}",
                       headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert sent == []
    ok = client.post("/valr/api/telegram/test", content="{}", headers=headers)
    assert ok.status_code == 200 and sent and sent[0][0] == 42


# What the pages say.

def test_home_hits_you_with_right_now(client):
    page = html.unescape(client.get("/valr", headers=GOOD).text)
    leads = contracts.leads(sample_leads())
    live = leads["leads"]
    assert all(r["stage"] != "closed" and r["status"] != "expired" for r in live)
    c = leads["counts"]
    assert c["live"] == len(live) == sum(c[b] for b in ("hot", "warm", "cool", "cold"))
    assert 'data-globe' in page and 'id="leads-data"' in page and f"Pipeline · {c['live']} active" in page
    words = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    for b in leads["bands"]:  # The globe's legend: "Hot 1 · Warm 3 ...", the same numbers as Leads.
        assert f'{b["label"]} {b["count"]}' in words
    assert "/static/leads-globe.js" in page and "/static/valr-globe-lite.js" in page  # 3D, lite fallback.
    # The first five seconds: the next interview (company, round, time, countdown, briefing), then Your move.
    order = [page.index(x) for x in ("Next interview", "Juniper", "System design", "data-countdown", "Open briefing",
                                     "Your move", 'id="leads-raw"', "Coming up")]
    assert order == sorted(order)
    assert "Reminder on Telegram at" in page or "Briefing call at" in page
    for gone in ("Status", "All good", "Nothing is broken", "Next Telegram heads-up", "To do", "live leads"):
        assert gone not in words, gone
    data = json.loads(page.split('id="leads-data">', 1)[1].split("</script>", 1)[0])
    assert len(data["leads"]) == len(leads["leads"]) and {s["key"] for s in data["stages"]} >= {"offer", "interviewing"}
    raw = json.loads(page.split('id="leads-raw">', 1)[1].split("</script>", 1)[0])
    # Live and closed (drawn only with "Show closed"); expired left out; no people, no links.
    assert {lead["id"] for lead in raw["leads"]} == {r["id"] for r in leads["leads"] + leads["closed"]}
    assert {lead["id"] for lead in raw["leads"] if lead["status"] == "closed"} == {r["id"] for r in leads["closed"]}
    assert all({"temperature", "band", "stage", "last_touch_at"} <= set(lead) for lead in raw["leads"])
    assert set(raw) == {"leads"}


def test_home_shows_prep_ticks_from_the_server(client):
    from app.interview_store import prep_item

    def view():
        v = sample_view(extras=True)
        slot = attention.next_slot(v)[1]
        slot["detail"]["prep"] = ["Size the system before drawing boxes", "Ask for their band before a number"]
        slot["prep_done"] = [prep_item("Size the system before drawing boxes")]
        return v
    client.box["view"] = view
    page = html.unescape(client.get("/valr", headers=GOOD).text)
    assert "Prep: 1 of 2 done" in page
    detail = client.get(f"/valr/interviews/{ref_of(client)}", headers=GOOD).text
    assert detail.count(" checked>") == 1 and 'data-prep-ref="' in detail and "1 of 2 done" in detail
    assert "ticks are saved" not in detail and "browser only" not in detail  # Saved on the server, said nowhere.
    def all_done():
        v = view()
        attention.next_slot(v)[1]["prep_done"].append(prep_item("Ask for their band before a number"))
        return v
    client.box["view"] = all_done
    page = html.unescape(client.get("/valr", headers=GOOD).text)
    assert "Prep: all 2 done" in page and 'class="prep-meter is-done"' in page


def test_interview_detail_is_written_for_reading(client, monkeypatch):
    def view():
        v = sample_view(extras=True)
        v["owner_name"] = "Alex"
        slot = attention.next_slot(v)[1]
        slot["brief"]["notes"] = [{"fact": "Alex's last round with them stalled on sizing at quillworks dot com.",
                                   "sensitive": False, "private": False, "sources": []}]
        slot["detail"]["prep"] = ["Your measured turns of 500-4,500", "Size the system before drawing boxes"]
        slot["detail"]["pay"]["range"] = {"min": 180, "max": 215, "unit": "k USD", "currency": "USD", "source_url": ""}
        slot["detail"]["pay"]["claims"] = [{"claim": "Base $180k-$215k in the US.", "url": ""}]
        return v
    client.box["view"] = view
    page = html.unescape(client.get(f"/valr/interviews/{ref_of(client)}", headers=GOOD).text)
    assert "your last round with them stalled on sizing at quillworks.com." in page.lower()
    assert "What VALR says on the call" not in page and 'data-hear="briefing-script"' in page  # Heard, not printed.
    assert "Your measured turns" not in page and "Size the system before drawing boxes" in page
    assert "No reliable India pay data found" in page and "180–215" not in page
    assert "More research" in page and "Calendar title" not in page
    for noise in ("Researched", "sensitive", "from your inbox", "All research notes", "India figures only",
                  "Where this comes from", "Last updated", "Briefing ready in"):
        assert noise not in page, noise
    for action in ('data-action="refresh"', 'data-sheet="fix-company"', 'id="fix-company"'):
        assert action in page, action  # The ⋯ menu: refresh, fix company, hear the briefing.


def test_past_interview_recording_and_impression(client):
    page = html.unescape(client.get("/valr/interviews/past/fathom-1006", headers=GOOD).text)
    assert "<iframe" not in page and "Open in Fathom" in page  # The link stays as the fallback.
    assert 'data-moments-id="fathom:1006"' in page  # Key moments load from /valr/api/recording.
    assert "With Jo Park, Lee Wu" in page  # Fathom's external invitees.
    player = client.get("/valr/interviews/past/fathom-1005", headers=GOOD).text
    assert '<div class="player-box" data-player="fathom:1005">' in player
    assert '<video controls preload="metadata" playsinline' in player and "fathom.video/embed" not in player
    assert "How the interviewer reacted" in player and "They dug in when you put numbers on the design." in player
    assert "How did you keep the queue from growing" in player  # A cue with its quote.
    # "Next time" first, then the reaction, the recording, question by question, the skills tested.
    order = [player.index(x) for x in ('id="next"', 'id="impression"', 'id="recording"', 'id="feedback"', 'id="scores"')]
    assert order == sorted(order)
    for gone in ("Impression score", "Positive signals", "Warning signs", "n/a", "Scores", "how we know",
                 "of the scheduled time", "Pay conversations"):
        assert gone not in player, gone
    assert "Question by question" in page and "Next time" in page
    unknown = client.get("/valr/interviews/past/fathom-1000", headers=GOOD).text
    assert "data-full" in unknown and "<iframe" not in unknown  # Not stored: ask the notetaker.
    assert "Couldn't read this recording's transcript from Fathom." in unknown  # Never "the transcript is gone".
    assert "transcript is gone" not in unknown and "data-reanalyse=\"fathom:1000\"" in unknown


def test_old_anchor_links_resolve_to_the_new_pages(client):
    from app import lead_worker
    from app.profile import post_call
    assert lead_worker.detail_url("fathom:1005") == "/valr/interviews/past/fathom-1005"
    assert post_call.deep_link("valr.example.com", "fathom:1005") == "https://valr.example.com/valr/interviews/past/fathom-1005"
    assert client.get(lead_worker.detail_url("fathom:1005"), headers=GOOD).status_code == 200
    assert contracts.new_detail_url("/valr#past-fathom-1005") == "/valr/interviews/past/fathom-1005"
    assert contracts.new_detail_url("/valr#performance") == "/valr/performance"
    script = (ROOT / "static/valr-app.js").read_text(encoding="utf-8")
    assert "#past-([A-Za-z0-9-]+)" in script and "/valr/interviews/past/${past[1]}" in script


def test_progress_trends_first_then_insights(client):
    page = html.unescape(client.get("/valr/progress", headers=GOOD).text)
    order = [page.index(x) for x in ('id="trends-title"', 'id="chart-overall"', 'id="chart-skills"', 'id="chart-reactions"',
                                     'id="chart-talk"', 'id="chart-funnel"', "What it means", "Where you stand",
                                     "What's working", "This week's focus", "What's costing you",
                                     "Salary conversations")]
    assert order == sorted(order)
    assert page.count('<figure class="tc') >= 5 and page.count('<table class="sr-only">') >= 5
    assert '<details class="fold card" id="salary">' in page  # The scripts start folded.
    assert 'download="valr-progress.csv"' in page and "All the numbers" not in page  # The table is a CSV in ⋯.
    assert page.count('type="checkbox" data-focus=') == 3
    assert "You give ground too fast when challenged" in page and "Say instead" in page
    assert "Post-call reports" not in page  # Delivery lives in Settings › Advanced.
    text = re.sub(r"<script.*?</script>|<[^>]+>", " ", page, flags=re.S)
    for jargon in ("r =", "r=", "rubric", "schema", "stddev", "Correlation", "placeholders on purpose", "browser only"):
        assert jargon not in text, jargon
    plain = client.box["view"]

    def no_coach():
        v = sample_view()
        v["performance"]["coach"] = None
        return v
    client.box["view"] = no_coach
    page = html.unescape(client.get("/valr/progress", headers=GOOD).text)
    assert "This week's focus" in page and "The coach's full read appears here" in page and "Where you stand" not in page
    client.box["view"] = plain


def test_leads_filters_live_in_the_url(client):
    offers = client.get("/valr/leads?stage=offer", headers=GOOD).text
    assert offers.count("data-lead-row=") == 1 and "Orbit Labs" in offers
    found = client.get("/valr/leads?q=kite", headers=GOOD).text
    assert found.count("data-lead-row=") == 1
    expired = client.get("/valr/leads?status=expired", headers=GOOD).text
    assert "Harbor" in expired and expired.count("data-lead-row=") == 1
    chosen = html.unescape(client.get(f"/valr/leads?lead={lead_id('Orbit Labs')}", headers=GOOD).text)
    assert 'id="lead"' in chosen and "lena@orbitlabs.example" in chosen and "Why this temperature" not in chosen
    assert "data-leads-sync" in chosen and "Check email now" in chosen  # In ⋯, not on show.
    for gone in ("Gmail: live", "last checked", "Check Gmail now", "/100", "Recency", "Nobody's move", "Role unknown"):
        assert gone not in chosen, gone
    client.box["leads"] = lambda: contracts.leads(None)
    assert "Connect Google and VALR will find your job emails." in html.unescape(client.get("/valr/leads", headers=GOOD).text)
    assert "Connect Google and VALR will find your job emails." in html.unescape(client.get("/valr", headers=GOOD).text)


def test_lead_corrections_are_on_the_lead_card(client):
    juniper = lead_id("Juniper Robotics")
    page = client.get(f"/valr/leads?lead={juniper}", headers=GOOD).text
    assert f'data-lead-fix="{juniper}"' in page
    for control in ('data-fix="stage"', 'data-fix="merge"', 'data-fix="dismiss"', "data-fix-stage", "data-fix-into"):
        assert control in page, control
    assert '<option value="closed-lost">' in page and f'<option value="{juniper}">' not in page  # Not into itself.
    assert 'data-sheet="lead-fix"' in page and 'id="lead-fix"' in page and "data-merge-search" in page  # In ⋯.
    script = (ROOT / "static/valr-app.js").read_text(encoding="utf-8")
    assert "/valr/api/leads/${encodeURIComponent(id)}/override" in script and "X-CSRF-Token" in script


def test_settings_reflect_reality(client):
    page = html.unescape(client.get("/valr/settings", headers=GOOD).text)
    assert "Connected as @sample_owner" in page and 'data-action="tgtest"' in page
    assert "Fathom" in page and "Optional" in page  # Granola: optional, never a to-do.
    assert 'id="setup"' not in page  # Every step is done in this sample: no checklist, no count.
    assert "of 4 essentials" not in page and 'id="advanced"' in page
    assert 'data-connect="fathom"' in page and 'type="password"' in page  # The connect form, empty.
    assert 'data-action="reach" data-mode="telegram" aria-pressed="true"' in page
    assert 'data-action="reach" data-mode="call" aria-pressed="false"' in page and "The default (Telegram heads-up)" in page
    for gone in ("Live updates", "How VALR checks it", "while the app is in testing", "Plays the recording right here"):
        assert gone not in re.sub(r'<p class="small nt-video">.*?</p>', "", page), gone


def test_reach_mode_is_an_owner_csrf_post_stored_server_side(client, monkeypatch):
    stored = {}

    async def set_flag(connection, name, enabled):
        stored[name] = enabled
    monkeypatch.setattr(interview_store, "set_flag", set_flag)
    assert client.get("/valr/api/reach", headers=GOOD).status_code == 405
    csrf = client.get("/valr", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}
    assert client.post("/valr/api/reach", content='{"mode": "call"}', headers={**GOOD, "Content-Type": "application/json"}).status_code == 403
    assert client.post("/valr/api/reach", content='{"mode": "call"}',
                       headers={**headers, "Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    assert client.post("/valr/api/reach", content='{"mode": "sometimes"}', headers=headers).status_code == 400
    assert stored == {}
    ok = client.post("/valr/api/reach", content='{"mode": "call"}', headers=headers)
    assert ok.status_code == 200 and stored == {interview_store.AUTO_DIAL: True}
    client.post("/valr/api/reach", content='{"mode": "telegram"}', headers=headers)
    assert stored == {interview_store.AUTO_DIAL: False}


def test_the_stored_choice_overrides_the_env_default(monkeypatch):
    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        def __init__(self, row):
            self.row = row

        async def execute(self, sql, params=()):
            return Cursor(self.row)
    monkeypatch.delenv("VALR_AUTO_DIAL", raising=False)
    assert asyncio.run(interview_store.auto_dial(Connection(None))) is False  # Default: heads-up only.
    assert asyncio.run(interview_store.auto_dial(Connection((True,)))) is True
    monkeypatch.setenv("VALR_AUTO_DIAL", "1")
    assert asyncio.run(interview_store.auto_dial(Connection(None))) is True
    assert asyncio.run(interview_store.auto_dial(Connection((False,)))) is False


def test_prep_ticks_are_an_owner_csrf_post(client, monkeypatch):
    ticks = []

    async def set_prep_check(connection, ref, item, done):
        ticks.append((ref, item, done))
    monkeypatch.setattr(interview_store, "set_prep_check", set_prep_check)
    csrf = client.get("/valr", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}
    body = '{"ref": "slot-abc", "item": "Size the system", "done": true}'
    assert client.post("/valr/api/prep", content=body, headers={**GOOD, "Content-Type": "application/json"}).status_code == 403
    assert client.post("/valr/api/prep", content='{"ref": "../x", "item": "a", "done": true}', headers=headers).status_code == 400
    assert client.post("/valr/api/prep", content=body, headers=headers).status_code == 200
    assert ticks == [("slot-abc", interview_store.prep_item("Size the system"), True)]
    assert interview_store.prep_item("Size  the system") == interview_store.prep_item("size the system")


# The needs-attention model.

def base_view(**changes) -> dict:
    view = sample_view()
    view["connections"]["google"].update(expires_soon=False)
    view["connections"]["notetakers"] = {"fathom": {"state": "connected"}}
    view["connections"]["telegram"] = {"configured": True, "linked": True}
    for key, value in changes.items():
        view[key] = value
    return view


def keys(view) -> list[str]:
    return [i["key"] for i in attention.items(view)]


def test_attention_is_empty_when_all_is_well():
    assert attention.counted(attention.items(base_view())) == []


def test_attention_google():
    view = base_view()
    view["connections"]["google"].update(state="not_connected")
    first = attention.items(view)[0]
    assert first["level"] == "bad" and first["fix"]["href"] == "/oauth/google/start"
    view["connections"]["google"].update(state="connected", expires_soon=True, age_days=5.0)
    item = attention.items(view)[0]
    assert item["title"] == "Reconnect Google in 2 days" and item["todo"] and item["level"] == "warn"


def test_attention_heads_up_missed_call_limits_and_order():
    view = base_view()
    upcoming = attention.next_slot(view)[0]
    upcoming["state"] = "heads_up_failed"
    past = dict(view["blocks"][1], id="b-past", state="unanswered",
                start=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    view["blocks"].append(past)
    view["dials"]["briefing"] = {"used": 12, "limit": 12}
    view["spend"]["month_usd"] = 39.0
    view["dials_paused"] = {"enabled": True, "updated_at": None}
    view["connections"]["calendar"]["last_error"] = "HTTP 401"
    items = attention.items(view)
    by = {i["key"]: i for i in items}
    assert by[f"heads-up-{upcoming['id']}"]["fix"]["action"] == "callnow"  # Still time: call now.
    assert by["missed-b-past"]["fix"] is None and by["missed-b-past"]["level"] == "warn"
    assert by["dials-briefing"]["fix"]["href"] == "/valr/settings#limits"
    assert by["budget"]["level"] == "warn" and by["calendar"]["fix"]["action"] == "sync"
    assert by["paused"]["level"] == "info" and by["paused"]["fix"]["data"] == {"paused": "false"}
    levels = [attention.LEVELS[i["level"]] for i in items]
    assert levels == sorted(levels)  # Worst first.
    assert all(i["fix"] is None or i["fix"].get("href") or i["fix"].get("action") for i in items)


def test_attention_telegram_phone_and_notetakers():
    view = base_view(mode="telegram")
    view["connections"]["telegram"] = {"configured": True, "linked": False}
    view["setup"]["phone_on_file"] = False
    view["connections"]["notetakers"] = {"fathom": {"state": "not_connected"}, "fireflies": {"state": "refused"},
                                         "granola": {"state": "refused"}}
    found = keys(view)
    assert "telegram" in found and "phone" in found and "notetaker" in found and "notetaker-fireflies" in found
    assert not any("granola" in k for k in found)  # Optional: never an issue.


def test_todos_are_three_at_most_and_useful():
    view = sample_view()
    items = attention.items(view)
    todos = attention.todos(view, items, contracts.leads(sample_leads()))
    assert len(todos) == 3 and todos[0]["title"] == "Read your Juniper briefing"
    assert all(t.get("href") or t.get("fix") for t in todos)


# Wording.

def test_interview_labels_from_attendee_domains():
    slot = {"title": "L2- Alex", "company": "", "interviewers": [
        {"name": "Recruiter", "email": "talent@quillworks.example"}, {"name": "Sam", "email": "sam@quillworks.example"},
        {"name": "Me", "email": "someone@gmail.com"}]}
    assert wording.interview_label(slot) == {"company": "Quillworks", "round": "Round 2",
                                             "headline": "Round 2 · Quillworks", "raw": "L2- Alex"}
    assert wording.company_from_people([{"email": "a@hr.acme.co.example"}]) == "Acme"
    assert wording.interview_label({"title": "Chat", "company": "", "interviewers": []})["company"] == "Company not identified"


def test_written_briefing_and_prep_filter():
    assert wording.written("Alex, here is your briefing for today. Alex's profile fits quillworks dot com.",
                           "Alex") == "Your profile fits quillworks.com."
    assert not wording.good_prep("Your measured turns of 500-4,500")
    assert not wording.good_prep("and the")
    assert not wording.good_prep("Ask about (their stack")
    assert wording.good_prep("Size the system out loud in the first five minutes")


def test_india_pay_only():
    pay = {"range": {"min": 180, "max": 215, "unit": "k", "currency": "USD"},
           "claims": [{"claim": "Base $180k in the US", "url": ""}, {"claim": "India roles 45-60 LPA", "url": ""}]}
    india = wording.india_pay(pay)
    assert india["range"] is None and [c["claim"] for c in india["claims"]] == ["India roles 45-60 LPA"]
    assert wording.india_pay({"range": {"min": 45, "max": 60, "unit": "LPA"}, "claims": []})["range"]


def test_reminders_say_what_actually_happened():
    assert wording.reminder_kind({"state": "unanswered"}, "telegram") == "call"  # Before Telegram.
    assert wording.reminder_kind({"state": "briefed"}, "telegram") == "call"
    assert wording.reminder_kind({"state": "scheduled", "heads_up": {"outcome": "sent"}}, "call") == "heads_up"
    assert wording.reminder_kind({"state": "scheduled"}, "telegram") == "heads_up"


def test_view_labels_stages_and_recorders():
    assert valr_view.stage("growth_late") == "Late-stage" and valr_view.stage("series_a_b") == "Series A–B"
    assert valr_view.company_name("Fathom") == "" and valr_view.company_name("Juniper") == "Juniper"
    assert valr_view.label("negotiation.information_control") == "Not revealing your numbers early"
    assert valr_view.name_from_email("maya.lin@x.example") == "Maya Lin"
    assert valr_view.name_from_email("jd42@x.example") == "jd42"


def test_contracts_tolerate_partial_data():
    assert "embed_url" not in contracts.recording({"url": "https://fathom.video/x", "embed_url": "https://evil.example/x"})
    assert contracts.recording({"url": "https://app.fireflies.ai/view/x", "provider": "fireflies"})["player"] is True  # Signed media.
    assert contracts.recording({"url": "http://insecure.example"}) is None
    assert contracts.recording({"share_url": "https://fathom.video/share/x", "provider": "fathom"})["provider"] == "Fathom"
    leads = contracts.leads({"leads": [{"id": "a b", "company": "Acme", "stage": "closed-lost",
                                        "timeline": [{"kind": "email", "summary": "call +1 415 555 0134"}]}]})
    assert leads["leads"] == [] and leads["counts"]["closed"] == 1  # Closed is its own bucket, not live.
    assert leads["closed"][0]["id"] == "a-b" and leads["closed"][0]["stage"] == "closed"
    assert leads["closed"][0]["stage_label"] == "Not moving forward" and leads["closed"][0]["band"] == "closed"
    assert "555 0134" not in json.dumps({k: v for k, v in leads.items() if k != "raw"})
    assert contracts.leads(None)["available"] is False and contracts.leads({"leads": []})["available"] is True


def test_waiting_on_you_comes_from_the_ball_in_court():
    lead = {"id": "x", "company": "Acme", "stage": "screening", "status": "active", "temperature": 55,
            "waiting_on": "you", "waiting_reason": "Recruiter: share your availability (2 days ago)"}
    row = contracts.leads({"leads": [lead]})["leads"][0]
    assert row["waiting_on_you"] is True and row["waiting_reason"].startswith("Recruiter")
    assert contracts.leads({"leads": [{**lead, "temperature": 20}]})["leads"][0]["waiting_on"] == "nobody"  # Cold.
    assert contracts.leads({"leads": [{**lead, "status": "closed", "stage": "closed-lost"}]})["closed"][0]["waiting_on"] == "nobody"
    assert contracts.leads({"leads": [{**lead, "waiting_on": None}]})["leads"][0]["waiting_on_you"] is False  # No guessing.


def test_notetaker_status_maps_to_one_state():
    rows = contracts.notetakers({"notetakers": {"fathom": {"state": "server"}, "fireflies": {"state": "refused"},
                                                "granola": {"state": "not_connected", "optional": True}}}, set())
    assert [r["state"] for r in rows] == ["connected", "error", "not_connected"]
    assert rows[1]["error"] and rows[0]["server"]


# Telegram: one tap.

def test_deep_link_and_bot_username(monkeypatch):
    assert telegram_api.deep_link("valr_bot", "123456") == "https://t.me/valr_bot?start=123456"
    for bad in (("valr bot", "1"), ("valr_bot", "a" * 65), ("valr_bot", "x y")):
        with pytest.raises(ValueError):
            telegram_api.deep_link(*bad)
    monkeypatch.setattr(telegram_api, "_bot_username", None)
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@from_env_bot")
    assert asyncio.run(telegram_api.bot_username()) == "from_env_bot"
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"username": "valr_sample_bot"}})
    bot = telegram_api.Bot("1:TEST", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(telegram_api.bot_username(bot)) == "valr_sample_bot"
    assert asyncio.run(telegram_api.bot_username(bot)) == "valr_sample_bot" and len(calls) == 1  # Cached.


def test_a_deep_link_start_links_and_keeps_the_username(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s" * 40)
    state = {}

    class Cursor:
        def __init__(self, row):
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        async def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO telegram_link"):
                state.update(mac=params[0], expires=params[1], tries=0)
            elif sql.startswith("UPDATE telegram_link SET chat_id"):
                state.update(chat=params[0], username=params[2])
            elif sql.startswith("SELECT code_mac"):
                return Cursor((state["mac"], state["expires"], state["tries"]))
            return Cursor(None)

    now = datetime.now(timezone.utc)
    code = asyncio.run(telegram_api.new_code(Connection(), now))
    assert telegram_api.START_PAYLOAD.match(code)  # Fits t.me/<bot>?start=.
    assert asyncio.run(telegram_api.redeem(Connection(), 7, code, now, username="sample_owner"))
    assert state["chat"] == 7 and state["username"] == "sample_owner"


# No PII in fixtures or the UI code.

FIXTURES = ["tests/valr_sample.py", "tests/valr_sample_extras.py", "tests/test_valr_pages.py",
            "scripts/preview_ui.py", *(str(p.relative_to(ROOT)) for p in (ROOT / "app/valr_ui").glob("*.py")),
            "static/valr-app.js", "static/valr-ds.css", "static/valr-notify.js", "static/valr-globe-lite.js", "tests/leads_sample.py"]


# Built from parts so this file doesn't contain what it forbids: a real phone number and a real name.
FORBIDDEN = ["93" + "22", "223 " + "209", "Chi" + "rag"]


def test_fixtures_use_example_domains_and_no_real_people():
    for name in FIXTURES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not [f for f in FORBIDDEN if f in text], name
        for email in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text):
            assert email.endswith((".example", "example.com", "localhost", "greenhouse.io", "gmail.com")), (name, email)


def test_rendered_pages_show_only_example_addresses(client):
    for path in pages(client):
        text = client.get(path, headers=GOOD).text
        for domain in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text):
            assert domain.endswith((".example", "example.com", "localhost")), (path, domain)
        assert "555 0134" not in text


def test_no_owner_name_in_the_code_only_in_settings(monkeypatch):
    from app import calendar_ics, interviews
    from app.profile import config
    for key in ("VALR_OWNER_NAME", "VALR_CANDIDATE_NAME"):
        monkeypatch.delenv(key, raising=False)
    assert interviews.owner_name() == "there" and config.settings().candidate_name == "Candidate"
    monkeypatch.setenv("VALR_CANDIDATE_NAME", "Sam Rivera")
    assert interviews.owner_name() == "Sam" and config.settings().candidate_name == "Sam Rivera"
    from app import company_names
    assert {"sam", "rivera"} <= company_names.owner_words()
    assert not calendar_ics.is_company_name("Rivera") and not calendar_ics.is_company_name("L2- Sam")
    monkeypatch.setenv("VALR_OWNER_NAME", "Alex")
    assert interviews.owner_name() == "Alex"


def test_leads_bands_closed_and_waiting_agree_everywhere(client):
    client.box["leads"] = lambda: contracts.leads(sample_leads(shape="live"))
    leads = contracts.leads(sample_leads(shape="live"))
    c = leads["counts"]
    assert (c["live"], c["closed"], c["expired"]) == (70, 39, 6)
    words = lambda page: " ".join(re.sub(r"<[^>]+>", " ", html.unescape(page)).split())  # noqa: E731
    home, page = (words(client.get(path, headers=GOOD).text) for path in ("/valr", "/valr/leads"))
    them = sum(1 for r in leads["leads"] if r["waiting_on"] == "them")
    assert f"Pipeline · {c['live']} active" in home and f"{c['waiting_on_you']} companies waiting on you" in home
    assert f"Your move {c['waiting_on_you']}" in page and f"Waiting on them {them}" in page and f"All {c['live']}" in page
    for b in ("Hot", "Warm", "Cool", "Cold"):
        assert f"{b} {c[b.lower()]}" in home
    rows = lambda text: text.count("data-lead-row=")  # noqa: E731
    assert rows(client.get("/valr/leads", headers=GOOD).text) == c["waiting_on_you"]  # Default tab: Your move.
    assert rows(client.get("/valr/leads?tab=them", headers=GOOD).text) == them
    assert rows(client.get("/valr/leads?tab=all", headers=GOOD).text) == c["live"]
    assert rows(client.get("/valr/leads?tab=all&closed=1", headers=GOOD).text) == c["live"] + c["closed"] + c["expired"]
    assert rows(client.get("/valr/leads?band=hot", headers=GOOD).text) == c["hot"]
    assert rows(client.get("/valr/leads?waiting=you", headers=GOOD).text) == c["waiting_on_you"]
    # Sorted by temperature; the band is a word, never the 0-100 number.
    listing = client.get("/valr/leads?tab=all", headers=GOOD).text
    bands = re.findall(r'class="band-pill b-(\w+)">', listing)
    order = {"hot": 0, "warm": 1, "cool": 2, "cold": 3}
    assert len(bands) == c["live"] and [order[b] for b in bands] == sorted(order[b] for b in bands)
    assert not re.search(r'band-pill[^>]*>.*?<span class="num">\d+', listing)
    # Home's Your move: the ask in plain words, with the thread.
    you = next(r for r in leads["leads"] if r["waiting_on"] == "you")
    from app.valr_ui import wording
    assert wording.ask_line(you["waiting_reason"]) in home


def test_past_list_takes_outcome_and_people_from_leads_and_recordings(client):
    page = html.unescape(client.get("/valr/interviews/past", headers=GOOD).text)
    juniper = page.split("/valr/interviews/past/fathom-1003", 1)[1].split("</li>", 1)[0]
    assert "Interviewing" in juniper and "Awaiting result" not in juniper  # Lead matched by the invitees' domain.
    assert "with Asha Rao" in juniper
    nimbus = page.split("/valr/interviews/past/fathom-1006", 1)[1].split("</li>", 1)[0]
    assert "Screening" in nimbus  # Lead matched by name.
    tessel = page.split("/valr/interviews/past/fathom-1005", 1)[1].split("</li>", 1)[0]
    assert "Didn't move on" in tessel  # The scorecard's own outcome wins over the lead's stage.


def test_enrich_never_uses_a_round_label_or_the_owner_as_company():
    from app.valr_ui import past
    leads = [{"company": "Quillworks", "domain": "quillworks.example", "stage": "closed", "stage_raw": "ghosted",
              "stage_label": "Gone quiet", "contacts": [{"email": "maya@gmail.com"}]},
             {"company": "Orbit Labs", "domain": "", "stage": "offer", "stage_raw": "offer", "stage_label": "Offer",
              "contacts": []}]
    iv = {"id": "fathom:1", "company": "L2- Sam", "outcome": "unknown"}
    row = {"company": "", "event_company": "Interview -L2- Sam", "company_domain": "",
           "invitee_hashes": json.dumps([past.base.email_hash("maya@gmail.com")]), "interviewers": '["Maya Lin"]'}
    out = past.enrich(iv, row, leads, owner={"sam"})
    assert out["company"] == "Quillworks"  # Junk refused; the lead found through a contact's (hashed) address.
    assert out["outcome_chip"][:2] == ("Ghosted", "warn") and out["interviewers"] == ["Maya Lin"]
    plain = past.enrich({"id": "fathom:2", "company": "", "outcome": "unknown"}, None, leads, owner={"sam"})
    assert plain["company"] == "" and "outcome_chip" not in plain  # Nothing to go on: "Outcome unknown".
    offer = past.enrich({"id": "fathom:3", "company": "Orbit Labs Inc", "outcome": "unknown"}, None, leads, owner=set())
    assert offer["outcome_chip"][:2] == ("Offer", "ok")
    assert past.lead_outcome({"stage_raw": "closed-lost", "stage": "closed"})[0] == "Rejected"


def test_calendar_titles_with_round_labels_are_not_companies(monkeypatch):
    from app import calendar_ics
    monkeypatch.setenv("VALR_OWNER_NAME", "Sam")
    assert not calendar_ics.is_company_name("L2- Sam")
    assert not calendar_ics.is_company_name("Round 2")
    assert calendar_ics.is_company_name("Quillworks")


def test_missing_impressions_come_from_the_read_only_copy(tmp_path, monkeypatch):
    live = {"schema_version": 3, "interviews": [
        {"interview_id": "fathom:1", "date": "2026-10-01", "impression": None},
        {"interview_id": "fathom:2", "date": "2026-10-02", "impression": {"coverage": "transcript", "score": 7.0, "cues": []}},
        {"interview_id": "fathom:3", "date": "2026-10-03"}]}  # A round the worker added since.
    seed = {"schema_version": 3, "coach": {"where_you_stand": "Steady."}, "interviews": [
        {"interview_id": "fathom-1", "impression": {"coverage": "transcript", "score": 6.0, "cues": [], "leaned_in": "Numbers."}},
        {"interview_id": "fathom:2", "impression": {"coverage": "transcript", "score": 1.0, "cues": []}}]}
    (tmp_path / "live.json").write_text(json.dumps(live))
    (tmp_path / "seed.json").write_text(json.dumps(seed))
    monkeypatch.setenv("VALR_PERFORMANCE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setenv("VALR_PERFORMANCE_FALLBACK", str(tmp_path / "seed.json"))
    perf, _ = valr_view.profile_files()
    by_id = {iv["interview_id"]: iv for iv in perf["interviews"]}
    assert by_id["fathom:1"]["impression"]["score"] == 6.0  # Filled in (the slug form of the id matches).
    assert by_id["fathom:2"]["impression"]["score"] == 7.0  # The live one is kept.
    assert "impression" not in by_id["fathom:3"] and perf["coach"] == {"where_you_stand": "Steady."}
    shown = valr_view.performance(perf)["interviews"][0]["impression"]
    assert shown and shown["leaned_in"] == "Numbers."


# System copy never reaches the page (the PM spec's P0 list), in any scenario.

BANNED = ["live", "Live updates", "last checked", "updated", "Scheduled", "Researching", "how we know",
          "Nothing is broken", "only you can see this", "n/a = not tested", "in this browser only",
          "Needs attention", "offer positive", "next positive", "Times in IST", "Outcome unknown", "Not advanced"]


def visible_text(page: str) -> str:
    """What a reader sees: no scripts, styles, templates, tags or attribute values."""
    page = re.sub(r"<(script|style|template)\b.*?</\1>", " ", page, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def test_no_system_strings_on_any_page(client):
    client.box["leads"] = lambda: contracts.leads(sample_leads(shape="live"))
    juniper = lead_id("Juniper Robotics")
    paths = pages(client) + ["/valr/leads?tab=all&closed=1", f"/valr/leads?lead={juniper}", "/valr/leads?tab=them"]
    for scenario in ("full", "empty", "error"):
        client.box["view"] = (lambda sc=scenario: sample_view(extras=sc == "full", scenario=sc))
        for path in paths:
            response = client.get(path, headers=GOOD)
            if response.status_code != 200:
                continue
            text = visible_text(response.text)
            for banned in BANNED:
                assert not re.search(rf"\b{re.escape(banned)}\b", text, flags=re.I), (scenario, path, banned)
            assert not re.search(r"\b(?:Hot|Warm|Cool|Cold) \d{1,3}\b(?! ·)", text.split("Pipeline")[0]), (scenario, path)


# Notifications: red dots until opened, Home's "New" strip, one count everywhere.

def note(i, kind, title, company, lead="", email=True, hours=1):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {"id": i, "kind": kind, "label": kind.capitalize(), "importance": "digest", "title": title, "body": "",
            "company": company, "link": f"/valr/leads?lead={lead}#lead" if lead else "/valr/leads",
            "gmail_url": "https://mail.google.com/mail/#all/t1" if email else "", "created_at": at, "day": "Today",
            "read": False, "lead_id": lead, "email": email}


def unread_count(page: str) -> str:
    return page.split("data-notif-count>", 1)[1].split("<", 1)[0]


def test_unread_items_show_red_dots_and_the_counts_agree(client):
    orbit, kite = lead_id("Orbit Labs"), lead_id("Kite")
    client.box["notes"] = [note(1, "offer", "Orbit Labs sent an offer", "Orbit Labs", orbit),
                           note(2, "update", "Kite sent an update", "Kite", kite, hours=3),
                           note(3, "calendar", "Interview with Juniper added: Mon 19 Oct, 6:00 PM", "Juniper",
                                email=False, hours=5)]
    home = client.get("/valr", headers=GOOD).text
    panel = home.split("data-notif-list>", 1)[1].split("</div>", 1)[0]
    assert panel.count('class="notif-item unread') == 3 and "Orbit Labs sent an offer" in panel and "h ago" in panel
    assert panel.index("Orbit Labs sent an offer") < panel.index("Kite sent an update")  # Newest first.
    # One number, on the bell only; the Leads nav dot when an email is unread.
    counts = re.findall(r"data-notif-count>(\d*)<", home)
    assert counts == ["3"] and 'class="notif-bell has-unread"' in home
    assert "data-lead-dot" in home
    leads = client.get("/valr/leads?tab=all", headers=GOOD).text
    orbit_row = leads.split(f'data-lead-row="{orbit}"', 1)[1].split("</tr>", 1)[0]
    kite_row = leads.split(f'data-lead-row="{kite}"', 1)[1].split("</tr>", 1)[0]
    assert "data-unread-lead" in orbit_row and "data-unread-lead" in kite_row
    assert leads.count("data-unread-lead") == 2  # Only leads with an unread email.


def test_opening_a_lead_clears_its_email_dots(client):
    orbit, kite = lead_id("Orbit Labs"), lead_id("Kite")
    client.box["notes"] = [note(1, "offer", "Orbit Labs sent an offer", "Orbit Labs", orbit),
                           note(2, "update", "Kite sent an update", "Kite", kite)]
    opened = client.get(f"/valr/leads?lead={orbit}", headers=GOOD).text
    assert unread_count(opened) == "1"  # Read by opening its card, before the page drew the counts.
    row = opened.split(f'data-lead-row="{orbit}"', 1)[1].split("</tr>", 1)[0]
    assert "data-unread-lead" not in row and 'id="lead"' in opened
    home = client.get("/valr", headers=GOOD).text
    assert "Orbit Labs sent an offer" not in home.split("data-notif-list>", 1)[1].split("</div>", 1)[0]
    assert "data-lead-dot" in home  # Kite's email is still unread.
    client.get(f"/valr/leads?lead={kite}", headers=GOOD)
    home = client.get("/valr", headers=GOOD).text
    assert "data-lead-dot" not in home and unread_count(home) == ""


def test_home_has_no_new_strip_only_the_bell(client):
    """The owner removed Home's "New" card: new events are in the bell, nowhere on Home."""
    orbit = lead_id("Orbit Labs")
    for notes in ([], [note(1, "offer", "Orbit Labs sent an offer", "Orbit Labs", orbit)]):
        client.box["notes"] = notes
        home = html.unescape(client.get("/valr", headers=GOOD).text)
        main = home.split('id="main"', 1)[1]
        assert "data-notif-strip" not in home and "notif-strip" not in home and "data-notif-show" not in home
        assert "You're all caught up." not in main and "Orbit Labs sent an offer" not in main
        assert "data-notif" in home  # The bell stays.
    assert unread_count(home) == "1" and "data-lead-dot" in home
    client.box["notes"] = []
    home = client.get("/valr", headers=GOOD).text
    assert unread_count(home) == "" and "has-unread" not in home and "data-lead-dot" not in home
    css = (ROOT / "static/valr-ds.css").read_text(encoding="utf-8")
    assert ".notif-strip" not in css and ".strip-list" not in css and ".notif-action" not in css


class NoteStore:
    """notifications.Store in memory, for the real load_notes and the real read API."""
    rows: list = []

    def __init__(self, connection):
        pass

    async def setup(self):
        return None

    async def listing(self, unread_only, since, limit=100):
        rows = [r for r in self.rows if not (unread_only and r["read_at"])]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    async def unread_count(self, since):
        return sum(1 for r in self.rows if not r["read_at"])

    async def unread_by_lead(self, since):
        out = {}
        for r in self.rows:
            if not r["read_at"] and r["lead_id"] and r["source_id"].startswith("gmail:"):
                out[r["lead_id"]] = out.get(r["lead_id"], 0) + 1
        return out

    async def mark_read(self, ids, now):
        hit = [r for r in self.rows if not r["read_at"] and (ids is None or r["id"] in ids)]
        for r in hit:
            r["read_at"] = now
        return len(hit)

    async def switches(self):
        from app import notifications
        return notifications.Switches.from_row(None)

    async def mark_lead_read(self, lead_id, now):
        hit = [r for r in self.rows if not r["read_at"] and r["lead_id"] == lead_id]
        for r in hit:
            r["read_at"] = now
        return len(hit)


def test_clicking_an_item_reads_it_on_the_server(client, monkeypatch):
    from app import notifications
    orbit = lead_id("Orbit Labs")
    now = datetime.now(timezone.utc)
    base = {"importance": "digest", "body": "", "gmail_url": "", "thread_id": "t1", "action": "", "interview_at": None,
            "tg_state": "sent", "tg_sent_at": None, "read_at": None}
    NoteStore.rows = [{**base, "id": 7, "source_id": "gmail:m7", "kind": "reply", "title": "Orbit Labs replied",
                       "company": "Orbit Labs", "link": f"/valr/leads?lead={orbit}", "lead_id": orbit,
                       "created_at": now - timedelta(minutes=30)},
                      {**base, "id": 8, "source_id": "calendar:ev1:added", "kind": "calendar", "thread_id": None,
                       "title": "Interview with Orbit Labs added: Mon 19 Oct, 6:00 PM", "company": "Orbit Labs",
                       "link": "/valr/interviews", "lead_id": None, "created_at": now - timedelta(minutes=10)}]
    monkeypatch.setattr(notifications, "Store", NoteStore)
    monkeypatch.setattr(routes, "load_notes", REAL_LOAD_NOTES)
    monkeypatch.setattr(routes, "mark_lead_read", REAL_MARK_LEAD_READ)
    home = client.get("/valr", headers=GOOD).text
    assert unread_count(home) == "2" and 'data-notif-item="7"' in home and "data-lead-dot" in home
    csrf = home.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    headers = {**GOOD, "Content-Type": "application/json", "X-CSRF-Token": csrf}
    # Showing the panel reads nothing; the bell's API lists both, still unread.
    listed = client.get("/valr/api/notifications", headers=GOOD).json()
    assert listed["unread"] == 2 and all(not i["read"] for i in listed["items"])
    assert [i["lead_id"] for i in listed["items"]] == ["", orbit] and [i["email"] for i in listed["items"]] == [False, True]
    done = client.post("/valr/api/notifications/read", content=json.dumps({"ids": [7]}), headers=headers).json()
    assert done["unread"] == 1
    home = client.get("/valr", headers=GOOD).text
    assert 'data-notif-item="7"' not in home and unread_count(home) == "1"
    assert "data-lead-dot" not in home  # Its email is read; the calendar change has no email dot.
    assert client.post("/valr/api/notifications/read", content=json.dumps({"lead": orbit}), headers=headers).json()["unread"] == 1
    assert client.post("/valr/api/notifications/read", content=b'{"all": true}', headers=headers).json()["unread"] == 0
    assert client.post("/valr/api/notifications/read", content=json.dumps({"lead": "../x"}), headers=headers).status_code == 400
    script = (ROOT / "static/valr-notify.js").read_text(encoding="utf-8")
    assert "[data-notif-item]" in script and "/valr/api/notifications/read" in script  # Clicks read; opening doesn't.


def test_message_links_name_linkedin_or_gmail():
    from app.valr_page import mail_source
    from app.valr_ui.shell import note_item
    now = datetime.now(timezone.utc)
    assert mail_source("https://www.linkedin.com/messaging/thread/2-abc/") == "LinkedIn"
    assert mail_source("https://mail.google.com/mail/u/0/#all/abc") == "Gmail"
    assert mail_source("https://linkedin.com.example/x") == "Gmail"
    item = {"id": "n1", "title": "Arcworks replied", "gmail_url": "https://www.linkedin.com/messaging/thread/2-abc/"}
    assert ">Open in LinkedIn</a>" in note_item(item, now)
    assert ">Open in Gmail</a>" in note_item({**item, "gmail_url": "https://mail.google.com/mail/u/0/#all/abc"}, now)
