"""Interview slots: the event snapshot, its diff, and who counts as an interviewer.

Standard library only: Temporal workflow code, the app and the research worker all
import it. Nothing here logs; attendee emails never leave these structures except
into the owner's Postgres and the Temporal history on the VM.

- `SlotEvent`: one calendar event (one occurrence) as last seen. Its `key` is stable
  per event: the Google event id (with the instance suffix for a recurring one); from
  the ICS fallback the UID is normalised to the same form.
- `diff(old, new)`: what changed, as the kinds that drive the work:
  new, cancelled, time, interviewers (added / removed), round (title or description),
  company; none when nothing material changed.
- `attendee_role`: the guard that keeps recruiting, HR, coordination people and tools,
  rooms and the owner out of interviewer research. Uncertain people are "candidate":
  a cheap classifier decides (app/interview_agents.py), with the default stated there.
"""
import hashlib
import re
from dataclasses import dataclass, field

# Scheduling, ATS and meeting tools: never people to research.
TOOL_DOMAINS = frozenset("""
    greenhouse.io greenhouse-mail.io lever.co hire.lever.co ashbyhq.com goodtime.io gem.com
    calendly.com workday.com myworkday.com smartrecruiters.com modernloop.io hirequotient.com
    zoom.us teams.microsoft.com calendar.google.com resource.calendar.google.com
    group.calendar.google.com icims.com jobvite.com recruitee.com workable.com breezy.hr
    darwinbox.in keka.com zohorecruit.com hackerrank.com hackerearth.com
    codility.com coderpad.io karat.com metaview.ai brighthire.ai fathom.video fireflies.ai otter.ai
    """.split())
PERSONAL_DOMAINS = frozenset("""
    gmail.com googlemail.com yahoo.com yahoo.co.in outlook.com hotmail.com live.com icloud.com
    me.com proton.me protonmail.com rediffmail.com
    """.split())
TOOL_LOCAL = re.compile(r"no-?reply|do-?not-?reply|recruit|talent|career|hiring|^jobs?$|^hr$|"
                        r"^people$|^interviews?$|^scheduling$|^calendar$|^notifications?$|"
                        r"^team$|^hello$|^info$|^support$|^ta$", re.I)
HR_NAME = re.compile(r"recruit|talent|human resources|people ?ops|people operations|"
                     r"coordinator|sourcer|sourcing|hiring team|scheduling|"
                     r"campus|staffing|headhunt", re.I)
HR_ABBREVIATION = re.compile(r"\b(HR|TA)\b")  # Case-sensitive: "Ta Nguyen" is a name.


@dataclass
class Person:
    email: str
    name: str = ""
    response: str = ""  # accepted, declined, tentative, needsAction.
    is_self: bool = False
    resource: bool = False
    organizer: bool = False


@dataclass
class SlotEvent:
    key: str
    title: str
    start_utc: str  # ISO 8601 with offset.
    end_utc: str
    all_day: bool = False
    status: str = "confirmed"  # confirmed, tentative, cancelled.
    organizer_email: str = ""
    organizer_name: str = ""
    attendees: list[Person] = field(default_factory=list)
    description: str = ""
    location: str = ""
    declined: bool = False  # By the owner.
    company: str = ""
    company_domain: str = ""


@dataclass
class SlotDiff:
    kinds: list[str]
    added: list[Person] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # Emails.


def slot_workflow_id(key: str) -> str:
    return "slot-" + hashlib.sha256(key.encode()).hexdigest()[:20]


def domain_of(email: str) -> str:
    return email.rpartition("@")[2].lower()


def attendee_role(person: Person, owners: set[str] | frozenset[str] = frozenset()) -> str:
    """owner | resource | tool | recruiter | candidate (to be classified)."""
    email = person.email.lower()
    local, _, domain = email.rpartition("@")
    if person.is_self or email in owners:
        return "owner"
    if person.resource or domain.endswith(("resource.calendar.google.com",
                                           "group.calendar.google.com", "group.v.calendar.google.com")):
        return "resource"
    if domain in TOOL_DOMAINS or any(domain.endswith("." + tool) for tool in TOOL_DOMAINS):
        return "tool"
    if TOOL_LOCAL.search(local):
        return "tool"
    if person.name and (HR_NAME.search(person.name) or HR_ABBREVIATION.search(person.name)):
        return "recruiter"
    return "candidate"


def people(event: SlotEvent, owners: set[str] | frozenset[str] = frozenset()) -> list[Person]:
    """The people who might be interviewers: attendees plus the organizer, minus the guard."""
    seen: dict[str, Person] = {}
    everyone = list(event.attendees)
    if event.organizer_email and all(p.email.lower() != event.organizer_email.lower()
                                     for p in everyone):
        everyone.append(Person(event.organizer_email, event.organizer_name, organizer=True))
    for person in everyone:
        if person.response == "declined" and not person.organizer:
            continue
        if attendee_role(person, owners) == "candidate":
            seen.setdefault(person.email.lower(), person)
    return list(seen.values())


def norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def diff(old: SlotEvent | None, new: SlotEvent,
         owners: set[str] | frozenset[str] = frozenset()) -> SlotDiff:
    if new.status == "cancelled" or new.declined:
        return SlotDiff(["cancelled"] if old is not None else ["none"])
    if old is None or old.status == "cancelled" or old.declined:
        return SlotDiff(["new"], added=people(new, owners))
    kinds: list[str] = []
    if old.start_utc != new.start_utc or old.end_utc != new.end_utc:
        kinds.append("time")
    before = {p.email.lower(): p for p in people(old, owners)}
    after = {p.email.lower(): p for p in people(new, owners)}
    added = [person for email, person in after.items() if email not in before]
    removed = [email for email in before if email not in after]
    if added or removed:
        kinds.append("interviewers")
    if norm(old.title) != norm(new.title) or norm(old.description) != norm(new.description):
        kinds.append("round")
    if norm(old.company) != norm(new.company) or old.company_domain != new.company_domain:
        kinds.append("company")
    return SlotDiff(kinds or ["none"], added, removed)


def company_key(event: SlotEvent) -> str:
    """Company research is cached per company (7 days) and shared across slots."""
    base = event.company_domain or norm(event.company) or norm(event.title)
    return "company-" + hashlib.sha256(base.encode()).hexdigest()[:16]


# Blocks: consecutive interviews briefed in one call.

BLOCK_GAP_S = 30 * 60  # A gap under 30 minutes keeps the next event in the block.


def briefable(event: SlotEvent) -> bool:
    return not event.all_day and not event.declined and event.status != "cancelled"


@dataclass
class BlockPlan:
    block_id: str
    start_utc: str
    end_utc: str
    slot_keys: list[str]


def block_id_for(start_utc: str) -> str:
    """Derived from the block's start only: the same block keeps its id on every sync."""
    from datetime import datetime, timezone
    moment = datetime.fromisoformat(start_utc).astimezone(timezone.utc)
    return f"briefing-{moment:%Y%m%dT%H%MZ}"


def group_blocks(events: list[SlotEvent]) -> list[BlockPlan]:
    from datetime import datetime
    blocks: list[BlockPlan] = []
    end = None
    for event in sorted((e for e in events if briefable(e)), key=lambda e: (e.start_utc, e.end_utc)):
        start, stop = datetime.fromisoformat(event.start_utc), datetime.fromisoformat(event.end_utc)
        if blocks and end is not None and (start - end).total_seconds() < BLOCK_GAP_S:
            blocks[-1].slot_keys.append(event.key)
            if stop > end:
                end = stop
                blocks[-1].end_utc = event.end_utc
        else:
            blocks.append(BlockPlan(block_id_for(event.start_utc), event.start_utc, event.end_utc,
                                    [event.key]))
            end = stop
    return blocks


def fingerprint(event: SlotEvent, owners: set[str] | frozenset[str] = frozenset()) -> str:
    """Changes whenever an input to the research changes (stored with the research)."""
    text = "|".join([event.key, event.company, event.company_domain, event.start_utc, event.title,
                     " ".join(event.description.split()),
                     ",".join(sorted(p.email.lower() for p in people(event, owners)))])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def brief_is_current(event: SlotEvent, slot: dict, owners: set[str] | frozenset[str] = frozenset()) -> bool:
    """A finished brief whose inputs are unchanged. A time-only move does not need new
    research, so the check uses the start the research was done for."""
    if not slot or slot.get("status") not in ("done", "partial") or not slot.get("spoken"):
        return False
    start = slot.get("start_utc")
    researched_start = start.isoformat() if hasattr(start, "isoformat") else str(start or event.start_utc)
    from dataclasses import replace
    stored = (slot.get("parts") or {}).get("_fingerprint")
    return stored == fingerprint(replace(event, start_utc=researched_start), owners)
