"""The VALR cockpit's data: one JSON-able dict for the dashboard page and /valr/api/state.

Read-only. Everything comes from the stores the workers already write (Postgres:
calendar snapshots, slot context, briefing blocks, research jobs and spend, OAuth and
calendar state, the pause flag; Temporal: today's dial counts; the private data dir:
the self-evaluation and the score table). Each source fails on its own: a missing
one becomes an "unavailable" section, never a 500.

Privacy: attendee emails and phone numbers never leave this module. Interviewers are
shown by name (or "Attendee n"), slots by an opaque reference (the slot workflow's
hashed id), and every free text shown is passed through `scrub`, which hides
anything shaped like an email address or a phone number. Comp figures in the
research notes are shown as stored: the page is owner-only behind Access.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import calendar_sync, google_oauth, interview_store, interviews, research_jobs
from app.interview_types import (TOOL_DOMAINS, SlotEvent, attendee_role, group_blocks, people,
                                 slot_workflow_id)
from app.interview_workflow import CALL_LEADS, RESEARCH_LEAD

logger = logging.getLogger("uvicorn.error")
LOOK_BACK = timedelta(hours=12)
LOOK_AHEAD = timedelta(days=8)
RESEARCHING = {"booked", "running"}
READY = {"done", "partial", "apologized"}
FINAL_BLOCK = {"briefed", "unanswered", "missed", "refused", "cancelled", "paused"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(\d{3}\)\s?\d{3}[-.\s]\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
                   r"|\b\d{5}\s?\d{5}\b")


def scrub(text: object) -> str:
    """Free text for display: emails and phone numbers hidden."""
    value = "" if text is None else str(text)
    return PHONE.sub("[phone hidden]", EMAIL.sub("[email hidden]", value))


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# Pure builders (unit-tested without a database).

def interviewer_rows(event: SlotEvent, researched: set[str], research_ran: bool,
                     owners: set[str]) -> list[dict[str, str]]:
    """Who is on the invite and what VALR did about them, by name only.

    role: interviewer (researched / not an interviewer per the classifier / pending),
    hr (skipped by the recruiting/HR guard). Owner, rooms and tools are left out.
    """
    rows: list[dict[str, str]] = []
    candidates = {p.email.lower() for p in people(event, owners)}
    everyone = list(event.attendees)
    if event.organizer_email and all(p.email.lower() != event.organizer_email.lower()
                                     for p in everyone):
        from app.interview_types import Person
        everyone.append(Person(event.organizer_email, event.organizer_name, organizer=True))
    seen: set[str] = set()
    for person in everyone:
        email = person.email.lower()
        if email in seen:
            continue
        seen.add(email)
        role = attendee_role(person, owners)
        if role == "tool" and not is_tool(email):
            role = "recruiter"  # talent@, hr@, careers@ and the like: a person or team in HR.
        if role in ("owner", "resource", "tool"):
            continue
        name = scrub(person.name).strip() or f"Attendee {len(rows) + 1}"
        if person.response == "declined" and not person.organizer:
            status = "declined"
        elif role == "recruiter":
            status = "skipped_hr"
        elif email in researched:
            status = "researched"
        elif email in candidates and research_ran:
            status = "skipped_classifier"
        else:
            status = "pending"
        rows.append({"name": name, "status": status})
    return rows


def is_tool(email: str) -> bool:
    local, _, domain = email.rpartition("@")
    return (domain in TOOL_DOMAINS or any(domain.endswith("." + t) for t in TOOL_DOMAINS)
            or bool(re.search(r"no-?reply|do-?not-?reply|notification|calendar|scheduling", local)))


def findings(part: Any) -> dict[str, Any] | None:
    """A researcher's stored part -> {ok, summary, claims: [{claim, url}]} (scrubbed)."""
    if not isinstance(part, dict):
        return None
    found = part.get("findings") or {}
    claims = []
    for claim in (found.get("claims") or []) if isinstance(found, dict) else []:
        if not isinstance(claim, dict):
            continue
        url = str(claim.get("source_url") or "")
        claims.append({"claim": scrub(claim.get("claim")),
                       "url": url if url.startswith(("https://", "http://")) else ""})
    summary = scrub(found.get("summary")) if isinstance(found, dict) else ""
    return {"ok": bool(part.get("ok")), "summary": summary, "claims": claims}


PAY = re.compile(r"\bLPA\b|\blakhs?\b|\bcrores?\b|\bCTC\b|\bbase\b|salar|compensation|\bpay\b|"
                 r"pay band|\bbonus|\bequity\b|\bESOPs?\b|\bRSUs?\b|\bOTE\b", re.I)


def merged(items: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    found = [i for i in items if i]
    if not found:
        return None
    return {"ok": any(i["ok"] for i in found),
            "summary": " ".join(i["summary"] for i in found if i["summary"]),
            "claims": [c for i in found for c in i["claims"]]}


def strings(values: Any, limit: int) -> list[str]:
    return [scrub(v) for v in (values or []) if isinstance(v, str) and v.strip()][:limit]


def interview_detail(parts: dict[str, Any], company: dict[str, Any] | None) -> dict[str, Any]:
    """What one interview's detail view shows, in the owner's terms.

    - from_you: the private researcher (his emails, past recordings, pushed notes);
    - round: what this round is like; people: what was found about the interviewers;
    - company: the company researcher minus pay lines, which go to `pay`;
    - likely_questions / prep: read from the round findings when the researcher
      provides them (`likely_questions`, `prep` lists), else empty;
    - pay: a structured `pay_range` {min, max, unit, currency, source_url} from the
      company findings when present, and the pay-related claims.
    """
    company_part = parts.get("company") or company
    found_company = findings(company_part)
    pay_claims = [c for c in (found_company or {}).get("claims", []) if PAY.search(c["claim"])]
    if found_company:
        found_company = {**found_company, "claims": [c for c in found_company["claims"]
                                                     if c not in pay_claims]}
    raw_round = ((parts.get("round") or {}).get("findings") or {}) if isinstance(parts.get("round"), dict) else {}
    raw_company = ((company_part or {}).get("findings") or {}) if isinstance(company_part, dict) else {}
    pay_range = raw_company.get("pay_range") if isinstance(raw_company.get("pay_range"), dict) else None
    if pay_range:
        url = str(pay_range.get("source_url") or "")
        pay_range = {"min": pay_range.get("min"), "max": pay_range.get("max"),
                     "unit": scrub(pay_range.get("unit") or ""), "currency": scrub(pay_range.get("currency") or ""),
                     "source_url": url if url.startswith(("https://", "http://")) else ""}
    return {"from_you": findings(parts.get("private")),
            "round": findings(parts.get("round")),
            "people": merged([findings(b) for b in (parts.get("interviewers_by_email") or {}).values()]),
            "company": found_company,
            "likely_questions": strings(raw_round.get("likely_questions"), 8),
            "prep": strings(raw_round.get("prep"), 6),
            "pay": {"range": pay_range, "claims": pay_claims}}


def brief(slot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not slot or not (slot.get("spoken") or slot.get("notes")):
        return None
    urls = {s.get("id"): s.get("url", "") for s in slot.get("sources") or [] if isinstance(s, dict)}
    notes = []
    for note in slot.get("notes") or []:
        if not isinstance(note, dict):
            continue
        links = [urls.get(sid, "") for sid in note.get("source_ids", [])]
        notes.append({"fact": scrub(note.get("fact")), "sensitive": bool(note.get("sensitive")),
                      "private": bool(note.get("private")),
                      "sources": [u for u in links if u.startswith(("https://", "http://"))]})
    return {"spoken": scrub(slot.get("spoken")), "notes": notes, "quality": slot.get("status")}


def slot_status(event: SlotEvent, slot: dict[str, Any] | None, latest_job: dict[str, Any] | None,
                block_state: str, now: datetime) -> str:
    """scheduled | researching | ready | calling | done | cancelled."""
    if event.status == "cancelled":
        return "cancelled"
    if block_state == "calling":
        return "calling"
    if block_state == "briefed" or parse(event.end_utc) < now:
        return "done"
    if latest_job and latest_job["status"] in RESEARCHING:
        return "researching"
    if slot and slot.get("status") in READY:
        return "ready"
    return "scheduled"


def cache_hits(jobs: list[dict[str, Any]]) -> list[str]:
    hits: list[str] = []
    for job in jobs:
        if job["status"] == "cached":
            hits.append("whole slot")
        for name in re.findall(r"cache: ([\w, ]+)", job.get("detail") or ""):
            hits += [n.strip() for n in name.split(",") if n.strip() and n.strip() != "slot"]
    return sorted(set(hits))


def next_call(blocks: list[dict[str, Any]], now: datetime, paused: bool) -> dict[str, Any] | None:
    """The next automatic briefing dial (T-15, else the next of T-10/T-5 still ahead)."""
    for block in blocks:
        if block["state"] in FINAL_BLOCK or paused:
            continue
        start = parse(block["start"])
        for lead in CALL_LEADS:
            due = start - lead
            if due > now:
                return {"block": block["id"], "at": iso(due), "label": block["label"],
                        "attempt": CALL_LEADS.index(lead) + 1}
    return None


V2_DIMENSIONS = {  # key -> label, for the 1-5 rubric adapted to the 1-10 shape.
    "technical_depth": "Technical depth", "problem_solving": "Problem solving",
    "communication_clarity": "Clarity", "structure": "Structure", "confidence": "Confidence",
    "handling_pushback": "Handling pushback", "compensation_handling": "Pay conversations",
}
MA_WINDOW = 3


def moving_average(values: list[float | None], window: int = MA_WINDOW) -> list[float | None]:
    out: list[float | None] = []
    for index in range(len(values)):
        recent = [v for v in values[max(0, index - window + 1):index + 1] if v is not None]
        out.append(round(sum(recent) / len(recent), 2) if recent and values[index] is not None else None)
    return out


def direction(series: list[float | None]) -> tuple[str, float | None]:
    """improving / flat / declining from the first to the last moving-average point (1-10)."""
    points = [v for v in series if v is not None]
    if len(points) < 2:
        return "insufficient data", None
    change = round(points[-1] - points[0], 1)
    return ("improving" if change >= 1 else "declining" if change <= -1 else "flat"), change


def scorecards_from_rubric(scores: dict[str, Any] | None,
                           evaluation: dict[str, Any] | None) -> dict[str, Any]:
    """Today's files (1-5 rows per dimension, evaluation.json) in the scorecard shape."""
    cards: dict[tuple[str, str], dict[str, Any]] = {}
    for row in (scores or {}).get("rows") or []:
        if not isinstance(row, dict) or row.get("score") is None or not row.get("date"):
            continue
        key = (str(row["date"])[:10], str(row.get("company") or ""))
        card = cards.setdefault(key, {"id": row.get("interview_id") or "|".join(key), "date": key[0],
                                      "company": row.get("company") or "", "round": row.get("round_type") or "",
                                      "scores": {}, "questions": [], "badges": []})
        card["scores"][row.get("dimension")] = float(row["score"]) * 2
    by_id = {}
    for analysis in (evaluation or {}).get("interviews") or []:
        if isinstance(analysis, dict):
            by_id[((analysis.get("date") or "")[:10], analysis.get("company") or "")] = analysis
    for key, card in cards.items():
        analysis = by_id.get(key) or {}
        for answer, good in ([(a, False) for a in analysis.get("weak_answers") or []]
                             + [(a, True) for a in analysis.get("strong_answers") or []]):
            if isinstance(answer, dict):
                card["questions"].append({"question": answer.get("topic") or "", "score": None,
                                          "feedback": answer.get("why") or "", "quote": answer.get("quote") or "",
                                          "strong": good})
    brief_ = (evaluation or {}).get("brief") or {}
    return {"dimensions": [{"key": k, "label": v, "subs": []} for k, v in V2_DIMENSIONS.items()],
            "interviews": sorted(cards.values(), key=lambda c: c["date"]),
            "focus_this_week": [{"action": a, "why": ""} for a in (brief_.get("prep_checklist") or [])[:3]],
            "badges": [], "scale": 10}


def performance(scores: dict[str, Any] | None, evaluation: dict[str, Any] | None,
                cards: dict[str, Any] | None = None) -> dict[str, Any]:
    """The performance view: per-interview scorecards (1-10), moving-average trends per
    dimension, per-question feedback, badges, most improved / biggest risk, focus this week.

    `cards` is the profile agent's scorecard file when present (same shape as
    `scorecards_from_rubric` returns); else today's 1-5 files are adapted (x2).
    """
    source = cards if isinstance(cards, dict) and cards.get("interviews") else \
        scorecards_from_rubric(scores, evaluation)
    dims = [{"key": str(d.get("key")), "label": scrub(d.get("label") or d.get("key")),
             "subs": [{"key": str(x.get("key")), "label": scrub(x.get("label") or x.get("key"))}
                      for x in d.get("subs") or [] if isinstance(x, dict)]}
            for d in source.get("dimensions") or [] if isinstance(d, dict)]
    interviews_ = []
    for card in sorted((c for c in source.get("interviews") or [] if isinstance(c, dict)),
                       key=lambda c: str(c.get("date") or "")):
        values = {k: float(v) for k, v in (card.get("scores") or {}).items() if isinstance(v, (int, float))}
        overall = card.get("overall")
        if not isinstance(overall, (int, float)):
            overall = round(sum(values.values()) / len(values), 1) if values else None
        interviews_.append({
            "id": scrub(card.get("id")), "date": str(card.get("date") or "")[:10],
            "company": scrub(card.get("company")), "round": scrub(card.get("round")),
            "overall": overall, "scores": values,
            "sub_scores": {k: float(v) for k, v in (card.get("sub_scores") or {}).items()
                           if isinstance(v, (int, float))},
            "badges": strings(card.get("badges"), 4),
            "questions": [{"question": scrub(q.get("question")), "score": q.get("score"),
                           "feedback": scrub(q.get("feedback")), "quote": scrub(q.get("quote") or ""),
                           "better": scrub(q.get("better") or ""), "strong": bool(q.get("strong"))}
                          for q in card.get("questions") or [] if isinstance(q, dict)][:12],
        })
    present = [d for d in dims if any(d["key"] in i["scores"] for i in interviews_)]
    trends = {}
    for d in present:
        raw = [i["scores"].get(d["key"]) for i in interviews_]
        ma = moving_average(raw)
        trend, change = direction(ma)
        trends[d["key"]] = {"raw": raw, "ma": ma, "direction": trend, "change": change,
                            "last": next((v for v in reversed(raw) if v is not None), None)}
    overall_raw = [i["overall"] for i in interviews_]
    overall_ma = moving_average(overall_raw)
    ranked = sorted(((t["change"], k) for k, t in trends.items() if t["change"] is not None))
    label = {d["key"]: d["label"] for d in dims}
    improved = source.get("most_improved") if isinstance(source.get("most_improved"), dict) else (
        {"dimension": label[ranked[-1][1]], "change": ranked[-1][0]} if ranked and ranked[-1][0] > 0 else None)
    risk = source.get("biggest_risk") if isinstance(source.get("biggest_risk"), dict) else (
        {"dimension": label[ranked[0][1]], "change": ranked[0][0],
         "why": f"down {abs(ranked[0][0]):.1f} points on the 3-interview average"}
        if ranked and ranked[0][0] < 0 else None)
    raw_brief = (evaluation or {}).get("brief") or {}
    highlights = {key: strings(raw_brief.get(key), 8)
                  for key in ("top_strengths", "recurring_weak_topics", "prep_checklist")}
    for key in ("communication", "negotiation_notes", "trend"):
        highlights[key] = scrub(raw_brief.get(key) or "")
    return {
        "dimensions": present, "interviews": interviews_, "trends": trends,
        "overall": {"raw": overall_raw, "ma": overall_ma, "direction": direction(overall_ma)[0]},
        "most_improved": {k: scrub(v) if isinstance(v, str) else v for k, v in improved.items()} if improved else None,
        "biggest_risk": {k: scrub(v) if isinstance(v, str) else v for k, v in risk.items()} if risk else None,
        "badges": [{"label": scrub(b.get("label")), "detail": scrub(b.get("detail") or "")}
                   for b in source.get("badges") or [] if isinstance(b, dict)][:6],
        "focus_this_week": [{"action": scrub(f.get("action")), "why": scrub(f.get("why") or "")}
                            for f in source.get("focus_this_week") or [] if isinstance(f, dict)][:3],
        "highlights": highlights,
        "generated_at": source.get("generated_at") or (evaluation or {}).get("generated_at"),
        "available": bool(interviews_ or raw_brief),
    }


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def profile_files() -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None,
                             dict[str, Any] | None]:
    """(scores, evaluation, scorecards, pay target) from the private data dir; None if missing."""
    from app.profile.config import settings, _path
    cfg = settings()
    cards = read_json(_path("VALR_SCORECARDS_PATH", "data/interview_scorecards.json"))
    profile = read_json(cfg.profile_path) or {}
    target = ((profile.get("compensation") or {}).get("target_lpa")
              if isinstance(profile.get("compensation"), dict) else None)
    target = target if isinstance(target, dict) and target.get("max") else None
    return read_json(cfg.scores_path.with_suffix(".json")), read_json(cfg.evaluation_path), cards, \
        ({"min": target.get("min"), "max": target.get("max"), "unit": "LPA", "currency": "INR"}
         if target else None)


def calendar_view(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    expires = state.get("channel_expires")
    push_on = os.getenv("VALR_GCAL_PUSH", "1") == "1"
    if not push_on:
        channel = "off"
    elif not state.get("channel_id") or expires is None or expires <= now:
        channel = "none"
    elif calendar_sync.needs_renewal(state, now):
        channel = "renewing"
    else:
        channel = "active"
    return {"source": state.get("source") or "", "channel": channel, "expires": iso(expires),
            "last_sync_at": iso(state.get("last_sync_at")),
            "last_full_sync_at": iso(state.get("last_full_sync_at")),
            "last_error": scrub(state.get("last_error") or "")[:200]}


def limits_view(limits: research_jobs.Limits) -> dict[str, Any]:
    return {"month_usd": limits.month_usd, "jobs_per_day": limits.jobs_per_day,
            "job_usd": limits.job_usd, "jobs_per_caller_day": limits.jobs_per_caller_day}


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    urls = {s.get("id"): s.get("url", "") for s in job.get("sources") or [] if isinstance(s, dict)}
    return {"topic": scrub(job.get("topic")), "caller": job.get("caller"), "status": job.get("status"),
            "kind": job.get("kind") or "topic", "created_at": iso(job.get("created_at")),
            "duration_s": job.get("duration_s"), "spend_usd": float(job.get("spend_usd") or 0),
            "promised_minutes": job.get("promised_minutes"), "spoken": scrub(job.get("spoken") or ""),
            "detail": scrub(job.get("detail") or ""),
            "cap_hits": dict(sorted((job.get("cap_hits") or {}).items())),
            "notes": [{"fact": scrub(n.get("fact")), "sensitive": bool(n.get("sensitive")),
                       "sources": [u for u in (urls.get(s, s) for s in n.get("source_ids", []))
                                   if str(u).startswith(("https://", "http://"))]}
                      for n in job.get("notes") or [] if isinstance(n, dict)]}


def valr_number() -> str:
    """VALR's own number (the one it calls from and answers on), for "Call VALR now"."""
    return os.getenv("VALR_PUBLIC_NUMBER") or os.getenv("TELNYX_FROM_NUMBER") or "+12288888007"


# Collection.

async def dial_counts(now: datetime) -> dict[str, Any]:
    """Today's (UTC) dials per budget from the DialBudgetWorkflows; None when unknown."""
    from app import callbacks
    from app.callback_workflow import MAX_DIALS_PER_DAY, DialBudgetWorkflow
    budgets = {"briefing": int(os.getenv("VALR_BRIEFING_DIALS_PER_DAY", 12)),
               "callback": MAX_DIALS_PER_DAY}
    out: dict[str, Any] = {name: {"used": None, "limit": limit} for name, limit in budgets.items()}
    client = await callbacks.temporal()
    if client is None:
        return out
    for name in budgets:
        handle = client.get_workflow_handle(f"{name}-dials-{now.date().isoformat()}")
        try:
            out[name]["used"] = await asyncio.wait_for(handle.query(DialBudgetWorkflow.dials), 2)
        except Exception as error:  # noqa: BLE001 - no workflow yet today: nothing dialled.
            out[name]["used"] = 0 if "not found" in str(error).lower() else None
    return out


def build_blocks(events: list[SlotEvent], recorded: dict[str, dict[str, Any]],
                 slots: dict[str, dict[str, Any]], jobs: dict[str, list[dict[str, Any]]],
                 companies: dict[str, Any], owners: set[str], now: datetime,
                 paused: bool) -> list[dict[str, Any]]:
    """Blocks of back-to-back interviews, each slot with status, brief and researchers.

    `recorded`: briefing_blocks rows by block id; `slots`: slot_context rows by event key;
    `jobs`: research_jobs rows per event key, newest first; `companies`: R3 cache by key.
    """
    from app.interview_worker import interviewed
    by_key = {e.key: e for e in events}
    blocks = []
    for plan in group_blocks(events):
        record = recorded.get(plan.block_id) or {}
        state = record.get("state") or "scheduled"
        rows = []
        for key in plan.slot_keys:
            event, slot = by_key[key], slots.get(key)
            slot_jobs = jobs.get(key, [])
            parts = (slot or {}).get("parts") or {}
            ckey = (slot or {}).get("company_key")
            ran = bool(slot and "interviewers_by_email" in parts and slot.get("status") in READY)
            rows.append({
                "ref": slot_workflow_id(key), "title": scrub(event.title),
                "company": scrub(event.company), "start": iso(parse(event.start_utc)),
                "end": iso(parse(event.end_utc)),
                "status": slot_status(event, slot, slot_jobs[0] if slot_jobs else None, state, now),
                "research_due": iso(parse(plan.start_utc) - RESEARCH_LEAD),
                "updated_at": iso((slot or {}).get("updated_at")),
                "runs": len([j for j in slot_jobs if j["status"] != "cached"]),
                "spend_usd": round(sum(float(j["spend_usd"] or 0) for j in slot_jobs), 4),
                "cache_hits": cache_hits(slot_jobs),
                "interviewers": interviewer_rows(event, interviewed(parts), ran, owners),
                "brief": brief(slot),
                "detail": interview_detail(parts, companies.get(ckey) if ckey else None),
            })
        label = scrub(record.get("label") or "") or ", ".join(
            r["company"] or r["title"] for r in rows)[:120]
        blocks.append({"id": plan.block_id, "start": iso(parse(plan.start_utc)),
                       "end": iso(parse(plan.end_utc)), "label": label,
                       "state": state, "call_at": iso(parse(plan.start_utc) - CALL_LEADS[0]),
                       "paused": paused, "slots": rows})
    return blocks


async def timeline(connection: Any, now: datetime, paused: bool) -> list[dict[str, Any]]:
    from app.interview_worker import owner_emails
    await calendar_sync.setup(connection)
    await interview_store.setup(connection)
    events = [e for e in await calendar_sync.upcoming(connection, now - LOOK_BACK, now + LOOK_AHEAD)
              if not e.all_day and not e.declined]
    recorded = {b["block_id"]: b for b in await interview_store.blocks_between(
        connection, now - LOOK_BACK - timedelta(hours=1), now + LOOK_AHEAD)}
    keys = [e.key for e in events]
    slots = {s["key"]: s for s in await interview_store.load_slots(connection, keys)}
    jobs: dict[str, list[dict[str, Any]]] = {}
    for job in await interview_store.slot_jobs(connection, keys):
        jobs.setdefault(job["event_key"], []).append(job)
    companies = {}
    for ckey in {s.get("company_key") for s in slots.values()} - {None, ""}:
        companies[ckey] = await interview_store.company(connection, ckey)
    return build_blocks(events, recorded, slots, jobs, companies, owner_emails(), now, paused)


async def collect(pool: Any, now: datetime | None = None) -> dict[str, Any]:
    """The whole cockpit view. `pool`: the app's psycopg pool (research.pool)."""
    now = now or datetime.now(timezone.utc)
    view: dict[str, Any] = {"generated_at": iso(now), "zone": str(interviews.owner_zone()),
                            "errors": []}
    async with pool.connection() as connection:
        await research_jobs.setup(connection)
        overview = await research_jobs.overview(connection, research_jobs.Limits.from_env())
        view["spend"] = {key: overview[key] for key in ("today_usd", "month_usd", "today_jobs",
                                                         "month_jobs", "today_cached", "promise_minutes")}
        view["spend"]["limits"] = limits_view(overview["limits"])
        view["spend"]["interview_limits"] = limits_view(research_jobs.Limits.interview_from_env())
        view["jobs"] = [job_view(job) for job in overview["jobs"]]
        try:
            await interview_store.setup(connection)
            paused = await interview_store.flag(connection, interview_store.DIALS_PAUSED)
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR pause flag unavailable: %s", type(error).__name__)
            paused, view["errors"] = {"enabled": False, "updated_at": None}, view["errors"] + ["pause"]
        view["dials_paused"] = {"enabled": paused["enabled"], "updated_at": iso(paused["updated_at"])}
        view["dial_env"] = os.getenv("VALR_BRIEFING_DIAL", "1") == "1"
        try:
            cursor = await connection.execute(
                "SELECT count(*) FROM briefing_blocks WHERE state = 'briefed'")
            view["_briefed_ever"] = bool((await cursor.fetchone() or [0])[0])
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR briefing history unavailable: %s", type(error).__name__)
        try:
            view["blocks"] = await timeline(connection, now, paused["enabled"])
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR timeline unavailable: %s", type(error).__name__)
            view["blocks"], view["errors"] = [], view["errors"] + ["timeline"]
        try:
            await google_oauth.setup(connection)
            google = await google_oauth.status(connection)
        except Exception as error:  # noqa: BLE001
            logger.warning("Google status unavailable: %s", type(error).__name__)
            google = {"state": "unknown"}
        try:
            await calendar_sync.setup(connection)
            calendar = calendar_view(await calendar_sync.load_state(connection), now)
        except Exception as error:  # noqa: BLE001
            logger.warning("Calendar state unavailable: %s", type(error).__name__)
            calendar = {"channel": "unknown"}
    view["next_call"] = next_call(view["blocks"], now, paused["enabled"] or not view["dial_env"])
    view["connections"] = {
        "google": {"state": google.get("state", "unknown"), "account": google.get("account") or "",
                   "age_days": google.get("age_days"), "expires_soon": bool(google.get("expires_soon")),
                   "last_ok_at": iso(google.get("last_ok_at")),
                   "configured": google_oauth.OAuthSettings.from_env() is not None},
        "fathom": {"configured": bool(os.getenv("FATHOM_API_KEY", "").strip())},
        "calendar": calendar,
    }
    view["dials"] = await dial_counts(now)
    scores, evaluation, cards, target = profile_files()
    view["performance"] = performance(scores, evaluation, cards)
    view["pay_target"] = target
    view["setup"] = {"phone_on_file": bool(os.getenv("VALR_OWNER_PHONE", "").strip()),
                     "test_call_done": view.pop("_briefed_ever", False),
                     "valr_number": valr_number()}
    return view
