"""Interviews → Processes: one hiring process per company, its patterns, and a replayable
timeline. Built from data VALR already has (no LLM): the leads JSON (app/leads.graph, which
holds each lead's emails, calendar interviews, recordings and scorecards as touchpoints).

A process (`derive`):
- Start: the first contact (an application, or a recruiter's outreach).
- Milestones: the first interview, each later round (a calendar interview, a scorecard or a
  recording, one per day), and the outcome.
- End and status: Offer, Rejected, Withdrew (a closing touch he sent, or one that says he
  withdrew; with no closing touch, the tracker's closed stage, e.g. a scorecard's verdict),
  Fizzled out (not closed, nothing scheduled and no activity for FIZZLE_DAYS: the
  same 21 days the lead tracker calls "ghosted"), else Active. A later round after a
  rejection re-opens it, like the tracker's stages. The owner's own stage wins.

The patterns (`patterns`) are medians and shares over these processes, worded plainly, and
say "needs more data" below MIN_SAMPLE. The timeline (`timeline`) lists each event's time,
company, kind and title only: never an email's text (`summary` is not read into it).
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from app.valr_page import e
from app.valr_ui.shell import empty_state, icon

FIZZLE_DAYS = 21
MIN_SAMPLE = 3
STATUS = {"active": "Active", "offer": "Offer", "rejected": "Rejected", "withdrew": "Withdrew",
          "fizzled": "Fizzled out"}
KIND_LABEL = {"start": "First contact", "email": "Email", "invite": "Interview invite", "interview": "Interview",
              "recording": "Recording", "scored": "Scorecard", "calendar_change": "Calendar change",
              "offer": "Offer", "rejection": "Rejection", "withdrew": "Withdrew", "fizzled": "Fizzled out",
              "call": "Call"}
OUTCOME_KIND = {"offer": "offer", "rejection": "rejected", "withdrew": "withdrew"}
ROUND_KINDS = ("interview", "scored", "recording")
WITHDRAW = re.compile(r"\bwithdr[ae]w|\bwithdrawing\b|\bnot (?:be )?(?:pursuing|proceeding|moving forward) with (?:the|your|this)\b"
                      r"|\bdeclin(?:e|ed|ing) (?:the|your) (?:offer|role|opportunity)", re.I)
INVITE = re.compile(r"\b(invite|invitation|schedul|availability|slot|interview|round|screen|call with|meet)", re.I)
CHANGE = re.compile(r"\b(reschedul|moved|updated invitation|cancel)", re.I)


@dataclass
class Event:
    at: datetime
    kind: str
    title: str


@dataclass
class Process:
    id: str
    company: str
    start: datetime
    events: list[Event] = field(default_factory=list)  # Past events, in time order.
    status: str = "active"
    end: datetime | None = None
    rounds: list[datetime] = field(default_factory=list)
    scheduled: bool = False  # An interview still to come.
    last_activity: datetime | None = None
    fizzle_at: datetime | None = None

    @property
    def first_interview(self) -> datetime | None:
        return self.rounds[0] if self.rounds else None

    def duration_days(self, now: datetime) -> int:
        return max(0, round(((self.end or now) - self.start).total_seconds() / 86400))


def when(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def event_kind(touch: dict[str, Any]) -> str:
    """A touchpoint's kind on the timeline, from its kind, stage hint and title only."""
    kind, hint = str(touch.get("kind") or ""), str(touch.get("stage_hint") or "")
    title = str(touch.get("title") or "")
    if kind == "evaluation":  # The interview itself; its verdict is the tracker's stage (below).
        return "scored"
    if hint == "closed-lost":
        return "withdrew" if touch.get("direction") == "out" or WITHDRAW.search(title) else "rejection"
    if hint in ("offer", "closed-won"):
        return "offer"
    if kind == "calendar":
        return "calendar_change" if CHANGE.search(title) else "interview"
    if kind == "recording":
        return "recording"
    if kind == "call":
        return "call"
    if kind == "email" and hint in ("screening", "interviewing", "final") and INVITE.search(title):
        return "invite"
    return "email"


def derive(lead: dict[str, Any], now: datetime) -> Process | None:
    """One lead's process; None for an agency with no employer yet, or nothing dated."""
    if lead.get("agency_only"):
        return None
    touches = [(when(t.get("at")), t) for t in lead.get("timeline") or [] if isinstance(t, dict)]
    touches = sorted(((at, t) for at, t in touches if at is not None), key=lambda pair: pair[0])
    past = [(at, t) for at, t in touches if at <= now]
    if not past:
        return None
    process = Process(str(lead.get("id") or ""), str(lead.get("company") or "Company not identified"), past[0][0])
    process.scheduled = any(at > now and t.get("kind") == "calendar" for at, t in touches)
    status, end = "active", None
    for at, touch in past:
        kind = event_kind(touch)
        process.events.append(Event(at, kind, str(touch.get("title") or "")[:140]))
        if kind in ROUND_KINDS and not any(abs(at - r) < timedelta(hours=20) for r in process.rounds):
            process.rounds.append(at)
            if status == "rejected":  # A new round after a rejection: it re-opened.
                status, end = "active", None
        if kind in OUTCOME_KIND:
            status, end = OUTCOME_KIND[kind], at
    process.last_activity = past[-1][0]
    # The tracker's stage (or the owner's) when no touch said how it ended: a scorecard's
    # verdict, or a stage he set by hand. His own setting also overrides a touch.
    closing = {"closed-won": "offer", "closed-lost": "rejected", "ghosted": "fizzled"}
    stage = lead.get("stage")
    if stage in closing and (status == "active" or lead.get("stage_manual")):
        if stage != "ghosted" or lead.get("stage_manual"):
            status, end = closing[stage], process.last_activity
    if status in ("active", "fizzled") and not process.scheduled \
            and now - process.last_activity > timedelta(days=FIZZLE_DAYS):
        status, end = "fizzled", process.last_activity
    elif status == "fizzled" and process.scheduled:
        status, end = "active", None
    if status == "fizzled":
        process.fizzle_at = min(now, process.last_activity + timedelta(days=FIZZLE_DAYS))
    process.status, process.end = status, end
    return process


def derive_all(raw: dict[str, Any] | None, now: datetime) -> list[Process]:
    leads = (raw or {}).get("leads") or []
    found = [derive(lead, now) for lead in leads if isinstance(lead, dict)]
    return sorted((p for p in found if p is not None), key=lambda p: (p.start, p.company.lower()))


# Patterns.

def days(delta: timedelta) -> float:
    return delta.total_seconds() / 86400


def plural(count: float, word: str) -> str:
    n = round(count)
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def pattern(key: str, title: str, values: list[float], say: str, unit: str = "day") -> dict[str, Any]:
    """A median, or "needs more data" below MIN_SAMPLE processes."""
    if len(values) < MIN_SAMPLE:
        return {"key": key, "title": title, "enough": False, "value": "",
                "detail": f"Needs more data: {len(values)} of {MIN_SAMPLE} processes so far."}
    mid = median(values)
    return {"key": key, "title": title, "enough": True, "value": plural(mid, unit),
            "detail": say.format(n=len(values), low=plural(min(values), unit), high=plural(max(values), unit))}


def gaps(process: Process) -> list[float]:
    times = [ev.at for ev in process.events]
    return [days(b - a) for a, b in zip(times, times[1:])]


def patterns(processes: list[Process], now: datetime) -> list[dict[str, Any]]:
    to_interview = [days(p.first_interview - p.start) for p in processes if p.first_interview]
    to_rejection = [days(p.end - p.start) for p in processes if p.status == "rejected" and p.end]
    after_round = [days(p.end - max(r for r in p.rounds if r <= p.end)) for p in processes
                   if p.status == "rejected" and p.end and any(r <= p.end for r in p.rounds)]
    # The longest quiet spell in each process that still moved on afterwards.
    recovered = [max(gaps(p)) for p in processes if p.status != "fizzled" and len(p.events) > 1]
    out = [
        pattern("first_interview", "First contact to first interview", to_interview,
                "Median of {n} processes (fastest {low}, slowest {high})."),
        pattern("rejection", "First contact to a rejection", to_rejection,
                "Median of {n} rejections (from {low} to {high})."),
        pattern("after_interview", "Last interview to a rejection", after_round,
                "Median of {n}: how long the wait for a no usually is."),
    ]
    fizzled = sum(1 for p in processes if p.status == "fizzled")
    silence = pattern("silence", "Longest silence that still came back", recovered,
                      "Typical; the longest was {high}. After " + str(FIZZLE_DAYS) + " quiet days VALR "
                      "calls a process fizzled out" + (f" ({fizzled} so far)." if fizzled else "."))
    out.append(silence)
    out.append(funnel(processes))
    return out


def funnel(processes: list[Process]) -> dict[str, Any]:
    total = len(processes)
    stages = [("Reached an interview", sum(1 for p in processes if p.rounds)),
              ("Two or more rounds", sum(1 for p in processes if len(p.rounds) >= 2)),
              ("Got an offer", sum(1 for p in processes if p.status == "offer"))]
    if total < MIN_SAMPLE * 2:
        return {"key": "funnel", "title": "How far processes get", "enough": False, "value": "", "stages": [],
                "detail": f"Needs more data: {total} of {MIN_SAMPLE * 2} processes so far."}
    rows = [{"label": label, "count": n, "share": round(100 * n / total)} for label, n in stages]
    return {"key": "funnel", "title": "How far processes get", "enough": True, "stages": rows,
            "value": f"{rows[0]['share']}%",
            "detail": f"Of {total} processes, {rows[0]['share']}% reached an interview and "
                      f"{rows[2]['share']}% an offer."}


# The data the page and /valr/api/processes share.

def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def milestones(p: Process) -> list[dict[str, Any]]:
    out = [{"at": iso(p.start), "kind": "start", "label": "First contact"}]
    for index, at in enumerate(p.rounds):
        out.append({"at": iso(at), "kind": "first_interview" if index == 0 else "round",
                    "label": "First interview" if index == 0 else f"Round {index + 1}"})
    if p.status in ("offer", "rejected", "withdrew"):
        out.append({"at": iso(p.end), "kind": p.status, "label": STATUS[p.status]})
    elif p.status == "fizzled":
        out.append({"at": iso(p.fizzle_at), "kind": "fizzled", "label": STATUS["fizzled"]})
    return out


def timeline(raw: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    processes = derive_all(raw, now)
    events = []
    for p in processes:
        events += [{"at": iso(ev.at), "process": p.id, "company": p.company, "kind": ev.kind,
                    "label": KIND_LABEL.get(ev.kind, "Event"), "title": ev.title} for ev in p.events]
        if p.status == "fizzled":
            events.append({"at": iso(p.fizzle_at), "process": p.id, "company": p.company, "kind": "fizzled",
                           "label": KIND_LABEL["fizzled"], "title": f"No word for {FIZZLE_DAYS} days"})
    events.sort(key=lambda ev: ev["at"])
    start = min((p.start for p in processes), default=now)
    return {
        "generated_at": iso(now), "fizzle_days": FIZZLE_DAYS,
        "range": {"start": iso(start), "end": iso(now)},
        "processes": [{"id": p.id, "company": p.company, "status": p.status, "status_label": STATUS[p.status],
                       "start": iso(p.start), "end": iso(p.end), "last_activity": iso(p.last_activity),
                       "fizzle_at": iso(p.fizzle_at), "duration_days": p.duration_days(now),
                       "rounds": len(p.rounds), "scheduled": p.scheduled, "milestones": milestones(p)}
                      for p in processes],
        "events": events,
        "patterns": patterns(processes, now),
        "counts": {key: sum(1 for p in processes if p.status == key) for key in STATUS},
    }


# The page.

def pct(moment: str | None, start: datetime, span: float) -> float:
    at = when(moment)
    if at is None or span <= 0:
        return 0.0
    return max(0.0, min(100.0, 100 * (at - start).total_seconds() / span))


def short(moment: str | None) -> str:
    at = when(moment)
    return f"{at.day} {at:%b}" if at else ""


def axis(start: datetime, end: datetime, span: float) -> str:
    """Month ticks (or weeks for a short range) along the shared time axis."""
    ticks, step_weeks = [], (end - start) < timedelta(days=50)
    if step_weeks:
        at = (start + timedelta(days=(7 - start.weekday()) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        while at < end:
            ticks.append((at, f"{at.day} {at:%b}"))
            at += timedelta(days=7)
    else:
        at = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while at < end:
            if at >= start:
                ticks.append((at, f"{at:%b}"))
            at = (at + timedelta(days=32)).replace(day=1)
    marks = "".join(f'<span class="proc-tick{" odd" if i % 2 else ""}" style="left:{100 * (t - start).total_seconds() / span:.2f}%">'
                    f'{e(label)}</span>' for i, (t, label) in enumerate(ticks))
    return f'<div class="proc-axis" aria-hidden="true"><span class="proc-axis-pad"></span><div class="proc-axis-track">{marks}</div></div>'


def row(p: dict[str, Any], start: datetime, span: float) -> str:
    x0 = pct(p["start"], start, span)
    if p["status"] == "active":
        x1 = 100.0
    else:
        x1 = pct(p["last_activity"] if p["status"] == "fizzled" else p["end"], start, span)
    width = max(0.6, x1 - x0)
    tail = ""
    if p["status"] == "fizzled":
        xf = pct(p["fizzle_at"], start, span)
        tail = f'<line class="proc-tail" x1="{x0 + width:.2f}%" x2="{xf:.2f}%" y1="12" y2="12"/>'
    marks = "".join(
        f'<circle class="proc-m m-{e(m["kind"])}" cx="{pct(m["at"], start, span):.2f}%" cy="12" r="{4 if m["kind"] in ("start", "round", "first_interview") else 5}" '
        f'data-at="{e(m["at"])}"><title>{e(m["label"])}, {e(short(m["at"]))}</title></circle>' for m in p["milestones"])
    told = ", ".join(f'{m["label"].lower() if i else m["label"]} {short(m["at"])}' for i, m in enumerate(p["milestones"]))
    label = f'{p["company"]}: {p["status_label"]}, {p["duration_days"]} days. {told}.'
    return (f'<li class="proc-row s-{e(p["status"])}" data-pid="{e(p["id"])}">'
            f'<span class="proc-name"><span class="proc-co">{e(p["company"])}</span>'
            f'<span class="proc-sub">{e(p["status_label"])} · <span class="num">{p["duration_days"]}</span> d</span></span>'
            f'<svg class="proc-track" height="24" role="img" aria-label="{e(label)}">'
            f'<rect class="proc-bar" x="{x0:.2f}%" y="7" width="{width:.2f}%" height="10" rx="5" data-x0="{x0:.3f}" data-x1="{x0 + width:.3f}"/>'
            f'{tail}{marks}</svg></li>')


def pattern_card(p: dict[str, Any]) -> str:
    if p["key"] == "funnel" and p["enough"]:
        bars = "".join(f'<li><span class="proc-fn-label">{e(s["label"])}</span>'
                       f'<span class="proc-fn-bar"><b style="width:{s["share"]}%"></b></span>'
                       f'<span class="proc-fn-num num">{s["share"]}%</span></li>' for s in p["stages"])
        body = f'<ul class="proc-funnel">{bars}</ul><p class="proc-pat-detail">{e(p["detail"])}</p>'
    elif p["enough"]:
        body = f'<p class="proc-pat-value num">{e(p["value"])}</p><p class="proc-pat-detail">{e(p["detail"])}</p>'
    else:
        body = f'<p class="proc-pat-value muted">—</p><p class="proc-pat-detail">{e(p["detail"])}</p>'
    cls = " wide" if p["key"] == "funnel" else ""
    return f'<li class="proc-pat card{cls}{"" if p["enough"] else " thin"}"><p class="proc-pat-title">{e(p["title"])}</p>{body}</li>'


LEGEND = (("active", "Active"), ("offer", "Offer"), ("rejected", "Rejected"), ("withdrew", "Withdrew"),
          ("fizzled", "Fizzled out"))


def section(data: dict[str, Any]) -> str:
    """The patterns, then the chart with its replay controls (static/valr-processes.js)."""
    processes = data["processes"]
    if not processes:
        return empty_state("chart", "No hiring processes yet.",
                           "They appear once VALR has seen your job emails or interviews.",
                           '<a class="btn primary" href="/valr/settings">Connect Gmail</a>')
    start, end = when(data["range"]["start"]), when(data["range"]["end"])
    span = max(1.0, (end - start).total_seconds())  # type: ignore[operator]
    counts = " · ".join(f'<span class="proc-count s-{k}"><i aria-hidden="true"></i>{n} {e(STATUS[k].lower())}</span>'
                        for k, n in data["counts"].items() if n)
    pats = "".join(pattern_card(p) for p in data["patterns"])
    rows = "".join(row(p, start, span) for p in processes)  # type: ignore[arg-type]
    options = "".join(f'<option value="{e(p["id"])}">{e(p["company"])}</option>'
                      for p in sorted(processes, key=lambda p: p["company"].lower()))
    legend = "".join(f'<li class="s-{k}"><i aria-hidden="true"></i>{e(label)}</li>' for k, label in LEGEND)
    return f"""
<section class="proc-patterns" aria-labelledby="proc-pat-h"><h2 class="sr-only" id="proc-pat-h">Patterns</h2>
  <ul class="proc-pats">{pats}</ul></section>
<section class="card proc-card" data-proc aria-labelledby="proc-h">
  <div class="proc-head"><div><h2 class="card-title" id="proc-h">{icon('list-checks', 18)}Every process, on one timeline</h2>
    <p class="card-meta">{counts}</p></div>
    <ul class="proc-legend" aria-label="Colours">{legend}<li class="m"><i aria-hidden="true"></i>Interview</li></ul></div>
  <div class="proc-player" data-proc-player hidden>
    <button class="btn primary proc-play" type="button" data-proc-play aria-label="Play the timeline">{icon('play', 16)}{icon('pause', 16)}<span>Play</span></button>
    <label class="proc-scrub"><span class="sr-only">Date on the timeline</span>
      <input type="range" min="0" max="1000" step="1" value="1000" data-proc-scrub aria-valuetext=""></label>
    <div class="proc-speed" role="radiogroup" aria-label="Speed">
      <button type="button" role="radio" aria-checked="true" data-speed="1">1×</button>
      <button type="button" role="radio" aria-checked="false" data-speed="2">2×</button>
      <button type="button" role="radio" aria-checked="false" data-speed="4">4×</button></div>
    <label class="proc-filter"><span class="sr-only">Company</span>
      <select data-proc-company><option value="">All companies</option>{options}</select></label>
  </div>
  <p class="proc-now" data-proc-now aria-live="polite">Today: {counts}</p>
  <div class="proc-chart" data-proc-chart>
    {axis(start, end, span)}
    <div class="proc-body"><div class="proc-playhead" data-proc-playhead aria-hidden="true" hidden></div>
      <ol class="proc-rows" data-proc-rows>{rows}</ol></div>
  </div>
  <div class="proc-feed" data-proc-feed hidden><h3 class="proc-feed-h">As it happened</h3><ol data-proc-events aria-live="off"></ol></div>
</section>
<script type="application/json" id="processes-data">{json_script(data)}</script>"""


def json_script(data: dict[str, Any]) -> str:
    import json
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def page(data: dict[str, Any], tabs_html: str) -> str:
    from app.valr_ui.shell import asset, page_head
    return (page_head("Interviews", sub="How each hiring process went, from first contact to the outcome.")
            + tabs_html + f'<link rel="stylesheet" href="{asset("valr-processes.css")}">'
            + section(data) + f'<script type="module" src="{asset("valr-processes.js")}"></script>')
