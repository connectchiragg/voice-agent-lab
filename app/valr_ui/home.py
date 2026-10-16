"""Home: the leads globe as the hero, then "right now" in four cells (the next interview,
the next heads-up or call, up to three things to do, and anything broken), then the
next few interviews. Everything else is one click away."""
import json
from typing import Any

from app.valr_page import Clock, countdown, e, phone_label
from app.valr_ui import attention, wording
from app.valr_ui.shell import STATIC, asset, attention_list, icon


def globe_src() -> str:
    """The leads branch's 3D globe (three.js); static/valr-globe-lite.js is the fallback."""
    return asset("leads-globe.js") if (STATIC / "leads-globe.js").exists() else asset("valr-globe-lite.js")


def safe_json(data: Any) -> str:
    """JSON for a non-executable script tag ("<" escaped)."""
    return json.dumps(data).replace("<", "\\u003c")


def globe_json(leads: dict[str, Any]) -> str:
    """The lite globe's data: the normalised leads (stage, company, next step)."""
    return safe_json({"leads": leads.get("leads") or [], "stages": leads.get("stages") or []})


def raw_json(leads: dict[str, Any]) -> str:
    """The 3D globe's data: GET /valr/api/leads as is, without expired leads."""
    raw = leads.get("raw") or {}
    active = {r["id"] for r in leads.get("leads") or []}
    kept = [lead for lead in raw.get("leads") or [] if lead.get("id") in active]
    ids = {lead["id"] for lead in kept}
    people = [{**c, "leads": [i for i in c.get("leads") or [] if i in ids]} for c in raw.get("contacts") or []]
    people = [c for c in people if c["leads"]]
    pids = {c["id"] for c in people}
    links = [k for k in raw.get("links") or [] if k.get("source") in ids and (k.get("target") in ids or k.get("target") in pids)]
    return safe_json({"leads": kept, "contacts": people, "links": links, "stages": raw.get("stages") or []})


def hero(leads: dict[str, Any], compact: bool = False) -> str:
    rows = leads.get("leads") or []
    live = [r for r in rows if r["stage"] != "closed"]
    interviewing = sum(1 for r in rows if r["stage"] == "interviewing")
    waiting = sum(1 for r in rows if r["waiting_on_you"])
    hot = sum(1 for r in live if (r.get("temperature") or 0) >= 60)
    legend = "".join(
        f'<a class="legend-item" href="/valr/leads?stage={e(s["key"])}"><span class="sw st-{e(s["key"])}" aria-hidden="true"></span>'
        f'{e(s["label"])} <span class="num muted">{s["count"]}</span></a>' for s in leads.get("stages") or [] if s["count"])
    if leads.get("available"):
        title = f'{len(live)} live lead{"s" if len(live) != 1 else ""}'
        sub = " · ".join(x for x in (f"{hot} hot" if hot else "", f"{interviewing} interviewing" if interviewing else "",
                                     f"{waiting} waiting on you" if waiting else "") if x) or "Nothing waiting on you"
    else:
        title, sub = "Your pipeline", "Leads appear here once the Gmail lead tracker is connected."
    temp = ('<p class="globe-key mono small" aria-hidden="true"><span>cold</span><i class="ramp"></i><span>hot</span></p>'
            if leads.get("raw") else "")
    return f"""
<section class="hero-globe{' compact' if compact else ''}" aria-labelledby="hero-title">
  <div class="globe" data-globe data-mode="{'leads' if compact else 'home'}" data-src="{globe_src()}"
       data-fallback="{asset('valr-globe-lite.js')}" role="img"
       aria-label="A globe of your job leads, coloured from cold blue to hot orange. The list on the Leads page has the same information.">
    <noscript><p class="muted small">The globe needs JavaScript; the Leads page lists everything.</p></noscript>
  </div>
  <div class="hero-copy">
    <p class="eyebrow"><span class="eb-rule"></span>Pipeline</p>
    <{'h2' if compact else 'h1'} id="hero-title" class="hero-title">{e(title)}</{'h2' if compact else 'h1'}>
    <p class="hero-sub">{e(sub)}</p>
    {f'<div class="legend">{legend}</div>' if legend else ''}{temp}
    <a class="hero-link mono" href="/valr/leads">{'All leads →' if leads.get('available') else 'Leads settings →'}</a>
  </div>
  <div class="lead-peek" data-lead-peek hidden aria-live="polite"></div>
  <script type="application/json" id="leads-data">{globe_json(leads)}</script>
  <script type="application/json" id="leads-raw">{raw_json(leads)}</script>
</section>"""


def next_cell(view: dict[str, Any], clock: Clock) -> str:
    nxt = attention.next_slot(view)
    if not nxt:
        empty = ("Connect Google to see them." if view["connections"]["google"]["state"] != "connected"
                 else "Nothing in the next 8 days.")
        return f'<div class="now-cell"><p class="label">Next interview</p><p class="now-big">None yet</p><p class="muted small">{e(empty)}</p></div>'
    _, slot = nxt
    lab = wording.interview_label(slot)
    people = [p["name"] for p in slot["interviewers"] if p["status"] not in ("skipped_hr", "declined")]
    return f"""
<a class="now-cell link-cell" href="/valr/interviews/{e(slot['ref'])}">
  <p class="label"><span class="dot live" aria-hidden="true"></span>Next interview</p>
  <p class="now-big">{e(lab['company'])}</p>
  <p class="now-when num">{e(clock.day(slot['start']))}, {clock.time(slot['start'])} · <span class="ice">{countdown(slot['start'])}</span></p>
  <p class="muted small">{e(lab['round'] or lab['raw'] or '')}{(' · with ' + e(', '.join(people[:2]))) if people else ''}</p>
  <span class="cell-go mono">Open briefing →</span>
</a>"""


def reminder_cell(view: dict[str, Any], clock: Clock) -> str:
    telegram = view.get("mode") == "telegram"
    noun = "heads-ups" if telegram else "calls"
    paused = (view.get("dials_paused") or {}).get("enabled")
    toggle = (f'<button class="btn small" type="button" data-action="dials" data-paused="{"false" if paused else "true"}" '
              f'aria-pressed="{"true" if paused else "false"}">{"Resume " + noun if paused else "Pause " + noun}</button>')
    label = "Telegram heads-up" if telegram else "Briefing call"
    if paused:
        return (f'<div class="now-cell warn-cell"><p class="label"><span class="dot warn" aria-hidden="true"></span>{label}</p>'
                f'<p class="now-big">Paused</p><p class="muted small">VALR still researches; "Call me now" still works.</p>{toggle}</div>')
    if not telegram and not view.get("dial_env"):
        return (f'<div class="now-cell"><p class="label">{label}</p><p class="now-big">Off</p>'
                '<p class="muted small">Automatic calls are switched off. Ask for one any time.</p></div>')
    nxt = view.get("next_call")
    how = ("A Telegram message 30 minutes before; tap 📞 for the call." if telegram
           else "VALR calls you 30 minutes before, once.")
    if not nxt:
        return f'<div class="now-cell"><p class="label">{label}</p><p class="now-big">None planned</p><p class="muted small">{e(how)}</p>{toggle}</div>'
    tg = (view.get("connections") or {}).get("telegram") or {}
    warn = ('<p class="small tone-bad">Telegram isn\'t connected: <a href="/valr/settings#telegram">connect it</a>.</p>'
            if telegram and not tg.get("linked") else "")
    return f"""
<div class="now-cell">
  <p class="label">{icon('telegram' if telegram else 'call', 14)} Next {'Telegram heads-up' if telegram else 'briefing call'}</p>
  <p class="now-big num">{clock.time(nxt['at'])}</p>
  <p class="now-when ice">{e(clock.day(nxt['at']))} · {countdown(nxt['at'])}</p>
  <p class="muted small">{e(how)}</p>{warn}{toggle}
</div>"""


def todo_cell(todos: list[dict[str, Any]]) -> str:
    if not todos:
        return '<div class="now-cell"><p class="label">To do</p><p class="now-big">Nothing</p><p class="muted small">You\'re all caught up.</p></div>'
    rows = []
    for t in todos:
        if t.get("href"):
            done = " done" if t.get("done") else ""
            rows.append(f'<li class="todo{done}"><a href="{e(t["href"])}"><span class="todo-box{done}" aria-hidden="true">'
                        f'{icon("check", 12) if done else ""}</span>{e(t["title"])}'
                        f'{"<span class=\"sr-only\"> (done)</span>" if done else ""}</a></li>')
        else:
            fix = t.get("fix") or {}
            target = fix.get("href") or "#"
            rows.append(f'<li><a href="{e(target)}"><span class="todo-box" aria-hidden="true"></span>{e(t["title"])}</a></li>')
    return f'<div class="now-cell"><p class="label">To do</p><ul class="todos">{"".join(rows)}</ul></div>'


def status_cell(items: list[dict[str, Any]]) -> str:
    shown = [i for i in items if not i.get("todo")][:3]
    counted = attention.counted(shown)
    if not shown:
        return (f'<div class="now-cell ok-cell"><p class="label">{icon("check", 14)} Status</p>'
                '<p class="now-big">All good</p><p class="muted small">Nothing is broken.</p></div>')
    head = f"{len(counted)} need{'s' if len(counted) == 1 else ''} attention" if counted else "Heads-up"
    return (f'<div class="now-cell {"bad-cell" if counted else ""}"><p class="label">{icon("alert", 14)} {e(head)}</p>'
            f'{attention_list(shown, "att-list compact")}</div>')


def coming_up(view: dict[str, Any], clock: Clock) -> str:
    slots = []
    for block in view.get("blocks") or []:
        for slot in block["slots"]:
            if clock.local(slot["end"]) >= clock.now and slot["status"] != "cancelled":
                slots.append(slot)
    if not slots:
        return ""
    rows = "".join(
        f'<li><a href="/valr/interviews/{e(s["ref"])}"><span class="cu-when mono num">{e(clock.day(s["start"]))} · {clock.time(s["start"], zone=False)}</span>'
        f'<span class="cu-what">{e(wording.interview_label(s)["headline"])}</span><span class="cu-end" aria-hidden="true">→</span></a></li>'
        for s in slots[:4])
    more = f'<a class="mono small" href="/valr/interviews">All interviews ({len(slots)}) →</a>'
    return f'<section class="coming"><div class="section-row"><h2 class="h-sm">Coming up</h2>{more}</div><ul class="cu-list">{rows}</ul></section>'


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]], leads: dict[str, Any]) -> str:
    todos = attention.todos(view, items, leads)
    number = view["setup"]["valr_number"]
    return f"""
{hero(leads)}
<section class="now" aria-label="Right now">
  {next_cell(view, clock)}{reminder_cell(view, clock)}{todo_cell(todos)}{status_cell(items)}
</section>
<div class="quick">
  <button class="btn primary" type="button" data-action="callnow">{icon('call', 16)} Call me now</button>
  <a class="btn" href="/call">{icon('mic', 16)} Talk in the browser</a>
  <a class="btn" href="tel:{e(number)}">Call VALR <span class="btn-sub num">{e(phone_label(number))}</span></a>
</div>
{coming_up(view, clock)}"""
