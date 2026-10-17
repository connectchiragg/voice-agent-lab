"""Calls: "Talk here" (the inline WebRTC call) and "Call my phone", with the same brain.

The offer's auth and CSRF, the context it passes (the next interview's brief, or general
owner mode), the shared caller memory, the no-interview fix for both kinds of call, and the
split button. The remembered default is tests/valr-call.test.mjs (node)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from aiortc import RTCPeerConnection, RTCSessionDescription
from fastapi.testclient import TestClient

from app import owner, call_now, callbacks, interview_store, interviews, main, memory, valr_dashboard, webrtc
from app.interview_types import SlotEvent
from app.valr_ui import shell
from tests.test_valr_pages import GOOD, OWNER, client  # noqa: F401 - the pages' client fixture.
from tests.test_webrtc import ToneTrack, csrf, engines, run, signed_in  # noqa: F401 - fixtures.

PHONE = "+15550100001"  # A fictional number.


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.setenv("VALR_OWNER_EMAILS", OWNER)
    monkeypatch.setenv("VALR_OWNER_PHONE", PHONE)
    monkeypatch.delenv("VALR_OWNER_KEYS", raising=False)
    monkeypatch.delenv("VALR_ADMIN_EMAILS", raising=False)


def offer_body(**extra):
    return {"type": "offer", "sdp": "x", "echo_cancelled": True, **extra}


# The offer: owner, same-origin and the page's CSRF token.

def test_the_offer_needs_the_csrf_token_and_the_same_origin(engines):
    client = TestClient(main.app)
    assert client.post("/webrtc/offer", json=offer_body()).status_code == 403  # No token.
    stale = {"X-CSRF-Token": valr_dashboard.csrf_token(OWNER, now=1)}
    assert client.post("/webrtc/offer", json=offer_body(), headers=stale).status_code == 403
    other = {"X-CSRF-Token": valr_dashboard.csrf_token("someone@example.com")}
    assert client.post("/webrtc/offer", json=offer_body(), headers=other).status_code == 403
    cross = {**csrf(), "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert client.post("/webrtc/offer", json=offer_body(), headers=cross).status_code == 403
    form = {**csrf(), "Content-Type": "text/plain"}
    assert client.post("/webrtc/offer", content=json.dumps(offer_body()), headers=form).status_code in (403, 415, 422)
    assert client.post("/webrtc/offer", json=offer_body(intent="anything"), headers=csrf()).status_code == 422
    # With the token the offer is read (and this fake SDP refused).
    assert client.post("/webrtc/offer", json=offer_body(), headers=csrf()).status_code == 400


def test_only_the_owner_may_start_a_browser_call(engines, monkeypatch):
    monkeypatch.setenv("VALR_ADMIN_EMAILS", "boss@example.com")  # Signed in, but not the owner.
    client = TestClient(main.app)
    response = client.post("/webrtc/offer", json=offer_body(), headers=csrf())
    assert response.status_code == 403 and "owner" in response.json()["detail"]
    assert client.post("/webrtc/hangup", json={"call_id": "x"}, headers=csrf()).status_code == 403


def test_signed_out_offers_and_hangups_are_refused():
    client = TestClient(main.app)
    for path, body in (("/webrtc/offer", offer_body()), ("/webrtc/hangup", {"call_id": "x"})):
        assert client.post(path, json=body).status_code in (401, 403), path


# The context: the same agent, brief and memory as the phone call.

def browser_call(intent, ref=""):
    """A real aiortc browser on localhost: offer with `intent`, then hang up from the panel."""
    async def scenario():
        browser = RTCPeerConnection()
        browser.addTrack(ToneTrack())
        channel = browser.createDataChannel("control")
        events, received = [], asyncio.Event()
        channel.on("message", lambda text: (events.append(json.loads(text)), received.set()))
        await browser.setLocalDescription(await browser.createOffer())
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post("/webrtc/offer", headers=csrf(), json={
                "type": "offer", "sdp": browser.localDescription.sdp, "echo_cancelled": True,
                "intent": intent, "ref": ref})
            answer = response.json()
            await browser.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
            async with asyncio.timeout(10):
                while not any(e["type"] == "caption" for e in events):  # The greeting is spoken.
                    received.clear()
                    await received.wait()
            (peer,) = main.app.state.webrtc_peers
            hung = await http.post("/webrtc/hangup", headers=csrf(), json={"call_id": answer["call_id"]})
        await browser.close()
        return answer, events, peer, hung
    return run(scenario())


def test_talk_here_briefs_the_next_interview_with_the_owner_memory(engines, owner_env, monkeypatch):
    prepared, greeted, callers = [], [], []

    async def prepare(conversation_id, intent, ref=""):
        prepared.append((conversation_id, intent, ref))
        return "brief"

    async def begin_call(conversation_id, caller_id):
        callers.append(caller_id)

    monkeypatch.setattr(interviews, "prepare_web_call", prepare)
    main.app.state.engines.begin_call = begin_call
    main.app.state.engines.greeting = lambda conversation_id: greeted.append(conversation_id) or "Hi, it's VALR."
    answer, events, peer, hung = browser_call("brief_next", "slot-abc")
    assert answer["mode"] == "brief" and answer["call_id"] == peer.id
    assert prepared[0][1:] == ("brief_next", "slot-abc")
    assert greeted == [prepared[0][0]]  # VALR speaks first, with this call's context.
    assert [e["type"] for e in events][0] == "ready"
    assert callers == [owner.KEY]  # The browser call uses the phone call's memory.
    assert hung.json() == {"ok": True} and peer.close_reason == "hung up by the caller"
    assert main.app.state.webrtc_peers == set()


def test_the_old_page_without_an_intent_is_not_greeted(engines, monkeypatch):
    called = []

    async def prepare(*args):
        called.append(args)
        return "talk"

    monkeypatch.setattr(interviews, "prepare_web_call", prepare)
    main.app.state.engines.greeting = lambda conversation_id: "Hi."
    client = TestClient(main.app)
    client.post("/webrtc/offer", json=offer_body(), headers=csrf())
    assert called == []


def slot_event(start, key="k1", company="Juniper"):
    return SlotEvent(key, f"{company} · Technical round", start.isoformat(),
                     (start + timedelta(hours=1)).isoformat(), company=company)


class Pool:
    def connection(self):
        class Context:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False
        return Context()


@pytest.fixture
def owner_call(owner_env, monkeypatch):
    """A conversation the owner is on (as caller memory knows him), cleaned up afterwards."""
    conversation_id = "conv-talk"
    monkeypatch.setattr(interviews, "profile_block", lambda: "")
    monkeypatch.setitem(memory.calls, conversation_id,
                        memory.CallerCall(f"tel:{PHONE}", datetime.now(timezone.utc)))
    monkeypatch.setattr(interviews, "research", type("R", (), {"pool": Pool()}))
    yield conversation_id
    interviews.call_ended(conversation_id)


def test_brief_next_loads_the_brief_call_me_would_give(owner_call, monkeypatch):
    now = datetime.now(timezone.utc)
    event = slot_event(now + timedelta(hours=3))

    async def next_event(ref=""):
        return event

    async def load_slots(connection, keys):
        return [{"key": keys[0], "title": event.title, "start_utc": now + timedelta(hours=3),
                 "spoken": "They build warehouse robots.", "notes": []}]

    monkeypatch.setattr(interviews, "next_event", next_event)
    monkeypatch.setattr(interview_store, "load_slots", load_slots)
    assert run(interviews.prepare_web_call(owner_call, "brief_next")) == "brief"
    assert callbacks.greeting(owner_call) == f"Hi {interviews.owner_name()}, it's VALR, back with your brief for your interview with Juniper. Ready?"
    note = interviews.prompt_note(owner_call)
    assert "They build warehouse robots." in note and "don't call get_briefing" in note


def test_no_upcoming_interview_starts_in_general_owner_mode(owner_call, monkeypatch):
    async def nothing(ref=""):
        return None

    monkeypatch.setattr(interviews, "next_event", nothing)
    assert run(interviews.prepare_web_call(owner_call, "brief_next")) == "talk"
    assert "Nothing's coming up" in callbacks.greeting(owner_call)
    note = interviews.prompt_note(owner_call)
    assert interviews.TALK_NOTE in note and "<briefing>\n" not in note
    interviews.call_ended(owner_call)
    assert owner_call not in interviews.talk_calls


def test_a_phone_call_about_nothing_is_a_talk_call(owner_call, monkeypatch):
    """call_now.call_talk_now's block has no interviews: the phone call opens in talk mode."""
    async def block(connection, block_id):
        return {"block_id": block_id, "start_utc": datetime.now(timezone.utc), "slot_keys": [],
                "label": call_now.TALK_LABEL, "state": "calling"}

    async def load_slots(connection, keys):
        return []

    monkeypatch.setattr(interview_store, "block", block)
    monkeypatch.setattr(interview_store, "load_slots", load_slots)
    monkeypatch.setitem(callbacks.calls, owner_call, callbacks.CallbackCall(
        "briefing-now-abc", 1, call_now.TALK_LABEL, "block:briefing-now-abc"))
    run(interviews.load_for_call(owner_call, "block:briefing-now-abc"))
    assert owner_call in interviews.talk_calls and owner_call not in interviews.briefings
    assert "Nothing's coming up" in callbacks.greeting(owner_call)


def test_call_talk_now_rings_once_with_no_interviews():
    class Client:
        def __init__(self):
            self.started = []

        async def start_workflow(self, run_, request, **kwargs):
            self.started.append(request)

            class Handle:
                async def signal(self, *args, **kw):
                    return None
            return Handle()

    client = Client()
    workflow_id = run(call_now.call_talk_now(client, f"tel:{PHONE}", PHONE))
    (request,) = client.started
    assert request.slot_keys == [] and request.label == call_now.TALK_LABEL and request.on_demand
    assert workflow_id.startswith("briefing-now-")


def test_call_my_phone_with_nothing_coming_up_still_calls(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VALR_OWNER_PHONE", PHONE)
    calls = []

    async def temporal():
        return object()

    async def upcoming(connection, start, end):
        return []

    async def setup(connection):
        return None

    async def talk_now(client_, owner_key, phone, **rpc):
        calls.append((owner_key, phone))
        return "briefing-now-x"

    from app import calendar_sync
    monkeypatch.setattr(callbacks, "temporal", temporal)
    monkeypatch.setattr(calendar_sync, "setup", setup)
    monkeypatch.setattr(calendar_sync, "upcoming", upcoming)
    monkeypatch.setattr(call_now, "call_talk_now", talk_now)
    token = client.get("/valr", headers=GOOD).text.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    response = client.post("/valr/api/call-now", json={},
                           headers={**GOOD, "X-CSRF-Token": token, "Content-Type": "application/json"})
    assert response.status_code == 200 and response.json()["mode"] == "talk"
    assert calls == [(owner.KEY, PHONE)]


# One memory for the owner, whichever way he calls.

def test_the_owner_browser_and_phone_keys_are_one_owner_key(owner_env):
    # Merging old phone/email memory into the owner key is app/owner.py's job (tests/test_owner.py).
    assert owner.canonical(f"email:{OWNER}") == owner.KEY
    assert owner.canonical(f"tel:{PHONE}") == owner.KEY
    assert owner.canonical("email:guest@example.com") == "email:guest@example.com"
    assert owner.canonical(None) is None


# The control.

def test_the_split_button_offers_both_choices_and_needs_a_phone_for_one():
    with_phone = shell.call_button(phone=True)
    assert 'data-has-phone="1"' in with_phone and "Call my phone" in with_phone and "Talk here" in with_phone
    assert with_phone.count('role="menuitemradio"') == 2 and "disabled" not in with_phone
    without = shell.call_button(phone=False)
    assert 'data-has-phone="0"' in without and 'data-choice="talk"' in without
    assert "Add your phone in Settings" in without and " disabled" in without
