"""The one place that decides which company an email, a calendar event, a recording or an
interview is about. Leads, touchpoints, past and upcoming interviews, briefings and Telegram
text all use it, so a name is either a real company or "Company not identified".

Evidence, strongest first (`resolve`):

1. the external attendees' / invitees' email domain: "acme-robotics.com" -> "Acme Robotics";
2. the Gmail sender's domain (an ATS or scheduler sender: the company in its display name or
   subject instead, "Acme via Greenhouse", "Your application to Acme");
3. a company named in the title: "Screen call with Aspire", "... connect at Zinnov Bengaluru";
   then a company the source stated (the email classifier's, the calendar's, the scorecard's);
4. an existing lead that shares one of the people (by email).

When the title spells the same company the domain names ("Juniper Robotics" vs
juniper.example), the title's spelling is kept.

Never a company: the owner's own names (VALR_OWNER_NAME, VALR_CANDIDATE_NAME and the local
parts of VALR_OWNER_EMAILS, any part, any case); round and meeting words (L1/L2/L3, Round N,
R1, Screen, Technical, HM, Final, Interview, Call, Sync, Chat, Debrief, Intro, Connect,
In-person, Post offer...); calendar and notetaker tools (Zoom, Google Meet, Fathom,
Fireflies, Granola, Calendly...); free-mail domains; ATS, job-board and scheduler domains
(evidence of a recruiter, not of the company). If nothing survives: NOT_IDENTIFIED, and the
owner can set the name ("Set company" on leads and interviews).

Standard library only; pure functions (tests/test_company_names.py).
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

NOT_IDENTIFIED = "Company not identified"

FREEMAIL = frozenset("""
    gmail.com googlemail.com yahoo.com yahoo.co.in yahoo.in ymail.com outlook.com outlook.in hotmail.com
    live.com live.in msn.com icloud.com me.com mac.com proton.me protonmail.com pm.me rediffmail.com
    aol.com zoho.com zohomail.in gmx.com gmx.net mail.com yandex.com hey.com fastmail.com tutanota.com
    example.com example.org example.net
    """.split())
# Applicant tracking, scheduling and job boards: a recruiter's tools, never the company.
ATS_DOMAINS = frozenset("""
    greenhouse.io greenhouse-mail.io lever.co hire.lever.co ashbyhq.com ashby.io goodtime.io gem.com
    calendly.com workday.com myworkday.com myworkdayjobs.com smartrecruiters.com modernloop.io icims.com
    jobvite.com recruitee.com workable.com breezy.hr darwinbox.in darwinbox.com keka.com zohorecruit.com
    teamtailor.com personio.de bamboohr.com rippling.com freshteam.com recruiterflow.com hirequotient.com
    hackerrank.com hackerearth.com codility.com coderpad.io karat.com testgorilla.com mettl.com
    linkedin.com naukri.com instahyre.com indeed.com glassdoor.com monster.com foundit.in iimjobs.com
    hirist.com hirist.tech wellfound.com angel.co cutshort.io uplers.com internshala.com shine.com
    timesjobs.com ziprecruiter.com dice.com apna.co hired.com otta.com welcometothejungle.com turing.com
    toptal.com unstop.com
    """.split())
# Calendar, video and notetaker tools.
TOOL_DOMAINS = frozenset("""
    zoom.us zoom.com calendar.google.com google.com meet.google.com microsoft.com teams.microsoft.com
    fathom.video fathom.ai fireflies.ai granola.ai granola.so otter.ai metaview.ai brighthire.ai
    tldv.io read.ai webex.com whereby.com
    """.split())
TOOL_NAMES = frozenset("""
    zoom google meet gmeet teams microsoft webex fathom fireflies granola calendly otter tldv
    greenhouse lever ashby workday goodtime linkedin naukri gmail outlook
    """.split())
# Round, meeting and title filler words: never a company on their own.
ROUND_WORDS = frozenset("""
    l1 l2 l3 l4 l5 r1 r2 r3 r4 r5 round rounds level stage screen screening phone technical tech
    hm hiring manager final finals interview interviews interviewing call calls sync chat chats debrief
    intro introduction introductory connect connection catch catchup up in person inperson onsite on site
    virtual remote post pre offer offers discussion meeting meet session loop panel coding system design
    culture fit behavioral behavioural assignment assessment take home review follow followup cultural
    and with for the of at x vs me you my your our a an to on by re fw fwd hr recruiter recruiting
    candidate role position opportunity job application applied update next steps 1 2 3 4 5 first
    second third founder founders ceo cto team bar raiser leadership values deep dive presentation
    case study pair programming whiteboard quick hello hi meeting invitation invite scheduled
    new event events reminder updated update canceled cancelled accepted declined thank thanks applying
    received confirmation confirmed rescheduled availability
    """.split())
PLACES = frozenset("""
    bengaluru bangalore mumbai pune delhi ncr gurgaon gurugram noida hyderabad chennai kolkata ahmedabad
    kochi jaipur india remote hybrid office hq london berlin dubai singapore nyc sf usa us uk
    """.split())
LEGAL = frozenset("""
    inc incorporated llc llp ltd limited pvt private corp corporation gmbh plc pte sa bv ag
    """.split())
SECOND_LEVEL = ("co", "com", "net", "org", "ac", "gov", "edu")
GENERIC_LABELS = frozenset("""
    mail email careers career jobs job hire hiring talent recruiting recruit people hr team app www
    notifications noreply no-reply info hello contact
    """.split())

# Title patterns, in order: a company named after "with/at/@", "X <> Y", "X | Y", "X: ...".
TITLE_PATTERNS = [
    re.compile(r"\b(?:with|at|@|from|for)\s+(?P<c>[A-Za-z0-9][\w&.'\- ]{1,50})", re.I),
    re.compile(r"(?P<c>[A-Za-z0-9][\w&.'\- ]{1,40}?)\s*(?:<>|&lt;&gt;|\sx\s|//)\s*", re.I),
    re.compile(r"(?:<>|&lt;&gt;|\sx\s|//)\s*(?P<c>[A-Za-z0-9][\w&.'\- ]{1,40})", re.I),
    re.compile(r"^(?P<c>[A-Za-z0-9][\w&.'\- ]{1,40}?)\s*[|:]\s"),
    re.compile(r"\b(?P<c>[A-Z][\w-]*\.(?:ai|io|co|so|dev|tech|app))\b"),
    re.compile(r"[|:\-–]\s*(?P<c>[A-Za-z0-9][\w&.'\- ]{1,40}?)\s*$"),
]
ATS_DISPLAY = [
    re.compile(r"^(?P<c>.+?)\s+(?:via|@|on behalf of|through)\s+\w+", re.I),
    re.compile(r"^(?P<c>.+?)\s+(?:hiring team|recruiting team|recruiting|talent acquisition|talent team|careers|"
               r"people team|hr team|jobs)\b", re.I),
]
ATS_SUBJECT = [
    re.compile(r"(?:application|applying|interest|candidacy)\s+(?:to|at|with|for)\s+(?P<c>[A-Z][\w&.'\- ]{1,40}?)"
               r"(?:\s*[-|:!,.(]|\s+for\b|\s+as\b|$)"),
    re.compile(r"\b(?:interview|invitation|update|next steps?)\s+(?:with|at|from)\s+(?P<c>[A-Z][\w&.'\- ]{1,40}?)"
               r"(?:\s*[-|:!,.(]|\s+for\b|$)", re.I),
    re.compile(r"^(?P<c>[A-Z][\w&.'\- ]{1,40}?)\s*[-|:]\s"),
]


@dataclass
class Resolution:
    name: str = ""         # "" when nothing survives; show NOT_IDENTIFIED
    domain: str = ""       # the company's registrable domain, when one is known
    source: str = "none"   # attendees | sender | ats | title | stated | lead | none

    @property
    def label(self) -> str:
        return self.name or NOT_IDENTIFIED


# The owner.

def owner_words(env: dict[str, str] | None = None) -> frozenset[str]:
    """Every part of the owner's names and address local parts, lower case."""
    env = os.environ if env is None else env
    raw = " ".join(env.get(k, "") for k in ("VALR_OWNER_NAME", "VALR_CANDIDATE_NAME"))
    for email in re.split(r"[,\s]+", " ".join(env.get(k, "") for k in ("VALR_OWNER_EMAILS", "VALR_GMAIL_ACCOUNT"))):
        raw += " " + email.partition("@")[0]
    return frozenset(w for w in re.findall(r"[a-z]+", raw.lower()) if len(w) >= 3)


def owner_domains(env: dict[str, str] | None = None) -> frozenset[str]:
    env = os.environ if env is None else env
    emails = re.split(r"[,\s]+", " ".join(env.get(k, "") for k in ("VALR_OWNER_EMAILS", "VALR_GMAIL_ACCOUNT")))
    return frozenset(d for d in (e.rpartition("@")[2].lower() for e in emails if "@" in e) if d not in FREEMAIL)


# Names.

def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower())


def is_company(name: str, owner: Iterable[str] | None = None) -> bool:
    """False for "", round labels, meeting words, tool names, places alone, or the owner's name."""
    owner = owner_words() if owner is None else frozenset(owner)
    rest = [w for w in words(name) if w not in ROUND_WORDS and w not in owner and w not in TOOL_NAMES
            and w not in PLACES and w not in LEGAL and not re.fullmatch(r"[lr]?\d{1,2}", w)]
    return any(len(w) >= 2 for w in rest)


def clean(name: str, owner: Iterable[str] | None = None) -> str:
    """A candidate name tidied: brackets, places and round words trimmed from the ends;
    "" when it is not a company ("Zinnov Bengaluru [In-person]" -> "Zinnov")."""
    owner = owner_words() if owner is None else frozenset(owner)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name or "")
    text = re.sub(r"\s+", " ", text).strip(" -–|:,.;/&")
    tokens = text.split()
    drop = lambda t: (t.lower().strip(".,'-") in ROUND_WORDS | PLACES | TOOL_NAMES or t.lower().strip(".,'-") in owner  # noqa: E731
                      or re.fullmatch(r"[lr]?\d{1,2}-?", t.lower()) is not None)
    while tokens and drop(tokens[-1]):
        tokens.pop()
    while tokens and drop(tokens[0]):
        tokens.pop(0)
    text = " ".join(tokens).strip(" -–|:,.;/&")
    text = re.sub(r"\s+(?:software\s+|technologies\s+|solutions\s+)?(?:private|pvt\.?)(?:\s+(?:limited|ltd\.?))?$|"
                  r"\s+(?:inc\.?|llc|ltd\.?|limited|gmbh|plc|corp\.?)$", "", text, flags=re.I).strip(" ,.")
    return text[:80] if is_company(text, owner) else ""


def key(name: str) -> str:
    """Comparable: "Razorpay Software Pvt. Ltd." -> "razorpay software" -> without legal words."""
    return " ".join(w for w in words(name) if w not in LEGAL)


def same(a: str, b: str) -> bool:
    """Same company: equal keys, or one is the other's leading words ("juniper" ~ "juniper robotics")."""
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if ka == kb or ka.replace(" ", "") == kb.replace(" ", ""):
        return True
    short, long_ = sorted((ka, kb), key=len)
    return len(short) >= 4 and (long_ + " ").startswith(short + " ")


# Domains.

def domain_of(email: str) -> str:
    return (email or "").rpartition("@")[2].strip().lower().strip(".")


def on(domain: str, names: frozenset[str]) -> bool:
    return any(domain == n or domain.endswith("." + n) for n in names)


def is_ats(domain: str) -> bool:
    return on(domain, ATS_DOMAINS)


def registrable(domain: str) -> str:
    """The company's registrable domain; "" for free-mail, ATS, job-board and tool domains."""
    domain = (domain or "").lower().strip(".")
    if not domain or "." not in domain or domain in FREEMAIL or on(domain, ATS_DOMAINS) or on(domain, TOOL_DOMAINS):
        return ""
    labels = domain.split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def from_domain(domain: str) -> str:
    """"careers.acme-robotics.com" -> "Acme Robotics"; "" when the domain names no company."""
    core = registrable(domain)
    if not core:
        return ""
    label = core.split(".")[0]
    if label in GENERIC_LABELS:
        return ""
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", label) if part)


def from_people(emails: Iterable[str], owner_emails: Iterable[str] = (),
                env: dict[str, str] | None = None) -> tuple[str, str]:
    """(name, domain) from the most common company domain among these addresses (not the owner's)."""
    owners = {e.lower() for e in owner_emails}
    mine = owner_domains(env)
    counts: Counter[str] = Counter()
    for email in emails:
        email = (email or "").strip().lower()
        if not email or email in owners:
            continue
        domain = registrable(domain_of(email))
        if domain and domain not in mine:
            counts[domain] += 1
    for domain, _ in counts.most_common():
        name = from_domain(domain)
        if name:
            return name, domain
    return "", ""


# Titles, subjects and ATS senders.

def from_title(title: str, owner: Iterable[str] | None = None) -> str:
    owner = owner_words() if owner is None else frozenset(owner)
    text = re.sub(r"\[[^\]]*\]", " ", title or "")
    for pattern in TITLE_PATTERNS:
        for match in pattern.finditer(text):
            name = clean(match.group("c"), owner)
            if name:
                return name
    return ""


def from_ats(display_name: str, subject: str, owner: Iterable[str] | None = None) -> str:
    owner = owner_words() if owner is None else frozenset(owner)
    for pattern in ATS_DISPLAY:
        match = pattern.search(display_name or "")
        if match:
            name = clean(match.group("c"), owner)
            if name:
                return name
    for pattern in ATS_SUBJECT:
        match = pattern.search(subject or "")
        if match:
            name = clean(match.group("c"), owner)
            if name:
                return name
    return ""


# The resolver.

def resolve(*, attendees: Iterable[str] = (), sender_email: str = "", sender_name: str = "", title: str = "",
            stated: str = "", leads: Iterable[Any] = (), owner_emails: Iterable[str] = (),
            owner_names: Iterable[str] = (), env: dict[str, str] | None = None) -> Resolution:
    """The company, by the evidence order in the module docstring. `leads`: objects or dicts
    with `company` and `contacts` (emails), for the last resort. `owner_names`: more names of
    the owner seen in the source (Fathom's recorder, the internal invitees), never a company."""
    owner = owner_words(env) | {w for n in owner_names for w in words(n) if len(w) >= 3}
    owners = {e.lower() for e in owner_emails} | {e for e in re.split(r"[,\s]+", (env or os.environ).get(
        "VALR_OWNER_EMAILS", "").lower()) if e}
    titled = from_title(title, owner)
    spoken = clean(stated, owner)

    def spelled(name: str, domain: str, source: str) -> Resolution:
        for better in (titled, spoken):  # The same company, as people spell it.
            if better and same(better, name) and len(better) >= len(name):
                return Resolution(better, domain, source)
        return Resolution(name, domain, source)

    name, domain = from_people(attendees, owners, env)
    if name:
        return spelled(name, domain, "attendees")
    sender = domain_of(sender_email)
    if sender and sender_email.lower() not in owners:
        if is_ats(sender):
            ats = from_ats(sender_name, title, owner)
            if ats:
                return Resolution(ats, "", "ats")
        else:
            name, domain = from_people([sender_email], owners, env)
            if name:
                return spelled(name, domain, "sender")
    if titled:
        return Resolution(titled, "", "title")
    if spoken:
        return Resolution(spoken, "", "stated")
    people = {(e or "").lower() for e in attendees} | ({sender_email.lower()} if sender_email else set())
    people -= owners | {""}
    for lead in leads or ():
        company = lead.get("company") if isinstance(lead, dict) else getattr(lead, "company", "")
        contacts = lead.get("contacts") if isinstance(lead, dict) else getattr(lead, "contacts", {})
        emails = {(c.get("email") if isinstance(c, dict) else getattr(c, "email", "")) or ""
                  for c in (contacts.values() if isinstance(contacts, dict) else contacts or [])}
        if people & {e.lower() for e in emails} and is_company(company, owner) and not company.startswith("Company not"):
            return Resolution(company, "", "lead")
    return Resolution()


def label(name: str, owner: Iterable[str] | None = None) -> str:
    """A name to show: the name if it is a company, else NOT_IDENTIFIED."""
    return name.strip() if name and is_company(name, owner) else NOT_IDENTIFIED
