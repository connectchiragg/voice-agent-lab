"""Home: "what now", on a stage with the leads globe as the hero.

At the very top, "New": the latest unread notifications (five at most, newest first), each
with a red dot until it is opened, and "See all" for the bell's panel; one quiet line when
nothing is unread.

Desktop: one dark stage. On the left, what matters in the first five seconds: the next
interview (company, round, time with a countdown, the panel, prep progress, the reminder
line and "Open briefing") and "Your move" (who is waiting on you, three at most). On the
right, the 3D leads globe with its legend. Phone: the next interview first, then Your move,
then the globe, smaller. Below the stage: the other interviews still ahead.

Anything broken shows as one line above the stage; everything else (a reconnect due next
week, feedback ready) lives in the notification bell.
"""
import json
from typing import Any

from app.valr_page import Clock, countdown, e
from app.valr_ui import attention, wording
from app.valr_ui.shell import STATIC, alert_strip, asset, empty_state, icon, note_item


def globe_src() -> str:
    """The leads branch's 3D globe (three.js); static/valr-globe-lite.js is the fallback."""
    return asset("leads-globe.js") if (STATIC / "leads-globe.js").exists() else asset("valr-globe-lite.js")


def safe_json(data: Any) -> str:
    """JSON for a non-executable script tag ("<" escaped)."""
    return json.dumps(data).replace("<", "\\u003c")


def globe_json(leads: dict[str, Any]) -> str:
    """The lite globe's data: the normalised leads (stage, company, next step)."""
    return safe_json({"leads": leads.get("leads") or [], "stages": leads.get("stages") or []})


def globe_leads(leads: dict[str, Any]) -> list[dict[str, Any]]:
    """What the 3D globe draws: the live and the closed leads (closed hidden until the toggle),
    one small record each. Expired leads, people and links stay out."""
    return [{"id": r["id"], "company": r["company"], "temperature": r["temperature"], "band": r["band"],
             "stage": r["stage_raw"] or r["stage"], "stage_label": r["stage_label"],
             "last_touch_at": r["last_activity_at"], "status": "closed" if r["bucket"] == "closed" else "active"}
            for r in (leads.get("leads") or []) + (leads.get("closed") or [])]


def raw_json(leads: dict[str, Any]) -> str:
    """The 3D globe's data (static/leads-globe.js)."""
    return safe_json({"leads": globe_leads(leads)})


def band_sw(band: str, temperature: Any = None) -> str:
    """A band swatch; with a score, its place inside the band sets the shade (static/leads.css)."""
    from app.leads import BANDS
    lows = dict(BANDS)
    f = 1.0
    if temperature is not None and band in lows:
        f = max(0.0, min(1.0, (float(temperature) - lows[band]) / 25))
    return f'<span class="band-sw b-{e(band)}" style="--f:{f:.2f}" aria-hidden="true"></span>'


def band_legend(leads: dict[str, Any], show_closed: bool = False) -> str:
    """"Hot 5 · Warm 12 · Cool 30 · Cold 15", each a link to that band on Leads, the size key
    and the "Show closed" toggle (the globe only)."""
    items = "".join(
        f'<a class="legend-item" href="/valr/leads?tab=all&amp;band={e(b["key"])}" title="{e(b["meaning"])}">{band_sw(b["key"])}'
        f'{e(b["label"])} <span class="num">{b["count"]}</span></a>' for b in leads.get("bands") or [])
    closed = leads.get("closed_band") or {}
    toggle = (f'<label class="globe-toggle" title="{e(closed.get("meaning", ""))}"><input type="checkbox" data-globe-closed'
              f'{" checked" if show_closed else ""}> Show closed <span class="num">{closed["count"]}</span></label>'
              if closed.get("count") else "")
    size = ('<span class="legend-size" aria-hidden="true"><i></i><i></i><i></i></span>'
            '<span class="legend-note">Bigger dot: further along</span>')
    return (f'<div class="band-legend" aria-label="Lead temperature">{items}</div>'
            f'<div class="legend-extra">{size}{toggle}</div>')


def pipeline_line(leads: dict[str, Any]) -> str:
    """"4 companies waiting on you · 7 in interviews"."""
    counts = leads.get("counts") or {}
    waiting = counts.get("waiting_on_you", 0)
    interviewing = sum(1 for r in leads.get("leads") or [] if r["stage"] in ("interviewing", "final"))
    parts = [f'{waiting} compan{"ies" if waiting != 1 else "y"} waiting on you' if waiting else "Nobody waiting on you",
             f"{interviewing} in interviews" if interviewing else ""]
    return " · ".join(p for p in parts if p)


def globe_panel(leads: dict[str, Any]) -> str:
    """The globe and its caption. On Home it is the hero; a click on a lead opens its peek."""
    if leads.get("available"):
        live = (leads.get("counts") or {}).get("live", 0)
        caption = (f'<p class="stage-eyebrow">Pipeline · {live} active</p>'
                   f'<p class="stage-title">{e(pipeline_line(leads))}</p>{band_legend(leads)}')
    else:
        caption = ('<p class="stage-eyebrow">Pipeline</p><p class="stage-title">Your companies appear here</p>'
                   '<p class="stage-note">Connect Google and VALR will find your job emails.</p>'
                   '<a class="btn sm on-stage" href="/oauth/google/start">Connect Google</a>')
    return f"""
<div class="stage-globe">
  <div class="globe" data-globe data-mode="home" data-src="{globe_src()}"
       data-fallback="{asset('valr-globe-lite.js')}" role="group"
       aria-label="A globe of your active leads: hottest at the top, coldest at the bottom. The Leads page lists the same companies.">
    <noscript><p class="stage-note">The globe needs JavaScript; the Leads page lists every company.</p></noscript>
  </div>
  <div class="stage-caption">{caption}</div>
  <div class="lead-peek" data-lead-peek hidden aria-live="polite"></div>
  <script type="application/json" id="leads-data">{globe_json(leads)}</script>
  <script type="application/json" id="leads-raw">{raw_json(leads)}</script>
</div>"""


def reminder_line(block: dict[str, Any], view: dict[str, Any], clock: Clock) -> str:
    """"Reminder on Telegram at 2:15 AM · Pause" (or the briefing call, or paused)."""
    telegram = view.get("mode") == "telegram"
    paused = (view.get("dials_paused") or {}).get("enabled")
    tg = (view.get("connections") or {}).get("telegram") or {}
    if paused:
        return (f'<p class="now-reminder">{icon("pause", 15)}<span>Reminders are paused</span>'
                '<button class="link-btn" type="button" data-action="dials" data-paused="false">Resume</button></p>')
    if telegram and not tg.get("linked"):
        return (f'<p class="now-reminder warn">{icon("send", 15)}<span>Connect Telegram to get a reminder</span>'
                '<a class="link-btn" href="/valr/settings#telegram">Connect</a></p>')
    if not telegram and not view.get("dial_env"):
        return ""
    at = block.get("call_at") or (view.get("next_call") or {}).get("at")
    if not at:
        return ""
    what = "Reminder on Telegram" if telegram else "Briefing call"
    return (f'<p class="now-reminder">{icon("send" if telegram else "phone", 15)}<span>{what} at {clock.time(at, zone=False)}</span>'
            '<button class="link-btn" type="button" data-action="dials" data-paused="true">Pause</button></p>')


def prep_meter(slot: dict[str, Any]) -> str:
    lines = attention.prep_lines(slot)
    if not lines:
        return ""
    done = attention.prep_done(slot, lines)
    word = f"Prep: all {len(lines)} done" if done == len(lines) else f"Prep: {done} of {len(lines)} done"
    pips = "".join(f'<i class="{"on" if i < done else ""}"></i>' for i in range(len(lines)))
    return (f'<a class="prep-meter{" is-done" if done == len(lines) else ""}" href="/valr/interviews/{e(slot["ref"])}#prep">'
            f'<span class="prep-pips" aria-hidden="true">{pips}</span><span>{e(word)}</span></a>')


def next_card(view: dict[str, Any], clock: Clock) -> str:
    nxt = attention.next_slot(view)
    if not nxt:
        if view["connections"]["google"]["state"] != "connected":
            return ('<article class="now-card is-empty">' + empty_state(
                "calendar", "Connect Google to see your interviews here.", "VALR reads invites only; it never changes your calendar.",
                '<a class="btn primary" href="/oauth/google/start">Connect Google</a>') + "</article>")
        return ('<article class="now-card is-empty">' + empty_state(
            "calendar", "No interviews on your calendar this week.", "",
            '<button class="btn" type="button" data-action="sync">Sync calendar</button>') + "</article>")
    block, slot = nxt
    lab = wording.interview_label(slot)
    people = [p["name"] for p in slot["interviewers"] if p["status"] not in ("skipped_hr", "declined")]
    with_ = (f' · with {e(", ".join(people[:2]))}{" +" + str(len(people) - 2) if len(people) > 2 else ""}'
             if people else "")
    ref = e(slot["ref"])
    if slot.get("brief"):
        go = f'<a class="btn on-stage" href="/valr/interviews/{ref}">{icon("file-text", 16)}Open briefing</a>'
    else:
        due = slot.get("research_due")
        go = (f'<a class="btn on-stage" href="/valr/interviews/{ref}">Open interview</a>'
              + (f'<span class="muted-line">Briefing coming ~{clock.time(due, zone=False)}</span>' if due else ""))
    return f"""
<article class="now-card" aria-labelledby="next-title">
  <p class="eyebrow">Next interview · {e(clock.day(slot['start']))}</p>
  <h2 class="now-company" id="next-title"><a href="/valr/interviews/{ref}">{e(lab['company'])}</a></h2>
  <p class="now-round">{e(lab['round'] or 'Interview')}{with_}</p>
  <p class="now-when"><span class="now-time num">{clock.time(slot['start'], zone=False)}</span><span class="pill accent">{countdown(slot['start'])}</span></p>
  {prep_meter(slot)}
  {reminder_line(block, view, clock)}
  <div class="now-actions">{go}</div>
</article>"""


def move_card(leads: dict[str, Any], limit: int = 3, unread: dict[str, int] | None = None) -> str:
    """Who is waiting on you: what they asked, since when, and the thread (a red dot: unread email)."""
    unread = unread or {}
    head = '<div class="card-head"><h2 class="card-title" id="move-title">Your move{count}</h2>{more}</div>'
    if not leads.get("available"):
        return ('<section class="move-card" aria-labelledby="move-title">' + head.format(count="", more="")
                + '<p class="quiet-line">Connect Google and VALR will find your job emails.</p></section>')
    you = [r for r in leads.get("leads") or [] if r["waiting_on"] == "you"]
    if not you:
        return ('<section class="move-card is-clear" aria-labelledby="move-title">' + head.format(count="", more="")
                + f'<p class="quiet-line">{icon("check-circle", 16)}Nobody\'s waiting on you.</p></section>')
    rows = "".join(
        f'<li class="move-row"><a class="move-main" href="/valr/leads?lead={e(r["id"])}#lead">'
        f'<span class="move-co">{band_sw(r["band"])}{e(r["company"])}{unread_mark(unread.get(r["id"], 0))}</span>'
        f'<span class="move-ask">{e(wording.ask_line(r["waiting_reason"]))}</span></a>'
        + (f'<a class="icon-btn sm" href="{e(r["url"])}" target="_blank" rel="noopener noreferrer" '
           f'aria-label="Open the {e(r["company"])} thread in Gmail">{icon("mail", 16)}</a>' if r.get("url") else "")
        + "</li>" for r in you[:limit])
    more = (f'<a class="card-link" href="/valr/leads?tab=you">See all {len(you)}{icon("arrow-right", 14)}</a>'
            if len(you) > limit else "")
    return (f'<section class="move-card" aria-labelledby="move-title">'
            + head.format(count=f' <span class="count">{len(you)}</span>', more=more)
            + f'<ul class="move-list">{rows}</ul></section>')


def unread_mark(n: int) -> str:
    return f'<span class="unread-dot" data-unread-lead title="Unread email"><span class="sr-only"> (unread email)</span></span>' if n else ""


def briefing_state(slot: dict[str, Any], clock: Clock) -> str:
    """"With Maya Lin · Briefing coming ~2:00 PM" (a ready briefing is just the link)."""
    people = [p["name"] for p in slot["interviewers"] if p["status"] not in ("skipped_hr", "declined")]
    with_ = ("With " + ", ".join(people[:2])) if people else ""
    if slot.get("brief") or not slot.get("research_due"):
        return with_
    due = clock.local(slot["research_due"]).strftime("%I:%M %p").lstrip("0")
    return " · ".join(x for x in (with_, f"Briefing coming ~{due}") if x)


def coming_up(view: dict[str, Any], clock: Clock) -> str:
    """The other interviews still ahead (four at most)."""
    nxt = attention.next_slot(view)
    first = nxt[1]["ref"] if nxt else None
    slots = [s for block in view.get("blocks") or [] for s in block["slots"]
             if clock.local(s["end"]) >= clock.now and s["status"] != "cancelled" and s["ref"] != first]
    if not slots:
        return ""
    rows = "".join(
        f'<li><a class="row-link" href="/valr/interviews/{e(s["ref"])}">'
        f'<span class="row-when"><span class="row-day">{e(clock.day(s["start"]))}</span><span class="num">{clock.time(s["start"], zone=False)}</span></span>'
        f'<span class="row-main"><span class="row-title">{e(wording.interview_label(s)["headline"])}</span>'
        f'<span class="row-sub">{e(briefing_state(s, clock))}</span></span>{icon("chevron-right", 18)}</a></li>'
        for s in slots[:4])
    return (f'<section class="section" aria-labelledby="later-title"><div class="section-head">'
            f'<h2 class="section-title" id="later-title">Coming up</h2></div>'
            f'<ul class="rows card">{rows}</ul></section>')


def news_strip(notes: dict[str, Any] | None, clock: Clock, limit: int = 5) -> str:
    """The latest unread notifications; "You're all caught up" when there are none."""
    notes = notes or {}
    unread = int(notes.get("unread") or 0)
    items = [n for n in notes.get("items") or [] if not n.get("read")][:limit]
    if not unread or not items:
        return (f'<p class="notif-strip is-clear" data-notif-strip>{icon("check-circle", 16)}'
                '<span>You\'re all caught up.</span></p>')
    rows = "".join(note_item(n, clock.now) for n in items)
    return f"""
<section class="notif-strip" aria-labelledby="new-title" data-notif-strip>
  <div class="strip-head"><h2 class="strip-title" id="new-title"><span class="strip-dot" aria-hidden="true"></span>New
    <span class="count" data-notif-count>{unread}</span></h2>
    <button class="card-link" type="button" data-notif-show>See all{icon("arrow-right", 14)}</button></div>
  <ul class="notif-items strip-list">{rows}</ul>
</section>"""


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]], leads: dict[str, Any],
         notes: dict[str, Any] | None = None) -> str:
    return f"""{alert_strip(items)}
{news_strip(notes, clock)}
<section class="stage hero-globe" aria-label="Right now">
  <div class="stage-now">
    {next_card(view, clock)}
    {move_card(leads, unread=(notes or {}).get("leads"))}
  </div>
  {globe_panel(leads)}
</section>
{coming_up(view, clock)}"""
