"""The leads page at /valr/leads: a shell around the 3D lead globe (static/leads-globe.js),
the filters, the lead panel and a plain list (the fallback on small screens, without WebGL
or JavaScript). Data travels as JSON in a non-executable script tag with "<" escaped; every
server-rendered value is escaped.
"""
import html
import json
from typing import Any

from app.valr_page import FONTS, ICON

STAGE_LABEL = {"sourced": "Sourced", "applied": "Applied", "screening": "Screening",
               "interviewing": "Interviewing", "offer": "Offer", "closed-won": "Won",
               "closed-lost": "Lost", "ghosted": "Ghosted"}


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


STOPS = ((0, (0x3b, 0x7b, 0xff)), (35, (0x8f, 0xd0, 0xff)), (65, (0xff, 0xc4, 0x6b)), (100, (0xff, 0x6a, 0x2b)))


def temp_color(t: float) -> str:
    """Cold blue -> hot orange, the same stops as static/leads-globe.js."""
    x = max(0.0, min(100.0, float(t)))
    for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
        if x <= b:
            f = (x - a) / (b - a)
            return "rgb({},{},{})".format(*(round(u + (v - u) * f) for u, v in zip(ca, cb)))
    return "rgb(255,106,43)"


def lead_row(lead: dict[str, Any]) -> str:
    return (f'<li class="lead-row" data-status="{e(lead["status"])}"><button type="button" class="lead-btn" '
            f'data-lead="{e(lead["id"])}"><span class="lt-dot" style="background:{temp_color(lead["temperature"])}"></span>'
            f'<span class="lr-main"><span class="lr-company">{e(lead["company"])}</span>'
            f'<span class="lr-role muted small">{e(lead["role"] or "Role unknown")}</span></span>'
            f'<span class="lr-stage mono">{e(STAGE_LABEL.get(lead["stage"], lead["stage"]))}</span>'
            f'<span class="lr-temp num">{int(lead["temperature"])}</span></button></li>')


def page(view: dict[str, Any], email: str, csrf: str) -> str:
    active = [l for l in view["leads"] if l["status"] == "active"]
    hot = sum(1 for l in active if l["temperature"] >= 60)
    scheduled = sum(1 for l in active if l.get("next_step_at"))
    gmail = view.get("gmail") or {}
    status = ("live" if gmail.get("watch") == "active" else "polling every 30 min") if gmail.get("enabled") else "off"
    options = "".join(f'<option value="{e(s)}">{e(STAGE_LABEL[s])}</option>' for s in view["stages"])
    rows = "".join(lead_row(l) for l in view["leads"] if l["status"] == "active") or \
        '<li class="muted small lead-empty">No active leads yet.</li>'
    data = json.dumps(view).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="csrf-token" content="{e(csrf)}">
<meta name="theme-color" content="#05070c">
<title>VALR · Leads</title>
<link rel="icon" href="{ICON}">{FONTS}
<link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/valr.css">
<link rel="stylesheet" href="/static/leads.css">
<script type="module" src="/static/leads.js"></script>
</head><body class="leads-body">
<header class="topbar">
  <a class="brand" href="/valr"><span class="brand-dot" aria-hidden="true"></span>valr<span class="brand-sub">/ leads</span></a>
  <nav class="nav leads-nav" aria-label="Pages"><a href="/valr">Cockpit</a><a href="/valr/leads" aria-current="page">Leads</a></nav>
  <p class="tz mono" title="Signed in as {e(email)}"><span class="live-dot" aria-hidden="true"></span>Gmail {e(status)}</p>
</header>
<main class="wrap leads-wrap">
  <section class="leads-hero">
    <p class="eyebrow"><span class="eb-num">01</span><span class="eb-rule"></span>Pipeline</p>
    <h1>Every opportunity, <span class="dim">by temperature<span class="accent">.</span></span></h1>
    <ul class="leads-stats mono">
      <li><b class="num">{len(active)}</b> active</li><li><b class="num">{hot}</b> hot</li>
      <li><b class="num">{scheduled}</b> with an interview booked</li>
      <li><b class="num">{len(view["leads"]) - len(active)}</b> expired</li>
    </ul>
  </section>
  <form class="leads-filters" aria-label="Filter leads" onsubmit="return false">
    <fieldset class="seg" role="group" aria-label="Status">
      <button type="button" data-status="active" aria-pressed="true">Active</button>
      <button type="button" data-status="expired" aria-pressed="false">Expired</button>
      <button type="button" data-status="all" aria-pressed="false">All</button>
    </fieldset>
    <label class="field"><span class="mono label">Stage</span>
      <select name="stage"><option value="">All stages</option>{options}</select></label>
    <label class="field temp-field"><span class="mono label">Min temperature <output name="tval">0</output></span>
      <input type="range" name="tmin" min="0" max="100" step="5" value="0"></label>
    <button class="btn small" type="button" data-action="leads-sync">Check Gmail now</button>
  </form>
  <div class="leads-stage">
    <div id="leads-globe" class="globe" role="img" aria-label="Leads globe: one node per lead, coloured from cold blue to hot orange, linked to recruiters and interviewers">
      <noscript><p class="muted">The globe needs JavaScript; the list below has every lead.</p></noscript>
    </div>
    <div class="legend mono" aria-hidden="true"><span>cold</span><i class="ramp"></i><span>hot</span>
      <span class="lg-sep"></span><span class="lg-c"></span><span>person</span></div>
    <aside id="lead-panel" class="lead-panel" aria-live="polite" hidden></aside>
  </div>
  <section class="leads-list-wrap" aria-labelledby="list-title">
    <h2 id="list-title" class="sub-title">All leads <span class="dim small">by temperature</span></h2>
    <ul id="leads-list" class="leads-list">{rows}</ul>
  </section>
  <footer class="foot mono">valr · leads <span class="muted">· signed in as {e(email)} · email summaries only, never message bodies</span></footer>
</main>
<div class="toast" role="status" aria-live="polite" hidden></div>
<script type="application/json" id="leads-data">{data}</script>
</body></html>"""


def error_page() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>VALR · Leads</title><link rel="icon" href="{ICON}">{FONTS}
<link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/valr.css"></head><body>
<main class="wrap"><section class="hero solo"><div class="hero-copy">
<h1>Leads are unavailable <span class="dim">right now<span class="accent">.</span></span></h1>
<p class="lede">The database didn't answer. Gmail keeps being read in the background. Try again in a minute.</p>
<div class="actions"><a class="btn primary" href="/valr/leads">Try again</a></div></div></section></main></body></html>"""
