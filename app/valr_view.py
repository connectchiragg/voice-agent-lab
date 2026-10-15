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
DIMENSION_LABELS = {
    "technical_depth": "Technical depth", "problem_solving": "Problem solving",
    "communication_clarity": "Clarity", "structure": "Structure", "confidence": "Confidence",
    "handling_pushback": "Pushback", "compensation_handling": "Comp talk",
}


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


def researchers(parts: dict[str, Any], company: dict[str, Any] | None) -> dict[str, Any]:
    """R1 private context, R2 interview intel (round + interviewers), R3 company and pay."""
    r2 = findings(parts.get("round")) or {"ok": False, "summary": "", "claims": []}
    for batch in (parts.get("interviewers_by_email") or {}).values():
        extra = findings(batch)
        if extra:
            r2 = {"ok": r2["ok"] or extra["ok"],
                  "summary": " ".join(s for s in (r2["summary"], extra["summary"]) if s),
                  "claims": r2["claims"] + extra["claims"]}
    return {"r1": findings(parts.get("private")),
            "r2": r2 if (r2["summary"] or r2["claims"]) else None,
            "r3": findings(parts.get("company") or company)}


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


def performance(scores: dict[str, Any] | None, evaluation: dict[str, Any] | None) -> dict[str, Any]:
    rows = []
    for row in (scores or {}).get("rows") or []:
        if not isinstance(row, dict) or row.get("score") is None or not row.get("date"):
            continue
        rows.append({"date": str(row["date"])[:10], "dimension": row.get("dimension"),
                     "score": row["score"], "company": scrub(row.get("company") or ""),
                     "round": scrub(row.get("round_type") or "")})
    trends = (scores or {}).get("trends") or (evaluation or {}).get("trends") or {}
    dims = [d for d in DIMENSION_LABELS if any(r["dimension"] == d for r in rows)]
    summary = {d: {"label": DIMENSION_LABELS[d],
                   "direction": (trends.get(d) or {}).get("direction", "insufficient data"),
                   "change": (trends.get(d) or {}).get("fitted_change"),
                   "n": (trends.get(d) or {}).get("n", 0),
                   "last": ((trends.get(d) or {}).get("last") or {}).get("score")} for d in dims}
    raw = (evaluation or {}).get("brief") or {}
    highlights = {key: [scrub(x) for x in raw.get(key) or []]
                  for key in ("top_strengths", "recurring_weak_topics", "prep_checklist")}
    for key in ("communication", "negotiation_notes", "trend"):
        highlights[key] = scrub(raw.get(key) or "")
    return {"rows": rows, "dimensions": summary, "highlights": highlights,
            "generated_at": (evaluation or {}).get("generated_at"),
            "interviews": len({(r["date"], r["company"]) for r in rows}),
            "available": bool(rows or raw)}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def profile_files() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from app.profile.config import settings
    cfg = settings()
    return read_json(cfg.scores_path.with_suffix(".json")), read_json(cfg.evaluation_path)


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
                "researchers": researchers(parts, companies.get(ckey) if ckey else None),
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
    scores, evaluation = profile_files()
    view["performance"] = performance(scores, evaluation)
    return view
