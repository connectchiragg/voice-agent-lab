"""The VALR cockpit's data: one JSON-able dict for the dashboard page and /valr/api/state.

Read-only. Everything comes from the stores the workers already write (Postgres:
calendar snapshots, slot context, briefing blocks, research jobs and spend, OAuth and
calendar state, the pause flag; Temporal: today's dial counts; the private data dir:
the self-evaluation and the score table). Each source fails on its own: a missing
one becomes an "unavailable" section, never a 500.

Privacy: the page is owner-only (Access + JWT) and the events are the owner's, so the
interview detail lists each attendee's name (or "Attendee n") and email address. Phone
numbers never leave this module: slots go by an opaque reference (the slot workflow's
hashed id), and every free text shown (titles, research notes) is passed through
`scrub`, which hides anything shaped like a phone number or an email address. Comp
figures in the research notes are shown as stored; none of this is logged.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import (calendar_sync, google_oauth, interview_store, interviews, research_jobs,
                 telegram_api)
from app.valr_ui import contracts
from app.interview_types import (TOOL_DOMAINS, SlotEvent, attendee_role, group_blocks, people,
                                 slot_workflow_id)
from app.interview_workflow import MIN_LEFT_TO_CALL, RESEARCH_LEAD, SINGLE_CALL_LEAD

logger = logging.getLogger("uvicorn.error")
LOOK_BACK = timedelta(hours=12)
LOOK_AHEAD = timedelta(days=8)
RESEARCHING = {"booked", "running"}
READY = {"done", "partial", "apologized"}
FINAL_BLOCK = {"briefed", "unanswered", "missed", "refused", "cancelled", "paused", "heads_up_sent",
               "heads_up_failed"}
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
    """Who is on the invite and what VALR did about them: name and email address.

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
        name = scrub(person.name).strip() or name_from_email(email) or f"Attendee {len(rows) + 1}"
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
        rows.append({"name": name, "email": email, "status": status})
    return rows


def name_from_email(email: str) -> str:
    """"maya.lin@x.example" -> "Maya Lin" when the invite has no display name; otherwise the
    local part as written ("jd42")."""
    local = email.split("@", 1)[0]
    words = [w for w in re.split(r"[._-]+", local) if w]
    if words and len(words) <= 3 and all(w.isalpha() for w in words):
        return " ".join(w.capitalize() for w in words)
    return scrub(local)


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


def questions(values: Any, limit: int) -> list[str]:
    """likely_questions: [{question, why_likely, source_url?}] (or plain strings) -> lines."""
    out = []
    for value in values or []:
        if isinstance(value, dict) and str(value.get("question") or "").strip():
            why = str(value.get("why_likely") or "").strip()
            out.append(scrub(value["question"] + (f" ({why})" if why else "")))
        elif isinstance(value, str) and value.strip():
            out.append(scrub(value))
    return out[:limit]


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
                     "level": scrub(pay_range.get("level") or ""),
                     "confidence": scrub(pay_range.get("confidence") or ""),
                     "source_url": url if url.startswith(("https://", "http://")) else ""}
    return {"from_you": findings(parts.get("private")),
            "round": findings(parts.get("round")),
            "people": merged([findings(b) for b in (parts.get("interviewers_by_email") or {}).values()]),
            "company": found_company,
            "likely_questions": questions(raw_round.get("likely_questions"), 8),
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
    """The next automatic briefing dial: one call at T-30 (no retries); a block added
    later than that is called once its research is ready, while 5 minutes are left."""
    for block in blocks:
        if block["state"] in FINAL_BLOCK or paused:
            continue
        start = parse(block["start"])
        due = start - SINGLE_CALL_LEAD
        if due > now:
            return {"block": block["id"], "at": iso(due), "label": block["label"], "attempt": 1}
        if start - MIN_LEFT_TO_CALL > now:
            return {"block": block["id"], "at": iso(now), "label": block["label"], "attempt": 1}
    return None


LABELS = {  # Plain names for the profile rubric's keys (app/profile/rubric.py).
    "technical": "Technical", "communication": "Communication", "behavioural": "Behavioural",
    "process": "Running the round", "negotiation": "Pay conversations",
    "system_design": "System design", "coding_problem_solving": "Coding & problem solving",
    "cs_fundamentals": "CS basics", "domain_depth": "Depth in your field",
    "tradeoff_reasoning": "Explaining trade-offs", "conciseness_answer_first": "Answering first",
    "structure": "Structured answers", "clarity": "Clarity",
    "listening_clarifying": "Answering what was asked",
    "pace_fillers": "Pace & filler words", "star_stories": "Stories with a result",
    "ownership_impact": "Showing your impact", "motivation_why_us": "Why this company",
    "pushback_curveballs": "Handling pushback", "time_management": "Pacing the round",
    "questions_at_end": "Your questions at the end", "anchoring": "Getting their range first",
    "information_control": "Not revealing your numbers early",
    "competing_offers": "Using other offers",
}
ROUNDS = {"screen": "Screen", "technical": "Technical", "system_design": "System design",
          "hiring_manager": "Hiring manager", "behavioural": "Behavioural", "founder": "Founder",
          "offer": "Offer", "unknown": "Other"}
BADGES = ("improving", "flat", "declining", "insufficient_data")


def label(key: str) -> str:
    """"communication.clarity" -> "Clarity"; unknown keys -> "Sentence case"."""
    last = str(key).split(".")[-1]
    return LABELS.get(last, last.replace("_", " ").capitalize())


def number(value: Any) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


STAGES = {"pre_seed": "Pre-seed", "seed": "Seed", "series_a": "Series A", "series_b": "Series B",
          "series_c": "Series C", "series_d": "Series D", "series_a_b": "Series A–B",
          "series_c_plus": "Series C+", "growth": "Growth stage", "growth_early": "Early growth",
          "growth_late": "Late-stage", "late": "Late-stage", "public": "Public", "enterprise": "Enterprise",
          "bootstrapped": "Bootstrapped", "startup": "Startup", "agency": "Agency", "services": "Services"}
RECORDERS = {"fathom", "fireflies", "fireflies.ai", "granola", "otter", "otter.ai", "zoom", "google meet",
             "meet", "teams", "microsoft teams", "tl;dv", "tldv", "read.ai"}


def stage(value: Any) -> str:
    """"series_c" -> "Series C", "growth_late" -> "Late-stage"; unknown -> ""."""
    key = scrub(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not key or key == "unknown":
        return ""
    if key in STAGES:
        return STAGES[key]
    words = key.split("_")
    return " ".join([words[0].capitalize()] + [w.upper() if len(w) == 1 else w for w in words[1:]])


def company_name(value: Any) -> str:
    """The interviewing company; "" when it is not one (app/company_names.py: the notetaker,
    e.g. "Fathom", a round label or the owner's name)."""
    from app import company_names
    text = scrub(value or "").strip()
    return "" if text.lower() in RECORDERS or not company_names.is_company(text) else text


def day_label(date: str) -> str:
    try:
        return datetime.fromisoformat(date).strftime("%d %b").lstrip("0")
    except ValueError:
        return date


def series_view(raw: Any) -> dict[str, Any]:
    """A performance.json SERIES -> {points[{x, y, ma3, company, round}], n, change, badge}."""
    raw = raw if isinstance(raw, dict) else {}
    points = [{"x": str(p.get("x") or "")[:10], "y": number(p.get("y")), "ma3": number(p.get("ma3")),
               "company": scrub(p.get("company") or ""), "round": ROUNDS.get(p.get("round_type") or "", "")}
              for p in raw.get("points") or [] if isinstance(p, dict) and number(p.get("y")) is not None]
    badge = raw.get("badge") if raw.get("badge") in BADGES else "insufficient_data"
    return {"points": points, "n": len(points), "change": number(raw.get("fitted_change")),
            "per_30d": number(raw.get("slope_per_30d")), "badge": badge}


def performance(perf: dict[str, Any] | None) -> dict[str, Any]:
    """The Performance section from the profile agent's performance.json (schema 2).

    Shown out of 10; null scores (not exercised) stay out of averages and charts.
    Focus this week = the latest interview's `do_next_time`. Free text is scrubbed.
    """
    perf = perf if isinstance(perf, dict) else {}
    rubric = perf.get("rubric") if isinstance(perf.get("rubric"), dict) else {}
    trends = perf.get("trends") if isinstance(perf.get("trends"), dict) else {}
    dims = [{"key": str(d), "label": label(d),
             "subs": [{"key": f"{d}.{sub}", "label": label(sub), "about": scrub(about)}
                      for sub, about in (subs.items() if isinstance(subs, dict) else [])]}
            for d, subs in rubric.items()]
    series = {"overall": series_view(trends.get("overall"))}
    for key, raw in (trends.get("dimensions") or {}).items():
        series[str(key)] = series_view(raw)
    for key, raw in (trends.get("subdimensions") or {}).items():
        series[str(key)] = series_view(raw)
    interviews = []
    for iv in sorted((i for i in perf.get("interviews") or [] if isinstance(i, dict)),
                     key=lambda i: str(i.get("date") or "")):
        interviews.append({
            "id": scrub(iv.get("interview_id")), "date": str(iv.get("date") or "")[:10],
            "company": company_name(iv.get("company")), "stage": stage(iv.get("company_stage")),
            "round": ROUNDS.get(iv.get("round_type") or "", label(iv.get("round_type") or "unknown")),
            "round_key": str(iv.get("round_type") or "unknown"),
            "outcome": iv.get("outcome") if iv.get("outcome") in ("advanced", "rejected") else "unknown",
            "outcome_evidence": scrub(iv.get("outcome_evidence") or ""),
            "overall": number(iv.get("overall_score")),
            "dimensions": {str(k): number(v) for k, v in (iv.get("dimension_scores") or {}).items()},
            "do_next_time": strings(iv.get("do_next_time"), 3),
            "questions": [{"question": scrub(q.get("question")), "went_well": scrub(q.get("went_well") or ""),
                           "improve": scrub(q.get("improve") or ""), "hint": scrub(q.get("model_answer_hint") or "")}
                          for q in iv.get("question_feedback") or [] if isinstance(q, dict)][:6],
            **interview_extras(iv),
        })
    callouts = perf.get("callouts") if isinstance(perf.get("callouts"), dict) else {}
    most, risk = callouts.get("most_improved"), callouts.get("biggest_risk")
    weak = [{"topic": scrub(t.get("topic")), "verdict": t.get("verdict") if t.get("verdict") in
             ("improved", "same", "worse", "mixed") else "same",
             "seen": len([o for o in t.get("occurrences") or [] if isinstance(o, dict)]),
             "last_note": scrub(next((o.get("note") for o in reversed(t.get("occurrences") or [])
                                      if isinstance(o, dict) and o.get("note")), ""))}
            for t in perf.get("recurring_weak_topics") or [] if isinstance(t, dict)][:6]
    rounds = [{"round": ROUNDS.get(k, label(k)), "n": v.get("n"), "overall": number(v.get("overall"))}
              for k, v in (perf.get("by_round_type") or {}).items() if isinstance(v, dict)]
    latest = interviews[-1] if interviews else None
    return {
        "dimensions": dims, "series": series, "interviews": interviews,
        "most_improved": {"key": str(most.get("key")), "label": label(most.get("key", "")),
                          "change": number(most.get("fitted_change")),
                          "first": number(most.get("first")), "last": number(most.get("last"))}
        if isinstance(most, dict) and most.get("key") else None,
        "biggest_risk": {"key": str(risk.get("key")), "label": label(risk.get("key", "")),
                         "latest": number(risk.get("latest_ma3")), "per_30d": number(risk.get("slope_per_30d")),
                         "badge": risk.get("badge") if risk.get("badge") in BADGES else "insufficient_data"}
        if isinstance(risk, dict) and risk.get("key") else None,
        "focus_this_week": [{"action": a, "why": f"From your {latest['company'] or 'last'} interview on {day_label(latest['date'])}"}
                            for a in latest["do_next_time"]] if latest else [],
        "recurring_weak_topics": weak, "by_round_type": rounds,
        "trend_text": scrub(perf.get("trend_text") or ""),
        "generated_at": perf.get("generated_at"),
        "available": bool(interviews),
        "coach": coach_view(perf.get("coach")),
        "impressions": impressions_overview(perf.get("impressions")),
    }


IMPRESSION_KINDS = {  # cue kind -> plain label
    "follow_up_probe": "Follow-up question", "deep_dive": "Dug deeper", "warm_acknowledgement": "Warm reaction",
    "flat_acknowledgement": "Flat reaction", "interruption": "Cut in", "explicit_praise": "Praise",
    "future_framing": "Talked as if you'd join", "team_or_logistics": "Team or logistics",
    "selling_company": "Sold the company", "rushing": "Rushing", "cut_short": "Cut short",
    "clarification_request": "Asked you to clarify", "skepticism": "Doubted the answer",
    "brush_off_close": "Generic goodbye", "concrete_next_step": "Clear next step",
    "vague_next_step": "Vague next step",
}
NEXT_STEP = {"concrete": "Clear next step", "vague": "Vague next step", "brush_off": "No next step, generic goodbye",
             "none": "No next step mentioned"}


def _text(value: Any, limit: int = 600) -> str:
    return scrub(value if isinstance(value, str) else "")[:limit]


def count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def impression_view(raw: Any) -> dict[str, Any] | None:
    """One interview's interviewer impression (performance.json schema 3), for display."""
    if not isinstance(raw, dict) or raw.get("coverage") not in ("transcript", "derived"):
        return None
    parts = raw.get("components") if isinstance(raw.get("components"), dict) else {}
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    cues = []
    for c in raw.get("cues") or []:
        if not isinstance(c, dict) or c.get("kind") not in IMPRESSION_KINDS:
            continue
        cues.append({"kind": IMPRESSION_KINDS[c["kind"]], "tone": "ok" if c.get("polarity") == "+" else "bad",
                     "quote": _text(c.get("quote"), 200), "at": _text(c.get("timestamp"), 8),
                     "after": _text(c.get("after"), 200)})
    share = number(metrics.get("candidate_talk_share"))
    return {"coverage": raw["coverage"], "score": number(raw.get("score")),
            "components": {k: number(parts.get(k)) for k in ("engagement", "positive", "negative", "next_steps", "balance")},
            "talk_share": share, "next_step": NEXT_STEP.get(raw.get("next_step") or "none", ""),
            "leaned_in": _text(raw.get("leaned_in")), "disengaged": _text(raw.get("disengaged")),
            "follow_ups": metrics.get("follow_ups") if isinstance(metrics.get("follow_ups"), int) else None,
            "follow_up_rate": number(metrics.get("follow_up_rate")),
            "answer_seconds": number(metrics.get("avg_answer_seconds")),
            "warm": count(metrics.get("warm_acknowledgements")), "flat": count(metrics.get("flat_acknowledgements")),
            "length_vs_slot": number(metrics.get("length_vs_slot")), "cues": cues[:14]}


def _quote(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("quote"):
        return None
    return {"quote": _text(raw.get("quote"), 300), "date": str(raw.get("date") or "")[:10],
            "company": _text(raw.get("company"), 80), "speaker": "interviewer" if raw.get("speaker") == "interviewer" else "you",
            "at": _text(raw.get("timestamp"), 8)}


def coach_view(raw: Any) -> dict[str, Any] | None:
    """The coach's read (app/profile/coach.py) for the page; None when absent."""
    if not isinstance(raw, dict) or not raw.get("where_you_stand"):
        return None
    rows = lambda key: [r for r in raw.get(key) or [] if isinstance(r, dict)]  # noqa: E731
    neg = raw.get("negotiation") if isinstance(raw.get("negotiation"), dict) else {}
    ivs = raw.get("interviewers") if isinstance(raw.get("interviewers"), dict) else {}
    return {
        "where_you_stand": _text(raw["where_you_stand"], 900),
        "working": [{"headline": _text(w.get("headline"), 160), "detail": _text(w.get("detail")),
                     "example": _quote(w.get("example"))} for w in rows("whats_working")][:3],
        "costing": [{k: _text(c.get(k)) for k in ("headline", "plain_meaning", "why_it_matters", "what_happened",
                                                   "do_instead", "model_rephrase")}
                    | {"example": _quote(c.get("example"))} for c in rows("whats_costing_you")][:3],
        "focus": [{k: _text(f.get(k)) for k in ("drill", "how", "done_when")} for f in rows("this_weeks_focus")][:3],
        "negotiation": {"read": _text(neg.get("read")),
                        "scripts": [{"moment": _text(x.get("moment"), 120), "say": _text(x.get("say"))}
                                    for x in neg.get("scripts") or [] if isinstance(x, dict)][:4]},
        "interviewers": {"leaned_in": _text(ivs.get("leaned_in_when")), "disengaged": _text(ivs.get("disengaged_when")),
                         "examples": [q for q in (_quote(x) for x in ivs.get("examples") or []) if q][:3]},
        "generated_at": raw.get("generated_at"), "stale": bool(raw.get("stale")),
    }


def impressions_overview(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("n_scored"):
        return None
    by = raw.get("by_outcome") if isinstance(raw.get("by_outcome"), dict) else {}
    return {"n": raw.get("n_scored"),
            "by_outcome": {k: {"n": (by.get(k) or {}).get("n"), "mean": number((by.get(k) or {}).get("mean"))}
                           for k in ("advanced", "rejected", "unknown")},
            "r_with_score": number(raw.get("r_with_rubric_overall")), "note": _text(raw.get("note"), 300)}


def interview_extras(iv: dict[str, Any]) -> dict[str, Any]:
    """Fields the notetaker and coach branches add to each interview (app/valr_ui/contracts.py)."""
    return {"source": scrub(iv.get("source") or ""),
            "recording": contracts.recording(iv.get("recording"), iv),
            "impression": impression_view(iv.get("impression"))}


def _fallback_series(interviews: list[dict[str, Any]], value: Any) -> dict[str, Any]:
    points = [{"x": iv["date"], "y": value(iv), "company": iv["company"], "round_type": iv["round_type"]}
              for iv in interviews if value(iv) is not None]
    for i, point in enumerate(points):
        window = [q["y"] for q in points[max(0, i - 2):i + 1]]
        point["ma3"] = round(sum(window) / len(window), 2)
    change = round(points[-1]["ma3"] - points[0]["ma3"], 2) if len(points) >= 3 else None
    badge = "insufficient_data" if change is None else \
        "improving" if change >= 1 else "declining" if change <= -1 else "flat"
    return {"points": points, "n": len(points), "fitted_change": change, "badge": badge}


def performance_from_v1(scores: dict[str, Any] | None, evaluation: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only when performance.json is missing: the old 1-5 table, doubled, in schema-2 shape."""
    v1 = {"technical_depth": "technical", "problem_solving": "technical", "communication_clarity": "communication",
          "structure": "communication", "confidence": "behavioural", "handling_pushback": "behavioural",
          "compensation_handling": "negotiation"}
    by_interview: dict[tuple[str, str], dict[str, list[float]]] = {}
    for row in (scores or {}).get("rows") or []:
        if isinstance(row, dict) and row.get("score") is not None and row.get("date") and row.get("dimension") in v1:
            key = (str(row["date"])[:10], str(row.get("company") or ""))
            by_interview.setdefault(key, {}).setdefault(v1[row["dimension"]], []).append(float(row["score"]) * 2)
    if not by_interview:
        return None
    interviews = []
    for (date, company), dims in sorted(by_interview.items()):
        means = {d: round(sum(v) / len(v), 2) for d, v in dims.items()}
        interviews.append({"interview_id": f"{date}:{company}", "date": date, "company": company,
                           "round_type": "unknown", "overall_score": round(sum(means.values()) / len(means), 2),
                           "dimension_scores": means, "do_next_time": [], "question_feedback": []})
    brief_ = (evaluation or {}).get("brief") or {}
    if interviews:
        interviews[-1]["do_next_time"] = list(brief_.get("prep_checklist") or [])[:3]
    dims = sorted({d for iv in interviews for d in iv["dimension_scores"]})
    return {"rubric": {d: {} for d in dims}, "interviews": interviews,
            "trends": {"overall": _fallback_series(interviews, lambda iv: iv["overall_score"]),
                       "dimensions": {d: _fallback_series(interviews, lambda iv, d=d: iv["dimension_scores"].get(d))
                                      for d in dims}},
            "recurring_weak_topics": [{"topic": t, "verdict": "same", "occurrences": []}
                                      for t in brief_.get("recurring_weak_topics") or []],
            "trend_text": brief_.get("trend") or "", "generated_at": (evaluation or {}).get("generated_at")}


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def profile_files() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(performance, pay target) from the private data dir; None if missing.

    performance.json (VALR_PERFORMANCE_PATH, default /data/performance.json, schema 2 or 3),
    with impressions it lacks filled in from VALR_PERFORMANCE_FALLBACK (default
    /data/performance.json, the read-only copy the live file was seeded from);
    only when it is missing, the old interview_scores.json + evaluation.json, adapted.
    """
    from app.profile.config import settings
    cfg = settings()
    path = Path(os.getenv("VALR_PERFORMANCE_PATH") or "/data/performance.json").expanduser()
    perf = read_json(path)
    fallback = Path(os.getenv("VALR_PERFORMANCE_FALLBACK") or "/data/performance.json").expanduser()
    if perf and fallback != path:  # The live copy may predate the impressions: fill them in.
        from app.profile.impressions import merge_missing
        perf = merge_missing(perf, read_json(fallback))
    if not (perf and isinstance(perf.get("schema_version"), int) and perf["schema_version"] >= 2):
        perf = performance_from_v1(read_json(cfg.scores_path.with_suffix(".json")),
                                   read_json(cfg.evaluation_path))
    profile = read_json(cfg.profile_path) or {}
    target = ((profile.get("compensation") or {}).get("target_lpa")
              if isinstance(profile.get("compensation"), dict) else None)
    target = target if isinstance(target, dict) and target.get("max") else None
    return perf, ({"min": target.get("min"), "max": target.get("max"), "unit": "LPA", "currency": "INR"}
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


def owner_name() -> str:
    """The owner's first name, only to turn call scripts into "you" on the page."""
    try:
        from app.profile.config import settings
        return settings().candidate_name.split()[0] if settings().candidate_name.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


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
                       "state": state, "call_at": iso(parse(plan.start_utc) - SINGLE_CALL_LEAD),
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
    blocks = build_blocks(events, recorded, slots, jobs, companies, owner_emails(), now, paused)
    try:
        await telegram_api.setup(connection)
        sent = await telegram_api.heads_ups(connection, [block["id"] for block in blocks])
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR Telegram heads-ups unavailable: %s", type(error).__name__)
        sent = {}
    for block in blocks:
        row = sent.get(block["id"])
        block["heads_up"] = None if row is None else {
            "outcome": row["outcome"], "sent_at": iso(row["sent_at"]),
            "outcome_at": iso(row["outcome_at"]), "call_at": iso(row["call_at"])}
    return blocks


async def telegram_view(connection: Any) -> dict[str, Any]:
    view = {"configured": telegram_api.configured(), "linked": False, "linked_at": None,
            "username": None}
    try:
        await telegram_api.setup(connection)
        status = await telegram_api.link_status(connection)
        view.update(linked=status["linked"], linked_at=iso(status["linked_at"]),
                    username=status.get("username"))
    except Exception as error:  # noqa: BLE001
        logger.warning("VALR Telegram link unavailable: %s", type(error).__name__)
        view["error"] = True
    return view


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
            chosen = await interview_store.flag_choice(connection, interview_store.AUTO_DIAL)
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR reach choice unavailable: %s", type(error).__name__)
            chosen = None
        auto = telegram_api.auto_dial() if chosen is None else chosen
        view["reach"] = {"auto_dial": auto, "source": "default" if chosen is None else "settings",
                         "default_auto_dial": telegram_api.auto_dial()}
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
            refs = [slot["ref"] for block in view["blocks"] for slot in block["slots"]]
            ticked = await interview_store.prep_checks(connection, refs)
            for block in view["blocks"]:
                for slot in block["slots"]:
                    slot["prep_done"] = sorted(ticked.get(slot["ref"], ()))
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR prep checks unavailable: %s", type(error).__name__)
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
        telegram = await telegram_view(connection)
        try:
            from app import notetakers
            notetaker_status = await notetakers.status_view(connection)
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR notetakers unavailable: %s", type(error).__name__)
            notetaker_status = {}
        try:
            from app import post_call
            reports = await post_call.overview(connection)
            view["post_call"] = {"pending": reports["pending"], "queued": reports["queued"],
                                 "failed": reports["failed"],
                                 "recent": [{"title": scrub(r["title"]), "status": r["status"],
                                             "updated_at": iso(r["updated_at"])} for r in reports["recent"]]}
        except Exception as error:  # noqa: BLE001
            logger.warning("VALR post-call reports unavailable: %s", type(error).__name__)
            view["post_call"] = None
    # telegram: a heads-up at T-30 and calls only on request; call: the automatic T-30 dial.
    view.setdefault("reach", {"auto_dial": telegram_api.auto_dial(), "source": "default",
                              "default_auto_dial": telegram_api.auto_dial()})
    view["mode"] = "call" if view["reach"]["auto_dial"] else "telegram"
    # The next T-30 step (a heads-up, or the dial; VALR_BRIEFING_DIAL=0 stops only the dial).
    view["next_call"] = next_call(view["blocks"], now, paused["enabled"] or (
        view["mode"] == "call" and not view["dial_env"]))
    view["connections"] = {
        "google": {"state": google.get("state", "unknown"), "account": google.get("account") or "",
                   "age_days": google.get("age_days"), "expires_soon": bool(google.get("expires_soon")),
                   "last_ok_at": iso(google.get("last_ok_at")),
                   "configured": google_oauth.OAuthSettings.from_env() is not None},
        "fathom": {"configured": bool(os.getenv("FATHOM_API_KEY", "").strip()) or
                   (notetaker_status.get("fathom") or {}).get("state") == "connected"},
        "notetakers": notetaker_status,
        "calendar": calendar,
        "telegram": telegram,
    }
    view["dials"] = await dial_counts(now)
    perf, target = profile_files()
    view["performance"] = performance(perf)
    view["pay_target"] = target
    view["owner_name"] = owner_name()
    view["setup"] = {"phone_on_file": bool(os.getenv("VALR_OWNER_PHONE", "").strip()),
                     "test_call_done": view.pop("_briefed_ever", False),
                     "valr_number": valr_number()}
    return view
