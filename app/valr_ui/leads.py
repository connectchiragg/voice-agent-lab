"""Leads: the globe (valr-leads' static/leads-globe.js; the lite globe as fallback), stage
filters and a search box (in the URL: ?stage=&q=&lead=&status=expired), and the list by
temperature. A selected lead (?lead=) opens its card: temperature and why, people, the
latest timeline, and the corrections (stage, merge, dismiss) that post to
/valr/api/leads/<id>/override. Data: app/valr_ui/contracts.leads()."""
from typing import Any

from app.company_names import NOT_IDENTIFIED

from app.valr_page import Clock, chip, e, link
from app.valr_ui.home import hero
from app.valr_ui.shell import page_head

STAGE_TONE = {"sourced": "muted", "applied": "muted", "screening": "info", "interviewing": "accent", "offer": "ok",
              "closed": "muted", "other": "muted"}
RAW_STAGE = {"sourced": "Sourced", "applied": "Applied", "screening": "Screening", "interviewing": "Interviewing",
             "offer": "Offer", "closed-won": "Won", "closed-lost": "Not moving forward", "ghosted": "Gone quiet"}
KIND = {"email": "Email", "calendar": "Interview", "recording": "Recording", "evaluation": "Scored"}
STOPS = ((0, (0x3b, 0x7b, 0xff)), (35, (0x8f, 0xd0, 0xff)), (65, (0xff, 0xc4, 0x6b)), (100, (0xff, 0x6a, 0x2b)))


def temp_color(t: float | None) -> str:
    """Cold blue -> hot orange, the same stops as static/leads-globe.js."""
    x = max(0.0, min(100.0, float(t or 0)))
    for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
        if x <= b:
            f = (x - a) / (b - a)
            return "rgb({},{},{})".format(*(round(u + (v - u) * f) for u, v in zip(ca, cb)))
    return "rgb(255,106,43)"


def temperature(lead: dict[str, Any]) -> str:
    t = lead.get("temperature")
    if t is None:
        return '<span class="muted">–</span>'
    return (f'<span class="temp-num num"><span class="temp-dot" style="background:{temp_color(t)}" aria-hidden="true"></span>'
            f'{int(t)}</span>')


def matches(lead: dict[str, Any], stage: str, q: str) -> bool:
    if stage and lead["stage"] != stage:
        return False
    if q:
        hay = " ".join([lead["company"], lead["role"], lead["next_step"], lead["location"], lead["domain"],
                        *(c["name"] + " " + c["email"] for c in lead["contacts"])]).lower()
        return q.lower() in hay
    return True


def company_how(lead: dict[str, Any]) -> str:
    """"Company: Zinnov · how we know: attendee domain zinnov.com", folded small."""
    how = lead.get("company_how") or ("not identified yet: set it below" if lead["company"] == NOT_IDENTIFIED else "")
    if not how:
        return ""
    return (f'<details class="how small"><summary class="muted">Company: {e(lead["company"])} · how we know</summary>'
            f'<p class="muted small">{linkify(how)}</p></details>')


def linkify(how: str) -> str:
    """The "found via web: <url>" citation as a link; the rest as text."""
    head, sep, url = how.partition("found via web: ")
    if sep and url.startswith("https://"):
        return e(head) + "found via web: " + link(url.split()[0])
    return e(how)


def corrections(lead: dict[str, Any], others: list[dict[str, Any]], stages: list[str]) -> str:
    """The owner's corrections (static/valr-app.js posts them with the CSRF token)."""
    options = "".join(f'<option value="{e(s)}"{" selected" if lead["stage_manual"] and lead["stage_raw"] == s else ""}>'
                      f'{e(RAW_STAGE.get(s, s))}</option>' for s in stages)
    into = "".join(f'<option value="{e(o["id"])}">{e(o["company"])}</option>'
                   for o in sorted(others, key=lambda o: o["company"].lower()) if o["id"] != lead["id"])
    merge = (f'<div class="fix-row"><label class="sr-only" for="fix-into">Merge into</label>'
             f'<select id="fix-into" data-fix-into>{into}</select>'
             '<button class="btn small" type="button" data-fix="merge">Merge into</button></div>' if into else "")
    return f"""
{company_how(lead)}
<details class="fix" data-lead-fix="{e(lead['id'])}" data-company="{e(lead['company'])}">
  <summary class="label">Correct this lead</summary>
  <div class="fix-row"><label class="sr-only" for="fix-company">Company</label>
    <input id="fix-company" data-fix-company maxlength="80" autocomplete="off" placeholder="Company name"
           value="{'' if lead['company'] == NOT_IDENTIFIED else e(lead['company'])}">
    <button class="btn small" type="button" data-fix="rename">Set company</button></div>
  <div class="fix-row"><label class="sr-only" for="fix-stage">Stage</label>
    <select id="fix-stage" data-fix-stage><option value=""{'' if lead['stage_manual'] else ' selected'}>Automatic stage</option>{options}</select>
    <button class="btn small" type="button" data-fix="stage">Set stage</button></div>
  {merge}
  <div class="fix-row"><button class="btn small danger" type="button" data-fix="dismiss">Dismiss lead</button>
    <span class="muted small">Hidden for good, with its email threads.</span></div>
</details>"""


def timeline_item(t: dict[str, Any], clock: Clock) -> str:
    links = " ".join(x for x in (
        link(t["url"], "Gmail" if t["kind"] == "email" else "Recording") if t["url"] else "",
        f'<a href="{e(t["detail_url"])}">Interview detail</a>' if t["detail_url"] else "") if x)
    return (f'<li class="tl tl-{e(t["kind"])}"><p class="label">{e(KIND.get(t["kind"], t["kind"]))}{" · you" if t["you"] else ""}'
            f'{(" · " + clock.when(t["at"])) if t["at"] else ""}</p><p class="tl-title">{e(t["title"])}'
            + (f' {chip(t["cue"], "ok" if t["cue"] == "positive" else "bad")}' if t["cue"] else "") + "</p>"
            + (f'<p class="small muted">{e(t["summary"])}</p>' if t["summary"] else "")
            + (f'<p class="small">{links}</p>' if links else "") + "</li>")


def lead_card(lead: dict[str, Any], clock: Clock, leads: dict[str, Any]) -> str:
    people = "".join(f'<li><span class="who">{e(c["name"])}</span> <span class="muted small">{e(c["role"])}</span>'
                     + (f' <a class="mono small email" href="mailto:{e(c["email"])}">{e(c["email"])}</a>' if c["email"] else "")
                     + "</li>" for c in lead["contacts"])
    parts = "".join(
        f'<li><span class="small">{e(p["name"])}</span><span class="tp-bar"><i style="width:{(p["points"] / p["max"] * 100) if p["max"] else 0:.0f}%"></i></span>'
        f'<span class="tp-val num">{p["points"]:g}<span class="muted">/{p["max"]:g}</span></span><span class="tp-why muted small">{e(p["why"])}</span></li>'
        for p in lead["parts"])
    timeline = "".join(timeline_item(t, clock) for t in lead["timeline"])
    stage = lead["stage_label"] + (" (set by you)" if lead["stage_manual"] else "")
    nxt = (f'<p><b>Next:</b> {e(lead["next_step"])}{(" · " + clock.when(lead["next_step_at"])) if lead["next_step_at"] else ""}</p>'
           if lead["next_step"] else "")
    return f"""
<section class="card lead-card" id="lead" aria-labelledby="lead-title">
  <div class="card-row"><h2 id="lead-title" class="card-title">{e(lead['company'])}</h2><a class="close" href="?" aria-label="Close">✕</a></div>
  <p class="muted">{e(lead['role'] or 'Role unknown')}{(' · ' + e(lead['domain'])) if lead['domain'] else ''}</p>
  <p>{chip(stage, STAGE_TONE[lead['stage']])} {chip('Waiting on you', 'warn') if lead['waiting_on_you'] else ''}
    {chip('Expired', 'muted') if lead['status'] == 'expired' else ''}</p>
  {nxt}
  <p class="small">Temperature {temperature(lead)}<span class="muted">/100</span></p>
  {f'<details class="fold"><summary class="small">Why this temperature</summary><ul class="temp-parts">{parts}</ul></details>' if parts else ''}
  {f'<h3 class="h-sm">People</h3><ul class="people plain">{people}</ul>' if people else ''}
  {f'<h3 class="h-sm">Latest</h3><ol class="timeline">{timeline}</ol>' if timeline else ''}
  <p class="muted small">{('Last activity ' + clock.when(lead['last_activity_at'])) if lead['last_activity_at'] else ''}{(' · from ' + e(lead['source'])) if lead['source'] else ''}</p>
  {f'<p>{link(lead["url"], "Open the email thread")}</p>' if lead['url'] else ''}
  {corrections(lead, leads.get('leads') or [], leads.get('raw_stages') or list(RAW_STAGE))}
</section>"""


def gmail_line(leads: dict[str, Any], clock: Clock) -> str:
    g = leads.get("gmail") or {}
    if not g.get("enabled"):
        state = "Gmail reading is off"
    elif g.get("watch") == "active":
        state = "Gmail: live"
    else:
        state = "Gmail: checked every 30 minutes"
    synced = f" · last checked {clock.when(g['last_sync_at'])}" if g.get("last_sync_at") else ""
    error = f' {chip("Last check failed", "bad")}' if g.get("last_error") else ""
    return (f'<div class="leads-tools"><span class="muted small">{e(state)}{synced}</span>{error}'
            '<button class="btn small" type="button" data-leads-sync>Check Gmail now</button></div>')


def page(view: dict[str, Any], clock: Clock, leads: dict[str, Any], stage: str, q: str, selected: str,
         status: str = "") -> str:
    expired = status == "expired"
    rows = (leads.get("expired") if expired else leads.get("leads")) or []
    stage = stage if stage in {s["key"] for s in leads.get("stages") or []} else ""
    shown = [r for r in rows if matches(r, stage, q)]
    qs = ("&amp;q=" + e(q)) if q else ""
    chips = [f'<a class="pick-link" href="?{("q=" + e(q)) if q else ""}"{" aria-current=\"true\"" if not stage and not expired else ""}>'
             f'Active <span class="num">{len(leads.get("leads") or [])}</span></a>']
    chips += [f'<a class="pick-link" href="?stage={e(s["key"])}{qs}"'
              f'{" aria-current=\"true\"" if stage == s["key"] and not expired else ""}><span class="sw st-{e(s["key"])}" aria-hidden="true"></span>'
              f'{e(s["label"])} <span class="num">{s["count"]}</span></a>' for s in leads.get("stages") or [] if s["count"]]
    if leads.get("expired"):
        chips.append(f'<a class="pick-link" href="?status=expired{qs}"{" aria-current=\"true\"" if expired else ""}>'
                     f'Expired <span class="num">{len(leads["expired"])}</span></a>')
    keep = ("&amp;status=expired" if expired else "") + (("&amp;stage=" + e(stage)) if stage else "")
    table = "".join(f"""
<tr data-lead-row="{e(r['id'])}" data-stage="{e(r['stage'])}"{' class="expired"' if r['status'] == 'expired' else ''}{' aria-current="true"' if r['id'] == selected else ''}>
  <td><a href="?lead={e(r['id'])}{keep}#lead"><b>{e(r['company'])}</b></a><span class="muted small block">{e(r['role'])}</span></td>
  <td>{temperature(r)}</td>
  <td><span class="sw st-{e(r['stage'])}" aria-hidden="true"></span>{e(r['stage_label'])}{' ' + chip('You', 'warn') if r['waiting_on_you'] else ''}</td>
  <td class="hide-sm">{e(r['next_step']) or '<span class="muted">–</span>'}</td>
  <td class="hide-sm num muted small">{e(clock.day(r['last_activity_at'])) if r['last_activity_at'] else '–'}</td>
</tr>""" for r in shown)
    everyone = (leads.get("leads") or []) + (leads.get("expired") or [])
    chosen = next((r for r in everyone if r["id"] == selected), None)
    if not leads.get("available"):
        body = ('<div class="empty"><p class="empty-title">The lead tracker isn\'t connected yet.</p>'
                '<p class="muted">It reads job emails in Gmail (read-only) and keeps this list up to date.</p>'
                '<a class="btn small" href="/valr/settings#google">Check Google</a></div>')
    else:
        body = f"""
{gmail_line(leads, clock)}
<form class="filters" method="get" action="/valr/leads" role="search">
  <div class="chips-row">{''.join(chips)}</div>
  {f'<input type="hidden" name="stage" value="{e(stage)}">' if stage else ''}{'<input type="hidden" name="status" value="expired">' if expired else ''}
  <label class="search"><span class="sr-only">Search leads</span>
    <input type="search" name="q" value="{e(q)}" placeholder="Search company, role or person" data-lead-search></label>
</form>
<div class="leads-layout">
  <div class="table-wrap"><table class="leads-table">
    <thead><tr><th scope="col">Company</th><th scope="col">Temp.</th><th scope="col">Stage</th><th scope="col" class="hide-sm">Next step</th><th scope="col" class="hide-sm">Last activity</th></tr></thead>
    <tbody>{table or '<tr><td colspan="5" class="muted">No leads match.</td></tr>'}</tbody>
  </table></div>
  {lead_card(chosen, clock, leads) if chosen else ''}
</div>"""
    return page_head("Leads", 'Every company <span class="dim">in play, by temperature.</span>') + hero(leads, compact=True) + body
