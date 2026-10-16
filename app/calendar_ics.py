"""The owner's calendar (a secret iCalendar URL): events, and the interview blocks briefed.

Standard library only, so Temporal workflow code and both images can import it.

- `parse(text)`: VEVENTs as `CalEvent`s. Times with TZID (IANA), UTC (`Z`) or floating
  (read in the calendar's X-WR-TIMEZONE); all-day events (`VALUE=DATE`) are kept but
  marked. Simple RRULEs (DAILY / WEEKLY, INTERVAL, UNTIL, COUNT, BYDAY) are expanded,
  EXDATEs dropped and RECURRENCE-ID overrides replace their occurrence.
- `declined`: the owner's own ATTENDEE line says PARTSTAT=DECLINED (the owner's
  addresses: the calendar's X-WR-CALNAME and VALR_OWNER_EMAILS).
- `group_blocks(events)`: timed, not declined, not cancelled events; consecutive ones
  with a gap under BLOCK_GAP form one block. A block's id is derived from its start
  only, so the same block keeps the same id on every sync, and a moved block gets a new one.

Attendee emails stay in memory and in the owner's Postgres; they are never logged.
"""
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BLOCK_GAP = timedelta(minutes=30)
MAX_DESCRIPTION_CHARS = 1500
MAX_OCCURRENCES = 400  # Per recurring event, so a runaway RRULE cannot blow up a sync.
GENERIC_DOMAINS = frozenset("""
    gmail.com googlemail.com yahoo.com yahoo.co.in outlook.com hotmail.com live.com icloud.com
    me.com proton.me protonmail.com rediffmail.com calendly.com google.com
    resource.calendar.google.com group.calendar.google.com zoom.us teams.microsoft.com
    microsoft.com ashbyhq.com greenhouse.io lever.co goodtime.io gem.com modernloop.io
    calendar.google.com cal.com hirequotient.com example.com
    """.split())
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


@dataclass
class Attendee:
    email: str
    name: str = ""
    partstat: str = ""


@dataclass
class CalEvent:
    uid: str
    title: str
    start: datetime  # Aware (UTC) for timed events; midnight UTC of the date if all-day.
    end: datetime
    all_day: bool = False
    status: str = ""  # CONFIRMED, TENTATIVE, CANCELLED.
    organizer: str = ""  # Email.
    organizer_name: str = ""
    attendees: list[Attendee] = field(default_factory=list)
    location: str = ""
    description: str = ""
    declined: bool = False  # By the owner.

    @property
    def key(self) -> str:
        """Unique per occurrence."""
        return f"{self.uid}@{self.start:%Y%m%dT%H%M%SZ}"


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def split_line(line: str) -> tuple[str, dict[str, str], str]:
    """`NAME;P1=a;P2="b:c":value` -> (NAME, {P1: a, P2: b:c}, value)."""
    head, value, quoted = "", "", False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            head, value = line[:index], line[index + 1:]
            break
    else:
        return line.upper(), {}, ""
    name, *params = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', head)
    parsed = {}
    for param in params:
        key, _, val = param.partition("=")
        parsed[key.upper()] = val.strip('"')
    return name.upper(), parsed, value


def unescape(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def zone_of(name: str | None, default: ZoneInfo) -> ZoneInfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default  # Windows zone names and the like: the calendar's own zone.


def parse_time(value: str, params: dict[str, str], default: ZoneInfo) -> tuple[datetime, bool]:
    """-> (aware UTC datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d")
        return day.replace(tzinfo=timezone.utc), True
    if value.endswith("Z"):
        return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc), False
    local = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    return local.replace(tzinfo=zone_of(params.get("TZID"), default)).astimezone(timezone.utc), False


def mail_of(value: str) -> str:
    return value.split(":", 1)[1].strip().lower() if value.lower().startswith("mailto:") else ""


@dataclass
class RawEvent:
    event: CalEvent
    rrule: str = ""
    exdates: set[datetime] = field(default_factory=set)
    recurrence_id: datetime | None = None
    local_start: datetime | None = None  # In its own zone, for expanding a recurrence.


def parse(text: str, owner_emails: set[str] | frozenset[str] = frozenset(),
          default_zone: str = "Asia/Kolkata") -> list[CalEvent]:
    """All events (occurrences of recurring events expanded), sorted by start."""
    lines = unfold(text)
    calendar_zone = ZoneInfo(default_zone)
    owners = {email.lower() for email in owner_emails}
    for line in lines:
        name, _, value = split_line(line)
        if name == "X-WR-TIMEZONE":
            calendar_zone = zone_of(value.strip(), calendar_zone)
        elif name == "X-WR-CALNAME" and "@" in value:
            owners.add(value.strip().lower())
        elif name == "BEGIN" and value == "VEVENT":
            break
    raws: list[RawEvent] = []
    current: RawEvent | None = None
    depth = 0  # Inside a VALARM within the event.
    for line in lines:
        name, params, value = split_line(line)
        if name == "BEGIN" and value == "VEVENT":
            current = RawEvent(CalEvent("", "", datetime.min.replace(tzinfo=timezone.utc),
                                        datetime.min.replace(tzinfo=timezone.utc)))
            depth = 0
            continue
        if current is None:
            continue
        if name == "BEGIN":
            depth += 1
            continue
        if name == "END" and value == "VEVENT":
            event = current.event
            if event.end <= event.start:
                event.end = event.start + (timedelta(days=1) if event.all_day else timedelta(hours=1))
            event.declined = any(a.email in owners and a.partstat == "DECLINED"
                                 for a in event.attendees)
            raws.append(current)
            current = None
            continue
        if name == "END":
            depth -= 1
            continue
        if depth > 0:
            continue
        event = current.event
        if name == "UID":
            event.uid = value.strip()
        elif name == "SUMMARY":
            event.title = unescape(value).strip()
        elif name == "DTSTART":
            event.start, event.all_day = parse_time(value, params, calendar_zone)
            tz = params.get("TZID")
            if not event.all_day and tz and not value.endswith("Z"):
                current.local_start = datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(
                    tzinfo=zone_of(tz, calendar_zone))
        elif name == "DTEND":
            event.end, _ = parse_time(value, params, calendar_zone)
        elif name == "DURATION":
            match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value.strip())
            if match:
                days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
                event.end = event.start + timedelta(days=days, hours=hours, minutes=minutes,
                                                    seconds=seconds)
        elif name == "STATUS":
            event.status = value.strip().upper()
        elif name == "LOCATION":
            event.location = unescape(value).strip()[:300]
        elif name == "DESCRIPTION":
            event.description = unescape(value).strip()[:MAX_DESCRIPTION_CHARS]
        elif name == "ORGANIZER":
            event.organizer, event.organizer_name = mail_of(value), params.get("CN", "")
        elif name == "ATTENDEE":
            email = mail_of(value)
            if email:
                event.attendees.append(Attendee(email, params.get("CN", "") if params.get("CN", "")
                                                != email else "", params.get("PARTSTAT", "").upper()))
        elif name == "RRULE":
            current.rrule = value.strip()
        elif name == "EXDATE":
            for item in value.split(","):
                current.exdates.add(parse_time(item, params, calendar_zone)[0])
        elif name == "RECURRENCE-ID":
            current.recurrence_id = parse_time(value, params, calendar_zone)[0]
    overrides = {(raw.event.uid, raw.recurrence_id): raw.event for raw in raws if raw.recurrence_id}
    events: list[CalEvent] = []
    for raw in raws:
        if raw.recurrence_id is not None:
            events.append(raw.event)
            continue
        for occurrence in expand(raw):
            if occurrence.start in raw.exdates or (occurrence.uid, occurrence.start) in overrides:
                continue
            events.append(occurrence)
    return sorted(events, key=lambda event: (event.start, event.title))


def rrule_parts(rule: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in rule.split(";") if "=" in part)


def expand(raw: RawEvent) -> list[CalEvent]:
    """The event itself, or each occurrence of a DAILY/WEEKLY RRULE (others: the first only)."""
    event = raw.event
    if not raw.rrule:
        return [event]
    rule = rrule_parts(raw.rrule)
    freq = rule.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY"):
        return [event]
    interval = max(int(rule.get("INTERVAL", "1") or 1), 1)
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        until, _ = parse_time(rule["UNTIL"], {}, ZoneInfo("UTC"))
        if len(rule["UNTIL"]) == 8:
            until += timedelta(days=1)
    duration = event.end - event.start
    base = raw.local_start or event.start  # Expand in local time, so DST keeps the clock time.
    days = {WEEKDAY_CODES.index(code[-2:]) for code in rule.get("BYDAY", "").split(",")
            if code[-2:] in WEEKDAY_CODES} if freq == "WEEKLY" else set()
    out: list[CalEvent] = []
    step = 0
    while len(out) < MAX_OCCURRENCES:
        if freq == "DAILY":
            candidates = [base + timedelta(days=step * interval)]
        else:
            week_start = base - timedelta(days=base.weekday()) + timedelta(weeks=step * interval)
            candidates = sorted(week_start + timedelta(days=day) for day in (days or {base.weekday()}))
            candidates = [moment for moment in candidates if moment >= base]
        step += 1
        stop = False
        for local in candidates:
            start = local.astimezone(timezone.utc)
            if (until is not None and start > until) or (count is not None and len(out) >= count):
                stop = True
                break
            out.append(CalEvent(event.uid, event.title, start, start + duration, event.all_day,
                                event.status, event.organizer, event.organizer_name,
                                list(event.attendees), event.location, event.description,
                                event.declined))
        if stop or (until is None and count is None and step > 104):
            break
    return out


# Interviews and blocks.

def company_domain(event: CalEvent) -> str:
    """The company's domain among the organizer and attendees (app/company_names.py)."""
    return _resolved(event).domain


def is_company_name(name: str) -> bool:
    """False for round labels and filler ("L2- Alex", "Round 2", "Interview") or the owner's name."""
    from app import company_names
    return company_names.is_company(name)


def company_name(event: CalEvent) -> str:
    """The company (app/company_names.py, the one resolver); "" when none is identified."""
    return _resolved(event).name


def _resolved(event: CalEvent):
    from app import company_names
    return company_names.resolve(attendees=[event.organizer] + [a.email for a in event.attendees],
                                 title=event.title)


def is_briefable(event: CalEvent) -> bool:
    return not event.all_day and not event.declined and event.status != "CANCELLED"


@dataclass
class Block:
    start: datetime
    end: datetime
    events: list[CalEvent]

    @property
    def block_id(self) -> str:
        return block_id(self.start)

    def fingerprint(self) -> str:
        """Changes when anything a briefing uses changes (times, titles, people)."""
        text = "|".join(f"{e.key}~{e.end:%H%M}~{e.title}~{','.join(sorted(a.email for a in e.attendees))}"
                        for e in self.events)
        return hashlib.sha256(text.encode()).hexdigest()[:16]


def block_id(start: datetime) -> str:
    return f"briefing-{start.astimezone(timezone.utc):%Y%m%dT%H%MZ}"


def group_blocks(events: list[CalEvent], gap: timedelta = BLOCK_GAP) -> list[Block]:
    """Briefable events into blocks: the next event joins if it starts less than `gap`
    after the block's latest end (overlapping events join too)."""
    blocks: list[Block] = []
    for event in sorted((e for e in events if is_briefable(e)), key=lambda e: (e.start, e.end)):
        if blocks and event.start - blocks[-1].end < gap:
            blocks[-1].events.append(event)
            blocks[-1].end = max(blocks[-1].end, event.end)
        else:
            blocks.append(Block(event.start, event.end, [event]))
    return blocks


def upcoming_blocks(events: list[CalEvent], now: datetime, horizon: timedelta) -> list[Block]:
    """Blocks starting after `now` and within `horizon`. Grouping uses every event from
    an hour ago on, so a block in progress is not mistaken for a new one."""
    recent = [event for event in events if event.end > now - timedelta(hours=1)]
    return [block for block in group_blocks(recent) if now < block.start <= now + horizon]


def on_day(events: list[CalEvent], day: date, zone: str) -> list[CalEvent]:
    tz = ZoneInfo(zone)
    out = []
    for event in events:
        if event.all_day:
            if event.start.date() <= day < event.end.date():
                out.append(event)
        elif event.start.astimezone(tz).date() == day:
            out.append(event)
    return out
