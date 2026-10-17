"""LinkedIn message and InMail notification emails: a person's message, not LinkedIn's mail.
Every name, company and link here is fictional (tests/fixtures/lead_emails.json, e26-e31)."""
import asyncio
import base64
import json
from datetime import timedelta
from types import SimpleNamespace

from app import gmail_api, lead_classify, lead_worker, leads
from app import notifications as n
from app.lead_classify import Decision
from tests.leads_sample import NOW, fixture_emails

OWNER, EMAILS = fixture_emails()
OWNERS = {OWNER}
BY_ID = {item["id"]: email for email, item in EMAILS}


def model_reply(**fields):
    body = {"keep": True, "category": "recruiter_outreach", "confidence": 0.9, "reason": "recruiter outreach",
            "company": "", "role": "Voice AI Engineer", "sender_role": "recruiter", "stage_hint": "sourced",
            "sentiment": 0.5, "cue": "none", "summary": "A recruiter asked about a voice AI role.",
            "next_step": "Reply with a time", "action_requested": True, "action": "Share a time to chat"} | fields
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(body))],
                           usage=SimpleNamespace(input_tokens=500, output_tokens=90, cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


class Model:
    def __init__(self, **fields):
        self.fields, self.prompts, self.messages = fields, [], self

    def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        return model_reply(**self.fields)


def test_message_and_inmail_are_recognised_with_name_headline_text_and_link():
    message = lead_classify.linkedin_message(BY_ID["e26"])
    assert (message.name, message.company, message.inmail) == ("Priya Raman", "Acme Robotics", False)
    assert message.headline == "Talent Acquisition Lead at Acme Robotics"
    assert message.text.startswith("Hi, I came across your profile") and "intended for" not in message.text
    assert message.url == "https://www.linkedin.com/messaging/thread/2-QUNNRS1mYWtlLXRocmVhZA==/"  # No tracking.
    assert message.group == "linkedin:2-QUNNRS1mYWtlLXRocmVhZA=="
    inmail = lead_classify.linkedin_message(BY_ID["e27"])
    assert (inmail.name, inmail.company, inmail.inmail) == ("Dev Kapoor", "Halcyon Voice", True)


def test_an_inmail_with_its_own_subject_now_reaches_the_model():
    email = BY_ID["e27"]
    assert not lead_classify.HUMAN_ON_BOARD.search(f"{email.subject}\n{email.snippet}")  # The old rule dropped it.
    assert lead_classify.prefilter(email) is None


def test_job_alerts_and_digests_stay_dropped_without_the_model():
    for key in ("e08", "e28", "e29", "e30"):
        email = BY_ID[key]
        assert lead_classify.linkedin_message(email) is None, key
        early = lead_classify.prefilter(email)
        assert early is not None and not early.keep and early.method == "heuristic", key


def test_subject_patterns_from_other_linkedin_senders():
    base = BY_ID["e30"]
    for subject, name in [("Maya Lin sent you a message", "Maya Lin"), ("New message from Maya Lin", "Maya Lin"),
                          ("Maya just messaged you", "Maya"), ("InMail: a role at Corvid", "")]:
        email = SimpleNamespace(**{**base.__dict__, "subject": subject, "from_name": "LinkedIn",
                                   "from_email": "notifications-noreply@linkedin.com", "text": "", "snippet": ""})
        found = lead_classify.linkedin_message(email)
        assert found is not None and found.name == name and found.url == lead_classify.LINKEDIN_MESSAGING, subject
    other = SimpleNamespace(**{**base.__dict__, "subject": "Maya Lin sent you an invitation",
                               "from_email": "invitations@linkedin.com"})
    assert lead_classify.linkedin_message(other) is None


def test_the_model_sees_the_person_and_the_message_not_linkedins_wrapper():
    model = Model(company="LinkedIn", category="job_board_alert")
    decision = lead_classify.classify(BY_ID["e26"], OWNERS, model)
    prompt = model.prompts[0]
    assert "Channel: LinkedIn message" in prompt and "From: Priya Raman, Talent Acquisition Lead at Acme Robotics" in prompt
    assert "intended for" not in prompt and "messages-noreply" not in prompt and "https://" not in prompt.split("<email>")[1]
    # The model said "LinkedIn": the company is the sender's stated employer instead, and a kept
    # person's message is never filed as a job-board alert (those never notify).
    assert decision.keep and decision.company == "Acme Robotics" and decision.category == "recruiter_outreach"


def test_a_social_message_is_dropped_by_the_model():
    model = Model(keep=False, category="other", reason="personal congratulations", action_requested=False, action="")
    decision = lead_classify.decide(BY_ID["e31"], OWNERS, False, lambda e: lead_classify.classify(e, OWNERS, model))
    assert not decision.keep and decision.method == "llm" and len(model.prompts) == 1


def kept(email, **fields):
    return lead_classify.classify(email, OWNERS, Model(**fields))


def test_the_touch_is_from_the_person_with_no_email_address_and_links_to_linkedin():
    email = BY_ID["e26"]
    touch = lead_worker.email_touch(email, kept(email), OWNERS)
    assert (touch.title, touch.direction, touch.domain) == ("LinkedIn message", "in", "")
    assert [(p.email, p.name, p.role) for p in touch.people] == [("", "Priya Raman", "recruiter")]
    assert touch.company == "Acme Robotics" and touch.stated == "Acme Robotics" and touch.company_how == "named by the source"
    assert touch.url.startswith("https://www.linkedin.com/messaging/thread/") and touch.thread == "linkedin:2-QUNNRS1mYWtlLXRocmVhZA=="
    assert "linkedin.com" not in json.dumps([touch.company, touch.domain, [p.email for p in touch.people]]).lower()


def test_without_a_conversation_link_messages_group_by_the_sender():
    email = BY_ID["e11"]  # No headline, no link: the model's company, grouped by name.
    touch = lead_worker.email_touch(email, kept(email, company="Vela"), OWNERS)
    assert touch.thread == "linkedin:name:nina shah" and touch.url == "https://www.linkedin.com/messaging/"
    assert touch.company == "Vela" and [p.name for p in touch.people] == ["Nina Shah"]


def test_two_messages_of_one_conversation_are_one_lead_named_for_the_company():
    first = BY_ID["e26"]
    second = SimpleNamespace(**{**first.__dict__, "id": "e26b", "thread_id": "t-other", "at": first.at + timedelta(days=1)})
    touches = [lead_worker.email_touch(e, kept(e), OWNERS) for e in (first, second)]
    built = leads.build(touches, NOW + timedelta(days=2))
    assert len(built) == 1 and built[0].company == "Acme Robotics" and len(built[0].touches) == 2
    assert [c.email for c in built[0].contacts.values()] == [""]


def test_the_notification_says_who_messaged_on_linkedin_and_opens_linkedin():
    from tests.test_notifications import MemoryStore
    email = SimpleNamespace(**{**BY_ID["e26"].__dict__, "at": NOW - timedelta(minutes=5)})
    decision = kept(email)
    touch = lead_worker.email_touch(email, decision, OWNERS)
    store = MemoryStore()
    asyncio.run(n.from_email(store, email, decision, touch, True, NOW))
    [row] = store.rows
    assert row["title"] == "Priya (Acme Robotics) messaged you on LinkedIn"
    assert row["gmail_url"] == touch.url and row["thread_id"] == touch.thread
    assert (row["kind"], row["importance"]) == ("reply_needed", "immediate")  # The usual rules: she asked for a time.
    assert "Raman" not in json.dumps(row, default=str) and "@" not in row["title"]
    plain = lead_worker.email_touch(email, kept(email, action_requested=False, action=""), OWNERS)
    assert n.silent_note(plain, True)["kind"] == "reply" and n.importance("reply") == "digest"
    assert n.silent_note(plain, True)["title"] == "Priya (Acme Robotics) messaged you on LinkedIn"


def test_an_html_only_notification_keeps_the_conversation_link():
    html = ('<p>Priya Raman</p><p>Recruiter at Acme Robotics</p><p>Hi there</p>'
            '<a href="https://www.linkedin.com/comm/messaging/thread/2-SFRNTA==/?lipi=x&amp;trk=y">View message</a>')
    payload = {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}
    text = gmail_api.body_text(payload)
    assert lead_classify.linkedin_url(text) == ("https://www.linkedin.com/messaging/thread/2-SFRNTA==/", "2-SFRNTA==")


class FakeConnection:
    """lead_emails holds e26 (a message), e28 (an alert) and e31 (a message) as dropped."""

    def __init__(self):
        self.updated = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        if sql.startswith("SELECT message_id FROM lead_emails"):
            return SimpleNamespace(fetchall=lambda: asyncio.sleep(0, [("e26",), ("e28",), ("e31",)]))
        if sql.startswith("UPDATE lead_emails"):
            self.updated.append(list(params[0]))
            return SimpleNamespace(rowcount=len(params[0]))
        return SimpleNamespace(fetchall=lambda: asyncio.sleep(0, []))


def test_relinkedin_queues_dropped_linkedin_messages_for_the_next_sync(monkeypatch):
    from app import interview_worker, leads_store
    from tests.test_lead_worker import raw_message
    connection = FakeConnection()

    async def connect():
        return connection

    async def access(conn, http):
        return "token", "connected"

    async def message(http, token, message_id):
        return raw_message(BY_ID[message_id])

    async def setup(conn):
        return None
    monkeypatch.setattr(lead_worker, "connect", connect)
    monkeypatch.setattr(interview_worker, "gmail_access", access)
    monkeypatch.setattr(gmail_api, "message", message)
    monkeypatch.setattr(leads_store, "setup", setup)
    dry = asyncio.run(lead_worker.relinkedin(dry_run=True))
    assert (dry["checked"], dry["linkedin_messages"], dry["queued_for_next_sync"]) == (3, 2, 0) and not connection.updated
    real = asyncio.run(lead_worker.relinkedin())
    assert real["queued_for_next_sync"] == 2 and connection.updated == [["e26", "e31"]]
    assert "Priya" not in json.dumps(real) and "Acme" not in json.dumps(real)  # Counts only.
