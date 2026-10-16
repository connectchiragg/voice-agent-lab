"""The lead tracker's pure logic: one lead per company or opportunity, built from touchpoints.

A touchpoint is one dated event about the owner's job search: a kept Gmail message, a
calendar interview, a Fathom recording or a scored interview from performance.json. Leads
are rebuilt from all touchpoints on every refresh, in time order, so the result does not
depend on the order in which sources arrived:

1. Merge (`Index.find`): a touchpoint that names a company joins the lead with the same
   company (difflib ratio >= 0.88 after removing legal suffixes, or one name the other's
   leading words: "Juniper" ~ "Juniper Robotics"), else the lead with its own company domain
   (an agency's or a tool's domain never counts), else it starts a new lead. One that names
   no company joins by domain, then by one of its people (email), then by a person's name
   (ratio >= 0.9). So an agency recruiter pitching two clients links two leads, not merges
   them. Emails and calendar interviews may start a lead; recordings and scores only attach.
2. Stage (`stage_of`): sourced -> applied -> screening -> interviewing -> offer, moving
   forward only, from each touchpoint's stage hint; closed-won and closed-lost when a
   touchpoint says so (a later forward hint at screening or beyond re-opens). A lead that
   is not closed and has had no touch for 21 days, with nothing scheduled, is ghosted.
3. Expiry: no touch for 30 days and nothing scheduled -> status "expired" (archived,
   hidden by default). Any new touch revives it.
4. Temperature 0-100 (`temperature`), shown with its parts on every lead:
       recency   35 x 0.5^(days since last touch / 7)            (half-life one week)
       stage     25 x stage weight                                 (table STAGE_WEIGHT)
       momentum  12 x min(1, touches in the last 14 days / 5)
                 + 8 if an interview is on the calendar in the next 14 days
       quality   performance: 8 x (mean score for the company / 10), else 4 (no data)
                 + sentiment: 6 x (mean of the last 3 inbound email sentiments + 1) / 2, else 3
                 + cue: latest explicit cue positive 6, none 3, negative 0
   closed-lost is capped at 10 and ghosted at 20.

Standard library only; times are aware datetimes in UTC.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

FORWARD = ("sourced", "applied", "screening", "interviewing", "offer")
CLOSED = ("closed-won", "closed-lost")
STAGES = FORWARD + CLOSED + ("ghosted",)
STAGE_WEIGHT = {"sourced": 0.2, "applied": 0.35, "screening": 0.55, "interviewing": 0.8,
                "offer": 1.0, "closed-won": 1.0, "closed-lost": 0.0, "ghosted": 0.05}
EXPIRE_AFTER = timedelta(days=30)
GHOSTED_AFTER = timedelta(days=21)
MOMENTUM_WINDOW = timedelta(days=14)
HALF_LIFE_DAYS = 7.0
COMPANY_RATIO = 0.88
NAME_RATIO = 0.9
CONTACT_ROLES = ("recruiter", "hiring_manager", "interviewer", "coordinator", "other")
KINDS = ("email", "calendar", "recording", "evaluation")
CREATES_LEADS = ("email", "calendar")

PERSONAL_DOMAINS = frozenset("""
    gmail.com googlemail.com yahoo.com yahoo.co.in outlook.com hotmail.com live.com icloud.com
    me.com proton.me protonmail.com rediffmail.com aol.com zoho.com
    """.split())
# Job boards, ATS and scheduling tools: their domain never names the company.
TOOL_DOMAINS = frozenset("""
    linkedin.com naukri.com instahyre.com indeed.com glassdoor.com monster.com foundit.in
    iimjobs.com hirist.com hirist.tech wellfound.com angel.co cutshort.io uplers.com
    internshala.com shine.com timesjobs.com ziprecruiter.com dice.com apna.co hired.com otta.com
    welcometothejungle.com turing.com toptal.com unstop.com
    greenhouse.io greenhouse-mail.io lever.co hire.lever.co ashbyhq.com goodtime.io gem.com
    calendly.com workday.com myworkday.com smartrecruiters.com modernloop.io icims.com jobvite.com
    recruitee.com workable.com breezy.hr darwinbox.in keka.com zohorecruit.com hackerrank.com
    hackerearth.com codility.com coderpad.io karat.com zoom.us calendar.google.com
    fathom.video fireflies.ai otter.ai metaview.ai brighthire.ai
    """.split())
LEGAL = re.compile(r"\b(inc|incorporated|llc|llp|ltd|limited|pvt|private|corp|corporation|co|gmbh|"
                   r"plc|technologies|technology|software|solutions|labs?|india|global|hq|group)\b\.?")
SECOND_LEVEL = ("co", "com", "net", "org", "ac", "gov")


@dataclass
class Contact:
    email: str = ""
    name: str = ""
    role: str = "other"

    @property
    def id(self) -> str:
        basis = self.email.lower() or "name:" + norm_name(self.name)
        return "c-" + hashlib.sha1(basis.encode()).hexdigest()[:10]


@dataclass
class Touch:
    id: str  # "gmail:<message id>", "cal:<event key>", "fathom:<recording id>", "perf:<interview id>"
    kind: str  # email | calendar | recording | evaluation
    at: datetime
    company: str = ""
    domain: str = ""
    people: list[Contact] = field(default_factory=list)
    title: str = ""
    summary: str = ""
    direction: str = ""  # in | out (emails)
    sentiment: float | None = None  # -1..1 (emails)
    cue: str = "none"  # positive | negative | none
    stage_hint: str = ""
    next_step: str = ""
    url: str = ""  # Gmail thread or Fathom share link
    detail_url: str = ""  # The VALR interview detail
    score: float | None = None  # 0-10 (evaluation)
    role: str = ""  # The job title, when the touchpoint names it
    lead_id: str = ""


@dataclass
class Lead:
    id: str
    company: str
    key: str
    domain: str = ""
    role: str = ""
    contacts: dict[str, Contact] = field(default_factory=dict)  # by Contact.id
    touches: list[Touch] = field(default_factory=list)
    stage: str = "sourced"
    status: str = "active"  # active | expired
    temperature: int = 0
    parts: dict[str, Any] = field(default_factory=dict)
    last_touch_at: datetime | None = None
    next_step: str = ""
    next_step_at: datetime | None = None


# Names and domains.

def norm_name(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def company_key(name: str) -> str:
    """"Acme Technologies Pvt. Ltd." -> "acme"; "" when nothing is left."""
    text = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    text = LEGAL.sub(" ", text)
    return " ".join(text.split())


def domain_of(email: str) -> str:
    return (email or "").rpartition("@")[2].strip().lower()


def is_tool_domain(domain: str) -> bool:
    return any(domain == tool or domain.endswith("." + tool) for tool in TOOL_DOMAINS)


def company_domain(domain: str) -> str:
    """The registrable part of a company's domain; "" for personal and tool domains."""
    domain = (domain or "").lower().strip(".")
    if not domain or domain in PERSONAL_DOMAINS or is_tool_domain(domain):
        return ""
    labels = domain.split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_company(domain: str) -> str:
    """"careers.acme.example" -> "Acme"."""
    core = company_domain(domain)
    if not core:
        return ""
    labels = core.split(".")
    label = labels[-3] if len(labels) >= 3 and labels[-2] in SECOND_LEVEL else labels[0]
    return label.replace("-", " ").title()


def similar(a: str, b: str, threshold: float) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if a[0] != b[0] or min(len(a), len(b)) < 4:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def same_company(a: str, b: str) -> bool:
    """Fuzzy equal, or one is the other's leading words ("juniper" ~ "juniper robotics")."""
    if similar(a, b, COMPANY_RATIO):
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and (long_ + " ").startswith(short + " ")


def own_domain(touch: "Touch") -> str:
    """The touch's company domain, only if it is the named company's own (not an agency's)."""
    domain = company_domain(touch.domain)
    key = company_key(touch.company)
    if not domain or not key:
        return domain
    return domain if same_company(company_key(domain_company(domain)), key) else ""


def lead_id_for(key: str) -> str:
    return "lead-" + hashlib.sha1(key.encode()).hexdigest()[:12]


# Merging touchpoints into leads.

class Index:
    def __init__(self) -> None:
        self.leads: dict[str, Lead] = {}
        self.by_email: dict[str, str] = {}
        self.by_domain: dict[str, str] = {}

    def add(self, lead: Lead) -> Lead:
        self.leads[lead.id] = lead
        if lead.domain:
            self.by_domain.setdefault(lead.domain, lead.id)
        return lead

    def find(self, touch: Touch) -> Lead | None:
        """Its company by name, else by its own domain; people only when it names no company
        (an agency recruiter pitching two clients must not merge them)."""
        key = company_key(touch.company)
        if key:
            for lead in self.leads.values():
                if same_company(key, lead.key):
                    return lead
        domain = own_domain(touch)
        if domain and domain in self.by_domain:
            return self.leads[self.by_domain[domain]]
        if key:
            return None
        for person in touch.people:
            if person.email and person.email.lower() in self.by_email:
                return self.leads[self.by_email[person.email.lower()]]
        names = [norm_name(p.name) for p in touch.people if len(norm_name(p.name).split()) >= 2]
        if names:
            for lead in self.leads.values():
                for contact in lead.contacts.values():
                    if any(similar(n, norm_name(contact.name), NAME_RATIO) for n in names):
                        return lead
        return None

    def attach(self, lead: Lead, touch: Touch) -> None:
        touch.lead_id = lead.id
        lead.touches.append(touch)
        domain = own_domain(touch)
        if domain and not lead.domain:
            lead.domain = domain
            self.by_domain.setdefault(domain, lead.id)
        if touch.role:
            lead.role = touch.role
        if touch.company and lead.company.startswith("Unknown"):
            lead.company = touch.company
        for person in touch.people:
            existing = lead.contacts.get(person.id)
            if existing is None:
                lead.contacts[person.id] = Contact(person.email.lower(), person.name, person.role)
            else:
                existing.name = existing.name or person.name
                if existing.role == "other" and person.role != "other":
                    existing.role = person.role
            if person.email:
                self.by_email.setdefault(person.email.lower(), lead.id)


def new_lead_for(touch: Touch) -> Lead | None:
    name = touch.company.strip() or domain_company(touch.domain)
    key = company_key(name)
    if not key:
        person = next((p for p in touch.people if p.email or p.name), None)
        if person is None:
            return None
        key = "person:" + (person.email.lower() or norm_name(person.name))
        name = f"Unknown ({person.name or domain_of(person.email) or 'contact'})"
    return Lead(id=lead_id_for(key), company=name, key=key, domain=own_domain(touch))


def build(touches: list[Touch], now: datetime) -> list[Lead]:
    """All leads from all touchpoints (time order); then stage, status and temperature."""
    index = Index()
    for touch in sorted(touches, key=lambda t: (t.at, t.id)):
        lead = index.find(touch)
        if lead is None and touch.kind in CREATES_LEADS:
            lead = new_lead_for(touch)
            if lead is not None:
                lead = index.leads.get(lead.id) or index.add(lead)
        if lead is not None:
            index.attach(lead, touch)
        else:
            touch.lead_id = ""
    for lead in index.leads.values():
        evaluate(lead, now)
    return sorted(index.leads.values(), key=lambda l: (-l.temperature, l.company.lower()))


# Stage, status, next step and temperature.

def stage_of(touches: list[Touch]) -> str:
    """Forward only; a closing hint closes; after closed-lost, a hint at screening or
    beyond (a new round, say) re-opens. closed-won stays."""
    stage = "sourced"
    for touch in sorted(touches, key=lambda t: t.at):
        hint = touch.stage_hint
        if hint in CLOSED:
            stage = hint
        elif hint in FORWARD:
            if stage == "closed-lost":
                if FORWARD.index(hint) >= FORWARD.index("screening"):
                    stage = hint
            elif stage not in CLOSED and FORWARD.index(hint) > FORWARD.index(stage):
                stage = hint
    return stage


def upcoming(lead: Lead, now: datetime) -> list[Touch]:
    return sorted((t for t in lead.touches if t.kind == "calendar" and t.at > now), key=lambda t: t.at)


def evaluate(lead: Lead, now: datetime) -> None:
    past = [t for t in lead.touches if t.at <= now]
    lead.last_touch_at = max((t.at for t in past), default=None)
    future = upcoming(lead, now)
    stage = stage_of(lead.touches)
    idle = now - lead.last_touch_at if lead.last_touch_at else EXPIRE_AFTER
    if stage not in CLOSED and not future and idle > GHOSTED_AFTER:
        stage = "ghosted"
    lead.stage = stage
    lead.status = "active" if future or idle <= EXPIRE_AFTER else "expired"
    if future:
        lead.next_step, lead.next_step_at = f"Interview: {future[0].title or 'on the calendar'}", future[0].at
    else:
        step = next((t for t in sorted(past, key=lambda t: t.at, reverse=True)
                     if t.next_step and now - t.at <= MOMENTUM_WINDOW), None)
        lead.next_step, lead.next_step_at = (step.next_step, None) if step else ("", None)
    lead.temperature, lead.parts = temperature(lead, now)


def temperature(lead: Lead, now: datetime) -> tuple[int, dict[str, Any]]:
    past = [t for t in lead.touches if t.at <= now]
    days = (now - lead.last_touch_at).total_seconds() / 86400 if lead.last_touch_at else None
    recency = 35 * 0.5 ** (days / HALF_LIFE_DAYS) if days is not None else 0.0
    stage = 25 * STAGE_WEIGHT.get(lead.stage, 0)
    recent = sum(1 for t in past if now - t.at <= MOMENTUM_WINDOW)
    scheduled = any(t.at - now <= MOMENTUM_WINDOW for t in upcoming(lead, now))
    frequency = 12 * min(1.0, recent / 5)
    next_part = 8.0 if scheduled else 0.0
    scores = [t.score for t in past if t.kind == "evaluation" and t.score is not None]
    perf = 8 * (sum(scores) / len(scores)) / 10 if scores else 4.0
    inbound = sorted((t for t in past if t.kind == "email" and t.direction == "in"
                      and t.sentiment is not None), key=lambda t: t.at)[-3:]
    mean_sentiment = sum(t.sentiment for t in inbound) / len(inbound) if inbound else None  # type: ignore[misc]
    sentiment = 6 * (mean_sentiment + 1) / 2 if mean_sentiment is not None else 3.0
    cued = [t for t in sorted(past, key=lambda t: t.at) if t.cue in ("positive", "negative")]
    latest_cue = cued[-1].cue if cued else "none"
    cue = {"positive": 6.0, "negative": 0.0}.get(latest_cue, 3.0)
    raw = recency + stage + frequency + next_part + perf + sentiment + cue
    cap = {"closed-lost": 10, "ghosted": 20}.get(lead.stage)
    total = int(round(max(0.0, min(100.0, raw if cap is None else min(raw, cap)))))
    parts = {
        "recency": {"points": round(recency, 1), "max": 35,
                    "why": f"last touch {days:.0f} days ago (halves every 7 days)" if days is not None else "no touch yet"},
        "stage": {"points": round(stage, 1), "max": 25, "why": f"stage {lead.stage}"},
        "momentum": {"points": round(frequency + next_part, 1), "max": 20,
                     "why": f"{recent} touches in 14 days" + ("; interview scheduled" if scheduled else "")},
        "quality": {"points": round(perf + sentiment + cue, 1), "max": 20,
                    "why": "; ".join([
                        f"interview score {sum(scores) / len(scores):.1f}/10" if scores else "no interview score",
                        f"recruiter tone {mean_sentiment:+.1f}" if mean_sentiment is not None else "no recruiter tone",
                        f"latest cue {latest_cue}"]),
                    "performance": round(perf, 1), "sentiment": round(sentiment, 1), "cue": round(cue, 1)},
        "cap": cap, "total": total,
    }
    return total, parts


# The view: leads, contacts and links for the page and the globe.

def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def touch_view(touch: Touch) -> dict[str, Any]:
    return {"id": touch.id, "kind": touch.kind, "at": iso(touch.at), "title": touch.title[:140],
            "summary": touch.summary[:280], "direction": touch.direction, "cue": touch.cue,
            "stage_hint": touch.stage_hint, "url": touch.url if touch.url.startswith("https://") else "",
            "detail_url": touch.detail_url if touch.detail_url.startswith("/") else "",
            "score": touch.score}


def graph(leads: list[Lead], now: datetime) -> dict[str, Any]:
    """{leads, contacts, links}: contacts link to their leads; leads that share a person link
    to each other (kind "shared", weight = people shared)."""
    contacts: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    out = []
    for lead in leads:
        recent = sum(1 for t in lead.touches if t.at <= now and now - t.at <= EXPIRE_AFTER)
        out.append({
            "id": lead.id, "company": lead.company, "role": lead.role, "domain": lead.domain,
            "stage": lead.stage, "status": lead.status, "temperature": lead.temperature,
            "parts": lead.parts, "activity": recent, "touches": len(lead.touches),
            "last_touch_at": iso(lead.last_touch_at), "next_step": lead.next_step,
            "next_step_at": iso(lead.next_step_at),
            "contacts": [{"id": c.id, "name": c.name, "email": c.email, "role": c.role}
                         for c in lead.contacts.values()],
            "timeline": [touch_view(t) for t in sorted(lead.touches, key=lambda t: t.at, reverse=True)][:40],
        })
        for contact in lead.contacts.values():
            entry = contacts.setdefault(contact.id, {"id": contact.id, "name": contact.name or contact.email,
                                                     "role": contact.role, "leads": []})
            entry["leads"].append(lead.id)
            links.append({"source": lead.id, "target": contact.id, "kind": "contact"})
    shared: dict[tuple[str, str], int] = {}
    for contact in contacts.values():
        ids = sorted(set(contact["leads"]))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                shared[(a, b)] = shared.get((a, b), 0) + 1
    links += [{"source": a, "target": b, "kind": "shared", "weight": n} for (a, b), n in sorted(shared.items())]
    return {"leads": out, "contacts": list(contacts.values()), "links": links,
            "stages": list(STAGES), "generated_at": iso(now)}
