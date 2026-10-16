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
# Recruiting agencies, staffing firms and assessment / interview platforms: evidence of who
# ran the process, never the employer (their name goes to `agency`).
AGENCY_DOMAINS = frozenset("""
    shl.com hirevue.com testgorilla.com mettl.com imocha.io xobin.com glider.ai interviewvector.com
    intervue.io barraiser.com incruiter.com flocareer.com gloroots.com recrew.ai recrew.in recrew.com
    watsonsearchpartner.com watsonsearchpartners.com successpact.com crossover.com andela.com
    randstad.com adecco.com michaelpage.com hays.com roberthalf.com teamlease.com quesscorp.com
    abcconsultants.net kellyservices.com manpowergroup.com ciel.co.in xpheno.com
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
# Role and job-title vocabulary: a candidate made only of these is never a company
# ("Deployed Engineer", "AI Software", "Software", "Consulting").
ROLE_WORDS = frozenset("""
    engineer engineers engineering deployed forward software ai ml developer developers sde swe sre staff senior
    sr junior jr founding founder backend frontend fullstack full stack platform platforms solutions solution
    consulting consultant consultants services service technologies technology tech labs lab systems system data
    cloud infra infrastructure applied research product products design designer member technical mts lead
    principal associate intern analyst scientist architect manager management director head vp operations ops
    devops security mobile web digital global group generative genai llm automation
    """.split())
# Common English words: never a company on their own ("You", "Considering", "Interest").
COMMON = frozenset("""
    you your yours we our ours us they them their he she it its this that these those here there
    considering consider interest interested regarding about thank thanks hello hey dear great good
    looking forward quick request requests reschedule rescheduled rescheduling invite question questions
    opportunity opportunities update reminder confirm confirmation follow please impromptu instant
    personal recurring adhoc ad hoc between
    sr senior jr junior staff principal lead engineer engineers engineering developer developers backend
    frontend fullstack full stack sde swe sre devops intern product designer analyst scientist architect
    consultant director vp head associate specialist programmer ml
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
    received confirmation confirmed rescheduled availability weekly daily monthly standup stand 1on1
    one lunch coffee dinner birthday personal busy hold block focus time ooo holiday leave
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
    agency: str = ""       # a recruiting agency or platform that was involved
    conflict: str = ""     # a domain that pointed at a different company (the model decides)

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
    """False for "", round labels, meeting words, tool names, common words, places alone, a bare
    1-2 letter word, or the owner's name."""
    owner = owner_words() if owner is None else frozenset(owner)
    rest = [w for w in words(name) if w not in ROUND_WORDS and w not in owner and w not in TOOL_NAMES
            and w not in COMMON and w not in PLACES and w not in LEGAL and not re.fullmatch(r"[lr]?\d{1,2}", w)
            and not re.fullmatch(r"\d+-?mins?", w)]
    if not rest or "between" in words(name) or len(words(name)) > 6:
        return False  # "Meeting between A and B" names people, not a company.
    if all(w in ROLE_WORDS for w in rest):
        return False  # Only role / title words: "Deployed Engineer", "AI Software".
    return any(len(w) >= 3 for w in rest) or (len(rest) == 1 and (name or "").strip().isupper() and len(rest[0]) >= 2)


LEADING = re.compile(r"^(?:(?:re|fwd?|fw)\s*:\s*|considering\s+|(?:your\s+)?interest(?:ed)?\s+in\s+|regarding\s+|"
                     r"about\s+|thank(?:s| you)\s+for\s+\w+(?:\s+(?:to|at|with))?\s+|application\s+(?:to|for|at)\s+|"
                     r"role\s+at\s+|position\s+at\s+|opportunity\s+(?:at|with)\s+|the\s+)", re.I)
TRAILING = re.compile(r"(?:\s*[-–|:,]?\s*(?:\d+\s*-?\s*mins?|reschedul\w*|interview\w*|requests?|call|sync|chat|"
                      r"meeting|invite|invitation|discussion|round|screen\w*|connect|catch\s*up|follow\s*up|"
                      r"next\s+steps?|update|confirmation))+\s*$", re.I)


def tidy(name: str) -> str:
    """"Considering Cisco" -> "Cisco"; "Nextiva Reschedule Interview Request" -> "Nextiva";
    "SoundHound AI - 15-Min" -> "SoundHound AI"; "ipsoftlabs.com" -> "Ipsoft Labs" (a raw,
    lower-case domain only: "Orvera.ai" stays)."""
    text = " ".join((name or "").split())
    for _ in range(3):
        text = LEADING.sub("", text).strip(" -–|:,.;/&")
        text = TRAILING.sub("", text).strip(" -–|:,.;/&")
    if re.fullmatch(r"[a-z0-9-]+\.(?:com|ai|io|co|in|app|tech|dev|net|org)(?:\.[a-z]{2})?", text):  # A raw domain.
        text = humanize_label(text.lower().split(".")[0])
    return text


def clean(name: str, owner: Iterable[str] | None = None) -> str:
    """A candidate name tidied: brackets, places and round words trimmed from the ends;
    "" when it is not a company ("Zinnov Bengaluru [In-person]" -> "Zinnov")."""
    owner = owner_words() if owner is None else frozenset(owner)
    if re.search(r"\bbetween\b", name or "", re.I):
        return ""  # "Meeting between A and B" names people, not a company.
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", tidy(name or ""))
    text = re.sub(r"\s+", " ", text).strip(" -–|:,.;/&")
    tokens = text.split()
    drop = lambda t: (t.lower().strip(".,'-") in ROUND_WORDS | PLACES | TOOL_NAMES | COMMON or t.lower().strip(".,'-") in owner  # noqa: E731
                      or re.fullmatch(r"[lr]?\d{1,2}-?", t.lower()) is not None)
    while tokens and drop(tokens[-1]):
        tokens.pop()
    while tokens and drop(tokens[0]):
        tokens.pop(0)
    text = tidy(" ".join(tokens).strip(" -–|:,.;/&"))
    text = re.sub(r"\s+(?:software\s+|technologies\s+|solutions\s+)?(?:private|pvt\.?)(?:\s+(?:limited|ltd\.?))?$|"
                  r"\s+(?:inc\.?|llc|ltd\.?|limited|gmbh|plc|corp\.?)$", "", text, flags=re.I).strip(" ,.")
    text = normalize(text)
    return text[:80] if is_company(text, owner) else ""


TLDS = frozenset("com io co in net org app dev ai".split())


def key(name: str) -> str:
    """Comparable: "Razorpay Software Pvt. Ltd." -> "razorpay software"; "Cermati.com" -> "cermati"."""
    parts = [w for w in words(name) if w not in LEGAL]
    if len(parts) > 1 and parts[-1] in TLDS and re.search(r"\.[a-z]{2,4}\s*$", (name or "").strip(), re.I):
        parts.pop()  # A trailing ".com" is not part of the name.
    return " ".join(parts)


def normalize(name: str) -> str:
    """Tidy casing and suffixes: "PLUGSCALE CONSULTING PRIVATE CONSULTING" -> "Plugscale Consulting";
    "FourKites, Inc." -> "FourKites"; "Revantage Global Services India Private Limited" ->
    "Revantage Global Services"; parentheticals dropped; legal words dropped anywhere; repeated
    words once."""
    text = re.sub(r"\([^)]*\)?|\[[^\]]*\]?", " ", name or "")
    text = re.sub(r"\s+(?:india\s+)?(?:private|pvt\.?)(?:\s+(?:limited|ltd\.?))?\b", " ", text, flags=re.I)
    tokens = [t for t in re.split(r"\s+", text.replace(",", " ").strip()) if t]
    out, seen = [], set()
    for token in tokens:
        bare = token.lower().strip(".,")
        if bare in LEGAL or bare in ("pvt", "pvt.", "private", "limited"):
            continue
        if bare in seen:
            continue
        seen.add(bare)
        out.append(token.strip(","))
    text = " ".join(out).strip(" .,-")
    if text.isupper() and any(len(t) > 3 for t in out):  # SHOUTING -> Title Case; short acronyms stay.
        text = " ".join(t if len(t) <= 3 else t.capitalize() for t in text.split())
    return text


# Trailing words that are never part of a brand when comparing spellings ("FurtherAI engineering",
# "Razorpay Software", "Aspireapp").
BRAND_JUNK = frozenset("engineering software app careers jobs hiring team recruiting hr".split())


def brand(name: str) -> str:
    """A name as compared and shown: normalised, parentheticals and meeting words gone, and
    trailing non-brand words stripped ("SoundHound AI - 15-Min" -> "SoundHound AI",
    "Guickly (Stealth Startup)" -> "Guickly", "Razorpay Software" -> "Razorpay",
    "Aspireapp" -> "Aspire")."""
    text = normalize(tidy(normalize(name)))
    tokens = text.split()
    while len(tokens) > 1 and tokens[-1].lower().strip(".,-") in BRAND_JUNK:
        tokens.pop()
    if len(tokens) == 1 and tokens[0].lower().endswith("app") and len(tokens[0]) >= 7 and not tokens[0].isupper():
        tokens = [tokens[0][:-3]]  # A domain artefact: "Aspireapp" -> "Aspire".
    return " ".join(tokens).strip(" -–|:,.;/&")


def canonical(name: str, known: Iterable[str] = ()) -> str:
    """The spelling to show for `name`, given the spellings stored for the same company (with
    repeats: how often each was seen). Every spelling is cleaned first (`brand`). The shorter
    clean form wins unless the longer one adds a real brand word: "Safe" -> "Safe Security",
    "Chasi" -> "Chasi AI" only when "Chasi AI" is the most common spelling. Then the most
    common casing ("Fourkites" -> "FourKites", "Micro1" -> "micro1"). Never a TLD added to a
    clean name ("Cermati.com" -> "Cermati"), never a parenthetical."""
    own = brand(name)
    if not own or not is_company(own, frozenset()):
        return own
    counts = Counter(b for b in (brand(k) for k in known if k) if b and same(b, own) and is_company(b, frozenset()))
    counts[own] += 0
    by_key: dict[str, Counter] = {}
    for spelling, n in counts.items():
        by_key.setdefault(key(spelling).replace(" ", ""), Counter())[spelling] += n
    totals = {k: sum(c.values()) for k, c in by_key.items()}
    shortest = min(by_key, key=len)
    chosen = shortest
    for k in sorted(by_key, key=len):
        if k == shortest:
            continue
        extra = k[len(shortest):] if k.startswith(shortest) else ""
        if not extra:
            continue
        if extra == "ai" and totals[k] <= totals[chosen]:
            continue  # "AI" is a brand word only when the longer spelling is the most common.
        chosen = k
    spellings = by_key[chosen]
    return max(spellings, key=lambda m: (spellings[m], not m.isupper(), not m.islower(), -len(m)))


def same(a: str, b: str) -> bool:
    """Same company: equal keys (spaces ignored), one the other's leading words ("juniper" ~
    "juniper robotics"), or a domain label with a suffix ("aspire" ~ "aspireapp")."""
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if ka == kb or ka.replace(" ", "") == kb.replace(" ", ""):
        return True
    short, long_ = sorted((ka, kb), key=len)
    if len(short) >= 4 and (long_ + " ").startswith(short + " "):
        return True
    squashed_short, squashed_long = short.replace(" ", ""), long_.replace(" ", "")
    return len(squashed_short) >= 4 and squashed_long.startswith(squashed_short) and \
        squashed_long[len(squashed_short):] in DOMAIN_SUFFIXES


# Domain label suffixes and prefixes that are not part of the name ("aspireapp", "getacme").
DOMAIN_SUFFIXES = frozenset({"app", "hq", "inc", "team", "careers", "jobs", "hiring", "io", "ai", "labs", "tech",
                             "global", "group", "india", "in", "co", "corp", "software", "systems", "data", "cloud",
                             "technologies", "technology", "solutions", "online", "web", "digital", "pay", "health",
                             "infoweb", "info"})
NAME_PARTS = ("technologies", "technology", "solutions", "software", "systems", "infoweb", "digital", "labs", "cloud",
              "health", "data", "tech", "group", "global", "ai")
DROP_SUFFIX = ("app", "hq", "inc", "team", "careers", "jobs", "hiring")
DROP_PREFIX = ("get", "try", "join", "use", "hello", "go", "meet", "with")


def humanize_label(label: str) -> str:
    """A domain label as a name: "ipsoftlabs" -> "Ipsoft Labs", "aspireapp" -> "Aspire",
    "acme-robotics" -> "Acme Robotics", "getzerion" -> "Zerion"."""
    parts = [p for p in re.split(r"[-_]+", label.lower()) if p]
    if len(parts) == 1:
        word = parts[0]
        for prefix in DROP_PREFIX:
            if word.startswith(prefix) and len(word) - len(prefix) >= 4:
                word = word[len(prefix):]
                break
        for suffix in DROP_SUFFIX:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[:-len(suffix)]
                break
        for part in NAME_PARTS:
            if word.endswith(part) and len(word) - len(part) >= 3:
                parts = [word[:-len(part)], part]
                break
        else:
            parts = [word]
    return " ".join(p.upper() if p == "ai" else p.capitalize() for p in parts)


# Domains.

def domain_of(email: str) -> str:
    return (email or "").rpartition("@")[2].strip().lower().strip(".")


def on(domain: str, names: frozenset[str]) -> bool:
    return any(domain == n or domain.endswith("." + n) for n in names)


def is_ats(domain: str) -> bool:
    return on(domain, ATS_DOMAINS)


def is_agency(domain: str, learned: Iterable[str] = ()) -> bool:
    """A recruiting agency, staffing firm or assessment platform (known, or `learned`: a
    sender domain that mailed about two or more different companies)."""
    domain = (domain or "").lower().strip(".")
    return bool(domain) and (on(domain, AGENCY_DOMAINS) or on(domain, frozenset(learned)))


def registrable(domain: str, agencies: Iterable[str] = ()) -> str:
    """The company's registrable domain; "" for free-mail, ATS, job-board, agency and tool domains."""
    domain = (domain or "").lower().strip(".")
    if not domain or "." not in domain or domain in FREEMAIL or on(domain, ATS_DOMAINS) or on(domain, TOOL_DOMAINS) \
            or is_agency(domain, agencies):
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
    name = humanize_label(label)
    return name if is_company(name, frozenset()) else ""


def agency_name(domain: str) -> str:
    """The agency or platform behind a domain, as a name (for `agency`, never the company)."""
    domain = (domain or "").lower().strip(".")
    labels = domain.split(".")
    if len(labels) < 2:
        return ""
    core = labels[-3] if len(labels) >= 3 and labels[-2] in SECOND_LEVEL else labels[-2]
    return humanize_label(core)


def from_people(emails: Iterable[str], owner_emails: Iterable[str] = (),
                env: dict[str, str] | None = None, agencies: Iterable[str] = ()) -> tuple[str, str]:
    """(name, domain) from the most common company domain among these addresses (not the owner's,
    not an agency's)."""
    owners = {e.lower() for e in owner_emails}
    mine = owner_domains(env)
    counts: Counter[str] = Counter()
    for email in emails:
        email = (email or "").strip().lower()
        if not email or email in owners:
            continue
        domain = registrable(domain_of(email), agencies)
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
    # A short title that is itself a name once a lead-in or meeting words go ("Considering
    # Cisco", "Nextiva Reschedule Interview Request"); only when something was stripped.
    name = clean(text, owner)
    stripped = tidy(text) != " ".join(text.split())
    if name and stripped and len(name.split()) <= 3 and any(w[:1].isupper() for w in name.split()):
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
            owner_names: Iterable[str] = (), agencies: Iterable[str] = (),
            env: dict[str, str] | None = None) -> Resolution:
    """The company, by the evidence order in the module docstring. `stated` is the name the
    source (or an earlier pass) gave: when it is a plausible company and a domain points at a
    *different* one, the stated name wins here (`conflict` is set, so the review pipeline asks the
    model). Agency and assessment-platform domains never name the company; they go to `agency`.
    `leads`: objects or dicts with `company` and `contacts` (emails), for the last resort.
    `owner_names`: more names of the owner seen in the source, never a company."""
    owner = owner_words(env) | {w for n in owner_names for w in words(n) if len(w) >= 3}
    owners = {e.lower() for e in owner_emails} | {e for e in re.split(r"[,\s]+", (env or os.environ).get(
        "VALR_OWNER_EMAILS", "").lower()) if e}
    agencies = frozenset(agencies)
    titled = from_title(title, owner)
    spoken = clean(stated, owner)
    everyone = [e for e in list(attendees) + [sender_email] if e]
    agency = next((agency_name(domain_of(e)) for e in everyone if is_agency(domain_of(e), agencies)), "")
    human = [(n, src) for n, src in ((spoken, "stated"), (titled, "title")) if n]

    def decide(name: str, domain: str, source: str) -> Resolution:
        """A domain's name, unless the source or title names a different company."""
        for better, _ in human:
            if same(better, name):  # The same company, as people spell it.
                return Resolution(better, domain, source, agency)
        if human:
            better, src = human[0]
            return Resolution(better, "", src, agency, conflict=f"{name} ({domain})")
        return Resolution(name, domain, source, agency)

    name, domain = from_people(attendees, owners, env, agencies)
    if name:
        return decide(name, domain, "attendees")
    sender = domain_of(sender_email)
    if sender and sender_email.lower() not in owners:
        if is_ats(sender) or is_agency(sender, agencies):
            ats = from_ats(sender_name, title, owner)
            if ats and not is_agency_name(ats, agencies):
                return Resolution(ats, "", "ats", agency)
        else:
            name, domain = from_people([sender_email], owners, env, agencies)
            if name:
                return decide(name, domain, "sender")
    for better, src in human:
        if not is_agency_name(better, agencies):
            return Resolution(better, "", src, agency)
    people = {(e or "").lower() for e in attendees} | ({sender_email.lower()} if sender_email else set())
    people -= owners | {""}
    for lead in leads or ():
        company = lead.get("company") if isinstance(lead, dict) else getattr(lead, "company", "")
        contacts = lead.get("contacts") if isinstance(lead, dict) else getattr(lead, "contacts", {})
        emails = {(c.get("email") if isinstance(c, dict) else getattr(c, "email", "")) or ""
                  for c in (contacts.values() if isinstance(contacts, dict) else contacts or [])}
        if people & {e.lower() for e in emails} and is_company(company, owner) and not company.startswith("Company not"):
            return Resolution(company, "", "lead", agency)
    return Resolution(agency=agency)


def is_agency_name(name: str, agencies: Iterable[str] = ()) -> bool:
    """Is this name one of the known (or learned) agencies or platforms?"""
    k = key(name).replace(" ", "")
    return bool(k) and any(key(agency_name(d)).replace(" ", "") == k for d in set(AGENCY_DOMAINS) | set(agencies))


def label(name: str, owner: Iterable[str] | None = None) -> str:
    """A name to show: the name if it is a company, else NOT_IDENTIFIED."""
    return name.strip() if name and is_company(name, owner) else NOT_IDENTIFIED
