"""System (linked from the footer and Settings, not the main nav): health, costs, limits
and recent research runs, for when something looks off."""
from typing import Any

from app.valr_page import Clock, chip, e, meter, money, telegram_line
from app.valr_ui.shell import attention_list, page_head


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]]) -> str:
    s, ilim = view["spend"], view["spend"]["interview_limits"]
    c = view["connections"]
    g, cal = c["google"], c.get("calendar") or {}
    channel = {"active": "live updates on", "renewing": "live updates renewing",
               "none": "no live updates (checks every 6 hours)", "off": "live updates off"}.get(cal.get("channel", ""), "unknown")
    dials = "".join(meter(view["dials"][k]["used"], view["dials"][k]["limit"], title,
                          f"{'?' if view['dials'][k]['used'] is None else view['dials'][k]['used']} / {view['dials'][k]['limit']}")
                    for k, title in (("briefing", "Briefing calls today"), ("callback", "Research call-backs today")))
    runs = []
    for job in view.get("jobs") or []:
        tone = {"done": "ok", "cached": "info", "partial": "warn", "running": "info", "booked": "info"}.get(job["status"], "bad")
        status = {"cached": "reused earlier research", "booked": "queued"}.get(job["status"], job["status"])
        duration = "–" if job["duration_s"] is None else f"{job['duration_s']:.0f} s"
        runs.append(f'<li class="run"><span class="run-topic">{e(job["topic"])}</span>{chip(status, tone)}'
                    f'<span class="muted small num">{clock.when(job["created_at"])} · {duration} · {money(job["spend_usd"])}</span></li>')
    health = attention_list(items) or '<p class="muted">Everything is working.</p>'
    return f"""{page_head("System", 'Health, costs <span class="dim">and recent runs.</span>', crumb="/valr/settings|Settings")}
<div class="sys-grid">
  <section class="sys-card wide" id="health"><h2 class="h-sm">Health</h2>{health}</section>
  <section class="sys-card" id="spend"><h2 class="h-sm">Research spend</h2>
    {meter(s['month_usd'], ilim['month_usd'], 'This month', f"{money(s['month_usd'])} of {money(ilim['month_usd'])}")}
    {meter(s['today_jobs'], ilim['jobs_per_day'], 'Research runs today', f"{s['today_jobs']} of {ilim['jobs_per_day']}")}
    <p class="muted small">Today {money(s['today_usd'])}, {s['today_cached']} reused for free. Each interview's research is capped at {money(ilim['job_usd'])}.</p></section>
  <section class="sys-card" id="calls"><h2 class="h-sm">Calls</h2>{dials}<p class="muted small">VALR stops dialling for the day at the limit.</p></section>
  <section class="sys-card" id="connections"><h2 class="h-sm">Connections</h2>
    <ul class="kv">
      <li><span>Google account</span><span>{e(g.get('account')) or '–'} · {e(str(g.get('state', 'unknown')).replace('_', ' '))}{f" · signed in {g['age_days']:.0f} days ago" if g.get('age_days') is not None else ''}</span></li>
      <li><span>Calendar</span><span>{e(channel)}{f" · last sync {clock.when(cal.get('last_sync_at'))}" if cal.get('last_sync_at') else ''}</span></li>
      {f'<li class="tone-bad"><span>Last calendar error</span><span>{e(cal["last_error"])}</span></li>' if cal.get('last_error') else ''}
      <li><span>Telegram</span><span>{telegram_line(c.get('telegram') or {}, clock).replace('send the /start code', 'connect it in Settings')}</span></li>
    </ul></section>
  <section class="sys-card wide" id="runs"><h2 class="h-sm">Recent research</h2><ul class="runs">{''.join(runs) or '<li class="muted small">Nothing yet.</li>'}</ul></section>
</div>"""
