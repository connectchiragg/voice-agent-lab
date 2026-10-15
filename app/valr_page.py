"""The VALR cockpit page: server-rendered HTML from app/valr_view.collect().

Everything is readable without JavaScript; static/valr.js adds live countdowns,
the performance chart, the action buttons and a quiet refresh. Every value is
escaped here; the chart's data travels as JSON in a non-executable script tag
with "<" escaped. No comments in the output carry data.
"""
import html
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

STATUS = {  # slot status -> (label, tone, what it means)
    "scheduled": ("Scheduled", "muted", "Research starts 60 min before the block."),
    "researching": ("Researching", "info", "R1, R2 and R3 are working now."),
    "ready": ("Ready", "ok", "The brief is written and waiting for the call."),
    "calling": ("Calling", "cyan", "VALR is dialling you now."),
    "done": ("Done", "accent", "Briefed, or the interview is over."),
    "cancelled": ("Cancelled", "bad", "Removed from the calendar."),
}
BLOCK = {  # block (briefing call) state -> (label, tone)
    "scheduled": ("Call not set up yet", "muted"), "waiting": ("Call scheduled", "info"),
    "waiting_for_call_end": ("Waiting for your call to end", "info"),
    "waiting_for_research": ("Waiting for research", "info"), "calling": ("Calling you", "cyan"),
    "briefed": ("Briefed", "ok"), "unanswered": ("Unanswered", "warn"), "missed": ("Missed", "warn"),
    "refused": ("Daily dial limit hit", "bad"), "cancelled": ("Cancelled", "muted"),
    "paused": ("Skipped: paused", "warn"),
}
PEOPLE = {
    "researched": ("researched", "ok"), "skipped_hr": ("skipped: HR / recruiting", "muted"),
    "skipped_classifier": ("skipped: not an interviewer", "muted"),
    "pending": ("not researched yet", "info"), "declined": ("declined", "muted"),
}
TREND = {"improving": ("Improving", "ok", "↑"), "flat": ("Flat", "info", "→"),
         "declining": ("Declining", "bad", "↓"), "insufficient data": ("Too few points", "muted", "·")}
RESEARCHERS = (
    ("r1", "R1", "Private context", "Your Gmail threads, Fathom recordings and notes about this company."),
    ("r2", "R2", "Interview intel", "The round format, what this company asks, and the interviewers' public work."),
    ("r3", "R3", "Company & pay", "What the company does, recent news and the market pay range (cached 7 days)."),
)


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def money(usd: float | None) -> str:
    if usd is None:
        return "–"
    return f"${usd:,.4f}" if 0 < usd < 0.01 else f"${usd:,.2f}"


def when(text: str | None, zone: ZoneInfo, fmt: str = "%a %d %b, %H:%M") -> str:
    if not text:
        return "–"
    moment = datetime.fromisoformat(text).astimezone(zone)
    return f'<time datetime="{e(text)}" data-fmt="{e(fmt)}">{e(moment.strftime(fmt))}</time>'


def pill(label: str, tone: str, title: str = "", plain: bool = False) -> str:
    hint = f' title="{e(title)}"' if title else ""
    return f'<span class="pill{" plain" if plain else ""} tone-{tone}"{hint}>{e(label)}</span>'


def link(url: str, text: str | None = None) -> str:
    if not url.startswith(("https://", "http://")):
        return e(url)
    label = text or url.split("://", 1)[1].split("/", 1)[0].removeprefix("www.")
    return (f'<a href="{e(url)}" rel="noopener noreferrer nofollow" target="_blank" '
            f'title="{e(url[:200])}">{e(label)}</a>')


def meter(used: float | None, limit: float | None, label: str, detail: str) -> str:
    share = 0.0 if not limit or used is None else min(used / limit, 1)
    tone = "bad" if share >= 0.9 else "warn" if share >= 0.7 else "ok"
    return (f'<div class="meter"><div class="meter-head"><span>{e(label)}</span>'
            f'<span class="num">{detail}</span></div>'
            f'<div class="bar" role="meter" aria-label="{e(label)}" aria-valuemin="0" '
            f'aria-valuemax="100" aria-valuenow="{share * 100:.0f}"><i class="tone-{tone}" '
            f'style="width:{share * 100:.1f}%"></i></div></div>')


# Sections.

def hero(view: dict[str, Any], zone: ZoneInfo) -> str:
    paused = view["dials_paused"]["enabled"]
    nxt = view.get("next_call")
    if not view.get("dial_env"):
        headline = "Automatic calls are off on the server"
        sub = "VALR_BRIEFING_DIAL=0: blocks are researched but never dialled."
        count = ""
    elif paused:
        headline = "Automatic calls are paused"
        sub = "Research still runs. Calls you ask for on a call still ring."
        count = ""
    elif nxt:
        headline = f"Next briefing call: {e(nxt['label']) or 'your next interview block'}"
        sub = (f"VALR calls you at {when(nxt['at'], zone, '%H:%M')} on "
               f"{when(nxt['at'], zone, '%a %d %b')} (attempt {nxt['attempt']} of 3; it retries at "
               "T-10 and T-5 if you miss it).")
        count = f'<div class="countdown num" data-countdown="{e(nxt["at"])}">–</div>'
    else:
        headline, sub, count = "No briefing calls coming up", "Nothing on the calendar in the next 8 days needs one.", ""
    checked = " checked" if not paused else ""
    return f"""
<section class="hero glass" aria-labelledby="hero-title">
  <div class="hero-main">
    <p class="eyebrow">Next up</p>
    <h2 id="hero-title">{headline}</h2>
    {count}
    <p class="muted">{sub}</p>
  </div>
  <div class="hero-actions">
    <label class="switch" title="Pause or resume the automatic T-15 / T-10 / T-5 briefing calls">
      <input type="checkbox" role="switch" id="dials-toggle" data-action="dials"{checked}
        {"disabled" if not view.get("dial_env") else ""} aria-describedby="dials-help">
      <span class="track" aria-hidden="true"><span class="thumb"></span></span>
      <span class="switch-label">Automatic calls <b data-dials-label>{"on" if not paused else "paused"}</b></span>
    </label>
    <p id="dials-help" class="muted small">Stored in the database; the call workflows check it before every dial.</p>
    <button class="btn primary" type="button" data-action="sync">Sync calendar now</button>
    <p class="action-result muted small" role="status" aria-live="polite"></p>
  </div>
</section>"""


def researcher_section(key: str, tag: str, title: str, about: str, data: dict[str, Any] | None) -> str:
    if not data or not (data.get("summary") or data.get("claims")):
        body = '<p class="muted small">Nothing yet.</p>'
    else:
        claims = "".join(f"<li>{e(c['claim'])} {('<span class=src>' + link(c['url']) + '</span>') if c['url'] else ''}</li>"
                         for c in data["claims"])
        body = (f"<p>{e(data['summary'])}</p>" if data["summary"] else "") + \
               (f"<ul class='claims'>{claims}</ul>" if claims else "")
    flag = "" if not data else pill("found" if data.get("ok") else "thin", "ok" if data.get("ok") else "warn")
    return (f'<section class="researcher r-{key}"><header><span class="tag">{tag}</span>'
            f'<h5>{e(title)}</h5>{flag}</header><p class="muted small">{e(about)}</p>{body}</section>')


def slot_card(slot: dict[str, Any], zone: ZoneInfo) -> str:
    label, tone, meaning = STATUS.get(slot["status"], (slot["status"], "muted", ""))
    people = "".join(
        f'<li class="person tone-{PEOPLE.get(p["status"], ("", "muted"))[1]}">'
        f'<span class="avatar" aria-hidden="true">{e((p["name"][:1] or "?").upper())}</span>'
        f'<span class="who">{e(p["name"])}</span><span class="what">{e(PEOPLE.get(p["status"], (p["status"],))[0])}</span></li>'
        for p in slot["interviewers"]) or '<li class="muted small">No interviewers on the invite.</li>'
    badges = [pill(label, tone, meaning)]
    if slot["cache_hits"]:
        badges.append(pill("cache: " + ", ".join(slot["cache_hits"]), "cyan",
                           "Reused research instead of paying for it again.", plain=True))
    if slot["spend_usd"]:
        badges.append(pill(money(slot["spend_usd"]), "muted", "Research spend for this interview", plain=True))
    brief = slot.get("brief")
    if brief:
        notes = "".join(
            f"<li>{e(n['fact'])}"
            + (" " + pill("sensitive", "warn", "Only said if you ask", plain=True) if n["sensitive"] else "")
            + (" " + pill("private", "info", "From your own mail or recordings", plain=True) if n["private"] else "")
            + "".join(f" <span class=src>{link(u)}</span>" for u in n["sources"]) + "</li>"
            for n in brief["notes"])
        quality = {"partial": " (partial: some researchers came back thin)",
                   "apologized": " (VALR found nothing reliable)"}.get(brief.get("quality") or "", "")
        brief_html = (f'<div class="spoken"><p class="eyebrow">What VALR will say{e(quality)}</p>'
                      f'<p>{e(brief["spoken"])}</p></div>'
                      + (f'<h5 class="notes-title">Notes for follow-up questions</h5><ul class="notes">{notes}</ul>' if notes else ""))
    else:
        brief_html = '<p class="muted">No brief yet. It is written after research, 60 minutes before the block.</p>'
    sections = "".join(researcher_section(k, t, title, about, slot["researchers"].get(k))
                       for k, t, title, about in RESEARCHERS)
    return f"""
<article class="slot" data-ref="{e(slot['ref'])}">
  <div class="slot-head">
    <div class="slot-time num">{when(slot['start'], zone, '%H:%M')}<span class="muted"> – {when(slot['end'], zone, '%H:%M')}</span></div>
    <div class="slot-title">
      <h4>{e(slot['company'] or slot['title'])}</h4>
      <p class="muted small">{e(slot['title']) if slot['company'] else ''}</p>
    </div>
    <div class="slot-badges" data-slot-status>{''.join(badges)}</div>
  </div>
  <ul class="people" aria-label="Interviewers">{people}</ul>
  <details class="briefing">
    <summary><span>Briefing</span><span class="muted small">spoken brief, notes and sources by researcher</span></summary>
    <div class="briefing-body">
      {brief_html}
      <div class="researchers">{sections}</div>
      <div class="slot-actions">
        <button class="btn small" type="button" data-action="refresh" data-ref="{e(slot['ref'])}"
          title="Run R1, R2 and R3 again now, bypassing the caches (costs up to one interview job)">Refresh research</button>
        <span class="muted small">Last updated {when(slot['updated_at'], zone)} · {slot['runs']} paid run(s)</span>
        <span class="action-result muted small" role="status" aria-live="polite"></span>
      </div>
    </div>
  </details>
</article>"""


def timeline(view: dict[str, Any], zone: ZoneInfo) -> str:
    blocks = view.get("blocks") or []
    if "timeline" in view.get("errors", []):
        body = '<p class="empty">The calendar store is unavailable right now.</p>'
    elif not blocks:
        body = ('<p class="empty">No interviews in the next 8 days. When one lands on your calendar, '
                'VALR picks it up within seconds (push) or at the 6-hourly sweep.</p>')
    else:
        days: dict[str, list[str]] = {}
        for block in blocks:
            day = datetime.fromisoformat(block["start"]).astimezone(zone).strftime("%A %d %B")
            label, tone = BLOCK.get(block["state"], (block["state"], "muted"))
            if block["paused"] and block["state"] in ("scheduled", "waiting"):
                label, tone = "Paused: no automatic call", "warn"
            n = len(block["slots"])
            days.setdefault(day, []).append(f"""
<div class="block glass">
  <div class="block-head">
    <div><p class="eyebrow">Block · {n} interview{'s' if n != 1 else ''}</p>
      <p class="block-call">Briefing call {when(block['call_at'], zone, '%H:%M')}
        <span class="muted small">(<span data-countdown="{e(block['call_at'])}" data-short>T-15</span>)</span></p></div>
    {pill(label, tone, "The briefing call for this block of back-to-back interviews")}
  </div>
  {''.join(slot_card(slot, zone) for slot in block['slots'])}
</div>""")
        body = "".join(f'<div class="day"><h3 class="day-title">{e(day)}</h3>{"".join(items)}</div>'
                       for day, items in days.items())
    return f"""
<section class="panel" id="upcoming" aria-labelledby="upcoming-title">
  <div class="panel-head"><h2 id="upcoming-title">Upcoming interviews</h2>
    <p class="muted small">Back-to-back interviews (gaps under 30 min) share one briefing call.
    Research runs at T-60, the call at T-15.</p></div>
  {body}
</section>"""


def spend(view: dict[str, Any]) -> str:
    s = view["spend"]
    lim, ilim = s["limits"], s["interview_limits"]
    return f"""
<section class="card glass" aria-labelledby="spend-title">
  <h2 id="spend-title">Spend &amp; limits</h2>
  {meter(s['month_usd'], ilim['month_usd'], 'This month', f"{money(s['month_usd'])} / {money(ilim['month_usd'])}")}
  {meter(s['today_usd'], ilim['job_usd'] * ilim['jobs_per_day'], 'Today (UTC)', f"{money(s['today_usd'])} · {s['today_jobs']} runs, {s['today_cached']} cached")}
  {meter(s['today_jobs'], ilim['jobs_per_day'], 'Interview runs today', f"{s['today_jobs']} / {ilim['jobs_per_day']}")}
  <p class="muted small">Interview runs cap at {money(ilim['job_usd'])} each. Ad-hoc research: {money(lim['job_usd'])} a job,
  {lim['jobs_per_day']} a day, {lim['jobs_per_caller_day']} per caller; a new job is refused once the month would pass the cap.</p>
</section>"""


def dials(view: dict[str, Any]) -> str:
    rows = []
    for name, title in (("briefing", "Briefing calls"), ("callback", "Research callbacks")):
        d = view["dials"][name]
        detail = f"{'?' if d['used'] is None else d['used']} / {d['limit']}"
        rows.append(meter(d["used"], d["limit"], title, detail))
    return f"""
<section class="card glass" aria-labelledby="dials-title">
  <h2 id="dials-title">Dials today</h2>{''.join(rows)}
  <p class="muted small">Each phone call VALR places counts once per UTC day; at the limit it stops dialling.
  "?" means Temporal did not answer.</p>
</section>"""


def connections(view: dict[str, Any], zone: ZoneInfo) -> str:
    c = view["connections"]
    g, cal = c["google"], c["calendar"]
    gstate = {"connected": ("Connected", "ok"), "reconnect_needed": ("Reconnect needed", "bad"),
              "not_connected": ("Not connected", "warn")}.get(g["state"], ("Unknown", "muted"))
    if g["state"] == "connected" and g["expires_soon"]:
        gstate = ("Expires soon", "warn")
    connect = ('<a class="btn small" href="/oauth/google/start">'
               + ("Reconnect" if g["state"] != "not_connected" else "Connect") + "</a>") \
        if g["configured"] else '<span class="muted small">OAuth client not configured</span>'
    gdetail = (f"{e(g['account'])} · token {g['age_days']} days old (Testing-mode tokens last 7)"
               if g["state"] == "connected" else "R1 reads your mail; the sync reads your calendar.")
    channel = {"active": ("Push active", "ok"), "renewing": ("Push renewing", "warn"),
               "none": ("No push channel", "warn"), "off": ("Push off", "muted")}.get(
        cal.get("channel", ""), ("Unknown", "muted"))
    cal_detail = (f"expires {when(cal.get('expires'), zone)}" if cal.get("expires") else "sweeps every 6 hours")
    cal_detail += f" · last sync {when(cal.get('last_sync_at'), zone)}" if cal.get("last_sync_at") else ""
    error = f'<p class="tone-bad small">Last error: {e(cal["last_error"])}</p>' if cal.get("last_error") else ""
    fathom = ("Configured", "ok") if c["fathom"]["configured"] else ("Not configured", "muted")
    return f"""
<section class="card glass" aria-labelledby="conn-title">
  <h2 id="conn-title">Connections</h2>
  <ul class="conns">
    <li><div><b>Gmail &amp; Calendar</b><p class="muted small">{gdetail}</p></div>
      <div class="conn-side">{pill(*gstate)}{connect if gstate[1] != 'ok' else ''}</div></li>
    <li><div><b>Calendar push</b><p class="muted small">{cal_detail}{(' · source ' + e(cal['source'])) if cal.get('source') else ''}</p>{error}</div>
      <div class="conn-side">{pill(*channel)}</div></li>
    <li><div><b>Fathom</b><p class="muted small">Your interview recordings: R1 context and the self-evaluation.</p></div>
      <div class="conn-side">{pill(*fathom)}</div></li>
  </ul>
</section>"""


def performance(view: dict[str, Any]) -> str:
    perf = view["performance"]
    if not perf["available"]:
        return """
<section class="panel" id="performance" aria-labelledby="perf-title">
  <div class="panel-head"><h2 id="perf-title">Performance trend</h2></div>
  <p class="empty">No self-evaluation on this server yet. Run <code>python scripts/import_recordings.py</code>
  and copy data/interview_scores.json and data/evaluation.json to the private data dir.</p>
</section>"""
    badges = "".join(
        f'<li class="trend"><span class="trend-name">{e(d["label"])}</span>'
        f'{pill(TREND.get(d["direction"], TREND["insufficient data"])[2] + " " + TREND.get(d["direction"], TREND["insufficient data"])[0], TREND.get(d["direction"], TREND["insufficient data"])[1], "Least-squares change across the window" + (f": {d['change']:+.1f} points" if d["change"] is not None else ""))}'
        f'<span class="muted small num">{"last " + str(d["last"]) + "/5" if d["last"] is not None else ""}</span></li>'
        for d in perf["dimensions"].values())
    h = perf["highlights"]

    def items(key: str) -> str:
        return "".join(f"<li>{e(x)}</li>" for x in h.get(key) or []) or '<li class="muted">–</li>'

    checklist = "".join(f'<li><label><input type="checkbox" data-check="{i}"> <span>{e(x)}</span></label></li>'
                        for i, x in enumerate(h.get("prep_checklist") or [])) or '<li class="muted">–</li>'
    data = json.dumps({"rows": perf["rows"], "dimensions": perf["dimensions"]}).replace("<", "\\u003c")
    trend_text = f'<p class="trend-text">{e(h["trend"])}</p>' if h.get("trend") else ""
    return f"""
<section class="panel" id="performance" aria-labelledby="perf-title">
  <div class="panel-head"><h2 id="perf-title">Performance trend</h2>
    <p class="muted small">Scores 1–5 per interview from your recordings, one fixed rubric.
    {perf['interviews']} interviews · evaluated {e((perf.get('generated_at') or '')[:10])}</p></div>
  <div class="perf glass">
    <div class="chart-wrap"><div id="perf-chart" class="chart" role="img" aria-label="Interview scores over time by dimension">
      <noscript><p class="muted">The chart needs JavaScript; the trends are listed beside it.</p></noscript></div>
      <div id="perf-legend" class="legend"></div></div>
    <ul class="trends">{badges}</ul>
  </div>
  {trend_text}
  <div class="highlights">
    <div class="card glass"><h3>Strengths</h3><ul>{items('top_strengths')}</ul></div>
    <div class="card glass"><h3>Weak topics</h3><ul>{items('recurring_weak_topics')}</ul></div>
    <div class="card glass"><h3>Prep checklist</h3><ul class="checklist">{checklist}</ul>
      <p class="muted small">Ticks stay in this browser only.</p></div>
  </div>
  <script type="application/json" id="perf-data">{data}</script>
</section>"""


def recent(view: dict[str, Any], zone: ZoneInfo) -> str:
    rows = []
    for job in view["jobs"]:
        tone = {"done": "ok", "cached": "cyan", "partial": "warn", "running": "info", "booked": "info"}.get(
            job["status"], "bad")
        duration = "–" if job["duration_s"] is None else f"{job['duration_s']:.0f} s"
        notes = "".join(f"<li>{e(n['fact'])}{' ' + pill('sensitive', 'warn', plain=True) if n['sensitive'] else ''}"
                        + "".join(f" <span class=src>{link(u)}</span>" for u in n["sources"]) + "</li>"
                        for n in job["notes"])
        caps = ", ".join(f"{e(k)} {v}" for k, v in job["cap_hits"].items()) or "none"
        rows.append(f"""
<details class="job">
  <summary>
    <span class="job-topic">{e(job['topic'])}</span>
    {pill(job['status'], tone)}
    <span class="job-meta muted small num">{when(job['created_at'], zone, '%d %b %H:%M')} · {e(job['kind'])} · {duration} · {money(job['spend_usd'])}</span>
  </summary>
  <div class="job-body">
    {f'<p class="spoken-inline">{e(job["spoken"])}</p>' if job['spoken'] else ''}
    {f'<p class="muted small">{e(job["detail"])}</p>' if job['detail'] else ''}
    <p class="muted small">Caller {e(job['caller'])} (anonymous hash) · cap hits: {caps}</p>
    {f'<ul class="notes">{notes}</ul>' if notes else ''}
  </div>
</details>""")
    body = "".join(rows) or '<p class="empty">No research runs yet.</p>'
    return f"""
<section class="panel" id="recent" aria-labelledby="recent-title">
  <div class="panel-head"><h2 id="recent-title">Recent research runs</h2>
    <p class="muted small">Every paid or cached run, interview slots and ad-hoc "look this up" calls alike.</p></div>
  <div class="jobs">{body}</div>
</section>"""


def page(view: dict[str, Any], email: str, csrf: str) -> str:
    zone = ZoneInfo(view.get("zone") or "UTC")
    now = datetime.now(timezone.utc).astimezone(zone)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><meta name="csrf-token" content="{e(csrf)}">
<meta name="theme-color" content="#060b1c">
<title>VALR · Interview cockpit</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23060b1c'/%3E%3Cpath d='M8 9l8 15 8-15' stroke='%232ee6f5' stroke-width='3.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/valr.css">
<script type="module" src="/static/valr.js"></script>
</head><body>
<a class="sr-only" href="#upcoming">Skip to upcoming interviews</a>
<header class="topbar">
  <div class="brand"><span class="logo" aria-hidden="true">V</span>
    <div><h1>VALR <span class="muted">interview cockpit</span></h1>
    <p class="muted small">Signed in as {e(email)} · times in {e(view.get('zone'))} ·
    updated <span data-updated>{now:%H:%M}</span></p></div></div>
  <nav class="jump" aria-label="Sections"><a href="#upcoming">Interviews</a><a href="#performance">Performance</a><a href="#recent">Runs</a></nav>
</header>
<main class="layout">
  {hero(view, zone)}
  <div class="columns">
    <div class="col-main">{timeline(view, zone)}</div>
    <aside class="col-side">{spend(view)}{dials(view)}{connections(view, zone)}</aside>
  </div>
  {performance(view)}
  {recent(view, zone)}
  <footer class="muted small">VALR researches each interview 60 minutes before its block, writes a 60–90 second spoken brief,
  and calls you 15 minutes before. Attendee emails and phone numbers are never shown here.</footer>
</main>
</body></html>"""
