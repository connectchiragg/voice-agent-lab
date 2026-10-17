"""Leads: every company and whose move it is.

Three tabs (Your move · Waiting on them · All, with counts), a stage dropdown and a search
box, all in the URL (?tab=&stage=&q=&lead=&closed=1; ?band= and ?status=expired still
work). Closed and expired leads appear only under All with "Show closed". Each row: the
company, its temperature as a word, the stage, the next step or the waiting line, and the
last activity. A selected lead (?lead=) opens its panel: whose move it is with the thread,
the next step, people and a timeline in plain words; ⋯ holds the corrections (company,
stage, merge, dismiss) that post to /valr/api/leads/<id>/override. The globe lives on Home.
Data: app/valr_ui/contracts.leads()."""
from typing import Any

from app.company_names import NOT_IDENTIFIED
from app.valr_page import Clock, chip, e, link, mail_source
from app.valr_ui import wording
from app.valr_ui.home import band_sw
from app.valr_ui.shell import empty_state, icon, menu, menu_item, page_head, sheet

STAGE_TONE = {"sourced": "muted", "applied": "muted", "screening": "info", "interviewing": "accent", "final": "accent",
              "offer": "ok", "ghosted": "muted", "closed": "muted", "other": "muted"}
RAW_STAGE = {"sourced": "Sourced", "applied": "Applied", "screening": "Screening", "interviewing": "Interviewing",
             "final": "Final stages", "offer": "Offer", "closed-won": "Won", "closed-lost": "Not moving forward", "ghosted": "Gone quiet"}
KIND = {"email": "Email", "calendar": "Interview", "recording": "Recording", "evaluation": "Review"}
BAND_LABEL = {"hot": "Hot", "warm": "Warm", "cool": "Cool", "cold": "Cold", "closed": "Closed"}
TABS = [("you", "Your move"), ("them", "Waiting on them"), ("all", "All")]


def temperature(lead: dict[str, Any]) -> str:
    """The band as a coloured word ("Hot"); the score only sets the swatch's shade."""
    band = lead.get("band") or "cold"
    return f'<span class="band-pill b-{e(band)}">{band_sw(band, lead.get("temperature"))}{e(BAND_LABEL.get(band, band))}</span>'


def court_line(lead: dict[str, Any]) -> str:
    who = lead.get("waiting_on") or "nobody"
    if who == "you" and lead.get("waiting_reason"):
        return f'<span class="court you">{e(wording.ask_line(lead["waiting_reason"]))}</span>'
    if who == "them" and lead.get("waiting_reason"):
        return f'<span class="court them">{e(wording.waiting_line(lead["waiting_reason"]))}</span>'
    return ""


def next_line(lead: dict[str, Any], clock: Clock) -> str:
    """"Interview: Arcworks · Panel" on Sat -> "Panel · Sat 18 Oct"."""
    if not lead.get("next_step"):
        return ""
    text = wording.without_company(lead["next_step"], lead["company"])
    when = clock.local(lead["next_step_at"]).strftime("%a %d %b").replace(" 0", " ") if lead.get("next_step_at") else ""
    return " · ".join(x for x in (text, when) if x)


def unread_dot(n: int) -> str:
    if not n:
        return ""
    return (f'<span class="unread-dot" data-unread-lead title="{n} unread email{"s" if n != 1 else ""}">'
            f'<span class="sr-only">{n} unread email{"s" if n != 1 else ""}: </span></span>')


def matches(lead: dict[str, Any], stage: str, q: str, band: str = "") -> bool:
    if stage and lead["stage"] != stage:
        return False
    if band and band != "closed" and lead["band"] != band:
        return False
    if q:
        hay = " ".join([lead["company"], lead["role"], lead["next_step"], lead["location"], lead["domain"],
                        *(c["name"] + " " + c["email"] for c in lead["contacts"])]).lower()
        return q.lower() in hay
    return True


def linkify(how: str) -> str:
    """The "found via web: <url>" citation as a link; the rest as text."""
    head, sep, url = how.partition("found via web: ")
    if sep and url.startswith("https://"):
        return e(head) + "found via web: " + link(url.split()[0])
    return e(how)


def corrections(lead: dict[str, Any], others: list[dict[str, Any]], stages: list[str]) -> str:
    """The owner's corrections, in a sheet (static/valr-app.js posts them with the CSRF token)."""
    options = "".join(f'<option value="{e(s)}"{" selected" if lead["stage_manual"] and lead["stage_raw"] == s else ""}>'
                      f'{e(RAW_STAGE.get(s, s))}</option>' for s in stages)
    into = "".join(f'<option value="{e(o["id"])}">{e(o["company"])}</option>'
                   for o in sorted(others, key=lambda o: o["company"].lower()) if o["id"] != lead["id"])
    merge = (f'<div class="fix-block"><label class="field-label" for="fix-into">Merge into another company</label>'
             f'<input class="input sm" type="search" data-merge-search placeholder="Search companies" aria-label="Search companies to merge into">'
             f'<div class="field-row"><select class="input" id="fix-into" data-fix-into>{into}</select>'
             '<button class="btn" type="button" data-fix="merge">Merge</button></div></div>' if into else "")
    body = f"""
<div class="fix-form" data-lead-fix="{e(lead['id'])}" data-company="{e(lead['company'])}">
  <div class="fix-block"><label class="field-label" for="fix-company">Company</label>
    <div class="field-row"><input class="input" id="fix-company" data-fix-company maxlength="80" autocomplete="off" placeholder="Company name"
           value="{'' if lead['company'] == NOT_IDENTIFIED else e(lead['company'])}">
    <button class="btn" type="button" data-fix="rename">Save</button></div></div>
  <div class="fix-block"><label class="field-label" for="fix-stage">Stage</label>
    <div class="field-row"><select class="input" id="fix-stage" data-fix-stage><option value=""{'' if lead['stage_manual'] else ' selected'}>Automatic</option>{options}</select>
    <button class="btn" type="button" data-fix="stage">Set stage</button></div></div>
  {merge}
  <div class="fix-block danger-zone"><button class="btn danger" type="button" data-fix="dismiss">Dismiss this lead</button>
    <span class="field-help">Hidden for good, with its email threads.</span></div>
</div>"""
    return sheet("lead-fix", "Fix this lead", body)


def timeline_item(t: dict[str, Any], lead: dict[str, Any], clock: Clock) -> str:
    """One touch in plain words: the email subject or a summary, never a classifier label."""
    title = wording.without_company(t["title"], lead["company"]) or t["summary"]
    summary = t["summary"] if t["summary"] and t["summary"] != title else ""
    links = " ".join(x for x in (
        link(t["url"], mail_source(t["url"]) if t["kind"] == "email" else "Recording") if t["url"] else "",
        f'<a href="{e(t["detail_url"])}">Interview</a>' if t["detail_url"] else "") if x)
    meta = " · ".join(x for x in (KIND.get(t["kind"], ""), "You" if t["you"] else "",
                                  f'{e(clock.day(t["at"]))}, {clock.time(t["at"], zone=False)}' if t["at"] else "") if x)
    return (f'<li class="tl tl-{e(t["kind"])}"><span class="tl-dot" aria-hidden="true"></span><p class="tl-meta">{meta}</p>'
            f'<p class="tl-title">{e(title)}</p>'
            + (f'<p class="tl-sum">{e(summary)}</p>' if summary else "")
            + (f'<p class="tl-links">{links}</p>' if links else "") + "</li>")


def court_block(lead: dict[str, Any]) -> str:
    """Whose move it is, with the thread to answer in."""
    who = lead.get("waiting_on") or "nobody"
    if who == "you":
        gmail = (f'<a class="btn sm" href="{e(lead["url"])}" target="_blank" rel="noopener noreferrer">{icon("mail", 14)}Open the thread</a>'
                 if lead.get("url") else "")
        return (f'<div class="court-box you"><p class="court-head">Your move</p>'
                f'<p class="court-text">{e(wording.ask_line(lead.get("waiting_reason") or ""))}</p>{gmail}</div>')
    if who == "them" and lead.get("waiting_reason"):
        return f'<div class="court-box them"><p class="court-text">{e(wording.waiting_line(lead["waiting_reason"]))}</p></div>'
    return ""


def lead_card(lead: dict[str, Any], clock: Clock, leads: dict[str, Any], keep: str) -> str:
    people = "".join(f'<li class="person"><span class="avatar" aria-hidden="true">{e((c["name"][:1] or "?").upper())}</span>'
                     f'<span class="person-id"><span class="who">{e(c["name"])}</span>'
                     + (f'<span class="person-role">{e(c["role"].capitalize())}</span>' if c["role"] and c["role"] != "other" else "")
                     + (f'<a class="email" href="mailto:{e(c["email"])}">{e(c["email"])}</a>' if c["email"] else "")
                     + "</span></li>" for c in lead["contacts"])
    timeline = "".join(timeline_item(t, lead, clock) for t in lead["timeline"])
    stage = lead["stage_label"]
    nxt = next_line(lead, clock)
    actions = menu([menu_item("Fix company", "pencil", attrs=' data-sheet="lead-fix" data-focus="#fix-company"'),
                    menu_item("Set stage", "arrow-up-right", attrs=' data-sheet="lead-fix" data-focus="#fix-stage"'),
                    menu_item("Merge", "users", attrs=' data-sheet="lead-fix" data-focus="[data-merge-search]"'),
                    menu_item("Dismiss", "x", attrs=' data-sheet="lead-fix" data-focus="[data-fix=dismiss]"')],
                   label=f"Fix {lead['company']}")
    unknown = lead["company"] == NOT_IDENTIFIED
    close = f"?{keep.removeprefix('&amp;')}" if keep else "?"
    return f"""
<section class="card lead-panel" id="lead" aria-labelledby="lead-title">
  <div class="lead-head"><div><h2 id="lead-title" class="lead-title">{e(lead['company'])}</h2>
    {f'<p class="lead-sub">{e(lead["role"])}</p>' if lead['role'] else ''}</div>
    <div class="lead-tools">{actions}<a class="icon-btn" href="{close}" aria-label="Close">{icon('x', 18)}</a></div></div>
  {f'<p class="inline-fix">{icon("help", 16)}<span>We couldn\'t tell which company this is.</span><button class="link-btn" type="button" data-sheet="lead-fix">Set company</button></p>' if unknown else ''}
  <p class="lead-tags">{temperature(lead)}{chip(stage, STAGE_TONE.get(lead['stage'], 'muted'))}{chip('Expired', 'muted') if lead['status'] == 'expired' else ''}</p>
  {court_block(lead)}
  {f'<p class="lead-next"><span class="label">Next</span>{e(nxt)}</p>' if nxt else ''}
  {f'<h3 class="sub-title">People</h3><ul class="people">{people}</ul>' if people else ''}
  {f'<h3 class="sub-title">Timeline</h3><ol class="timeline">{timeline}</ol>' if timeline else ''}
</section>
{corrections(lead, leads.get('leads') or [], leads.get('raw_stages') or list(RAW_STAGE))}"""


def page(view: dict[str, Any], clock: Clock, leads: dict[str, Any], stage: str, q: str, selected: str,
         status: str = "", band: str = "", waiting: str = "", tab: str = "", closed: bool = False,
         unread: dict[str, int] | None = None) -> str:
    """`unread`: lead id -> unread emails (a red dot on the row until its card is opened)."""
    unread = unread or {}
    sync = menu_item("Check email now", "refresh", attrs=" data-leads-sync")
    head = page_head("Leads", actions=menu([sync]))
    if not leads.get("available"):
        return head + empty_state("inbox", "Connect Google and VALR will find your job emails.", "",
                                  '<a class="btn primary" href="/oauth/google/start">Connect Google</a>')
    live = leads.get("leads") or []
    expired = status == "expired"
    band = band if band in BAND_LABEL else ""
    stage = stage if stage in {s["key"] for s in leads.get("stages") or []} else ""
    counts = {"you": sum(1 for r in live if r["waiting_on"] == "you"),
              "them": sum(1 for r in live if r["waiting_on"] == "them"), "all": len(live)}
    if waiting == "you":
        tab = "you"
    picked = next((r for r in live if r["id"] == selected), None)
    if tab not in counts and picked and not (stage or q or band or expired or closed):
        tab = picked["waiting_on"] if picked["waiting_on"] in ("you", "them") else "all"
    if tab not in counts:
        tab = "all" if (stage or q or band or expired or closed or not counts["you"]) else "you"
    if band == "closed":
        closed = True
    if expired:
        rows = leads.get("expired") or []
    elif tab == "all":
        rows = live + ((leads.get("closed") or []) + (leads.get("expired") or []) if closed else [])
    else:
        rows = [r for r in live if r["waiting_on"] == tab]
    shown = [r for r in rows if matches(r, stage, q, band)]
    qs = ("&amp;q=" + e(q)) if q else ""
    keep = (f"&amp;tab={tab}" + ("&amp;status=expired" if expired else "") + (f"&amp;stage={e(stage)}" if stage else "")
            + (f"&amp;band={e(band)}" if band else "") + ("&amp;closed=1" if closed and tab == "all" else "") + qs)
    tab_links = "".join(
        f'<a href="?tab={k}{qs}"{" aria-current=\"page\"" if k == tab and not expired else ""}>{e(label)} '
        f'<span class="count">{counts[k]}</span></a>' for k, label in TABS)
    stage_opts = "".join(f'<option value="{e(s["key"])}"{" selected" if s["key"] == stage else ""}>{e(s["label"])} ({s["count"]})</option>'
                         for s in leads.get("stages") or [] if s["count"] or s["key"] == stage)
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in
                     (("tab", tab), ("band", band), ("status", "expired" if expired else "")) if v)
    toggle = (f'<label class="switch"><input type="checkbox" name="closed" value="1" data-autosubmit{" checked" if closed else ""}>'
              f'<span class="switch-ui" aria-hidden="true"></span>Show closed</label>' if tab == "all" and not expired else "")
    band_note = (f'<p class="filter-note">{band_sw(band)}Showing {e(BAND_LABEL[band])} leads · <a href="?tab=all{qs}">Show all</a></p>'
                 if band else "")
    expired_note = (f'<p class="filter-note">Showing expired leads · <a href="?tab=all">Back to all</a></p>' if expired else "")
    gmail = leads.get("gmail") or {}
    broken = (f'<p class="inline-fix warn">{icon("alert", 16)}<span>The last email check failed.</span>'
              '<button class="link-btn" type="button" data-leads-sync>Check again</button></p>' if gmail.get("last_error") else "")
    body_rows = "".join(f"""
<tr data-lead-row="{e(r['id'])}" data-stage="{e(r['stage'])}" data-band="{e(r['band'])}"{' class="is-faded"' if r['bucket'] != 'live' else ''}{' aria-current="true"' if r['id'] == selected else ''}>
  <td class="c-company"><a class="row-anchor" href="?lead={e(r['id'])}{keep}#lead">{unread_dot(unread.get(r['id'], 0))}{e(r['company'])}</a>{f'<span class="cell-sub">{e(r["role"])}</span>' if r['role'] else ''}{court_line(r)}</td>
  <td class="c-temp">{temperature(r)}</td>
  <td class="c-stage"><span class="sw st-{e(r['stage'])}" aria-hidden="true"></span>{e(r['stage_label'])}</td>
  <td class="c-next">{e(next_line(r, clock))}</td>
  <td class="c-when num">{e(clock.day(r['last_activity_at'])) if r['last_activity_at'] else ''}</td>
</tr>""" for r in shown)
    everyone = live + (leads.get("closed") or []) + (leads.get("expired") or [])
    chosen = next((r for r in everyone if r["id"] == selected), None)
    empty_row = {"you": "Nobody's waiting on you.", "them": "You're not waiting on anyone."}.get(tab, "No leads match.")
    return f"""{head}
{broken}
<form class="filters" method="get" action="/valr/leads" role="search" data-filters>
  <nav class="tabs" aria-label="Leads">{tab_links}</nav>
  <div class="filter-tools">
    {hidden}
    <label class="select-wrap"><span class="sr-only">Stage</span><select class="input sm" name="stage" data-autosubmit>
      <option value="">All stages</option>{stage_opts}</select></label>
    <label class="search"><span class="sr-only">Search leads</span>{icon('search', 16)}
      <input class="input sm" type="search" name="q" value="{e(q)}" placeholder="Search company, role or person" data-lead-search></label>
    {toggle}
    <noscript><button class="btn sm" type="submit">Apply</button></noscript>
  </div>
</form>
{band_note}{expired_note}
<div class="leads-layout{' has-panel' if chosen else ''}">
  <div class="table-card card"><table class="leads-table">
    <thead><tr><th scope="col">Company</th><th scope="col">Temperature</th><th scope="col">Stage</th><th scope="col" class="c-next">Next step</th><th scope="col" class="c-when">Last activity</th></tr></thead>
    <tbody>{body_rows or f'<tr class="empty-row"><td colspan="5">{e(empty_row)}</td></tr>'}</tbody>
  </table></div>
  {lead_card(chosen, clock, leads, keep) if chosen else ''}
</div>"""
