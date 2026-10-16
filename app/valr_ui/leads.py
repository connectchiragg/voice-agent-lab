"""Leads: the globe, stage filters and a search box (in the URL: ?stage=&q=&lead=), and the
list. A selected lead (?lead=) opens its card. Data: app/valr_ui/contracts.leads()."""
from typing import Any

from app.valr_page import Clock, chip, e, link
from app.valr_ui.home import hero
from app.valr_ui.shell import page_head

STAGE_TONE = {"applied": "muted", "screening": "info", "interviewing": "accent", "offer": "ok",
              "closed": "muted", "other": "muted"}


def matches(lead: dict[str, Any], stage: str, q: str) -> bool:
    if stage and lead["stage"] != stage:
        return False
    if q:
        hay = " ".join([lead["company"], lead["role"], lead["next_step"], lead["location"],
                        *(c["name"] + " " + c["email"] for c in lead["contacts"])]).lower()
        return q.lower() in hay
    return True


def lead_card(lead: dict[str, Any], clock: Clock) -> str:
    people = "".join(f'<li><span class="who">{e(c["name"])}</span>'
                     + (f' <a class="mono small email" href="mailto:{e(c["email"])}">{e(c["email"])}</a>' if c["email"] else "")
                     + "</li>" for c in lead["contacts"])
    stage = dict((s, s.capitalize()) for s in STAGE_TONE)[lead["stage"]]
    return f"""
<section class="card lead-card" id="lead" aria-labelledby="lead-title">
  <div class="card-row"><h2 id="lead-title" class="card-title">{e(lead['company'])}</h2><a class="close" href="?" aria-label="Close">✕</a></div>
  <p class="muted">{e(lead['role'])}{(' · ' + e(lead['location'])) if lead['location'] else ''}</p>
  <p>{chip(stage, STAGE_TONE[lead['stage']])} {chip('Waiting on you', 'warn') if lead['waiting_on_you'] else ''}</p>
  {f'<p><b>Next:</b> {e(lead["next_step"])}</p>' if lead['next_step'] else ''}
  {f'<p class="muted small">{e(lead["summary"])}</p>' if lead['summary'] else ''}
  {f'<ul class="people plain">{people}</ul>' if people else ''}
  <p class="muted small">{('Last activity ' + clock.when(lead['last_activity_at'])) if lead['last_activity_at'] else ''}{(' · from ' + e(lead['source'])) if lead['source'] else ''}</p>
  {f'<p>{link(lead["url"], "Open the email thread")}</p>' if lead['url'] else ''}
</section>"""


def page(view: dict[str, Any], clock: Clock, leads: dict[str, Any], stage: str, q: str, selected: str) -> str:
    rows = leads.get("leads") or []
    stage = stage if stage in {s["key"] for s in leads.get("stages") or []} else ""
    shown = [r for r in rows if matches(r, stage, q)]
    chips = [f'<a class="pick-link" href="?{("q=" + e(q)) if q else ""}"{" aria-current=\"true\"" if not stage else ""}>All <span class="num">{len(rows)}</span></a>']
    chips += [f'<a class="pick-link" href="?stage={e(s["key"])}{("&amp;q=" + e(q)) if q else ""}"'
              f'{" aria-current=\"true\"" if stage == s["key"] else ""}><span class="sw st-{e(s["key"])}" aria-hidden="true"></span>'
              f'{e(s["label"])} <span class="num">{s["count"]}</span></a>' for s in leads.get("stages") or [] if s["count"]]
    table = "".join(f"""
<tr data-lead-row="{e(r['id'])}" data-stage="{e(r['stage'])}"{' aria-current="true"' if r['id'] == selected else ''}>
  <td><a href="?lead={e(r['id'])}{('&amp;stage=' + e(stage)) if stage else ''}#lead"><b>{e(r['company'])}</b></a><span class="muted small block">{e(r['role'])}</span></td>
  <td><span class="sw st-{e(r['stage'])}" aria-hidden="true"></span>{e(r['stage'].capitalize())}{' ' + chip('You', 'warn') if r['waiting_on_you'] else ''}</td>
  <td class="hide-sm">{e(r['next_step']) or '<span class="muted">–</span>'}</td>
  <td class="hide-sm num muted small">{e(clock.day(r['last_activity_at'])) if r['last_activity_at'] else '–'}</td>
</tr>""" for r in shown)
    chosen = next((r for r in rows if r["id"] == selected), None)
    if not leads.get("available"):
        body = ('<div class="empty"><p class="empty-title">The lead tracker isn\'t connected yet.</p>'
                '<p class="muted">It reads job emails in Gmail (read-only) and keeps this list up to date.</p>'
                '<a class="btn small" href="/valr/settings#google">Check Google</a></div>')
    else:
        body = f"""
<form class="filters" method="get" action="/valr/leads" role="search">
  <div class="chips-row">{''.join(chips)}</div>
  {f'<input type="hidden" name="stage" value="{e(stage)}">' if stage else ''}
  <label class="search"><span class="sr-only">Search leads</span>
    <input type="search" name="q" value="{e(q)}" placeholder="Search company, role or person" data-lead-search></label>
</form>
<div class="leads-layout">
  <div class="table-wrap"><table class="leads-table">
    <thead><tr><th scope="col">Company</th><th scope="col">Stage</th><th scope="col" class="hide-sm">Next step</th><th scope="col" class="hide-sm">Last activity</th></tr></thead>
    <tbody>{table or '<tr><td colspan="4" class="muted">No leads match.</td></tr>'}</tbody>
  </table></div>
  {lead_card(chosen, clock) if chosen else ''}
</div>"""
    return page_head("Leads", 'Every company <span class="dim">in play.</span>') + hero(leads, compact=True) + body
