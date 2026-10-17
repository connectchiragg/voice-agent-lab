"""Page wording from the stored data: clean interview labels, a written (second-person)
briefing instead of the phone script, prep items worth showing, India-only pay, and what
kind of reminder a block actually got (a heads-up on the phone or a phone call).

Pure functions; unit-tested in tests/test_valr_pages.py.
"""
from app.company_names import NOT_IDENTIFIED
import re
from collections import Counter
from typing import Any

GENERIC_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                   "icloud.com", "me.com", "proton.me", "protonmail.com", "example.com", "aol.com",
                   "zoho.com", "rediffmail.com"}
SUFFIXES = {"com", "co", "in", "io", "ai", "net", "org", "dev", "tech", "app", "example", "us", "uk",
            "de", "so", "xyz", "inc", "global", "group", "careers", "jobs", "hr", "mail", "team"}
ROUND_WORDS = [("system design", "System design"), ("hiring manager", "Hiring manager"),
               ("coding", "Coding"), ("technical", "Technical"), ("culture", "Culture fit"),
               ("behavioral", "Behavioural"), ("behavioural", "Behavioural"), ("screen", "Screen"),
               ("final", "Final round"), ("founder", "Founder chat"), ("hr", "HR round"),
               ("offer", "Offer chat")]
CALL_STATES = {"briefed", "unanswered", "missed", "refused", "calling", "dial_scheduled",
               "waiting_for_call_end", "waiting_for_research"}
HEADS_UP_STATES = {"heads_up_sent", "heads_up_failed"}
INDIA_PAY = re.compile(r"\bLPA\b|\blakhs?\b|\bcrores?\b|₹|\bINR\b|\bRs\.?\s?\d", re.I)
STOP_START = {"your", "the", "a", "an", "and", "or", "of", "to", "with", "in", "on", "for", "but"}


# Interview labels.

def company_from_people(people: list[dict[str, Any]]) -> str:
    """The outside people's company domain as a name (app/company_names.py): "Quillworks"."""
    from app import company_names
    return company_names.from_people([str(p.get("email") or "") for p in people if not p.get("is_self")])[0]


def round_from_title(title: str) -> str:
    """"L2- Alex" -> "Round 2"; "...: system design" -> "System design"; else ""."""
    match = re.search(r"\b(?:L|R|Round\s*|Level\s*)(\d)\b", title or "", re.I)
    if match:
        return f"Round {match.group(1)}"
    lowered = (title or "").lower()
    for word, label in ROUND_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return label
    return ""


def interview_label(slot: dict[str, Any]) -> dict[str, str]:
    """{"company", "round", "headline", "raw"}: the company (from the calendar, else the
    attendees' domain), the round, "Round 2 · quillworks" and the raw title, kept secondary."""
    from app import company_names
    company = company_names.display(slot.get("company") or "")
    company = company or company_from_people(slot.get("interviewers") or [])
    title = slot.get("title") or ""
    rnd = round_from_title(title)
    headline = " · ".join(x for x in (rnd, company) if x) or title or "Interview"
    return {"company": company or NOT_IDENTIFIED, "round": rnd, "headline": headline,
            "raw": title if title and title != headline else ""}


# The written briefing.

def written(text: str, owner: str = "") -> str:
    """A call script, for reading: no vocative or "here is your briefing", the owner's name
    as "you"/"your", and "quillworks dot com" as "quillworks.example"."""
    out = re.sub(r"\b([a-z0-9-]+) dot (com|io|ai|in|co|org|net|dev)\b", r"\1.\2", text or "", flags=re.I)
    if owner:
        name = re.escape(owner)
        out = re.sub(rf"^\s*(?:hi|hey|hello)?,?\s*{name}\s*[,.!:-]\s*", "", out, flags=re.I)
        out = re.sub(rf"\b{name}'s\b", "your", out)
        out = re.sub(rf"\b{name}\b", "you", out)
    out = re.sub(r"^\s*(?:ok(?:ay)?,?\s*)?(?:here is|here's|this is) your (?:briefing|brief)[^.:!]*[.:!]\s*",
                 "", out, flags=re.I)
    out = re.sub(r"\byou is\b", "you are", out)
    out = re.sub(r"\byou has\b", "you have", out)
    out = out.strip()
    return out[:1].upper() + out[1:]


def sentences(text: str, limit: int = 5) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text or "") if p.strip()]
    return parts[:limit]


def key_points(brief: dict[str, Any] | None, owner: str = "", limit: int = 5) -> list[str]:
    """What to read before the interview: the brief's notes (not the sensitive ones), else
    the first sentences of the written-out script."""
    if not brief:
        return []
    notes = [written(n["fact"], owner) for n in brief.get("notes") or []
             if not n.get("sensitive") and n.get("fact") and "[email hidden]" not in n["fact"]]
    if notes:
        return notes[:limit]
    return sentences(written(brief.get("spoken") or "", owner), limit)


def good_prep(item: str) -> bool:
    """Hide garbled or truncated prep lines ("Your measured turns of 500-4,500")."""
    words = (item or "").split()
    if len(words) < 3 or len(item) > 220:
        return False
    if item.count("(") != item.count(")") or item.count('"') % 2:
        return False
    first = words[0].lower().strip("\"'")
    if first in STOP_START and len(words) < 8:
        return False  # A fragment, not an instruction.
    if re.search(r"\d[\d,.]*\s*[-–]\s*[\d,.]+\s*$", item) and len(words) < 8:
        return False  # Ends on a bare number range.
    return not item.rstrip().endswith((",", " of", " and", " the", " to", "-"))


# Pay.

def india_pay(pay: dict[str, Any]) -> dict[str, Any]:
    """Only pay stated for India (LPA / INR / lakh); anything else is left out."""
    market = pay.get("range") if isinstance(pay.get("range"), dict) else None
    if market and not (str(market.get("unit") or "").upper() == "LPA"
                       or str(market.get("currency") or "").upper() == "INR"):
        market = None
    claims = [c for c in pay.get("claims") or [] if INDIA_PAY.search(c.get("claim") or "")]
    return {"range": market, "claims": claims}


# Reminders.

def reminder_kind(block: dict[str, Any], mode: str) -> str:
    """"heads_up" or "call": what this block actually got (older blocks were phone calls)."""
    if block.get("heads_up") or block.get("state") in HEADS_UP_STATES:
        return "heads_up"
    if block.get("state") in CALL_STATES:
        return "call"
    return "call" if mode == "call" else "heads_up"


# Leads, in the owner's words (no classifier labels, no repeated company names).

ROLE_PREFIX = re.compile(r"^(?:recruiter|hiring manager|hr|talent|coordinator|interviewer|founder|agency|they)\s*:\s*", re.I)
AGE = re.compile(r"\s*\((yesterday|today|\d+\s+(?:day|week|hour)s?\s+ago)\)\s*$", re.I)
VERBS = {"share": "Send", "send": "Send", "decide": "Decide", "confirm": "Confirm", "reply": "Reply",
         "book": "Book", "pick": "Pick", "choose": "Pick", "complete": "Finish", "fill": "Fill in"}


def age_words(text: str) -> str:
    """"(2 days ago)" -> "2 days", "(yesterday)" -> "1 day", "(today)" -> "today"."""
    text = text.lower().strip()
    if text == "yesterday":
        return "1 day"
    return text.removesuffix(" ago")


def ask_line(reason: str) -> str:
    """"Recruiter: share your availability for a screen (2 days ago)" ->
    "Send your availability for a screen · 2 days"."""
    text = (reason or "").strip()
    age = AGE.search(text)
    text = AGE.sub("", text)
    text = ROLE_PREFIX.sub("", text).strip()
    if text:
        first, _, rest = text.partition(" ")
        verb = VERBS.get(first.lower())
        text = (verb + (" " + rest if rest else "")) if verb else text[:1].upper() + text[1:]
    return text + (f" · {age_words(age.group(1))}" if age and text else "")


def waiting_line(reason: str) -> str:
    """"Waiting on Pikeworks for 1 day" -> "Waiting on them · 1 day" (the row already names them)."""
    match = re.match(r"^\s*waiting on .+? for (.+?)\s*$", reason or "", re.I)
    if match:
        return f"Waiting on them · {match.group(1)}"
    return (reason or "").strip()


def without_company(text: str, company: str) -> str:
    """"Interview: Arcworks · Panel" -> "Panel"; "Arcworks: offer" -> "Offer"."""
    out = re.sub(r"^\s*interview\s*:\s*", "", text or "", flags=re.I)
    if company:
        out = re.sub(rf"^\s*{re.escape(company)}\s*(?::|·|-|–)\s*", "", out, flags=re.I)
    out = out.strip()
    return out[:1].upper() + out[1:]
