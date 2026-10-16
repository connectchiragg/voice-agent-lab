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
       recency   30 x 0.5^(days since THEIR last move / 7)       (half-life one week; his own
                                                                 follow-ups do not warm a lead)
       stage     30 x stage weight                                 (table STAGE_WEIGHT)
       momentum  next step: interview in <= 7 days 15, in <= 14 days 9, an open offer 15
                 + 10 x min(1, touches in the last 14 days / 4)
       quality   performance: 5 x (mean score for the company / 10), else 2.5 (no data)
                 + tone: 5 x (mean of the last 3 inbound email sentiments + 1) / 2, else 2.5
                 + cue: latest explicit cue positive 5, none 2.5, negative 0
   closed-lost is capped at 10 and ghosted at 24 (always Cold).
5. Band (`band_of`): hot >= 75 (an active process, next step soon), warm >= 50 (engaged
   recently), cool >= 25 (applied or sourced, waiting), cold < 25 (quiet or ghosted);
   closed-won and closed-lost are "closed" whatever the score. Status: "expired" (30 quiet
   days, nothing scheduled), else "closed", else "active" (= live).
6. Ball in court (`ball_in_court`): waiting_on "you" | "them" | "nobody" with a reason.
   See the function; cold, closed and expired leads are always "nobody".

Company names come from app/company_names.py (the one resolver): a touchpoint's `company` is
dropped when it is not a company (a round label, the owner's name, a tool), and a lead with
no company is shown as "Company not identified" until the owner names it (a "rename"
override). `company_how` says how the name was found ("attendee domain acme.com", "found via
web: ...", "set by you"). Lead ids come from the company, so a better name changes a lead's
id: `lead_aliases` maps each vanished id to the lead now holding its touchpoints, the owner's
corrections follow it, and the worker re-keys `lead_overrides` (leads_store.move_overrides).

Standard library only; times are aware datetimes in UTC.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from app import company_names

FORWARD = ("sourced", "applied", "screening", "interviewing", "offer")
CLOSED = ("closed-won", "closed-lost")
STAGES = FORWARD + CLOSED + ("ghosted",)
STAGE_WEIGHT = {"sourced": 0.2, "applied": 0.3, "screening": 0.55, "interviewing": 0.8,
                "offer": 1.0, "closed-won": 1.0, "closed-lost": 0.0, "ghosted": 0.1}
# Bands, coldest first: (name, lowest score). Closed leads are their own band.
BANDS = (("cold", 0), ("cool", 25), ("warm", 50), ("hot", 75))
BAND_NAMES = tuple(name for name, _ in BANDS) + ("closed",)
CAPS = {"closed-lost": 10, "ghosted": 24}
SOON = timedelta(days=7)
ASK_MAX_AGE = timedelta(days=14)  # An unanswered request older than this: nobody (cooling).
THEM_MAX_AGE = timedelta(days=21)  # The same as GHOSTED_AFTER.
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
    thread: str = ""  # Gmail thread id (emails)
    lead_id: str = ""
    category: str = ""  # The classifier's category (emails; "" on rows stored before it was kept)
    action_requested: bool | None = None  # The email asks him to do something (None: not known)
    action: str = ""  # What it asks, in a few words
    stated: str = ""  # The company the source itself gave (classifier, calendar, scorecard)
    company_how: str = ""  # How `company` was found (app/company_resolver.py)


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
    stage_manual: bool = False  # The owner set the stage.
    band: str = "cold"  # hot | warm | cool | cold | closed
    waiting_on: str = "nobody"  # you | them | nobody
    waiting_reason: str = ""
    waiting_since: datetime | None = None
    company_how: str = ""


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
    """The registrable part of a company's domain; "" for personal, ATS and tool domains."""
    domain = (domain or "").lower().strip(".")
    if not domain or domain in PERSONAL_DOMAINS or is_tool_domain(domain):
        return ""
    return company_names.registrable(domain)


def domain_company(domain: str) -> str:
    """"careers.acme-robotics.example" -> "Acme Robotics" (app/company_names.from_domain)."""
    return company_names.from_domain(company_domain(domain))


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
        self.by_thread: dict[str, str] = {}

    def add(self, lead: Lead) -> Lead:
        self.leads[lead.id] = lead
        if lead.domain:
            self.by_domain.setdefault(lead.domain, lead.id)
        return lead

    def find(self, touch: Touch) -> Lead | None:
        """Its company by name, else by its own domain; people only when it names no company
        (an agency recruiter pitching two clients must not merge them). A Gmail thread
        already on a lead keeps its later messages there."""
        if touch.thread and touch.thread in self.by_thread:
            return self.leads[self.by_thread[touch.thread]]
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
        if touch.thread:
            self.by_thread.setdefault(touch.thread, lead.id)
        domain = own_domain(touch)
        if domain and not lead.domain:
            lead.domain = domain
            self.by_domain.setdefault(domain, lead.id)
        if touch.role:
            lead.role = touch.role
        if touch.company and (lead.company == company_names.NOT_IDENTIFIED or lead.company.startswith("Unknown")):
            lead.company = touch.company
            lead.company_how = touch.company_how
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
    how = touch.company_how if touch.company.strip() else (f"Gmail sender domain {company_domain(touch.domain)}"
                                                           if name else "")
    key = company_key(name)
    if not key:
        person = next((p for p in touch.people if p.email or p.name), None)
        if person is None:
            return None
        key = "person:" + (person.email.lower() or norm_name(person.name))
        name, how = company_names.NOT_IDENTIFIED, ""
    return Lead(id=lead_id_for(key), company=name, key=key, domain=own_domain(touch), company_how=how)


@dataclass
class Overrides:
    """The owner's corrections (table lead_overrides), applied on every rebuild.

    dismissed: touchpoint ids and Gmail threads of dismissed leads; they never count again
    (a new thread or event can still start the lead afresh). stages: lead id -> stage,
    kept until cleared. merges: lead id -> the lead it was merged into."""
    dismissed_touches: set[str] = field(default_factory=set)
    dismissed_threads: set[str] = field(default_factory=set)
    stages: dict[str, str] = field(default_factory=dict)
    merges: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # lead id -> the company he set
    aliases: dict[str, str] = field(default_factory=dict)  # Filled by build: old lead id -> its new id

    def renamed(self, aliases: dict[str, str]) -> "Overrides":
        """The same corrections, keyed by the leads' current ids."""
        move = lambda i: aliases.get(i, i)  # noqa: E731
        return Overrides(self.dismissed_touches, self.dismissed_threads,
                         {move(k): v for k, v in self.stages.items()},
                         {move(k): move(v) for k, v in self.merges.items() if move(k) != move(v)},
                         {move(k): v for k, v in self.names.items()}, dict(aliases))

    def dismissed(self, touch: Touch) -> bool:
        return touch.id in self.dismissed_touches or bool(touch.thread and touch.thread in self.dismissed_threads)

    def target(self, lead_id: str) -> str:
        seen = {lead_id}
        while lead_id in self.merges and self.merges[lead_id] not in seen:
            lead_id = self.merges[lead_id]
            seen.add(lead_id)
        return lead_id


def merge_into(target: Lead, source: Lead) -> None:
    for touch in source.touches:
        touch.lead_id = target.id
        target.touches.append(touch)
    for key, contact in source.contacts.items():
        target.contacts.setdefault(key, contact)
    target.role = target.role or source.role
    target.domain = target.domain or source.domain


def build(touches: list[Touch], now: datetime, overrides: Overrides | None = None) -> list[Lead]:
    """All leads from all touchpoints (time order), the owner's merges applied; then
    stage (or his stage), status and temperature. Dismissed touchpoints count nowhere."""
    overrides = overrides or Overrides()
    before = {t.id: t.lead_id for t in touches if t.lead_id}  # Each touchpoint's lead at the last save.
    index = Index()
    owner = company_names.owner_words()
    for touch in touches:  # Never a round label, the owner's name or a tool, even from old data.
        if touch.company and not company_names.is_company(touch.company, owner):
            touch.company, touch.company_how = "", ""
    for touch in sorted(touches, key=lambda t: (t.at, t.id)):
        if overrides.dismissed(touch):
            touch.lead_id = ""
            continue
        lead = index.find(touch)
        if lead is None and touch.kind in CREATES_LEADS:
            lead = new_lead_for(touch)
            if lead is not None:
                lead = index.leads.get(lead.id) or index.add(lead)
        if lead is not None:
            index.attach(lead, touch)
        else:
            touch.lead_id = ""
    given = overrides
    overrides = overrides.renamed(lead_aliases(index.leads, before, overrides))
    given.aliases = overrides.aliases
    for source_id in list(index.leads):
        target_id = overrides.target(source_id)
        if target_id != source_id and target_id in index.leads:
            merge_into(index.leads[target_id], index.leads.pop(source_id))
    for lead in index.leads.values():
        if overrides.names.get(lead.id):
            lead.company, lead.company_how = overrides.names[lead.id], "set by you"
        evaluate(lead, now, overrides.stages.get(lead.id, ""))
    return sorted(index.leads.values(), key=lambda l: (-l.temperature, l.company.lower()))


def lead_aliases(current: dict[str, Lead], before: dict[str, str], overrides: Overrides) -> dict[str, str]:
    """Old lead id -> new id, when a lead's id changed but its touchpoints did not (lead ids
    come from the company name, and the resolver can rename a company). Each vanished id goes
    to the lead now holding most of its touchpoints, so the owner's stage, name and merge
    corrections follow the lead. A merged lead's touchpoints were saved under its target: a
    brand-new lead made only of the target's touchpoints, with a merge into that target left
    dangling, is that merged lead renamed."""
    votes: dict[str, dict[str, int]] = {}
    for lead in current.values():
        for touch in lead.touches:
            old = before.get(touch.id)
            if old and old not in current:
                votes.setdefault(old, {}).setdefault(lead.id, 0)
                votes[old][lead.id] += 1
    aliases = {old: max(sorted(counts), key=lambda new: counts[new]) for old, counts in votes.items()}
    known = set(before.values())
    for source, target in overrides.merges.items():
        if source in current or source in aliases:
            continue
        target = aliases.get(target, target)
        fresh = [lead.id for lead in current.values() if lead.id not in known and lead.id != target
                 and lead.touches and all(aliases.get(before.get(t.id, ""), before.get(t.id)) == target for t in lead.touches)]
        if len(fresh) == 1:
            aliases[source] = fresh[0]
    return aliases


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


def evaluate(lead: Lead, now: datetime, manual_stage: str = "") -> None:
    past = [t for t in lead.touches if t.at <= now]
    lead.last_touch_at = max((t.at for t in past), default=None)
    future = upcoming(lead, now)
    stage = stage_of(lead.touches)
    idle = now - lead.last_touch_at if lead.last_touch_at else EXPIRE_AFTER
    if stage not in CLOSED and not future and idle > GHOSTED_AFTER:
        stage = "ghosted"
    lead.stage = manual_stage if manual_stage in STAGES else stage
    lead.stage_manual = manual_stage in STAGES
    lead.status = status_of(lead.stage, bool(future), idle)
    if future:
        lead.next_step, lead.next_step_at = f"Interview: {future[0].title or 'on the calendar'}", future[0].at
    else:
        step = next((t for t in sorted(past, key=lambda t: t.at, reverse=True)
                     if t.next_step and now - t.at <= MOMENTUM_WINDOW), None)
        lead.next_step, lead.next_step_at = (step.next_step, None) if step else ("", None)
    lead.temperature, lead.parts = temperature(lead, now)
    lead.band = band_of(lead.temperature, lead.stage)
    lead.waiting_on, lead.waiting_reason, lead.waiting_since = ball_in_court(lead, now)


def status_of(stage: str, scheduled: bool, idle: timedelta) -> str:
    """expired (quiet 30 days, nothing scheduled; hidden) > closed (won or lost) > active (live)."""
    if not scheduled and idle > EXPIRE_AFTER:
        return "expired"
    return "closed" if stage in CLOSED else "active"


def band_of(temperature: float | None, stage: str = "") -> str:
    """hot >= 75, warm >= 50, cool >= 25, else cold; closed-won and closed-lost are "closed"."""
    if stage in CLOSED or stage == "closed":
        return "closed"
    t = float(temperature or 0)
    return next(name for name, low in reversed(BANDS) if t >= low)


def their_move(past: list[Touch]) -> Touch | None:
    """The latest touch that is not his own email: a reply, an interview, a recording."""
    theirs = [t for t in past if not (t.kind == "email" and t.direction == "out")]
    return max(theirs or past, key=lambda t: t.at, default=None)


def temperature(lead: Lead, now: datetime) -> tuple[int, dict[str, Any]]:
    past = [t for t in lead.touches if t.at <= now]
    moved = their_move(past)
    days = (now - moved.at).total_seconds() / 86400 if moved else None
    recency = 30 * 0.5 ** (days / HALF_LIFE_DAYS) if days is not None else 0.0
    stage = 30 * STAGE_WEIGHT.get(lead.stage, 0)
    recent = sum(1 for t in past if now - t.at <= MOMENTUM_WINDOW)
    soon = [t for t in upcoming(lead, now) if t.at - now <= MOMENTUM_WINDOW]
    frequency = 10 * min(1.0, recent / 4)
    if lead.stage == "offer":
        next_part, next_why = 15.0, "an offer waits on your decision"
    elif soon:
        within = (soon[0].at - now).total_seconds() / 86400
        next_part = 15.0 if soon[0].at - now <= SOON else 9.0
        next_why = f"interview in {max(0, round(within))} days"
    else:
        next_part, next_why = 0.0, "nothing scheduled"
    scores = [t.score for t in past if t.kind == "evaluation" and t.score is not None]
    perf = 5 * (sum(scores) / len(scores)) / 10 if scores else 2.5
    inbound = sorted((t for t in past if t.kind == "email" and t.direction == "in"
                      and t.sentiment is not None), key=lambda t: t.at)[-3:]
    mean_sentiment = sum(t.sentiment for t in inbound) / len(inbound) if inbound else None  # type: ignore[misc]
    sentiment = 5 * (mean_sentiment + 1) / 2 if mean_sentiment is not None else 2.5
    cued = [t for t in sorted(past, key=lambda t: t.at) if t.cue in ("positive", "negative")]
    latest_cue = cued[-1].cue if cued else "none"
    cue = {"positive": 5.0, "negative": 0.0}.get(latest_cue, 2.5)
    raw = recency + stage + frequency + next_part + perf + sentiment + cue
    cap = CAPS.get(lead.stage)
    total = int(round(max(0.0, min(100.0, raw if cap is None else min(raw, cap)))))
    parts = {
        "recency": {"points": round(recency, 1), "max": 30,
                    "why": f"they last moved {days:.0f} days ago (halves every 7 days)" if days is not None else "no touch yet"},
        "stage": {"points": round(stage, 1), "max": 30,
                  "why": f"stage {lead.stage}" + (" (set by you)" if lead.stage_manual else "")},
        "momentum": {"points": round(frequency + next_part, 1), "max": 25,
                     "why": f"{recent} touches in 14 days; {next_why}"},
        "quality": {"points": round(perf + sentiment + cue, 1), "max": 15,
                    "why": "; ".join([
                        f"interview score {sum(scores) / len(scores):.1f}/10" if scores else "no interview score",
                        f"recruiter tone {mean_sentiment:+.1f}" if mean_sentiment is not None else "no recruiter tone",
                        f"latest cue {latest_cue}"]),
                    "performance": round(perf, 1), "sentiment": round(sentiment, 1), "cue": round(cue, 1)},
        "cap": cap, "total": total,
    }
    return total, parts


# Ball in court: who owes the next move.

ASKS = re.compile(r"\b(availability|available|slot|schedul\w*|book|pick|choose|confirm|reply|respond|answer|"
                  r"let (?:us|me|them) know|share|send|submit|upload|complete|fill|sign|accept|decide|decision|"
                  r"take-?home|assignment|assessment|documents?|resume|cv|salary|compensation|ctc|expectations?|"
                  r"notice period|references?)\b", re.I)
THEY_DO = re.compile(r"^\s*(?:we|they|the team|team|recruiter|hiring manager|hr|panel|company)\b|\b(?:will|to) "
                     r"(?:review|get back|follow up|reach out|be in touch|share feedback)\b", re.I)
SCHEDULING = re.compile(r"\b(availability|available|slot|schedul\w*|book|time to (?:talk|chat)|calendar)\b", re.I)
NOT_ASKING = ("rejection", "job_board_alert", "newsletter", "own_reply")
ROLE_WORD = {"recruiter": "Recruiter", "hiring_manager": "Hiring manager", "interviewer": "Interviewer",
             "coordinator": "Coordinator"}


def asks_you(touch: Touch) -> str:
    """What this inbound email asks him to do ("" when nothing). The classifier's
    action_requested decides when it is known; for older rows, only a stored next step that
    clearly asks him (or an offer) counts."""
    if touch.kind != "email" or touch.direction != "in" or touch.cue == "negative" \
            or touch.stage_hint in CLOSED or touch.category in NOT_ASKING:
        return ""
    automated = not any(p.email or p.name for p in touch.people)
    if touch.action_requested is not None:
        if not touch.action_requested:
            return ""
        if automated and touch.category not in ("scheduling", "assessment", "offer"):
            return ""  # A no-reply or ATS sender: only a scheduling link, a test or an offer counts.
        return touch.action or touch.next_step or touch.summary or "asked for a reply"
    if automated:
        return ""
    if touch.stage_hint == "offer":
        return touch.next_step or "decide on the offer"
    step = touch.next_step.strip()
    if step and ASKS.search(step) and not THEY_DO.search(step):
        return step
    return ""


def ago(delta: timedelta) -> str:
    days = int(delta.total_seconds() // 86400)
    return "today" if days < 1 else "yesterday" if days == 1 else f"{days} days ago"


def ball_in_court(lead: Lead, now: datetime) -> tuple[str, str, datetime | None]:
    """("you" | "them" | "nobody", reason, since).

    you: a live lead, not Cold; their latest email (automated mail aside: no-reply senders
    and ATS acknowledgements) asks something of him (`asks_you`); he has not acted since (no
    email of his in that thread; for a scheduling ask, no calendar event after it); and it
    is under 14 days old. Older: nobody (the lead is cooling).
    them: he moved last (replied, applied, interviewed) or they sent a message that asks
    nothing, under 21 days ago. Cold, closed and expired leads: always nobody."""
    if lead.status == "expired":
        return "nobody", "Expired: quiet for 30 days", None
    if lead.band == "closed" or lead.status == "closed":
        return "nobody", "Closed", None
    if lead.band == "cold":
        return "nobody", "Gone cold: nobody is waiting", None
    past = sorted((t for t in lead.touches if t.at <= now and t.kind in ("email", "calendar")), key=lambda t: t.at)
    future = upcoming(lead, now)
    human = [t for t in past if not (t.kind == "email" and t.direction == "in" and not t.people
                                     and t.action_requested is not True)]
    last = (human or past)[-1] if past else None
    if last is None:
        return "nobody", "No messages yet", None
    inbound = [t for t in human if t.kind == "email" and t.direction == "in"]
    request = inbound[-1] if inbound else None
    asked = asks_you(request) if request else ""
    booking = request is not None and (request.category == "scheduling" or bool(SCHEDULING.search(asked)))
    if asked and not any(t.at > request.at and ((t.kind == "calendar" and booking) or (
            t.kind == "email" and t.direction == "out" and (not t.thread or not request.thread or t.thread == request.thread)))
            for t in lead.touches):
        if now - request.at >= ASK_MAX_AGE:
            return "nobody", f"Unanswered for {ago(now - request.at).removesuffix(' ago')}: the lead is cooling", None
        who = next((ROLE_WORD[p.role] for p in request.people if p.role in ROLE_WORD), lead.company)
        return "you", f"{who}: {asked} ({ago(now - request.at)})", request.at
    if future:
        return "nobody", f"Interview booked: {future[0].title or 'on the calendar'}", None
    if now - last.at >= THEM_MAX_AGE:
        return "nobody", "No reply for 3 weeks", None
    days = int((now - last.at).total_seconds() // 86400)
    span = "since today" if days < 1 else f"for {days} day{'s' if days != 1 else ''}"
    return "them", f"Waiting on {lead.company} {span}", last.at


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
            "id": lead.id, "company": lead.company, "company_how": lead.company_how, "role": lead.role,
            "domain": lead.domain,
            "stage": lead.stage, "stage_manual": lead.stage_manual, "status": lead.status, "temperature": lead.temperature,
            "band": lead.band, "waiting_on": lead.waiting_on, "waiting_reason": lead.waiting_reason,
            "waiting_since": iso(lead.waiting_since),
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
            "stages": list(STAGES), "bands": list(BAND_NAMES), "counts": counts(leads), "generated_at": iso(now)}


def counts(leads: list[Lead]) -> dict[str, Any]:
    """The one set of numbers every page shows: live = not closed and not expired."""
    live = [l for l in leads if l.status == "active"]
    return {"live": len(live), "closed": sum(1 for l in leads if l.status == "closed"),
            "expired": sum(1 for l in leads if l.status == "expired"),
            "bands": {name: sum(1 for l in live if l.band == name) for name, _ in reversed(BANDS)},
            "waiting_on_you": sum(1 for l in live if l.waiting_on == "you")}
