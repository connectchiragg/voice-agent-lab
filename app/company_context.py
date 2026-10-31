"""Everything VALR already knows about the owner's history with one company, in one compact block.

The single company-context builder. Used by the pre-interview research (the private researcher's
input and the slot synthesis), the briefing call's prompt and the interview page ("What you've
already discussed with X"), so all three see the same thing.

Keyed by the canonical company (app/company_names.same), its registrable domain and the people's
email addresses (outside attendees only). Sources, all already in Postgres or on disk:

- `leads` and `lead_touchpoints` (app/leads_store.py): the lead's stage, next step and contacts;
  every kept email (date, who, the stored summary of at most ~24 words, what it asks), calendar
  interviews, recordings and scores. The encrypted Gmail snippet is never read.
- `notetaker_recordings` + `recording_transcripts`: every recording of this company from any
  provider (Fathom, Fireflies, Granola and iPhone calls, provider "phone"), hidden ones excluded;
  the interviewer's questions and any pay talk come from the owner's encrypted transcript copy.
- performance.json (the coach's reviews): round type, score, outcome, questions asked and the
  coach's suggestions, per scored interview.
- `calendar_history`: past interviews without a recording.
- `call_summaries`: the owner's own notes from his VALR calls that name the company.

Compact by construction: one line per item, items ranked by relevance and recency, then cut to
TOKEN_BUDGET (about 2.5k tokens, so a Groq free-tier request still fits its per-minute limit)
and rendered newest first. Pay figures are quoted as said, labelled "discussed, not a
confirmed budget". Email addresses and phone numbers are hidden. Nothing here logs content.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app import company_names

logger = logging.getLogger("valr")

TOKEN_BUDGET = 2500
CHARS_PER_TOKEN = 3.5  # Conservative for English on Llama-family tokenizers (about 4).
MAX_RECORDINGS = 6       # Recordings read in full (transcripts decrypted), newest first.
LINE_CHARS = 260
RECORDING_CHARS = 700
PROVIDER_LABEL = {"fathom": "Fathom", "fireflies": "Fireflies", "granola": "Granola", "phone": "phone call"}
# Relevance of each kind of item: interviews and their reviews first, then emails.
WEIGHT = {"review": 1.0, "recording": 0.95, "round": 0.8, "email": 0.6, "note": 0.55, "calendar": 0.5}
FORWARD = ("sourced", "applied", "screening", "interviewing", "final", "offer")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{7,}\d(?![\w])")
MONEY = re.compile(
    r"(?:(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)\s?\d[\d,]*(?:\.\d+)?\s?(?:k|l|lakhs?|lacs?|lpa|cr|crores?|m|mn|million)?\b"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:-|to|–)?\s?(?:\d[\d,]*(?:\.\d+)?\s?)?"
    r"(?:lpa|lakhs?|lacs?|l\b|crores?|cr\b|k\b|ctc\b))", re.I)
PAY_WORDS = re.compile(r"salary|ctc|compensation|\bcomp\b|budget|\bband\b|expect|offer|fixed|variable|"
                       r"\besop|equity|stock|bonus|package|hike|lpa|per annum|current pay", re.I)


@dataclass
class Item:
    at: datetime | None
    kind: str  # email | calendar | round | recording | review | note
    text: str
    weight: float = 0.5


@dataclass
class Context:
    company: str
    lead: dict[str, Any] | None = None
    stages: list[tuple[datetime, str]] = field(default_factory=list)
    asks: list[str] = field(default_factory=list)
    pay: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.lead or self.items or self.pay)


# Identity.

def registrable(domain: str) -> str:
    from app import leads
    return leads.company_domain(domain or "")


def same(name: str, company: str) -> bool:
    return bool(name and company and company_names.same(name, company))


def lead_matches(lead: dict[str, Any], company: str, domain: str, emails: set[str]) -> bool:
    if domain and registrable(lead.get("domain") or "") == domain:
        return True
    if same(lead.get("company") or "", company):
        return True
    contacts = lead.get("contacts") or []
    return bool(emails) and any(str(c.get("email") or "").lower() in emails for c in contacts if isinstance(c, dict))


def recording_matches(row: dict[str, Any], company: str, domain: str, hashes: set[str]) -> bool:
    if domain and registrable(row.get("company_domain") or "") == domain:
        return True
    if row.get("company_domain") and domain:
        return False  # Another company's domain: a namesake, not this one.
    if same(row.get("company") or "", company) or same(row.get("event_company") or "", company):
        return True
    return bool(hashes & set(names(row.get("invitee_hashes"))))


def names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in loaded if v] if isinstance(loaded, list) else []


# Text helpers.

def clean(text: Any, limit: int = LINE_CHARS) -> str:
    value = " ".join(str(text or "").split())
    value = PHONE.sub("[phone hidden]", EMAIL.sub("[email hidden]", value))
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def day(moment: datetime | None) -> str:
    return moment.astimezone(timezone.utc).strftime("%d %b %Y").lstrip("0") if moment else "undated"


def as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def pay_mentions(text: str) -> list[str]:
    """Sentences that state a pay figure ("Budget is 40-45 LPA fixed"), as said."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n| / ", text or ""):
        if MONEY.search(sentence) and PAY_WORDS.search(sentence):
            out.append(clean(sentence, 180))
    return out


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def who(people: Iterable[dict[str, Any]]) -> str:
    out = []
    for person in people:
        name = str(person.get("name") or "").strip() or (str(person.get("email") or "").partition("@")[0])
        role = str(person.get("role") or "")
        if name:
            out.append(f"{name} ({role.replace('_', ' ')})" if role and role != "other" else name)
    return ", ".join(out[:3])


# Items from each source (pure).

def touch_items(touches: list[dict[str, Any]], skip_recordings: set[str] = frozenset()) -> list[Item]:
    """lead_touchpoints rows {id, kind, at, data} -> items. Recording/score touches that the
    recordings and reviews already cover (`skip_recordings`: "<provider>:<id>") are left out."""
    out = []
    for row in touches:
        data, kind, at = row.get("data") or {}, row.get("kind"), as_dt(row.get("at"))
        if kind == "email":
            direction = "you wrote" if data.get("direction") == "out" else f"from {who(data.get('people') or []) or 'them'}"
            ask = f" Asks: {clean(data.get('action'), 90)}." if data.get("action_requested") and data.get("action") else ""
            body = ". ".join(part for part in (clean(data.get("title"), 90), clean(data.get("summary"), 180)) if part)
            text = f"Email {direction}: {body or 'no summary'}{ask}"
            out.append(Item(at, "email", text, WEIGHT["email"] + (0.1 if ask else 0)))
        elif kind == "calendar":
            people = who(data.get("people") or [])
            out.append(Item(at, "calendar", f"Calendar: {clean(data.get('title'), 120)}"
                                            + (f" with {people}" if people else ""), WEIGHT["calendar"]))
        elif kind == "recording" and row.get("id") not in skip_recordings:
            out.append(Item(at, "recording", f"{clean(data.get('title'), 120)}: {clean(data.get('summary'), 200)}",
                            WEIGHT["recording"] - 0.2))
        elif kind == "evaluation" and str(row.get("id") or "").removeprefix("perf:") not in skip_recordings:
            out.append(Item(at, "review", f"{clean(data.get('title'), 80)}. {clean(data.get('summary'), 80)}",
                            WEIGHT["review"] - 0.3))
    return out


def review_text(review: dict[str, Any]) -> str:
    """One scored interview from performance.json, compact."""
    bits = [str(review.get("round_type") or "interview").replace("_", " ")]
    if review.get("overall_score") is not None:
        bits.append(f"coach score {review['overall_score']}/10")
    if review.get("outcome") and review.get("outcome") != "unknown":
        bits.append(f"outcome {review['outcome']}")
    asked = [q.get("question") for q in review.get("question_feedback") or [] if isinstance(q, dict)] \
        or list(review.get("questions_asked") or [])
    improve = [q.get("improve") for q in review.get("question_feedback") or [] if isinstance(q, dict) and q.get("improve")]
    text = "Coach review: " + ", ".join(bits) + "."
    if asked:
        text += " They asked: " + "; ".join(clean(q, 90) for q in asked[:4]) + "."
    if improve:
        text += " Coach suggested: " + "; ".join(clean(s, 80) for s in improve[:2]) + "."
    elif review.get("do_next_time"):
        text += " Next time: " + clean(review["do_next_time"][0], 90) + "."
    return text


def transcript_points(turns: list[dict[str, Any]], candidate: str = "") -> tuple[list[str], list[str]]:
    """(their questions, pay lines) from a transcript's turns, without a model."""
    mine = {w for w in (candidate or "").lower().split() if len(w) > 1}
    questions, pay = [], []
    for turn in turns:
        speaker = str(turn.get("speaker") or "")
        text = " ".join(str(turn.get("text") or "").split())
        is_me = bool(mine) and bool(mine & set(speaker.lower().split()))
        for line in pay_mentions(text):
            pay.append(f"{'you' if is_me else (speaker.split(' (')[0] or 'they')}: {line}")
        if not is_me and "?" in text and len(text) > 25:
            question = next((s for s in re.split(r"(?<=[.!?])\s+", text) if s.endswith("?") and len(s) > 20), "")
            if question:
                questions.append(clean(question, 110))
    return questions, pay


def recording_item(row: dict[str, Any], turns: list[dict[str, Any]] | None, review: dict[str, Any] | None,
                   candidate: str = "") -> tuple[Item, list[str]]:
    """One recording: its review when scored, else the interviewer's questions; and its pay lines."""
    provider = PROVIDER_LABEL.get(row.get("provider") or "", "recording")
    people = ", ".join(names(row.get("interviewers"))[:3])
    head = f"Recorded {provider}: {clean(row.get('title') or row.get('event_title'), 100)}" \
           + (f" with {clean(people, 80)}" if people else "")
    questions, pay = transcript_points(turns or [], candidate)
    if review:
        body = review_text(review)
    elif questions:
        body = "They asked: " + "; ".join(questions[:4]) + "."
    elif row.get("eval_status") == "handed_off" or turns:
        body = "Not an interview per the coach (no review)." if row.get("not_interview") else "Transcript kept, no review yet."
    else:
        body = "No transcript kept."
    when = as_dt(row.get("started_at"))
    return Item(when, "recording", clean(f"{head}. {body}", RECORDING_CHARS), WEIGHT["recording"]), \
        [f"{day(when)} {provider}, {line}" for line in pay]


def stage_history(touches: list[dict[str, Any]]) -> list[tuple[datetime, str]]:
    """Forward stage moves (and a close) in time order, from the touches' stage hints."""
    out: list[tuple[datetime, str]] = []
    best = -1
    for row in sorted(touches, key=lambda r: as_dt(r.get("at")) or datetime.min.replace(tzinfo=timezone.utc)):
        hint = str((row.get("data") or {}).get("stage_hint") or "")
        at = as_dt(row.get("at"))
        if at is None:
            continue
        if hint in FORWARD and FORWARD.index(hint) > best:
            best = FORWARD.index(hint)
            out.append((at, hint))
        elif hint in ("closed-won", "closed-lost") and (not out or out[-1][1] != hint):
            out.append((at, hint))
    return out


def open_asks(touches: list[dict[str, Any]]) -> list[str]:
    """Their requests he has not answered: an inbound email that asks something, with no later
    outbound email in the same thread."""
    replied: dict[str, datetime] = {}
    for row in touches:
        data = row.get("data") or {}
        at = as_dt(row.get("at"))
        if row.get("kind") == "email" and data.get("direction") == "out" and data.get("thread") and at:
            replied[data["thread"]] = max(at, replied.get(data["thread"], at))
    out = []
    for row in sorted(touches, key=lambda r: as_dt(r.get("at")) or datetime.min.replace(tzinfo=timezone.utc),
                      reverse=True):
        data, at = row.get("data") or {}, as_dt(row.get("at"))
        if row.get("kind") != "email" or data.get("direction") == "out" or not data.get("action_requested"):
            continue
        if data.get("thread") in replied and at and replied[data["thread"]] > at:
            continue
        if data.get("action"):
            out.append(f"{day(at)}: {clean(data['action'], 120)}")
    return out[:4]


# Assembly and rendering (pure).

def build(company: str, lead: dict[str, Any] | None, touches: list[dict[str, Any]], recordings: list[dict[str, Any]],
          transcripts: dict[str, list[dict[str, Any]]], reviews: list[dict[str, Any]],
          calendar: list[dict[str, Any]] | None = None, notes: list[dict[str, Any]] | None = None,
          candidate: str = "") -> Context:
    ctx = Context(company=company, lead=lead)
    by_id = {str(r.get("interview_id") or ""): r for r in reviews if r.get("interview_id")}
    covered: set[str] = set()
    for row in recordings:
        rid = f"{row.get('provider')}:{row.get('external_id')}"
        review = by_id.pop(rid, None)
        item, pay = recording_item(row, transcripts.get(rid), review, candidate)
        ctx.items.append(item)
        ctx.pay += pay
        covered.add(rid)
    for review in by_id.values():  # Scored interviews whose recording row is gone or unmatched.
        ctx.items.append(Item(as_dt(review.get("date")), "review", review_text(review), WEIGHT["review"]))
        covered.add(str(review.get("interview_id")))
    ctx.items += touch_items(touches, covered)
    for row in touches:
        data = row.get("data") or {}
        if row.get("kind") == "email":
            ctx.pay += [f"{day(as_dt(row.get('at')))} email, {line}"
                        for line in pay_mentions(f"{data.get('title') or ''}. {data.get('summary') or ''}")]
    seen_starts = {i.at.date() for i in ctx.items if i.at and i.kind in ("recording", "review")}
    for row in calendar or []:
        at = as_dt(row.get("start_utc"))
        if at and at.date() not in seen_starts:
            people = ", ".join(names(row.get("interviewers"))[:3])
            ctx.items.append(Item(at, "round", f"Past round (no recording): {clean(row.get('title'), 120)}"
                                               + (f" with {people}" if people else ""), WEIGHT["round"]))
    for row in notes or []:
        text = clean(row.get("summary"), 220)
        if text:
            ctx.items.append(Item(as_dt(row.get("at")), "note", f"Your note from a VALR call: {text}", WEIGHT["note"]))
            ctx.pay += [f"{day(as_dt(row.get('at')))} your note, {line}" for line in pay_mentions(text)]
    ctx.stages = stage_history(touches)
    ctx.asks = open_asks(touches)
    ctx.pay = list(dict.fromkeys(ctx.pay))
    return ctx


def rank(items: list[Item], now: datetime) -> list[Item]:
    """Most relevant first: kind weight, halved for every 60 days of age."""
    def score(item: Item) -> float:
        age = (now - item.at).days if item.at else 365
        return item.weight * 0.5 ** (max(age, 0) / 60)
    return sorted(items, key=score, reverse=True)


def header_lines(ctx: Context) -> list[str]:
    lines = []
    lead = ctx.lead or {}
    if lead:
        status = f"Lead stage: {lead.get('stage') or 'unknown'}"
        if lead.get("status") and lead.get("status") != "active":
            status += f" ({lead['status']})"
        if lead.get("next_step"):
            status += f"; next step: {clean(lead['next_step'], 120)}"
            if lead.get("next_step_at"):
                status += f" ({day(as_dt(lead['next_step_at']))})"
        lines.append(status + ".")
        contacts = who(lead.get("contacts") or [])
        if contacts:
            lines.append(f"People so far: {clean(contacts, 200)}.")
    if ctx.stages:
        lines.append("Stage history: " + " → ".join(f"{s} ({day(at)})" for at, s in ctx.stages[-6:]) + ".")
    if ctx.asks:
        lines.append("Open asks (no reply yet): " + "; ".join(ctx.asks) + ".")
    if ctx.pay:
        lines.append("Pay discussed (as said; not a confirmed budget): " + " | ".join(ctx.pay[:5]) + ".")
    return lines


def render(ctx: Context, now: datetime | None = None, budget_tokens: int = TOKEN_BUDGET) -> str:
    """The block, at most `budget_tokens` (estimated): the header, then the most relevant items
    that fit, shown newest first. "" when there is no history."""
    if ctx.empty:
        return ""
    now = now or datetime.now(timezone.utc)
    title = f"Previous interactions with {ctx.company or 'this company'} (private data, newest first):"
    lines = [title] + header_lines(ctx)
    budget = int(budget_tokens * CHARS_PER_TOKEN)
    used = sum(len(line) + 1 for line in lines)
    if used > budget:  # A very long header: keep its start.
        return "\n".join(lines)[:budget]
    chosen, dropped = [], 0
    for item in rank(ctx.items, now):
        line = f"- {day(item.at)}: {item.text}"
        if used + len(line) + 1 > budget - 60:
            dropped += 1
            continue
        chosen.append((item, line))
        used += len(line) + 1
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    lines += [line for _, line in sorted(chosen, key=lambda pair: pair[0].at or epoch, reverse=True)]
    if dropped:
        lines.append(f"({dropped} older or less relevant items left out.)")
    return "\n".join(lines)


def view(ctx: Context, now: datetime | None = None, budget_tokens: int = TOKEN_BUDGET) -> dict[str, Any]:
    """For the interview page: the same selection as `render`, as fields."""
    text = render(ctx, now, budget_tokens)
    if not text:
        return {}
    lines = text.split("\n")[1:]
    return {"company": ctx.company,
            "summary": [line for line in lines if not line.startswith("- ") and not line.startswith("(")],
            "timeline": [line[2:] for line in lines if line.startswith("- ")],
            "more": next((line for line in lines if line.startswith("(")), ""),
            "tokens": estimate_tokens(text)}


# Loading (Postgres; every source optional).

def performance_reviews(path: Path | None = None) -> list[dict[str, Any]]:
    """The scored interviews in the owner's performance files (none for a guest's workspace)."""
    from app import workspaces
    if path is None and not workspaces.is_owner():
        return []
    paths = [path] if path else []
    if not paths:
        env = os.getenv("VALR_PERFORMANCE_PATH", "").strip()
        try:
            from app.profile.config import settings
            paths.append(settings().performance_path)
        except Exception:  # noqa: BLE001 - the web image may lack the profile settings.
            pass
        if env:
            paths.append(Path(env).expanduser())
    for source in paths:
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        return [i for i in data.get("interviews") or [] if isinstance(i, dict)]
    return []


async def _rows(connection: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        cursor = await connection.execute(sql, params)
        columns = [c.name if hasattr(c, "name") else c[0] for c in cursor.description or []]
        return [dict(zip(columns, row)) for row in await cursor.fetchall()]
    except Exception as error:  # noqa: BLE001 - a missing table means no data from that source.
        logger.info("VALR company context: a source is unavailable (%s)", type(error).__name__)
        try:
            await connection.rollback()
        except Exception:  # noqa: BLE001 - autocommit connections have nothing to roll back.
            pass
        return []


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "null")
    except (TypeError, ValueError):
        return None


async def load_transcript(connection: Any, provider: str, external_id: str) -> list[dict[str, Any]]:
    try:
        from app.notetakers import transcripts
        data = await transcripts.load(connection, provider, external_id)
    except Exception:  # noqa: BLE001
        data = None
    if data is None and provider == "phone":  # Kept by VALR itself until the transcript copy exists.
        try:
            from app import phone_recordings
            data = await phone_recordings.sealed_meeting(connection, external_id)
        except Exception:  # noqa: BLE001
            data = None
    return list((data or {}).get("turns") or [])


async def gather(connection: Any, company: str, domain: str = "", emails: Iterable[str] = (),
                 before: datetime | None = None, exclude_event: str = "", candidate: str = "",
                 reviews: list[dict[str, Any]] | None = None) -> Context:
    """Everything stored about this company (any provider), before `before` (default now)."""
    from app.notetakers.base import email_hash
    domain = registrable(domain)
    emails = {e.strip().lower() for e in emails if e and "@" in e}
    company = company_names.display(company) if company else (company_names.from_domain(domain) if domain else "")
    before = before or datetime.now(timezone.utc)
    leads = [r for r in await _rows(connection, "SELECT id, company, domain, stage, status, next_step, next_step_at, "
                                                "contacts, last_touch_at FROM leads")
             if lead_matches({**r, "contacts": _json(r.get("contacts")) or []}, company, domain, emails)]
    for lead in leads:
        lead["contacts"] = _json(lead.get("contacts")) or []
    leads.sort(key=lambda r: as_dt(r.get("last_touch_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    lead = leads[0] if leads else None
    if lead and not company:
        company = lead.get("company") or ""
    touches = []
    if leads:
        touches = await _rows(connection, "SELECT id, kind, at, data FROM lead_touchpoints WHERE lead_id = ANY(%s) "
                                          "AND at < %s ORDER BY at", ([r["id"] for r in leads], before))
        for row in touches:
            row["data"] = _json(row.get("data")) or {}
    hashes = {email_hash(e) for e in emails}
    rows = await _rows(connection,
                       "SELECT r.provider, r.external_id, r.title, r.started_at, r.event_key, r.event_title, "
                       "r.event_company, r.company, r.company_domain, r.interviewers, r.invitee_hashes, r.eval_status, "
                       "p.status AS report_status FROM notetaker_recordings r LEFT JOIN post_call_reports p "
                       "ON p.provider = r.provider AND p.external_id = r.external_id "
                       "WHERE (r.started_at IS NULL OR r.started_at < %s) "
                       "AND NOT EXISTS (SELECT 1 FROM notetaker_hidden h WHERE h.provider = r.provider "
                       "AND h.external_id = r.external_id) ORDER BY r.started_at DESC NULLS LAST", (before,))
    if not rows:  # Older databases: no post_call_reports table yet.
        rows = await _rows(connection,
                           "SELECT provider, external_id, title, started_at, event_key, event_title, event_company, "
                           "company, company_domain, interviewers, invitee_hashes, eval_status FROM notetaker_recordings "
                           "WHERE (started_at IS NULL OR started_at < %s) ORDER BY started_at DESC NULLS LAST", (before,))
    recorded_on_lead = {t["id"] for t in touches if t.get("kind") == "recording"}
    recordings = [r for r in rows if (r.get("event_key") or "") != exclude_event or not exclude_event]
    recordings = [r for r in recordings if recording_matches(r, company, domain, hashes)
                  or f"{r['provider']}:{r['external_id']}" in recorded_on_lead]
    for row in recordings:
        row["not_interview"] = row.get("report_status") == "not_interview"
    transcripts = {}
    for row in recordings[:MAX_RECORDINGS]:
        turns = await load_transcript(connection, row["provider"], row["external_id"])
        if turns:
            transcripts[f"{row['provider']}:{row['external_id']}"] = turns
    reviews = performance_reviews() if reviews is None else reviews
    rec_ids = {f"{r['provider']}:{r['external_id']}" for r in recordings}
    perf_on_lead = {t["id"].removeprefix("perf:") for t in touches if t.get("kind") == "evaluation"}
    reviews = [r for r in reviews
               if (as_dt(r.get("date")) or before) <= before
               and (str(r.get("interview_id")) in rec_ids or str(r.get("interview_id")) in perf_on_lead
                    or same(str(r.get("company") or ""), company))]
    calendar = [r for r in await _rows(connection, "SELECT event_key, title, company, start_utc, interviewers "
                                                   "FROM calendar_history WHERE start_utc < %s", (before,))
                if r.get("event_key") != exclude_event and same(r.get("company") or "", company)]
    notes = []
    if company and len(company_names.key(company)) >= 3:
        pattern = r"(?<!\w)" + re.escape(company_names.key(company).split()[0]) + r"(?!\w)"
        for row in await _rows(connection, "SELECT ended_at AS at, summary FROM call_summaries "
                                           "WHERE summary IS NOT NULL AND ended_at < %s ORDER BY ended_at DESC LIMIT 200",
                               (before,)):
            if re.search(pattern, str(row.get("summary") or ""), re.I) and len(notes) < 5:
                notes.append(row)
    return build(company, lead, touches, recordings, transcripts, reviews, calendar, notes, candidate)


async def for_event_context(connection: Any, event: Any, owners: Iterable[str] = (), candidate: str = "") -> Context:
    """The context for an upcoming interview (app/interview_types.SlotEvent): its company, domain
    and outside attendees' emails; the event itself and anything after it excluded."""
    owners = {o.lower() for o in owners}
    emails = [p.email for p in getattr(event, "attendees", []) or [] if p.email and p.email.lower() not in owners]
    if getattr(event, "organizer_email", "") and event.organizer_email.lower() not in owners:
        emails.append(event.organizer_email)
    return await gather(connection, event.company or "", event.company_domain or "", emails,
                        before=as_dt(event.start_utc), exclude_event=event.key, candidate=candidate)


async def for_event(connection: Any, event: Any, owners: Iterable[str] = (), candidate: str = "",
                    budget_tokens: int = TOKEN_BUDGET) -> str:
    """`for_event_context`, rendered."""
    return render(await for_event_context(connection, event, owners, candidate), budget_tokens=budget_tokens)
