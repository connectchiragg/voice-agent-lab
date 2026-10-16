"""Leads: the globe (valr-leads' static/leads-globe.js; the lite globe as fallback), band,
stage and "waiting on you" filters and a search box (in the URL: ?band=&stage=&waiting=you&q=
&lead=&status=expired), and the list by temperature with a band pill. A selected lead (?lead=) opens its card: temperature and why, people, the
latest timeline, and the corrections (stage, merge, dismiss) that post to
/valr/api/leads/<id>/override. Data: app/valr_ui/contracts.leads()."""
from typing import Any

from app.company_names import NOT_IDENTIFIED

from app.valr_page import Clock, chip, e, link
from app.valr_ui.home import band_sw, hero
from app.valr_ui.shell import page_head

STAGE_TONE = {"sourced": "muted", "applied": "muted", "screening": "info", "interviewing": "accent", "offer": "ok",
              "closed": "muted", "other": "muted"}
RAW_STAGE = {"sourced": "Sourced", "applied": "Applied", "screening": "Screening", "interviewing": "Interviewing",
             "offer": "Offer", "closed-won": "Won", "closed-lost": "Not moving forward", "ghosted": "Gone quiet"}
KIND = {"email": "Email", "calendar": "Interview", "recording": "Recording", "evaluation": "Scored"}
BAND_LABEL = {"hot": "Hot", "warm": "Warm", "cool": "Cool", "cold": "Cold", "closed": "Closed"}


def temperature(lead: dict[str, Any]) -> str:
    """The band pill: "● Hot 82" (the swatch's shade is the score's place inside the band)."""
    t = lead.get("temperature")
    band = lead.get("band") or "cold"
    score = f'<span class="num">{int(t)}</span>' if t is not None else '<span class="num muted">–</span>'
    return f'<span class="band-pill b-{e(band)}">{band_sw(band, t)}{e(BAND_LABEL.get(band, band))} {score}</span>'


def court_line(lead: dict[str, Any], tag: str = "span") -> str:
    who = lead.get("waiting_on") or "nobody"
    if who == "nobody" or not lead.get("waiting_reason"):
        return ""
    return f'<{tag} class="court {e(who)}">{"Your move: " if who == "you" else ""}{e(lead["waiting_reason"])}</{tag}>'


def matches(lead: dict[str, Any], stage: str, q: str, band: str = "", waiting: str = "") -> bool:
    if stage and lead["stage"] != stage:
        return False
    if band and band != "closed" and lead["band"] != band:
        return False
    if waiting == "you" and lead["waiting_on"] != "you":
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
  {court_block(lead)}
  {nxt}
  <p class="small">Temperature {temperature(lead)}<span class="muted"> /100 · {e(BAND_MEANING.get(lead['band'], ''))}</span></p>
  {f'<details class="fold"><summary class="small">Why this temperature</summary><ul class="temp-parts">{parts}</ul></details>' if parts else ''}
  {f'<h3 class="h-sm">People</h3><ul class="people plain">{people}</ul>' if people else ''}
  {f'<h3 class="h-sm">Latest</h3><ol class="timeline">{timeline}</ol>' if timeline else ''}
  <p class="muted small">{('Last activity ' + clock.when(lead['last_activity_at'])) if lead['last_activity_at'] else ''}{(' · from ' + e(lead['source'])) if lead['source'] else ''}</p>
  {f'<p>{link(lead["url"], "Open the email thread")}</p>' if lead['url'] else ''}
  {corrections(lead, leads.get('leads') or [], leads.get('raw_stages') or list(RAW_STAGE))}
</section>"""


BAND_MEANING = {"hot": "an active process, next step soon", "warm": "engaged recently",
                "cool": "applied or sourced, waiting", "cold": "quiet or ghosted", "closed": "won or not moving forward"}


def court_block(lead: dict[str, Any]) -> str:
    """Whose move it is, with the thread to answer in."""
    who = lead.get("waiting_on") or "nobody"
    head = {"you": "Your move", "them": "Their move", "nobody": "Nobody's move"}[who if who in ("you", "them") else "nobody"]
    gmail = link(lead["url"], "Open the thread") if who == "you" and lead.get("url") else ""
    return (f'<p class="court-line {e(who)}"><b>{head}</b><span>{e(lead.get("waiting_reason") or "")}</span>'
            f'{gmail}</p>')


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


def chip_link(href: str, label: str, count: int, current: bool, lead: str = "") -> str:
    return (f'<a class="pick-link" href="?{href}"{" aria-current=\"true\"" if current else ""}>{lead}{e(label)} '
            f'<span class="num">{count}</span></a>')


def page(view: dict[str, Any], clock: Clock, leads: dict[str, Any], stage: str, q: str, selected: str,
         status: str = "", band: str = "", waiting: str = "") -> str:
    expired = status == "expired"
    band = band if band in BAND_LABEL else ""
    waiting = "you" if waiting == "you" else ""
    closed = band == "closed" and not expired
    rows = (leads.get("expired") if expired else leads.get("closed") if closed else leads.get("leads")) or []
    stage = stage if stage in {s["key"] for s in leads.get("stages") or []} else ""
    shown = [r for r in rows if matches(r, stage, q, band, waiting)]
    counts = leads.get("counts") or {}
    qs = ("&amp;q=" + e(q)) if q else ""
    plain = not (stage or band or waiting or expired)
    chips = [chip_link(qs.removeprefix("&amp;"), "Live", counts.get("live", 0), plain)]
    if counts.get("waiting_on_you"):
        chips.append(chip_link(f"waiting=you{qs}", "Waiting on you", counts["waiting_on_you"], waiting == "you" and not expired))
    chips.append('<span class="chips-sep" aria-hidden="true"></span>')
    chips += [chip_link(f"band={e(b['key'])}{qs}", b["label"], b["count"], band == b["key"] and not expired, band_sw(b["key"]))
              for b in leads.get("bands") or []]
    chips.append(chip_link(f"band=closed{qs}", "Closed", counts.get("closed", 0), closed, band_sw("closed")))
    chips.append('<span class="chips-sep" aria-hidden="true"></span>')
    chips += [chip_link(f"stage={e(s['key'])}{qs}", s["label"], s["count"], stage == s["key"] and not expired,
                        f'<span class="sw st-{e(s["key"])}" aria-hidden="true"></span>')
              for s in leads.get("stages") or [] if s["count"]]
    if leads.get("expired"):
        chips.append(chip_link(f"status=expired{qs}", "Expired", len(leads["expired"]), expired))
    keep = ("&amp;status=expired" if expired else "") + (("&amp;stage=" + e(stage)) if stage else "") \
        + (("&amp;band=" + e(band)) if band else "") + ("&amp;waiting=you" if waiting else "")
    table = "".join(f"""
<tr data-lead-row="{e(r['id'])}" data-stage="{e(r['stage'])}" data-band="{e(r['band'])}"{' class="expired"' if r['status'] == 'expired' else ''}{' aria-current="true"' if r['id'] == selected else ''}>
  <td><a href="?lead={e(r['id'])}{keep}#lead"><b>{e(r['company'])}</b></a><span class="muted small block">{e(r['role'])}</span>{court_line(r)}</td>
  <td class="temp">{temperature(r)}</td>
  <td><span class="sw st-{e(r['stage'])}" aria-hidden="true"></span>{e(r['stage_label'])}{' ' + chip('You', 'warn') if r['waiting_on'] == 'you' else ''}</td>
  <td class="hide-sm">{e(r['next_step']) or '<span class="muted">–</span>'}</td>
  <td class="hide-sm num muted small">{e(clock.day(r['last_activity_at'])) if r['last_activity_at'] else '–'}</td>
</tr>""" for r in shown)
    everyone = (leads.get("leads") or []) + (leads.get("closed") or []) + (leads.get("expired") or [])
    chosen = next((r for r in everyone if r["id"] == selected), None)
    if not leads.get("available"):
        body = ('<div class="empty"><p class="empty-title">The lead tracker isn\'t connected yet.</p>'
                '<p class="muted">It reads job emails in Gmail (read-only) and keeps this list up to date.</p>'
                '<a class="btn small" href="/valr/settings#google">Check Google</a></div>')
    else:
        hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in
                         (("stage", stage), ("band", band), ("waiting", waiting), ("status", "expired" if expired else "")) if v)
        body = f"""
{gmail_line(leads, clock)}
<form class="filters" method="get" action="/valr/leads" role="search">
  <div class="chips-row">{''.join(chips)}</div>
  {hidden}
  <label class="search"><span class="sr-only">Search leads</span>
    <input type="search" name="q" value="{e(q)}" placeholder="Search company, role or person" data-lead-search></label>
</form>
<div class="leads-layout">
  <div class="table-wrap"><table class="leads-table">
    <thead><tr><th scope="col">Company</th><th scope="col">Temperature</th><th scope="col">Stage</th><th scope="col" class="hide-sm">Next step</th><th scope="col" class="hide-sm">Last activity</th></tr></thead>
    <tbody>{table or '<tr><td colspan="5" class="muted">No leads match.</td></tr>'}</tbody>
  </table></div>
  {lead_card(chosen, clock, leads) if chosen else ''}
</div>"""
    return page_head("Leads", 'Every company <span class="dim">in play, by temperature.</span>') + hero(leads, compact=True, show_closed=closed) + body
