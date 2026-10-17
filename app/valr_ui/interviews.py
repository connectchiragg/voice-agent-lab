"""Interviews: upcoming (by day), past, and processes (app/valr_ui/processes.py), and one page
per interview.

Upcoming detail (the briefing, one screen): header with the countdown and a ⋯ menu (Refresh
research, Fix company, Hear the briefing); then the summary, likely questions and prep
ticks; the people and one pay line beside them; "More research" folded, the only place
with sources.

Past detail (the review): the outcome, then "Next time", how the interviewer reacted (in
plain words, with quotes), the recording with its key moments, question by question, and
the skills this round tested.
"""
from typing import Any

from app.company_names import NOT_IDENTIFIED
from app.interview_store import prep_item
from app.valr_page import OUTCOME, Clock, chip, countdown, e, found_block, link, score
from app.valr_ui import attention, wording
from app.valr_ui.shell import empty_state, icon, menu, menu_item, page_head, sheet

PEOPLE_ROLE = {"skipped_hr": "Recruiter", "declined": "Declined the invite"}


def tabs(active: str, past_count: int) -> str:
    items = [("upcoming", "Upcoming", "/valr/interviews"), ("past", "Past", "/valr/interviews/past"),
             ("processes", "Processes", "/valr/interviews/processes")]
    return ('<nav class="tabs" aria-label="Interviews">' + "".join(
        f'<a href="{h}"{" aria-current=\"page\"" if k == active else ""}>{e(t)}'
        + (f' <span class="count">{past_count}</span>' if k == "past" and past_count else "") + "</a>"
        for k, t, h in items) + "</nav>")


def reminder_note(block: dict[str, Any], view: dict[str, Any], clock: Clock) -> str:
    """"One reminder at 2:15 AM for both rounds"; a chip only when something went wrong."""
    kind = wording.reminder_kind(block, view.get("mode") or "call")
    n = len(block["slots"])
    at = clock.time(block.get("call_at"), zone=False) if block.get("call_at") else ""
    rounds = "both rounds" if n == 2 else f"all {n} rounds"
    if kind == "heads_up":
        text = (f"One reminder at {at} for {rounds}" if n > 1 else f"Reminder on Telegram at {at}") if at else ""
    else:
        text = (f"One briefing call at {at} for {rounds}" if n > 1 else f"Briefing call at {at}") if at else ""
    sent = block.get("heads_up") or {}
    state = block.get("state")
    problem = ""
    if state == "heads_up_failed" or sent.get("outcome") == "failed":
        problem = chip("Reminder not sent", "bad")
    elif state in ("unanswered", "missed"):
        problem = chip("Missed the call", "warn")
    elif state == "refused":
        problem = chip("Skipped: daily call limit", "bad")
    elif block.get("paused") and state in ("scheduled", "waiting"):
        problem = chip("Paused", "warn")
    elif sent.get("outcome") == "called":
        text = "You asked for the call"
    elif sent.get("outcome") in ("calling", "later") and sent.get("call_at"):
        text = f"VALR calls you at {clock.time(sent['call_at'], zone=False)}"
    elif sent.get("outcome") == "sent" or state in ("heads_up_sent", "briefed"):
        text = text.replace("Reminder on Telegram at", "Reminder sent at").replace("One reminder at", "One reminder sent at")
    if not text and not problem:
        return ""
    glyph = icon("send" if kind == "heads_up" else "phone", 14)
    return f'<p class="group-note">{glyph}<span>{text}</span>{problem}</p>'


def people_line(slot: dict[str, Any]) -> str:
    names = [p["name"] for p in slot["interviewers"] if p["status"] not in ("skipped_hr", "declined")]
    if not names:
        return ""
    return "With " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")


def row(slot: dict[str, Any], clock: Clock) -> str:
    lab = wording.interview_label(slot)
    sub = " · ".join(x for x in (people_line(slot), "" if slot.get("brief") or not slot.get("research_due")
                                 else f"Briefing coming ~{clock.local(slot['research_due']).strftime('%I:%M %p').lstrip('0')}") if x)
    cancelled = slot["status"] == "cancelled"
    end = (chip("Cancelled", "bad") if cancelled else
           f'<span class="row-go">Open briefing{icon("chevron-right", 16)}</span>' if slot.get("brief") else icon("chevron-right", 18))
    return (f'<li><a class="row-link{" is-cancelled" if cancelled else ""}" href="/valr/interviews/{e(slot["ref"])}">'
            f'<span class="row-when"><span class="num">{clock.time(slot["start"], zone=False)}</span></span>'
            f'<span class="row-main"><span class="row-title">{e(lab["headline"])}</span>'
            + (f'<span class="row-sub">{e(sub)}</span>' if sub else "") + f'</span>{end}</a></li>')


def day_title(clock: Clock, start: str) -> str:
    """"Today · Fri 16 Oct", "Mon 19 Oct" (never the date twice)."""
    day = clock.day(start)
    date = clock.local(start).strftime("%a %d %b").replace(" 0", " ")
    return f"{day} · {date}" if day in ("Today", "Tomorrow") else date


def upcoming_list(view: dict[str, Any], clock: Clock) -> str:
    blocks = [b for b in view.get("blocks") or [] if clock.local(b["end"]) >= clock.now]
    if "timeline" in (view.get("errors") or []):
        return empty_state("calendar", "We couldn't load your interviews just now.",
                           "VALR keeps preparing in the background; only this page failed to read them.",
                           '<button class="btn" type="button" data-action="reload">Try again</button>', "error")
    if not blocks and view["connections"]["google"]["state"] != "connected":
        return empty_state("calendar", "Connect Google to see your interviews here.",
                           "VALR reads invites only; it never changes your calendar.",
                           '<a class="btn primary" href="/oauth/google/start">Connect Google</a>')
    if not blocks:
        return empty_state("calendar", "No interviews on your calendar this week.", "",
                           '<button class="btn" type="button" data-action="sync">Sync calendar</button>')
    days: dict[str, list[str]] = {}
    for block in blocks:
        days.setdefault(day_title(clock, block["start"]), []).append(
            f'<div class="group card">{reminder_note(block, view, clock)}<ul class="rows">'
            + "".join(row(s, clock) for s in block["slots"]) + "</ul></div>")
    return "".join(f'<section class="day"><h2 class="day-title">{e(d)}</h2>{"".join(g)}</section>'
                   for d, g in days.items())


def upcoming_page(view: dict[str, Any], clock: Clock) -> str:
    past = len((view.get("performance") or {}).get("interviews") or [])
    actions = menu([menu_item("Sync calendar", "refresh", attrs=' data-action="sync"')])
    return page_head("Interviews", actions=actions) + tabs("upcoming", past) + upcoming_list(view, clock)


# One upcoming interview.

def find_slot(view: dict[str, Any], ref: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for block in view.get("blocks") or []:
        for slot in block["slots"]:
            if slot["ref"] == ref:
                return block, slot
    return None


def pay_section(pay: dict[str, Any], target: dict[str, Any] | None) -> str:
    """Pay in India terms only (LPA / INR): their band against your target, one line of advice
    and one source link. Anything else: "No reliable India pay data found"."""
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
    elif india["claims"]:
        claim = india["claims"][0]
        rows = f'<p>{e(claim["claim"])} {link(claim["url"], "source") if claim["url"] else ""}</p>'
    if not rows:
        return '<p class="muted">No reliable India pay data found for this company yet.</p>'
    source = link(market["source_url"], "source") if market and market.get("source_url") else ""
    return f'<div class="ranges">{rows}</div>' + (f'<p class="verdict">{e(verdict)} {source}</p>' if verdict else "")


def people_section(slot: dict[str, Any]) -> str:
    rows = "".join(
        f'<li class="person"><span class="avatar" aria-hidden="true">{e((p["name"][:1] or "?").upper())}</span>'
        f'<span class="person-id"><span class="who">{e(p["name"])}</span>'
        + (f'<span class="person-role">{e(PEOPLE_ROLE[p["status"]])}</span>' if p["status"] in PEOPLE_ROLE else "")
        + (f'<a class="email" href="mailto:{e(p["email"])}">{e(p["email"])}</a>' if p.get("email") else "")
        + "</span></li>" for p in slot["interviewers"])
    return f'<ul class="people">{rows}</ul>' if rows else '<p class="muted">No one else is on the invite.</p>'


def company_sheet(entity_id: str, company: str) -> str:
    """"Fix company" (static/valr-app.js posts /valr/api/interviews/company): the owner's name wins."""
    body = (f'<div class="fix-form" data-company-fix="{e(entity_id)}"><label class="field-label" for="co-{e(entity_id)}">Company name</label>'
            f'<div class="field-row"><input class="input" id="co-{e(entity_id)}" data-company-input maxlength="80" autocomplete="off" '
            f'placeholder="Company name" value="{e(company)}"><button class="btn primary" type="button" data-company-save>Save</button></div></div>')
    return sheet("fix-company", "Fix company", body)


def unidentified_note(company: str) -> str:
    """Inline only when the company really is unknown."""
    if company and company != NOT_IDENTIFIED:
        return ""
    return (f'<p class="inline-fix">{icon("help", 16)}<span>We couldn\'t tell which company this is.</span>'
            '<button class="link-btn" type="button" data-sheet="fix-company">Set company</button></p>')


def detail_page(view: dict[str, Any], block: dict[str, Any], slot: dict[str, Any], clock: Clock) -> str:
    d, brief = slot["detail"], slot.get("brief")
    lab = wording.interview_label(slot)
    owner = view.get("owner_name") or ""
    points = wording.key_points(brief, owner, limit=3)
    if brief:
        quality = {"partial": "Some research came back thin, so this one is shorter.",
                   "apologized": "VALR couldn't find anything reliable for this one."}.get(brief.get("quality") or "", "")
        summary = (("<ul class=\"points\">" + "".join(f"<li>{e(p)}</li>" for p in points) + "</ul>" if points else "")
                   + (f'<p class="muted">{e(quality)}</p>' if quality else ""))
    else:
        due = slot.get("research_due")
        summary = f'<p class="muted">Briefing coming ~{clock.time(due, zone=False)}.</p>' if due else \
            '<p class="muted">The briefing appears here once VALR has looked into this round.</p>'
    questions = "".join(f"<li>{e(q)}</li>" for q in d["likely_questions"])
    perf = view.get("performance") or {}
    prep = [p for p in d["prep"] if wording.good_prep(p)]
    from_past = not prep
    if from_past:
        prep = [f["action"] for f in perf.get("focus_this_week") or []]
    ticked = set(slot.get("prep_done") or [])
    done = sum(1 for x in prep if prep_item(x) in ticked)
    checklist = "".join(f'<li><label class="check"><input type="checkbox" data-prep-ref="{e(slot["ref"])}"'
                        f'{" checked" if prep_item(x) in ticked else ""}><span>{e(x)}</span></label></li>'
                        for x in prep) or '<li class="muted">Nothing yet.</li>'
    more = "".join((
        f'<section class="d-sec"><h3>What to expect in this round</h3>{found_block(d["round"], "Nothing found about this round yet.")}</section>',
        f'<section class="d-sec"><h3>About the people</h3>{found_block(d["people"], "Nothing public found about them yet.")}</section>',
        f'<section class="d-sec"><h3>From your emails and past rounds</h3>{found_block(d["from_you"], "Nothing in your inbox or recordings about this company yet.")}</section>',
        f'<section class="d-sec"><h3>The company</h3>{found_block(d["company"], "Nothing found about the company yet.")}</section>'))
    ref = e(slot["ref"])
    spoken = (brief or {}).get("spoken") or ""
    actions = menu([
        menu_item("Hear the briefing", "volume", attrs=' data-hear="briefing-script"') if spoken else "",
        menu_item("Refresh research", "refresh", attrs=f' data-action="refresh" data-ref="{ref}"'),
        menu_item("Fix company", "pencil", attrs=' data-sheet="fix-company"'),
    ])
    when = f"{e(clock.day(slot['start']))} · {clock.time(slot['start'], zone=False)}–{clock.time(slot['end'], zone=False)}"
    head = page_head(e(lab["headline"]), eyebrow=when, crumb="/valr/interviews|Interviews", actions=actions)
    prep_head = (f'<span class="card-meta">{done} of {len(prep)} done</span>' if prep and not from_past else
                 '<span class="card-meta">From your recent interviews</span>' if prep else "")
    company = lab["company"] if lab["company"] != NOT_IDENTIFIED else ""
    return f"""{head}
<div class="detail-meta"><span class="pill accent">{icon('clock', 14)}{countdown(slot['start'])}</span>{reminder_note(block, view, clock)}</div>
{unidentified_note(lab['company'])}
<div class="detail-grid">
  <div class="detail-main">
    <section class="card" id="briefing"><h2 class="card-title">{icon('file-text', 18)}Summary</h2>{summary}</section>
    <section class="card" id="questions"><h2 class="card-title">{icon('help', 18)}Likely questions</h2>
      {f'<ol class="questions">{questions}</ol>' if questions else '<p class="muted">Listed once VALR has looked into this round.</p>'}</section>
    <section class="card" id="prep"><div class="card-head"><h2 class="card-title">{icon('list-checks', 18)}Prep</h2>{prep_head}</div>
      <ul class="checklist">{checklist}</ul></section>
  </div>
  <aside class="detail-side">
    <section class="card" id="people"><h2 class="card-title">{icon('users', 18)}People</h2>{people_section(slot)}</section>
    <section class="card" id="pay"><h2 class="card-title">{icon('rupee', 18)}Pay</h2>{pay_section(d['pay'], view.get('pay_target'))}</section>
  </aside>
</div>
<details class="fold" id="more"><summary><span class="fold-title">More research</span><span class="fold-sub">The round, the people, your emails, the company</span>{icon('chevron-down', 18)}</summary>
  <div class="fold-body">{more}</div>
</details>
{company_sheet("slot:" + slot["ref"], company)}
{f'<template id="briefing-script">{e(spoken)}</template>' if spoken else ''}"""


# Past interviews.

REVIEW = {"pending": ("Review pending", "muted"), "unavailable": ("Review not available", "muted"),
          "sent": ("Review sent", "ok"), "no_recording": ("No recording", "muted")}


def outcome_chip(iv: dict[str, Any], with_evidence: bool = False) -> str:
    """The scorecard's outcome, else the matched lead's stage (app/valr_ui/past.py); a stored
    recording without a scorecard shows its review state instead."""
    if iv.get("recording_only"):
        return chip(*REVIEW.get(iv.get("review") or "pending", REVIEW["pending"]))
    if iv.get("outcome_chip"):
        return chip(*iv["outcome_chip"][:2])
    return chip(*OUTCOME[iv["outcome"]], iv.get("outcome_evidence", "") if with_evidence else "")


def past_list(view: dict[str, Any], clock: Clock) -> str:
    interviews = list(reversed((view.get("performance") or {}).get("interviews") or []))
    if not interviews:
        return empty_state("video", "Connect Fathom, Fireflies or Granola to get feedback after each interview.", "",
                           '<a class="btn primary" href="/valr/settings#notetakers">Connect</a>')
    rows = "".join(f"""
<li><a class="row-link" href="/valr/interviews/past/{e(attention.past_slug(iv))}">
  <span class="row-when"><span class="num">{e(clock.short_date(iv['date']) if iv['date'] else '')}</span></span>
  <span class="row-main"><span class="row-title">{e(past_name(iv))}</span>
    <span class="row-sub">{e(iv['round'])}{(' · with ' + e(', '.join(iv['interviewers'][:2]))) if iv.get('interviewers') else ''}</span></span>
  <span class="row-end">{outcome_chip(iv)}{f'<span class="score-chip num" title="Overall, out of 10">{score(iv["overall"])}</span>' if iv.get('overall') is not None else ''}</span>
</a></li>""" for iv in interviews)
    return f'<ul class="rows card">{rows}</ul>'


def past_name(iv: dict[str, Any]) -> str:
    """The company; for a stored recording without one, its contact ("Call with Maheku") or title."""
    if iv.get("company") or not iv.get("recording_only"):
        return iv.get("company") or NOT_IDENTIFIED
    people = iv.get("interviewers") or []
    return f"Call with {people[0]}" if people else iv.get("title") or "Recording"


def remove_item(iv: dict[str, Any]) -> str:
    """⋯ "Delete recording" (a phone upload: its data lives in VALR) or "Hide" (a notetaker's,
    which lives at the provider). static/valr-app.js confirms, then posts with the CSRF token."""
    provider, _, external_id = str(iv.get("id") or "").partition(":")
    if not external_id or provider == "calendar":  # A calendar-only entry (no recording).
        return ""
    if provider == "phone":
        return menu_item("Delete recording", "trash", attrs=f' data-recording-remove="delete" data-id="{e(iv["id"])}"')
    return menu_item("Hide", "eye-off", attrs=f' data-recording-remove="hide" data-id="{e(iv["id"])}"')


def past_page(view: dict[str, Any], clock: Clock) -> str:
    past = len((view.get("performance") or {}).get("interviews") or [])
    return page_head("Interviews") + tabs("past", past) + past_list(view, clock)


def find_past(view: dict[str, Any], slug: str) -> dict[str, Any] | None:
    """By slug; a raw "<provider>-<id>" link (a notification's) is normalised the same way."""
    wanted = {slug, attention.past_slug({"id": slug})}
    return next((iv for iv in (view.get("performance") or {}).get("interviews") or []
                 if attention.past_slug(iv) in wanted), None)


def recording_block(rec: dict[str, Any] | None, interview_id: str = "") -> str:
    """The player (Fathom: streamed from Fathom's download API by static/valr-player.js), the
    "Open in Fathom" link, and the key moments, which seek the player. The video URL and the
    moments come from /valr/api/recording/media and /valr/api/recording when the page opens."""
    has_id = bool(interview_id and ":" in interview_id)
    moments = (f'<div class="moments-box" data-moments-id="{e(interview_id)}" aria-live="polite">'
               '<div class="muted" data-recording-body>Looking for key moments…</div></div>'
               if has_id else "")
    if not rec and moments:  # Not stored yet: ask the notetaker (player, link and moments).
        return (f'<div class="moments-box" data-moments-id="{e(interview_id)}" data-full aria-live="polite">'
                '<div class="muted" data-recording-body>Looking for the recording in your notetakers…</div></div>')
    if not rec:
        return empty_state("video", "No recording linked to this interview yet.", "",
                           '<a class="btn sm" href="/valr/settings#notetakers">Notetakers</a>', "slim")
    open_ = link(rec["url"], f"Open in {rec['provider']}") if rec.get("url") else ""
    if rec.get("player") and has_id:
        frame = player_html(interview_id, rec.get("title") or "")
        return f'{frame}<p class="player-alt">{open_}</p>{moments}'
    frame = (f'<div class="embed embed-wait">{icon("play", 28)}'
             f'<span class="muted">This recording opens in {e(rec["provider"])}.</span></div>')
    return f'{frame}<p class="player-alt">{open_}</p>{moments}'


def player_html(interview_id: str, title: str = "") -> str:
    """The 16:9 player shell; static/valr-player.js asks for the signed video and fills it."""
    return (f'<div class="player-box" data-player="{e(interview_id)}">'
            f'<video controls preload="metadata" playsinline aria-label="{e(title or "Interview recording")}" hidden></video>'
            f'<div class="player-wait" data-player-status aria-live="polite">{icon("play", 28)}'
            '<span class="muted">Preparing the video…</span></div></div>')


def reaction_facts(imp: dict[str, Any]) -> str:
    """"You talked 81% of the time (aim for under 70%). Ended early with no next step." """
    out = []
    if imp.get("talk_share") is not None:
        pct = round(imp["talk_share"] * 100)
        out.append(f"You talked {pct}% of the time" + (" (aim for under 70%)." if pct > 70 else "."))
    step = (imp.get("next_step") or "").lower()
    none, vague, clear = step.startswith("no next step"), step.startswith("vague"), step.startswith("clear")
    early = imp.get("length_vs_slot") is not None and imp["length_vs_slot"] < 0.75
    if early:
        out.append("Ended early with no next step." if none else "Ended early, with a vague next step." if vague
                   else "Ended early, with a clear next step." if clear else "Ended early.")
    elif none:
        out.append("No next step was agreed.")
    elif vague:
        out.append("The next step was vague.")
    elif clear:
        out.append("You left with a clear next step.")
    return " ".join(out)


def cue_html(c: dict[str, Any]) -> str:
    return (f'<li class="cue tone-{e(c["tone"])}"><p class="cue-head"><span class="chip tone-{e(c["tone"])}">{e(c["kind"])}</span>'
            + (f'<span class="cue-at num">{e(c["at"])}</span>' if c["at"] else "")
            + f'</p><blockquote class="cue-quote">“{e(c["quote"])}”</blockquote>'
            + (f'<p class="cue-after">After you said: {e(c["after"])}</p>' if c["after"] else "") + "</li>")


TRANSCRIPT_FROM = {"fathom": "Fathom", "fireflies": "Fireflies"}


def reaction_block(imp: dict[str, Any] | None, iv: dict[str, Any] | None = None) -> str:
    if not imp:
        return '<p class="muted">How you came across appears here once the coach has reviewed this interview.</p>'
    if imp["coverage"] == "derived":
        # The cues need the transcript: re-read from the notetaker (python -m app.notetakers history,
        # or "Try again", POST /valr/api/recording/reanalyse).
        iid = str((iv or {}).get("id") or "")
        source = TRANSCRIPT_FROM.get(iid.partition(":")[0], "the notetaker")
        share = f" You talked {imp['talk_share'] * 100:.0f}% of the time." if imp["talk_share"] is not None else ""
        again = (f' <button type="button" class="btn sm" data-reanalyse="{e(iid)}">Try again</button>'
                 if iid.partition(":")[0] in TRANSCRIPT_FROM else "")
        return f'<p class="muted">Couldn\'t read this recording\'s transcript from {e(source)}.{e(share)}{again}</p>'
    verdict = "".join((
        f'<p class="verdict-line"><span class="chip tone-ok">Leaned in</span><span>{e(imp["leaned_in"])}</span></p>' if imp["leaned_in"] else "",
        f'<p class="verdict-line"><span class="chip tone-bad">Disengaged</span><span>{e(imp["disengaged"])}</span></p>' if imp["disengaged"] else ""))
    facts = reaction_facts(imp)
    cues = imp["cues"]
    shown = "".join(cue_html(c) for c in cues[:3])
    rest = "".join(cue_html(c) for c in cues[3:])
    return (f'<div class="reaction">{verdict}' + (f'<p class="facts-line">{e(facts)}</p>' if facts else "")
            + (f'<ul class="cues">{shown}</ul>' if shown else "")
            + (f'<details class="fold inline"><summary><span class="fold-title">{len(cues) - 3} more moments</span>{icon("chevron-down", 16)}</summary>'
               f'<ul class="cues">{rest}</ul></details>' if rest else "") + "</div>")


def skills_block(iv: dict[str, Any], labels: dict[str, str]) -> str:
    tested = [(k, v) for k, v in iv["dimensions"].items() if v is not None]
    if not tested:
        return ""
    bars = "".join(f'<li><span class="sb-name">{e(labels.get(k, k))}</span><span class="sb-track">'
                   f'<i style="width:{v * 10:.0f}%"></i></span><span class="sb-val num">{v:.1f}</span></li>' for k, v in tested)
    return (f'<section class="card" id="scores"><div class="card-head"><h2 class="card-title">{icon("trending-up", 18)}Skills in this round</h2>'
            f'<span class="card-meta">out of 10</span></div><ul class="score-bars">{bars}</ul></section>')


def past_detail_page(view: dict[str, Any], iv: dict[str, Any], clock: Clock) -> str:
    perf = view.get("performance") or {}
    labels = {d["key"]: d["label"] for d in perf.get("dimensions") or []}
    nxt = "".join(f"<li>{e(x)}</li>" for x in iv["do_next_time"])
    qs = "".join(
        f'<li class="q"><p class="q-text">{e(q["question"])}</p>'
        + (f'<p class="q-line q-good"><span class="q-tag">Went well</span>{e(q["went_well"])}</p>' if q["went_well"] else "")
        + (f'<p class="q-line q-improve"><span class="q-tag">Improve</span>{e(q["improve"])}</p>' if q["improve"] else "")
        + (f'<p class="q-line q-better"><span class="q-tag">Try</span>{e(q["hint"])}</p>' if q["hint"] else "")
        + "</li>" for q in iv["questions"])
    when = clock.short_date(iv["date"]) if iv["date"] else ""
    rec = iv.get("recording") or {}
    actions = menu([menu_item(f"Open in {rec['provider']}", "external", rec["url"], external=True) if rec.get("url") else "",
                    menu_item("Fix company", "pencil", attrs=' data-sheet="fix-company"') if iv.get("id") else "",
                    remove_item(iv)])
    sub = (f'{outcome_chip(iv, True)}'
           + (f'<span class="score-chip num" title="Overall, out of 10">{score(iv["overall"])}<span class="of">/10</span></span>'
              if iv.get("overall") is not None else "")
           + (f'<span class="muted">With {e(", ".join(iv["interviewers"]))}</span>' if iv.get("interviewers") else ""))
    head = page_head(e(iv["company"] or NOT_IDENTIFIED), eyebrow=e(" · ".join(x for x in (when, iv["round"]) if x)),
                     crumb="/valr/interviews/past|Past interviews", actions=actions, sub=sub)
    return f"""{head}
{unidentified_note(iv['company'])}
<div class="review">
  <section class="card next-time" id="next"><h2 class="card-title">{icon('lightbulb', 18)}Next time</h2>{f'<ol class="points numbered">{nxt}</ol>' if nxt else '<p class="muted">No notes yet.</p>'}</section>
  <section class="card" id="impression"><h2 class="card-title">{icon('users', 18)}How the interviewer reacted</h2>{reaction_block(iv.get('impression'), iv)}</section>
  <section class="card" id="recording"><h2 class="card-title">{icon('video', 18)}Recording</h2>{recording_block(iv.get('recording'), iv.get('id') or '')}</section>
  {f'<section class="card" id="feedback"><h2 class="card-title">{icon("help", 18)}Question by question</h2><ul class="qlist">{qs}</ul></section>' if qs else ''}
  {skills_block(iv, labels)}
</div>
{transcript_section(iv)}
{company_sheet("interview:" + (iv.get("id") or ""), iv["company"]) if iv.get("id") else ""}"""


def transcript_section(iv: dict[str, Any]) -> str:
    """The owner's transcript, folded (static/valr-player.js loads it from the owner-only
    /valr/api/recording/transcript when opened): times seek the player, a find box, a .txt."""
    iid = str(iv.get("id") or "")
    if ":" not in iid or iv.get("no_recording"):
        return ""
    return f"""<details class="fold" id="transcript" data-transcript-id="{e(iid)}"><summary><span class="fold-title">Transcript</span><span class="fold-sub">Only you can see it</span>{icon('chevron-down', 18)}</summary>
  <div class="fold-body">
    <p><input type="search" class="input" placeholder="Find in transcript" aria-label="Find in transcript" data-transcript-search>
      <a class="btn sm" href="/valr/api/recording/transcript.txt?id={e(iid)}" download>Download transcript (.txt)</a></p>
    <div class="muted" data-transcript-body aria-live="polite">Open to load the transcript.</div>
  </div>
</details>"""


def review_block(state: str) -> str:
    if state == "no_recording":
        return '<p class="muted">No recording of this interview, so there is no coach review.</p>'
    if state == "unavailable":
        return ('<p class="muted">Review not available: the coach found no interview to score in this call '
                '(too short, or no transcript).</p>')
    if state == "sent":
        return '<p class="muted">The coach review was sent to Telegram; its scorecard appears here shortly.</p>'
    return '<p class="muted" aria-live="polite">Review pending… The coach reviews the call within a few minutes.</p>'


def recording_detail_page(iv: dict[str, Any], clock: Clock) -> str:
    """A stored recording without a scorecard (yet): title, date, company, the player, the
    transcript's key moments (/valr/api/recording) and the review state."""
    when = clock.short_date(iv["date"]) if iv.get("date") else ""
    rec = iv.get("recording") or {}
    actions = menu([menu_item(f"Open in {rec['provider']}", "external", rec["url"], external=True) if rec.get("url") else "",
                    menu_item("Fix company", "pencil", attrs=' data-sheet="fix-company"'),
                    remove_item(iv)])
    sub = (outcome_chip(iv)
           + (f'<span class="muted">With {e(", ".join(iv["interviewers"]))}</span>' if iv.get("interviewers") else ""))
    title = iv.get("title") or ""
    eyebrow = " · ".join(x for x in (when, title if title != past_name(iv) else "") if x)
    head = page_head(e(past_name(iv)), eyebrow=e(eyebrow), crumb="/valr/interviews/past|Past interviews",
                     actions=actions, sub=sub)
    return f"""{head}
{unidentified_note(iv.get('company') or '')}
<div class="review">
  <section class="card" id="recording"><h2 class="card-title">{icon('video', 18)}Recording</h2>{recording_block(iv.get('recording'), '' if iv.get('no_recording') else iv['id'])}</section>
  <section class="card" id="coach"><h2 class="card-title">{icon('lightbulb', 18)}Coach review</h2>{review_block(iv.get('review') or 'pending')}</section>
</div>
{transcript_section(iv)}
{company_sheet("interview:" + iv["id"], iv.get("company") or "")}"""
