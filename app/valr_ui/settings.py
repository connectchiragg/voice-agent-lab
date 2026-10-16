"""Settings: one short page.

- "Finish setting up" (every step a link or button that does it), only until it is done.
- Connections: Google, Telegram, the notetakers (with their connect forms) and the phone.
- Reminders: a Telegram heads-up or an automatic call, and Pause.
- Advanced (folded, /valr/settings#advanced; /valr/system redirects here): usage limits,
  research spend, the calendar sync, post-call delivery, the research log and VALR's
  dial-in number.

Statuses come from the real state; nothing here is shown unless it is useful or broken.
"""
from typing import Any

from app.valr_page import Clock, chip, e, meter, money, phone_label
from app.valr_ui import attention, contracts, pwa
from app.valr_ui.shell import icon, page_head

STATE = {"connected": ("Connected", "ok"), "not_connected": ("Not connected", "warn"),
         "error": ("Needs a fix", "bad"), "unknown": ("Not checked yet", "muted")}
STEP_STATE = {"done": ("Done", "ok"), "todo": ("To do", "info"), "attention": ("Needs a fix", "warn")}
REPORT_STATUS = {"sent": ("Sent on Telegram", "ok"), "pending_telegram": ("Waiting for Telegram", "warn"),
                 "queued": ("Queued", "info"), "processing": ("Being reviewed", "info"), "failed": ("Failed", "bad")}
RUN_STATUS = {"done": ("Done", "ok"), "cached": ("Reused", "info"), "partial": ("Partial", "warn"),
              "running": ("Running", "info"), "booked": ("Queued", "info")}


def row(anchor: str, title: str, glyph: str, status: str, body: str, action: str = "") -> str:
    return f"""
<li class="set-row" id="{anchor}">
  <span class="set-icon" aria-hidden="true">{icon(glyph, 18)}</span>
  <div class="set-main"><div class="set-title-row"><h3 class="set-title">{e(title)}</h3>{status}</div><div class="set-body">{body}</div></div>
  {f'<div class="set-action">{action}</div>' if action else ''}
</li>"""


def google_row(view: dict[str, Any], clock: Clock) -> str:
    g = view["connections"]["google"]
    state = g.get("state")
    expiring = [i for i in attention.google_items(view) if i["key"] == "google_expiring"]
    if state == "connected":
        status = chip("Connected", "ok") + (f' {chip(expiring[0]["title"], "warn")}' if expiring else "")
        body = (f'<p class="muted">{e(g.get("account") or "Your Google account")} · read-only: calendar invites and '
                'job emails.</p>')
        action = '<a class="btn sm" href="/oauth/google/start">Reconnect</a>'
    elif state == "reconnect_needed":
        status, body = chip("Expired", "bad"), '<p class="muted">New invites and emails won\'t show up until you reconnect.</p>'
        action = '<a class="btn sm primary" href="/oauth/google/start">Reconnect Google</a>'
    else:
        status = chip("Not connected", "warn") if state == "not_connected" else chip("Not checked yet", "muted")
        body = '<p class="muted">So VALR can see your interview invites and job emails. Read-only.</p>'
        action = '<a class="btn sm primary" href="/oauth/google/start">Connect Google</a>'
    if not g.get("configured"):
        action = '<span class="muted">Google sign-in isn\'t available yet.</span>'
    return row("google", "Google", "mail", status, body, action)


def telegram_row(view: dict[str, Any], clock: Clock) -> str:
    tg = view["connections"].get("telegram") or {}
    if not tg.get("configured"):
        return row("telegram", "Telegram", "send", chip("Not available yet", "muted"),
                   '<p class="muted">The VALR bot isn\'t set up yet, so reminders can\'t be sent.</p>')
    if tg.get("linked"):
        who = f"Connected as @{tg['username']}" if tg.get("username") else "Connected"
        return row("telegram", "Telegram", "send", chip(who, "ok"),
                   '<p class="muted">Reminders arrive in your VALR chat 30 minutes before each interview.</p>',
                   '<button class="btn sm" type="button" data-action="tgtest">Send a test</button>'
                   '<button class="btn sm ghost" type="button" data-action="tglink">Reconnect</button>')
    pending = (view.get("post_call") or {}).get("pending")
    waiting = (f'<p class="field-help warn">{pending} post-call report{"s are" if pending != 1 else " is"} waiting for Telegram.</p>'
               if pending else "")
    return row("telegram", "Telegram", "send", chip("Not connected", "warn"), f"""
<p class="muted">One tap: Telegram opens the VALR bot, you press <b>Start</b>, and you're connected.</p>{waiting}
<div class="tg-link" data-tg-panel hidden>
  <a class="btn primary" data-tg-open href="#" target="_blank" rel="noopener noreferrer">Open Telegram</a>
  <div class="qr" data-tg-qr aria-hidden="true"></div>
  <p class="muted" data-tg-help></p>
</div>""", '<button class="btn sm primary" type="button" data-action="tglink">Connect Telegram</button>')


def setup(view: dict[str, Any]) -> str:
    """The set-up checklist; gone once every step is done. Granola is optional, never a step."""
    from app.notetakers import ui as notetaker_ui
    google = view["connections"]["google"]
    tg = view["connections"].get("telegram") or {}
    takers = (view.get("connections") or {}).get("notetakers") or {}
    s = view["setup"]
    number = phone_label(s["valr_number"])
    g_done = google["state"] == "connected"
    g_state = "done" if g_done else "attention" if google["state"] != "not_connected" else "todo"
    g_action = ("" if g_done else '<a class="btn sm primary" href="/oauth/google/start">'
                + ("Reconnect" if google["state"] != "not_connected" else "Connect Google") + "</a>")
    required = [t for t in takers.values() if not t.get("optional")]
    refused = any(t["state"] == "refused" for t in required) and not any(notetaker_ui.is_on(t) for t in required)
    n_state = "attention" if refused else "done" if any(notetaker_ui.is_on(t) for t in required) else "todo"
    steps = [("Connect Google", g_state, "So VALR can see your interview invites and job emails. Read-only.", g_action),
             ("Connect a notetaker", n_state, "Fathom or Fireflies: VALR reviews each recorded interview and plays it back here.",
              "" if n_state == "done" else (notetaker_ui.summary_buttons(takers) or '<a class="btn sm" href="#notetakers">Notetakers</a>'))]
    if view.get("mode") == "telegram":
        if tg.get("linked"):
            steps.append(("Connect Telegram", "done", "Reminders arrive in your VALR chat.", ""))
        elif tg.get("configured"):
            steps.append(("Connect Telegram", "todo", "VALR sends a reminder there 30 minutes before each interview.",
                          '<button class="btn sm primary" type="button" data-action="tglink">Open Telegram</button>'))
        else:
            steps.append(("Connect Telegram", "attention", "The VALR bot isn't installed on the server yet.", ""))
    steps += [
        ("Confirm your phone number", "done" if s["phone_on_file"] else "todo",
         "VALR only ever calls the number on file." if s["phone_on_file"] else
         "Add your number to the server settings and restart VALR.", ""),
        ("Make a test call", "done" if s["test_call_done"] else "todo",
         f"Call {number} and ask \"what's on my calendar today?\"",
         "" if s["test_call_done"] else f'<a class="btn sm" href="tel:{e(s["valr_number"])}">Call VALR</a>'),
    ]
    done = sum(1 for _, state, _, _ in steps if state == "done")
    if done == len(steps):
        return ""
    items = "".join(f"""
<li class="step{' done' if state == 'done' else ''}">
  <span class="step-mark" aria-hidden="true">{icon('check', 14) if state == 'done' else i}</span>
  <div class="step-body"><p class="step-title">{e(title)}</p><p class="muted">{e(text)}</p></div>
  <div class="step-action">{action if state != 'done' and action else chip(*STEP_STATE[state])}</div>
</li>""" for i, (title, state, text, action) in enumerate(steps, 1))
    pct = round(done / len(steps) * 100)
    return f"""
<section class="card setup" id="setup" aria-labelledby="setup-title">
  <div class="card-head"><h2 class="card-title" id="setup-title">Finish setting up</h2><span class="card-meta">{done} of {len(steps)} done</span></div>
  <div class="progress-bar" role="progressbar" aria-label="Set-up progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pct}"><i style="width:{pct}%"></i></div>
  <ol class="steps">{items}</ol>
</section>"""


def notetaker_row(view: dict[str, Any]) -> str:
    """valr-notetakers' cards: status, the connect form (the key is posted, never shown again),
    "Check for new recordings", and Granola's export upload."""
    from app.notetakers import ui as notetaker_ui
    takers = view["connections"].get("notetakers") if isinstance(view["connections"].get("notetakers"), dict) else {}
    if takers:
        body = notetaker_ui.cards(takers)
    else:  # Status unavailable: what the imported recordings show.
        states = contracts.notetakers(view["connections"], attention.imported(view))
        body = '<ul class="mini-rows">' + "".join(
            f'<li class="mini-row"><span>{e(t["label"])}</span>{chip(*STATE[t["state"]])}</li>' for t in states) + "</ul>"
    return row("notetakers", "Notetakers", "video", "",
               '<p class="muted">VALR reviews your recorded interviews to coach you. Fathom or Fireflies is enough; '
               'Granola is optional.</p>' + body)


def phone_row(view: dict[str, Any]) -> str:
    s = view["setup"]
    return row("phone", "Your phone", "phone", chip("On file", "ok") if s.get("phone_on_file") else chip("Missing", "bad"),
               '<p class="muted">VALR only ever calls the number on file. It isn\'t shown here.</p>')


def reach_row(view: dict[str, Any]) -> str:
    """Telegram heads-up (the default) or an automatic call, stored on the server; and Pause."""
    telegram = view.get("mode") == "telegram"
    reach = view.get("reach") or {}
    paused = (view.get("dials_paused") or {}).get("enabled")
    how = ("A Telegram message 30 minutes before each interview. Tap Call me in it for the briefing call; no reply, no call."
           if telegram else "VALR calls you 30 minutes before each interview, once. No retries.")
    default = "Telegram heads-up" if not reach.get("default_auto_dial") else "automatic call"
    source = "Your choice." if reach.get("source") == "settings" else f"The default ({default})."
    toggle = (f'<div class="seg" role="group" aria-label="How VALR reminds you">'
              f'<button type="button" data-action="reach" data-mode="telegram" aria-pressed="{"true" if telegram else "false"}">Telegram heads-up</button>'
              f'<button type="button" data-action="reach" data-mode="call" aria-pressed="{"false" if telegram else "true"}">Automatic call</button></div>')
    return row("calls", "Before each interview", "clock", chip("Paused", "warn") if paused else "",
               f'{toggle}<p class="muted">{e(how)} {e(source)}</p>',
               f'<button class="btn sm" type="button" data-action="dials" data-paused="{"false" if paused else "true"}" '
               f'aria-pressed="{"true" if paused else "false"}">{"Resume reminders" if paused else "Pause reminders"}</button>')


def run_row(job: dict[str, Any], clock: Clock) -> str:
    took = "" if job["duration_s"] is None else f"{job['duration_s']:.0f} s · "
    return (f'<li><span>{e(job["topic"])}</span><span class="kv-end">{chip(*RUN_STATUS.get(job["status"], ("Failed", "bad")))}'
            f'<span class="muted num">{clock.when(job["created_at"])} · {took}{money(job["spend_usd"])}</span></span></li>')


PHONE_STEPS = [
    "In the Shortcuts app, tap + and name the shortcut “Send to VALR”.",
    "Tap ⓘ, turn on “Show in Share Sheet”, and set it to receive Audio and Files.",
    "Add “Get Contents of URL”: the upload address below, Method POST, a header Authorization = "
    "Bearer followed by your upload token, Request Body Form, a field file (type File) = Shortcut Input.",
    "Add “Show Result” so you see “Got it”.",
    "After a recorded call: open it in Truecaller, tap Share, choose “Send to VALR”.",
]


def phone_recordings_row() -> str:
    """The iPhone's call recordings (Truecaller -> the Shortcut -> VALR). static/valr-notify.js
    fills the status from /valr/api/phone/status and runs the two token buttons."""
    steps = "".join(f"<li>{e(step)}</li>" for step in PHONE_STEPS)
    body = ('<p class="muted" data-phone-status>Checking…</p>'
            '<p class="muted">Share a call recording from Truecaller to the “Send to VALR” Shortcut: VALR transcribes it, '
            'adds it to Past interviews and the coach reviews it. The audio is stored encrypted and deleted a week later.</p>'
            '<p><a href="/valr/settings/phone-help">iPhone shortcut steps</a>: one at a time, or all at once from a Home Screen button.</p>'
            '<div class="notif-token" data-phone-token hidden><code class="token" data-phone-token-value></code>'
            '<button class="btn sm" type="button" data-phone-copy>Copy</button>'
            '<p class="field-help">Copy it now: it won’t be shown again.</p></div>'
            f'<details class="fold inline" id="phone-steps"><summary><span class="fold-title">Set up the iPhone Shortcut</span>{icon("chevron-down", 16)}</summary>'
            f'<ol class="notif-steps">{steps}</ol><p class="field-help">Upload address: '
            '<code class="token" data-phone-url>/hooks/recording-upload</code></p></details>')
    actions = ('<button class="btn sm" type="button" data-phone-action="show">Show upload token</button>'
               '<button class="btn sm ghost" type="button" data-phone-action="rotate">Rotate token</button>')
    return row("phone-recordings", "Phone call recordings", "mic", "", body, actions)


def alerts_row() -> str:
    """Telegram alert switches (app/notifications.py); static/valr-notify.js loads and saves them."""
    switches = "".join(
        f'<label class="switch notif-switch"><input type="checkbox" data-notif-switch="{key}" disabled>'
        f'<span class="switch-ui" aria-hidden="true"></span><span><span class="switch-title">{e(title)}</span>'
        f'<span class="field-help">{e(help_)}</span></span></label>'
        for key, title, help_ in (("immediate", "Immediate alerts", "Offers, interview invites and replies that need you"),
                                  ("digest", "Daily digest at 09:00", "Rejections, new leads, stage changes and other updates"),
                                  ("quiet", "Quiet hours 23:00–08:00", "Only interviews in the next 12 hours get through")))
    return row("alerts", "Telegram alerts", "bell", "", f'<div class="notif-switches">{switches}</div>')


def advanced(view: dict[str, Any], clock: Clock) -> str:
    """Limits, spend, the calendar sync, post-call delivery, the research log, the dial-in number."""
    s, lim = view["spend"], view["spend"]["interview_limits"]
    dials = view.get("dials") or {}
    meters = "".join(meter(dials[k]["used"], dials[k]["limit"], title,
                           f"{'?' if dials[k]['used'] is None else dials[k]['used']} of {dials[k]['limit']}")
                     for k, title in (("briefing", "Briefing calls today"), ("callback", "Research call-backs today"))
                     if k in dials)
    meters += meter(s["today_jobs"], lim["jobs_per_day"], "Research runs today", f"{s['today_jobs']} of {lim['jobs_per_day']}")
    meters += meter(s["month_usd"], lim["month_usd"], "Research spend this month", f"{money(s['month_usd'])} of {money(lim['month_usd'])}")
    cal = view["connections"].get("calendar") or {}
    synced = f"Last synced {clock.when(cal['last_sync_at'])}." if cal.get("last_sync_at") else "Not synced yet."
    if cal.get("last_error"):
        synced += " The last sync failed."
    pc = view.get("post_call") or {}
    reports = "".join(f'<li><span>{e(r["title"] or "Interview")}</span><span class="kv-end">{chip(*REPORT_STATUS.get(r["status"], (r["status"], "muted")))}'
                      f'<span class="muted num">{clock.when(r["updated_at"]) if r.get("updated_at") else ""}</span></span></li>'
                      for r in pc.get("recent") or [])
    runs = "".join(run_row(job, clock) for job in view.get("jobs") or [])
    number = view["setup"]["valr_number"]
    return f"""
<details class="fold card advanced" id="advanced"><summary><span class="fold-title">Advanced</span>
  <span class="fold-sub">Limits, spend, research log, post-call reports, VALR's number</span>{icon('chevron-down', 18)}</summary>
  <div class="fold-body adv-grid">
    <section class="adv" id="limits"><h3 class="sub-title">Usage</h3><div class="meters">{meters}</div>
      <p class="muted">VALR stops for the day at a limit, and tells you when one is close. Each interview's research is capped at {money(lim['job_usd'])}.</p></section>
    <section class="adv" id="dial-in"><h3 class="sub-title">VALR's number</h3>
      <p><a class="num big-link" href="tel:{e(number)}">{e(phone_label(number))}</a></p>
      <p class="muted">Call it any time from your phone and ask about your day.</p>
      <h3 class="sub-title">Calendar</h3><p class="muted">{synced}</p>
      <button class="btn sm" type="button" data-action="sync">Sync calendar now</button></section>
    <section class="adv wide" id="reports"><h3 class="sub-title">Post-call reports</h3>
      {f'<ul class="kv-rows">{reports}</ul>' if reports else '<p class="muted">None yet.</p>'}</section>
    <section class="adv wide" id="runs"><h3 class="sub-title">Research log</h3>
      {f'<ul class="kv-rows">{runs}</ul>' if runs else '<p class="muted">None yet.</p>'}</section>
  </div>
</details>"""


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]]) -> str:
    return f"""{page_head("Settings")}
{setup(view)}
<section class="section" aria-labelledby="g-conn"><div class="section-head"><h2 id="g-conn" class="section-title">Connections</h2></div>
  <ul class="set-list card">{google_row(view, clock)}{telegram_row(view, clock)}{notetaker_row(view)}{phone_recordings_row()}{phone_row(view)}</ul></section>
<section class="section" aria-labelledby="g-rem"><div class="section-head"><h2 id="g-rem" class="section-title">Reminders</h2></div>
  <ul class="set-list card">{reach_row(view)}{alerts_row()}{pwa.card()}</ul></section>
{advanced(view, clock)}"""
