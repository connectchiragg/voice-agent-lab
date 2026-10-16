"""Interviews: upcoming (by day) and past, and one page per interview.

Upcoming detail: the written briefing first (key points, likely questions, prep), the
people with their emails and the India pay snapshot beside it, and the deeper research
folded under "More research". Past detail: the recording, how it came across
(impression cues), the feedback and the scores.
"""
from typing import Any

from app.valr_page import (CALL_STATUS, HEADS_UP, OUTCOME, PERSON, Clock, chip, countdown, e,
                           found_block, link, score, status_label)
from app.valr_ui import attention, wording
from app.valr_ui.shell import icon, page_head

ACTIONS = ('<button class="btn small" type="button" data-action="sync">Sync calendar</button>'
           '<button class="btn small primary" type="button" data-action="callnow">Call me now</button>')


def tabs(active: str, past_count: int) -> str:
    items = [("upcoming", "Upcoming", "/valr/interviews"), ("past", f"Past ({past_count})", "/valr/interviews/past")]
    return ('<nav class="seg" aria-label="Interviews">' + "".join(
        f'<a href="{h}"{" aria-current=\"page\"" if k == active else ""}>{e(t)}</a>' for k, t, h in items) + "</nav>")


def reminder(block: dict[str, Any], view: dict[str, Any], clock: Clock) -> str:
    """"Telegram heads-up at 2:40 PM IST" or "Briefing call at ...", by what it actually was."""
    kind = wording.reminder_kind(block, view.get("mode") or "call")
    label, tone = CALL_STATUS.get(block["state"], (block["state"], "muted"))
    if block.get("paused") and block["state"] in ("scheduled", "waiting"):
        label, tone = ("Paused: no heads-up" if kind == "heads_up" else "Paused: no call"), "warn"
    sent = block.get("heads_up")
    if sent:
        label, tone = HEADS_UP.get(sent["outcome"], (sent["outcome"], "muted"))
        label = label.format(at=clock.local(sent["call_at"]).strftime("%I:%M %p").lstrip("0")
                             if sent.get("call_at") else "–")
    what = "Telegram heads-up" if kind == "heads_up" else "Briefing call"
    n = len(block["slots"])
    covers = f" · covers {n} back-to-back interviews" if n > 1 else ""
    return (f'<p class="call-line">{icon("telegram" if kind == "heads_up" else "call", 16)}'
            f'<span class="call-what">{what} at <b>{clock.time(block["call_at"])}</b>{covers}</span>'
            f'<span class="muted small">{countdown(block["call_at"], short=True)}</span>{chip(label, tone)}</p>')


def people_names(slot: dict[str, Any]) -> str:
    names = [p["name"] for p in slot["interviewers"] if p["status"] not in ("skipped_hr", "declined")]
    if not names:
        return ""
    return "With " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")


def row(slot: dict[str, Any], clock: Clock) -> str:
    lab = wording.interview_label(slot)
    people = people_names(slot)
    return f"""
<li class="iv"><a class="iv-link" href="/valr/interviews/{e(slot['ref'])}">
  <span class="iv-time num">{clock.time(slot['start'])}</span>
  <span class="iv-main"><span class="iv-company">{e(lab['headline'])}</span>
    {f'<span class="iv-title muted small">{e(lab["raw"])}</span>' if lab['raw'] else ''}
    {f'<span class="iv-people small">{e(people)}</span>' if people else ''}</span>
  <span class="iv-side">{status_label(slot, clock)}<span class="iv-open mono">Open →</span></span>
</a></li>"""


def day_title(clock: Clock, start: str) -> str:
    """"Today · Fri 16 Oct", "Mon 19 Oct" (never the date twice)."""
    day = clock.day(start)
    date = clock.local(start).strftime("%a %d %b").replace(" 0", " ")
    return f"{day} · {date}" if day in ("Today", "Tomorrow") else date


def upcoming_list(view: dict[str, Any], clock: Clock, limit: int | None = None) -> str:
    now = clock.now
    blocks = [b for b in view.get("blocks") or [] if clock.local(b["end"]) >= now]
    if "timeline" in (view.get("errors") or []):
        return ('<div class="empty error" role="alert"><p class="empty-title">We couldn\'t load your interviews just now.</p>'
                '<p class="muted">VALR keeps researching in the background; only this page failed to read them.</p>'
                '<button class="btn small" type="button" data-action="reload">Try again</button></div>')
    if not blocks and view["connections"]["google"]["state"] != "connected":
        return ('<div class="empty"><p class="empty-title">Connect your Google Calendar to see your interviews here.</p>'
                '<p class="muted">VALR reads invites only; it never changes your calendar.</p>'
                '<a class="btn small" href="/oauth/google/start">Connect Google</a></div>')
    if not blocks:
        return ('<div class="empty"><p class="empty-title">No interviews in the next 8 days.</p>'
                '<p class="muted">New invites show up here within a minute of landing on your calendar.</p></div>')
    days: dict[str, list[str]] = {}
    for block in blocks[:limit] if limit else blocks:
        days.setdefault(day_title(clock, block["start"]), []).append(
            f'<div class="group">{reminder(block, view, clock)}<ul class="ivs">'
            + "".join(row(s, clock) for s in block["slots"]) + "</ul></div>")
    return "".join(f'<section class="day"><h2 class="day-title mono">{e(d)}</h2>{"".join(g)}</section>'
                   for d, g in days.items())


def upcoming_page(view: dict[str, Any], clock: Clock) -> str:
    past = len((view.get("performance") or {}).get("interviews") or [])
    return (page_head("Interviews", 'Your interviews <span class="dim">this week.</span>', actions=ACTIONS)
            + tabs("upcoming", past) + upcoming_list(view, clock))


# One upcoming interview.

def find_slot(view: dict[str, Any], ref: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for block in view.get("blocks") or []:
        for slot in block["slots"]:
            if slot["ref"] == ref:
                return block, slot
    return None


def pay_section(pay: dict[str, Any], target: dict[str, Any] | None) -> str:
    """Pay in India terms only (LPA / INR); anything else: "No reliable India pay data found"."""
    india = wording.india_pay(pay)
    market = india["range"]
    rows = verdict = ""
    if market and target and all(isinstance(x, (int, float)) for x in
                                 (market.get("min"), market.get("max"), target.get("min"), target.get("max"))):
        low, high = min(market["min"], target["min"]), max(market["max"], target["max"])
        span = (high - low) or 1

        def bar(name: str, r: dict[str, Any], cls: str) -> str:
            left, width = (r["min"] - low) / span * 100, max((r["max"] - r["min"]) / span * 100, 2)
            return (f'<div class="range-row"><span class="range-name">{e(name)}</span><span class="range-track">'
                    f'<i class="{cls}" style="left:{left:.1f}%;width:{width:.1f}%"></i></span>'
                    f'<span class="range-val num">{e(r["min"])}–{e(r["max"])} LPA</span></div>')
        rows = bar("Their band", market, "market") + bar("Your target", target, "target")
        if target["min"] > market["max"]:
            verdict = "Your target is above their band: ask early whether the role has room, and lead with scope."
        elif target["max"] < market["min"]:
            verdict = "Their band is above your target: let them name a number first."
        elif target["max"] > market["max"]:
            verdict = "Your target overlaps the top of their band: anchor on scope and impact, not the number."
        else:
            verdict = "Your target sits inside their band: ask for their range before giving yours."
    elif market:
        rows = f'<p class="num">Their band: <b>{e(market.get("min"))}–{e(market.get("max"))} LPA</b></p>'
    claims = "".join(f"<li>{e(c['claim'])} {link(c['url']) if c['url'] else ''}</li>" for c in india["claims"])
    if not rows and not claims:
        return '<p class="muted small">No reliable India pay data found for this company yet.</p>'
    source = link(market["source_url"], "source") if market and market.get("source_url") else ""
    return (f'<div class="ranges">{rows}</div>' + (f'<p class="verdict">{e(verdict)} {source}</p>' if verdict else "")
            + (f'<details class="more"><summary>Where this comes from</summary><ul class="facts">{claims}</ul></details>'
               if claims else ""))


def people_section(slot: dict[str, Any]) -> str:
    rows = "".join(
        f'<li class="person"><span class="avatar" aria-hidden="true">{e((p["name"][:1] or "?").upper())}</span>'
        f'<span class="person-id"><span class="who">{e(p["name"])}</span>'
        + (f'<a class="mono small email" href="mailto:{e(p["email"])}">{e(p["email"])}</a>' if p.get("email") else "")
        + f'</span>{chip(*PERSON.get(p["status"], (p["status"], "muted")))}</li>'
        for p in slot["interviewers"])
    return f'<ul class="people">{rows}</ul>' if rows else '<p class="muted small">No one else is on the invite.</p>'


def detail_page(view: dict[str, Any], block: dict[str, Any], slot: dict[str, Any], clock: Clock) -> str:
    d, brief = slot["detail"], slot.get("brief")
    lab = wording.interview_label(slot)
    owner = view.get("owner_name") or ""
    points = wording.key_points(brief, owner)
    if brief:
        quality = {"partial": "Some research came back thin, so this one is shorter.",
                   "apologized": "VALR couldn't find anything reliable for this one."}.get(brief.get("quality") or "", "")
        summary = (("<ul class=\"points\">" + "".join(f"<li>{e(p)}</li>" for p in points) + "</ul>" if points else "")
                   + (f'<p class="muted small">{e(quality)}</p>' if quality else "")
                   + f'<details class="more"><summary>What VALR says on the call</summary>'
                     f'<blockquote class="spoken">{e(brief["spoken"])}</blockquote></details>')
    else:
        summary = (f'<p class="muted">The briefing is written after research, around '
                   f'{clock.time(slot.get("research_due"))}. Check back then, or refresh the research now.</p>')
    questions = "".join(f"<li>{e(q)}</li>" for q in d["likely_questions"])
    perf = view.get("performance") or {}
    prep = [p for p in d["prep"] if wording.good_prep(p)]
    source = "For this round"
    if not prep:
        prep, source = [f["action"] for f in perf.get("focus_this_week") or []], "From your recent interviews"
    checklist = "".join(f'<li><label><input type="checkbox" data-check="{e(slot["ref"])}:{i}"> <span>{e(x)}</span></label></li>'
                        for i, x in enumerate(prep)) or '<li class="muted small">Nothing yet.</li>'
    notes = "".join(
        f"<li>{e(wording.written(n['fact'], owner))}"
        + (" " + chip("sensitive", "warn", "VALR only says this if you ask") if n["sensitive"] else "")
        + (" " + chip("from your inbox", "info") if n["private"] else "")
        + "".join(f" {link(u)}" for u in n["sources"]) + "</li>" for n in (brief or {}).get("notes") or [])
    more = "".join(x for x in (
        f'<section class="d-sec"><h3>What to expect in this round</h3>{found_block(d["round"], "Nothing found about this round yet.")}</section>',
        f'<section class="d-sec"><h3>About the people</h3>{found_block(d["people"], "Nothing public found about them yet.")}</section>',
        f'<section class="d-sec"><h3>From your emails &amp; past rounds</h3>{found_block(d["from_you"], "Nothing in your inbox or recordings about this company yet.")}</section>',
        f'<section class="d-sec"><h3>The company</h3>{found_block(d["company"], "Nothing found about the company yet.")}</section>',
        f'<section class="d-sec"><h3>All research notes</h3><ul class="facts">{notes}</ul></section>' if notes else ""))
    ref = e(slot["ref"])
    when = f"{e(clock.day(slot['start']))} · {clock.time(slot['start'], zone=False)} – {clock.time(slot['end'])}"
    head = page_head(when, e(lab["headline"]), crumb="/valr/interviews|Interviews",
                     actions=('<button class="btn small primary" type="button" data-action="callnow">Call me now</button>'
                              f'<button class="btn small" type="button" data-action="refresh" data-ref="{ref}">Refresh research</button>'))
    return f"""{head}
<div class="iv-meta">{status_label(slot, clock)}<span class="mono small ice">{countdown(slot['start'])}</span>
  {f'<span class="muted small">Calendar title: {e(lab["raw"])}</span>' if lab['raw'] else ''}</div>
<div class="iv-reminder">{reminder(block, view, clock)}</div>
<div class="detail-grid">
  <div class="detail-main">
    <section class="card" id="briefing"><h2 class="card-title">Your briefing</h2>{summary}</section>
    <section class="card" id="questions"><h2 class="card-title">Likely questions</h2>
      {f'<ol class="questions">{questions}</ol>' if questions else '<p class="muted small">Listed once VALR has researched this round.</p>'}</section>
    <section class="card" id="prep"><h2 class="card-title">Prep checklist</h2><p class="muted small">{e(source)} · ticks stay on this device</p>
      <ul class="checklist">{checklist}</ul></section>
  </div>
  <aside class="detail-side">
    <section class="card" id="people"><h2 class="card-title">Who you're meeting</h2>{people_section(slot)}</section>
    <section class="card" id="pay"><h2 class="card-title">Pay snapshot</h2>{pay_section(d['pay'], view.get('pay_target'))}
      <p class="muted small">India figures only.</p></section>
  </aside>
</div>
<details class="more-research"><summary><span class="label">More research</span><span class="muted small">round, people, your emails, the company, sources</span></summary>
  {more}
  <p class="muted small">Last updated {clock.when(slot['updated_at'])}. "Refresh research" runs everything again (a few minutes).</p>
</details>"""


# Past interviews.

def past_list(view: dict[str, Any], clock: Clock) -> str:
    interviews = list(reversed((view.get("performance") or {}).get("interviews") or []))
    if not interviews:
        return ('<div class="empty"><p class="empty-title">Past interviews show up here once they are recorded.</p>'
                '<p class="muted">Connect a notetaker so VALR can review each one.</p>'
                '<a class="btn small" href="/valr/settings#notetakers">Connect a notetaker</a></div>')
    rows = "".join(f"""
<li class="iv"><a class="iv-link" href="/valr/interviews/past/{e(attention.past_slug(iv))}">
  <span class="iv-time num">{e(clock.short_date(iv['date']) if iv['date'] else '–')}</span>
  <span class="iv-main"><span class="iv-company">{e(iv['company'] or 'Unknown company')}</span>
    <span class="iv-title muted small">{e(iv['round'])}{(' · ' + e(iv['stage'])) if iv['stage'] else ''}</span></span>
  <span class="iv-side">{chip(*OUTCOME[iv['outcome']])}<span class="score-sm num">{score(iv['overall'])}<span class="of">/10</span></span>
    {f'<span class="iv-open mono">{icon("play", 12)} Recording</span>' if iv.get('recording') else ''}</span>
</a></li>""" for iv in interviews)
    return f'<ul class="ivs group">{rows}</ul>'


def past_page(view: dict[str, Any], clock: Clock) -> str:
    past = len((view.get("performance") or {}).get("interviews") or [])
    return (page_head("Interviews", 'How past rounds <span class="dim">went.</span>')
            + tabs("past", past) + past_list(view, clock))


def find_past(view: dict[str, Any], slug: str) -> dict[str, Any] | None:
    return next((iv for iv in (view.get("performance") or {}).get("interviews") or []
                 if attention.past_slug(iv) == slug), None)


def recording_block(rec: dict[str, Any] | None) -> str:
    if not rec:
        return ('<div class="empty slim"><p class="muted">No recording linked to this interview.</p>'
                '<a class="btn small" href="/valr/settings#notetakers">Notetaker settings</a></div>')
    # The player (Fathom plays inline; valr-notetakers supplies embed_url). Until it does, the
    # same 16:9 space holds a link, so the page doesn't jump when the player lands.
    frame = (f'<div class="embed"><iframe src="{e(rec["embed_url"])}" title="{e(rec["title"] or "Interview recording")}" '
             f'loading="lazy" referrerpolicy="no-referrer" allow="fullscreen; picture-in-picture" '
             f'sandbox="allow-scripts allow-same-origin allow-popups allow-presentation"></iframe></div>'
             if rec.get("embed_url") else
             f'<div class="embed embed-wait"><span class="muted small">{icon("play", 28)}'
             f'The {e(rec["provider"])} player appears here.</span></div>')
    open_ = link(rec["url"], f"Open in {rec['provider']}") if rec.get("url") else ""
    return f'{frame}<p class="small">{open_}</p>'


def impression_block(imp: dict[str, Any] | None) -> str:
    if not imp:
        return '<p class="muted small">How you came across appears here once the coach has reviewed this interview.</p>'
    tone = {"positive": ("ok", "+"), "negative": ("bad", "−"), "neutral": ("info", "·")}
    cues = "".join(f'<li class="cue tone-{tone[c["tone"]][0]}"><span class="cue-mark mono" aria-hidden="true">{tone[c["tone"]][1]}</span>'
                   f'<span><span class="cue-text">{e(c["cue"])}</span>'
                   + (f'<span class="q-quote small">“{e(c["quote"])}”</span>' if c["quote"] else "")
                   + f'<span class="sr-only"> ({e(c["tone"])})</span></span></li>' for c in imp["cues"])
    return (f'<p class="lead-text">{e(imp["summary"])}</p>' if imp["summary"] else "") + (f'<ul class="cues">{cues}</ul>' if cues else "")


def past_detail_page(view: dict[str, Any], iv: dict[str, Any], clock: Clock) -> str:
    perf = view.get("performance") or {}
    labels = {d["key"]: d["label"] for d in perf.get("dimensions") or []}
    bars = "".join(f'<li><span class="sb-name">{e(labels.get(k, k))}</span><span class="sb-track">'
                   f'<i style="width:{v * 10:.0f}%"></i></span><span class="sb-val num">{v:.1f}</span></li>'
                   if v is not None else
                   f'<li class="na"><span class="sb-name">{e(labels.get(k, k))}</span><span class="sb-track"></span>'
                   f'<span class="sb-val num">n/a</span></li>' for k, v in iv["dimensions"].items())
    nxt = "".join(f"<li>{e(x)}</li>" for x in iv["do_next_time"])
    qs = "".join(
        f'<li class="q"><p class="q-text">{e(q["question"])}</p>'
        + (f'<p class="small q-good"><span class="mono">Went well</span> {e(q["went_well"])}</p>' if q["went_well"] else "")
        + (f'<p class="small q-improve"><span class="mono">Improve</span> {e(q["improve"])}</p>' if q["improve"] else "")
        + (f'<p class="small q-better"><span class="mono">Try</span> {e(q["hint"])}</p>' if q["hint"] else "")
        + "</li>" for q in iv["questions"])
    when = clock.short_date(iv["date"]) if iv["date"] else ""
    head = page_head(e(f"{when} · {iv['round']}" + (f" · {iv['stage']}" if iv["stage"] else "")),
                     e(iv["company"] or "Unknown company"), crumb="/valr/interviews/past|Past interviews")
    return f"""{head}
<div class="iv-meta">{chip(*OUTCOME[iv['outcome']], iv['outcome_evidence'])}<span class="score-md num">{score(iv['overall'])}<span class="of">/10</span></span></div>
<div class="detail-grid">
  <div class="detail-main">
    <section class="card" id="recording"><h2 class="card-title">Recording</h2>{recording_block(iv.get('recording'))}</section>
    <section class="card" id="impression"><h2 class="card-title">How you came across</h2>{impression_block(iv.get('impression'))}</section>
    <section class="card" id="next"><h2 class="card-title">Next time</h2>{f'<ul class="points">{nxt}</ul>' if nxt else '<p class="muted small">No notes yet.</p>'}</section>
    {f'<section class="card" id="feedback"><h2 class="card-title">Question by question</h2><ul class="qlist">{qs}</ul></section>' if qs else ''}
  </div>
  <aside class="detail-side">
    <section class="card"><h2 class="card-title">Scores</h2><p class="muted small">Out of 10; n/a = not tested in this round.</p><ul class="score-bars">{bars}</ul></section>
  </aside>
</div>"""
