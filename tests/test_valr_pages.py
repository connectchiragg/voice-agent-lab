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
from fastapi.testclient import TestClient

from app import interview_store, research, telegram_api, valr_dashboard, valr_view
from app.valr_ui import attention, contracts, routes, wording
from tests.valr_sample import sample_view
from tests.valr_sample_extras import sample_leads

ROOT = Path(__file__).resolve().parent.parent
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
    box = {"view": lambda: sample_view(extras=True), "leads": lambda: contracts.leads(sample_leads())}

    async def collect(pool, now=None):
        return box["view"]()

    async def load_leads():
        return box["leads"]()

    async def setup(connection):
        return None

    monkeypatch.setattr(valr_view, "collect", collect)
    monkeypatch.setattr(routes, "load_leads", load_leads)
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


def pages(client) -> list[str]:
    return ["/valr", "/valr/interviews", f"/valr/interviews/{ref_of(client)}", "/valr/interviews/past",
            "/valr/interviews/past/fathom-1006", "/valr/leads", "/valr/leads?stage=offer&q=x&lead=lead-5",
            "/valr/performance", "/valr/settings", "/valr/system"]


# Routes and auth.

def test_every_page_needs_a_verified_access_login(client):
    for path in pages(client):
        assert client.get(path).status_code == 403, path
        assert client.get(path, headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 403, path
        page = client.get(path, headers=GOOD)
        assert page.status_code == 200, path
        assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
        assert 'name="csrf-token"' in page.text and 'aria-current="page"' in page.text
    for path in ("/valr/interviews/slot-gone", "/valr/interviews/past/nope"):
        assert client.get(path).status_code == 403
        assert client.get(path, headers=GOOD).status_code == 404


def test_navigation_and_deep_links(client):
    home = client.get("/valr", headers=GOOD).text
    for href in ("/valr/interviews", "/valr/leads", "/valr/performance", "/valr/settings", "/valr/system", "/call"):
        assert f'href="{href}"' in home, href
    assert f'href="/valr/interviews/{ref_of(client)}"' in home  # The next interview, one tap.
    assert 'data-theme="light"' in client.get("/valr?theme=light", headers=GOOD).text
    assert "data-theme=" not in client.get("/valr?theme=<x>", headers=GOOD).text.split("<head>")[0]
    assert re.search(r'/static/valr-app\.css\?v=[0-9a-f]{10}', home)  # Cache-busted assets.


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
    assert 'data-globe' in page and 'id="leads-data"' in page and "12 live leads" in page
    for text in ("Next interview", "Juniper", "Next briefing call", "To do", "Read your Juniper briefing",
                 "Reconnect Google tomorrow", "Call me now", "Coming up"):
        assert text in page, text
    data = json.loads(page.split('id="leads-data">', 1)[1].split("</script>", 1)[0])
    assert len(data["leads"]) == 14 and {s["key"] for s in data["stages"]} >= {"offer", "interviewing"}


def test_interview_detail_is_written_for_reading(client, monkeypatch):
    def view():
        v = sample_view(extras=True)
        v["owner_name"] = "Alex"
        slot = v["blocks"][0]["slots"][0]
        slot["brief"]["notes"] = [{"fact": "Alex's last round with them stalled on sizing at quillworks dot com.",
                                   "sensitive": False, "private": False, "sources": []}]
        slot["detail"]["prep"] = ["Your measured turns of 500-4,500", "Size the system before drawing boxes"]
        slot["detail"]["pay"]["range"] = {"min": 180, "max": 215, "unit": "k USD", "currency": "USD", "source_url": ""}
        slot["detail"]["pay"]["claims"] = [{"claim": "Base $180k-$215k in the US.", "url": ""}]
        return v
    client.box["view"] = view
    page = html.unescape(client.get(f"/valr/interviews/{ref_of(client)}", headers=GOOD).text)
    assert "your last round with them stalled on sizing at quillworks.com." in page.lower()
    assert "What VALR says on the call" in page  # The script, folded away.
    assert "Your measured turns" not in page and "Size the system before drawing boxes" in page
    assert "No reliable India pay data found" in page and "180–215" not in page
    assert "More research" in page and "Calendar title:" in page


def test_past_interview_recording_and_impression(client):
    page = html.unescape(client.get("/valr/interviews/past/fathom-1006", headers=GOOD).text)
    assert "<iframe" not in page and "The Fathom player appears here" in page and "Open in Fathom" in page
    player = client.get("/valr/interviews/past/fathom-1005", headers=GOOD).text
    assert '<iframe src="https://fathom.video/embed/sample-5"' in player and "sandbox=" in player
    assert "How you came across" in page and "Sized the system unprompted" in page
    assert "Question by question" in page and "Next time" in page


def test_performance_leads_with_the_coach(client):
    page = client.get("/valr/performance", headers=GOOD).text
    assert page.index("Your coach") < page.index("perf-chart")
    assert "pushback is where rounds slip" in page and "Work on next" in page
    assert "Information control" not in page and "Answer first<" not in page
    plain = client.box["view"]
    client.box["view"] = lambda: sample_view()
    assert "Focus this week" in client.get("/valr/performance", headers=GOOD).text  # Until the coach lands.
    client.box["view"] = plain


def test_leads_filters_live_in_the_url(client):
    offers = client.get("/valr/leads?stage=offer", headers=GOOD).text
    assert offers.count("data-lead-row=") == 1 and "Orbit" in offers
    found = client.get("/valr/leads?q=kite", headers=GOOD).text
    assert found.count("data-lead-row=") == 1
    chosen = client.get("/valr/leads?lead=lead-5", headers=GOOD).text
    assert 'id="lead"' in chosen and "talent@orbit.example" in chosen
    client.box["leads"] = lambda: contracts.leads(None)
    assert "The lead tracker isn" in client.get("/valr/leads", headers=GOOD).text
    assert "Leads appear here once" in client.get("/valr", headers=GOOD).text


def test_settings_reflect_reality_without_server_talk(client):
    page = html.unescape(client.get("/valr/settings", headers=GOOD).text)
    assert "Connected as @sample_owner" in page and 'data-action="tgtest"' in page
    assert "Fathom" in page and "Optional" in page  # Granola: optional, never a to-do.
    assert not re.search(r"\b[A-Z]{3,}_[A-Z_]{3,}\b", page.split("<body", 1)[1])  # No env var names.
    assert "owner@example.com" in page  # The owner's own accounts are shown to him.


# The needs-attention model.

def base_view(**changes) -> dict:
    view = sample_view()
    view["connections"]["google"].update(expires_soon=False)
    view["connections"]["notetakers"] = {"fathom": {"connected": True}}
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
    upcoming = view["blocks"][0]
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
    view["connections"]["notetakers"] = {"fathom": {"connected": False}, "fireflies": {"error": "Token revoked"},
                                         "granola": {"error": "nope"}}
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
    assert wording.interview_label(slot) == {"company": "quillworks", "round": "Round 2",
                                             "headline": "Round 2 · quillworks", "raw": "L2- Alex"}
    assert wording.company_from_people([{"email": "a@hr.acme.co.example"}]) == "acme"
    assert wording.interview_label({"title": "Chat", "company": "", "interviewers": []})["company"] == "Unknown company"


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
    assert contracts.coach(None) is None and contracts.coach({"narrative": "One.\n\nTwo."})["narrative"] == ["One.", "Two."]
    assert contracts.impression(["Calm"])["cues"][0] == {"cue": "Calm", "tone": "neutral", "quote": ""}
    assert contracts.recording({"url": "https://fathom.video/x", "embed_url": "https://evil.example/x"})["embed_url"] == ""
    assert contracts.recording({"url": "http://insecure.example"}) is None
    leads = contracts.leads({"leads": [{"id": "a b", "company": "Acme", "stage": "rejected",
                                        "summary": "call +1 415 555 0134"}]})
    assert leads["leads"][0]["id"] == "a-b" and leads["leads"][0]["stage"] == "closed"
    assert "555 0134" not in json.dumps(leads)


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
            "static/valr-app.js", "static/valr-app.css", "static/valr-globe-lite.js"]


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
