"""The one company resolver (app/company_names.py) and its review pipeline
(app/company_resolver.py): table-driven rules, then a fake LLM and fake search. Every
name, company and address here is made up; the owner is "Sam Rivera"."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import company_names as names
from app import company_resolver as resolver
from app import leads
from app.leads import Contact, Overrides, Touch

OWNER_ENV = {"VALR_OWNER_NAME": "Sam", "VALR_CANDIDATE_NAME": "Sam Rivera",
             "VALR_OWNER_EMAILS": "sam.rivera@gmail.com"}


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    for key, value in OWNER_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("title, company", [
    ("Interview -L2- Sam", ""),
    ("L2- Sam", ""),
    ("Interview -L2- sam rivera", ""),
    ("Round 2", ""),
    ("Debrief", ""),
    ("Zoom meeting", ""),
    ("Google Meet", ""),
    ("Fathom", ""),
    ("HM / Final - Sam", ""),
    ("Screen Call interview with Aspire", "Aspire"),
    ("Sam -In Person Post offer connect at Zinnov Bengaluru [In-person]", "Zinnov"),
    ("Juniper Robotics: Senior Backend, system design", "Juniper Robotics"),
    ("Orbit <> Sam Rivera", "Orbit"),
    ("Sam x Quillworks | Intro", "Quillworks"),
    ("Interview with Acme Robotics (HM round)", "Acme Robotics"),
    ("Technical round - Kite", "Kite"),
])
def test_titles(title, company):
    assert names.resolve(title=title).name == company


@pytest.mark.parametrize("kwargs, company, source", [
    # External attendee domain first; free-mail ignored; the title only spells it.
    ({"attendees": ["maya@acme-robotics.example", "pat@gmail.com"], "title": "Interview -L2- Sam"},
     "Acme Robotics", "attendees"),
    ({"attendees": ["maya@juniper.example"], "title": "Juniper Robotics: hiring manager chat"},
     "Juniper Robotics", "attendees"),
    # Free-mail-only attendees and a junk title: nothing.
    ({"attendees": ["pat@gmail.com", "lee@outlook.com"], "title": "Interview -L2- Sam"}, "", "none"),
    # The owner's own address never names a company.
    ({"attendees": ["sam.rivera@gmail.com", "sam@rivera-consulting.example"], "title": "Sync",
      "owner_emails": ["sam@rivera-consulting.example"]}, "", "none"),
    # An ATS sender: the company from the display name, else the subject.
    ({"sender_email": "no-reply@greenhouse-mail.io", "sender_name": "Acme Robotics via Greenhouse",
      "title": "Thank you for applying"}, "Acme Robotics", "ats"),
    ({"sender_email": "jobs@hire.lever.co", "sender_name": "Lever",
      "title": "Your application to Nimbus Labs - Backend Engineer"}, "Nimbus Labs", "ats"),
    # A scheduler is a recruiter's tool, not the company.
    ({"sender_email": "notifications@calendly.com", "sender_name": "Calendly", "title": "New event: Sam Rivera"},
     "", "none"),
    # A company sender's own domain.
    ({"sender_email": "talent@quillworks.example", "sender_name": "Quillworks Talent", "title": "Next steps"},
     "Quillworks", "sender"),
    # The last resort: a lead with the same person.
    ({"attendees": ["pat@gmail.com"], "title": "Catch up",
      "leads": [{"company": "Vela", "contacts": [{"email": "pat@gmail.com"}]}]}, "Vela", "lead"),
])
def test_evidence_order(kwargs, company, source):
    found = names.resolve(**kwargs)
    assert (found.name, found.source) == (company, source)
    assert found.label == (company or "Company not identified")


def test_domains_and_names():
    assert names.from_domain("careers.acme-robotics.com") == "Acme Robotics"
    assert names.from_domain("hr.acme.co.example") == "Acme"
    for domain in ("gmail.com", "greenhouse.io", "lever.co", "ashbyhq.com", "calendly.com", "goodtime.io",
                   "myworkday.com", "smartrecruiters.com", "zoom.us", "fathom.video"):
        assert names.from_domain(domain) == "", domain
    assert names.owner_words() >= {"sam", "rivera"}
    assert not names.is_company("RIVERA") and not names.is_company("In-person") and names.is_company("Zinnov")
    assert names.label("L2- Sam") == "Company not identified" and names.label("Kite") == "Kite"


# The lead tracker drops junk names, even from old stored data, and honours "Set company".

def at(day):
    return datetime(2026, 10, day, 9, tzinfo=timezone.utc)


def test_leads_never_named_after_a_round_label_and_rename_wins():
    touches = [Touch("cal:a", "calendar", at(1), company="L2- Sam", people=[Contact("pat@gmail.com", "Pat Lee")],
                     title="Interview -L2- Sam", stage_hint="interviewing"),
               Touch("gmail:b", "email", at(2), company="Aspire", domain="aspire.example",
                     people=[Contact("ravi@aspire.example", "Ravi")], title="Screen call", stage_hint="screening")]
    built = {l.company: l for l in leads.build(touches, at(3))}
    assert "L2- Sam" not in built and "Aspire" in built
    assert "Company not identified" in built  # The person-only lead, until he names it.
    lost = built["Company not identified"]
    renamed = leads.build(touches, at(3), Overrides(names={lost.id: "Zinnov"}))
    assert {l.company for l in renamed} == {"Aspire", "Zinnov"}
    assert next(l for l in renamed if l.company == "Zinnov").company_how == "set by you"


# The review pipeline: fake gateway, fake search, in-memory tables.

class FakeConn:
    """Just the SQL company_resolver issues, in memory."""

    def __init__(self):
        self.cache, self.overrides, self.today = {}, {}, 0

    async def execute(self, sql, params=()):
        rows = []
        if sql.lstrip().startswith("CREATE"):
            pass
        elif "FROM company_resolutions WHERE fingerprint" in sql:
            row = self.cache.get(params[0])
            rows = [row] if row else []
        elif "INSERT INTO company_resolutions" in sql:
            fp, company, domain, agency, role, conf, source, how, citation, usd = params
            self.cache[fp] = (company, domain, conf, source, how, citation, agency, role)
            self.today += 1
        elif "count(*) FROM company_resolutions" in sql:
            rows = [(self.today,)]
        elif "FROM company_overrides WHERE entity_id = %s" in sql:
            rows = [(self.overrides[params[0]],)] if params[0] in self.overrides else []
        elif "INSERT INTO company_overrides" in sql:
            self.overrides[params[0]] = params[1]
        elif "research_spend" in sql or "research_jobs" in sql:
            pass
        return SimpleNamespace(fetchone=lambda: _first(rows), fetchall=lambda: _all(rows))

    def transaction(self):
        class Tx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False
        return Tx()


async def _first(rows):
    return rows[0] if rows else None


async def _all(rows):
    return rows


class FakeClient:
    """messages.create: scripted JSON answers; a call with tools counts as a web search."""

    def __init__(self, *answers, web=None):
        self.answers, self.web, self.calls = list(answers), web, []
        self.messages = self

    def create(self, **kw):
        searched = bool(kw.get("tools"))
        self.calls.append("web" if searched else "review")
        body = self.web if searched else self.answers.pop(0)
        content = [SimpleNamespace(type="text", text=json.dumps(body))]
        if searched:
            content.insert(0, SimpleNamespace(type="web_search_tool_result",
                                              content=[SimpleNamespace(url="https://news.example/zinnov-hires")]))
        usage = SimpleNamespace(input_tokens=900, output_tokens=80, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0,
                                server_tool_use=SimpleNamespace(web_search_requests=1) if searched else None)
        return SimpleNamespace(content=content, usage=usage)


def run(coro):
    return asyncio.run(coro)


def make(client, **kw):
    return resolver.Resolver(client=(lambda: client) if client else None, owner_emails={"sam.rivera@gmail.com"},
                             candidate="Sam Rivera", **kw)


UNCLEAR = resolver.Evidence(kind="calendar", title="Interview -L2- Sam",
                            people=[("Pat Lee", "pat@gmail.com"), ("Ravi Nair", "ravi@talentbridge.example"),
                                    ("Mia Chen", "mia@zinnov.example")])


def test_a_confident_rule_skips_the_model():
    client = FakeClient()
    ev = resolver.Evidence(kind="calendar", title="Technical round", people=[("Maya", "maya@acme-robotics.example")])
    got = run(make(client).resolve(FakeConn(), ev))
    assert (got.company, got.source, got.how) == ("Acme Robotics", "rules", "attendee domain acme-robotics.example")
    assert client.calls == []


def test_low_confidence_asks_the_model_and_caches():
    client = FakeClient({"company": "Zinnov", "domain": "zinnov.example", "confidence": 0.86, "agency": "TalentBridge",
                         "reasoning": "The interviewer is at Zinnov; TalentBridge is the agency.", "needs_web": False})
    conn, r = FakeConn(), make(client)
    got = run(r.resolve(conn, UNCLEAR))
    assert (got.company, got.source, got.agency) == ("Zinnov", "llm", "TalentBridge")
    assert got.how.startswith("AI review:") and client.calls == ["review"] and r.usd > 0
    again = run(make(FakeClient()).resolve(conn, UNCLEAR))  # Cached: no model call.
    assert (again.company, again.source) == ("Zinnov", "cache")


def test_needs_web_searches_then_decides_with_a_citation():
    client = FakeClient({"company": "", "confidence": 0.3, "reasoning": "Unclear.", "needs_web": True},
                        web={"company": "Zinnov", "domain": "zinnov.example", "confidence": 0.8,
                             "reasoning": "The domain belongs to Zinnov.", "source_url": ""})
    got = run(make(client).resolve(FakeConn(), UNCLEAR))
    assert client.calls == ["review", "web"]
    assert (got.company, got.source, got.citation) == ("Zinnov", "web", "https://news.example/zinnov-hires")
    assert got.how == "found via web: https://news.example/zinnov-hires"


def test_still_unclear_after_research_is_not_identified():
    client = FakeClient({"company": "Zinnov", "confidence": 0.4, "needs_web": True},
                        web={"company": "Zinnov", "confidence": 0.45, "reasoning": "Weak."})
    got = run(make(client).resolve(FakeConn(), UNCLEAR))
    assert got.company == "" and got.label == "Company not identified"


@pytest.mark.parametrize("answer", ["Sam Rivera", "L2", "Greenhouse", "gmail.com", "Zoom", "Round 2 - Sam"])
def test_the_model_can_never_name_the_owner_a_round_or_a_tool(answer):
    client = FakeClient({"company": answer, "confidence": 0.99, "needs_web": False})
    got = run(make(client).resolve(FakeConn(), UNCLEAR))
    assert got.company == "", answer


def test_daily_cap_and_month_cap_fall_back_to_the_rules(monkeypatch):
    monkeypatch.setenv("VALR_COMPANY_LLM_PER_DAY", "1")
    conn = FakeConn()
    conn.today = 1
    client = FakeClient()
    got = run(make(client).resolve(conn, UNCLEAR))
    assert client.calls == [] and got.source in ("rules", "none")
    monkeypatch.setenv("VALR_COMPANY_LLM_PER_DAY", "100")
    got = run(make(client, month_ok=lambda c: False).resolve(FakeConn(), UNCLEAR))
    assert client.calls == []


def test_the_owners_override_always_wins():
    conn = FakeConn()
    run(resolver.set_override(conn, "interview:fathom:1", "Zinnov", "owner"))
    client = FakeClient()
    got = run(make(client).resolve(conn, UNCLEAR, entity_id="interview:fathom:1"))
    assert (got.company, got.source, got.how) == ("Zinnov", "override", "set by you") and client.calls == []


def test_the_model_sees_domains_not_addresses():
    ev = resolver.Evidence(kind="email", title="Next steps", people=[("Pat", "pat.lee@zinnov.example")],
                           sender_name="Pat Lee", sender_email="pat.lee@zinnov.example", snippet="x" * 900)
    text = ev.render([])
    assert "pat.lee@" not in text and "@zinnov.example" in text and "x" * 301 not in text
    assert ev.fingerprint() == resolver.Evidence(kind="email", title="Re: Next steps", people=ev.people,
                                                 sender_name="Pat Lee", sender_email=ev.sender_email).fingerprint()


def test_without_owner_settings_the_recorders_side_is_still_never_a_company(monkeypatch):
    from app.notetakers import base
    for key in OWNER_ENV:
        monkeypatch.delenv(key, raising=False)
    invitees = [{"name": "Dana Okoro", "email": "dana@gmail.com", "is_external": False},
                {"name": "Pat Lee", "email": "pat@gmail.com", "is_external": True}]
    details = base.invitee_details(invitees, set(), "Interview -L2- Dana", recorder="Dana Okoro")
    assert details["company"] == "" and details["interviewers"] == ["Pat Lee"]
    assert names.resolve(title="Interview -L2- Dana", owner_names=["Dana Okoro"]).name == ""
    assert names.clean("Razorpay Software Private Limited") == "Razorpay"


def test_repair_re_derives_touch_companies_idempotently():
    pytest.importorskip("temporalio")
    from app import lead_worker
    touches = [Touch("cal:a", "calendar", at(1), company="L2- Sam", title="Interview -L2- Sam",
                     people=[Contact("mia@zinnov.example", "Mia Chen")]),
               Touch("gmail:b", "email", at(2), company="", title="Your application", direction="in",
                     people=[Contact("no-reply@greenhouse-mail.io", "Aspire via Greenhouse")])]
    conn = FakeConn()
    client = FakeClient({"company": "Aspire", "confidence": 0.9, "reasoning": "Greenhouse mail for Aspire."})
    changes = run(lead_worker.resolve_touches(conn, touches, make(client), dry_run=True))
    assert [(c[1], c[2]) for c in changes] == [("L2- Sam", "Zinnov"), ("", "Aspire")]
    assert client.calls == ["review"]  # The attendee domain was confident; the ATS mail was reviewed.
    assert touches[0].stated == "L2- Sam" and touches[0].company_how == "attendee domain zinnov.example"
    assert run(lead_worker.resolve_touches(conn, touches, make(FakeClient()), dry_run=True)) == []  # Idempotent.
