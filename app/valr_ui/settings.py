"""Settings: the set-up checklist (every step a link or button that does it), connections
(Google, calendar, Telegram, the notetakers with their connect forms), the phone, call
preferences (how VALR reaches you: a Telegram heads-up or an automatic call; pause) and
today's limits. Statuses come from the real state."""
from typing import Any

from app.valr_page import Clock, chip, e, eyebrow, meter, money, phone_label
from app.valr_ui import attention, contracts
from app.valr_ui.shell import attention_list, page_head

STATE = {"connected": ("Connected", "ok"), "not_connected": ("Not connected", "warn"),
         "error": ("Needs attention", "bad"), "unknown": ("Not checked yet", "muted")}


def row(anchor: str, title: str, status: str, body: str, action: str = "") -> str:
    return f"""
<li class="set-row" id="{anchor}">
  <div class="set-main"><h3 class="set-title">{e(title)}</h3>{status}<div class="set-body">{body}</div></div>
  {f'<div class="set-action">{action}</div>' if action else ''}
</li>"""


def google_row(view: dict[str, Any], clock: Clock) -> str:
    g = view["connections"]["google"]
    state = g.get("state")
    expiring = [i for i in attention.google_items(view) if i["key"] == "google_expiring"]
    if state == "connected":
        status = chip("Connected", "ok") + (f' {chip(expiring[0]["title"], "warn")}' if expiring else "")
        body = (f'<p class="muted small">{e(g.get("account") or "Your Google account")} · read-only: calendar invites and '
                'the recruiters\' emails about them. Google asks you to reconnect about once a week.</p>')
        action = '<a class="btn small" href="/oauth/google/start">Reconnect</a>'
    elif state == "reconnect_needed":
        status, body = chip("Expired", "bad"), '<p class="muted small">New invites won\'t show up until you reconnect.</p>'
        action = '<a class="btn small primary" href="/oauth/google/start">Reconnect Google</a>'
    else:
        status = chip("Not connected", "warn") if state == "not_connected" else chip("Not checked yet", "muted")
        body = '<p class="muted small">So VALR can see your interview invites. Read-only; it never changes anything.</p>'
        action = '<a class="btn small primary" href="/oauth/google/start">Connect Google</a>'
    if not g.get("configured"):
        action = '<span class="muted small">Google sign-in isn\'t available yet.</span>'
    return row("google", "Google", status, body, action)


def calendar_row(view: dict[str, Any], clock: Clock) -> str:
    cal = view["connections"].get("calendar") or {}
    live = cal.get("channel") in ("active", "renewing")
    status = chip("Live updates" if live else "Checks every few hours", "ok" if live else "info")
    if cal.get("last_error"):
        status += " " + chip("Last sync failed", "bad")
    synced = f"Last synced {clock.when(cal['last_sync_at'])}." if cal.get("last_sync_at") else "Not synced yet."
    return row("calendar", "Calendar", status, f'<p class="muted small">{synced}</p>',
               '<button class="btn small" type="button" data-action="sync">Sync now</button>')


def telegram_row(view: dict[str, Any], clock: Clock) -> str:
    tg = view["connections"].get("telegram") or {}
    if not tg.get("configured"):
        return row("telegram", "Telegram", chip("Not available yet", "muted"),
                   '<p class="muted small">The VALR bot isn\'t set up yet, so heads-ups can\'t be sent.</p>')
    if tg.get("linked"):
        who = f"Connected as @{tg['username']}" if tg.get("username") else "Connected"
        since = f" · since {clock.when(tg['linked_at'])}" if tg.get("linked_at") else ""
        return row("telegram", "Telegram", chip(who, "ok"),
                   f'<p class="muted small">Heads-ups arrive in your VALR chat 30 minutes before each interview{since}.</p>',
                   '<button class="btn small" type="button" data-action="tgtest">Send test message</button>'
                   '<button class="btn small ghost" type="button" data-action="tglink">Reconnect</button>')
    return row("telegram", "Telegram", chip("Not connected", "warn"), """
<p class="muted small">One tap: Telegram opens the VALR bot, you press <b>Start</b>, and you're connected.</p>
<div class="tg-link" data-tg-panel hidden>
  <a class="btn primary" data-tg-open href="#" target="_blank" rel="noopener noreferrer">Open Telegram</a>
  <div class="qr" data-tg-qr aria-hidden="true"></div>
  <p class="muted small" data-tg-help></p>
</div>""", '<button class="btn small primary" type="button" data-action="tglink">Open Telegram &amp; connect</button>')


STEP_STATE = {"done": ("Done", "ok"), "todo": ("To do", "info"), "attention": ("Needs attention", "warn")}


def setup(view: dict[str, Any]) -> str:
    """The set-up checklist (from the old single page); Granola is optional, never a step."""
    from app.notetakers import ui as notetaker_ui
    google = view["connections"]["google"]
    tg = view["connections"].get("telegram") or {}
    takers = (view.get("connections") or {}).get("notetakers") or {}
    s = view["setup"]
    number = phone_label(s["valr_number"])
    g_done = google["state"] == "connected" and not google["expires_soon"]
    g_text = {"reconnect_needed": "Google access expired. Reconnect so VALR can see new invites.",
              "connected": "Connected. Google asks you to reconnect about once a week while the app is in testing."}
    g_state = "done" if g_done else "attention" if google["state"] != "not_connected" else "todo"
    g_action = ('<a class="btn small' + ("" if g_done else " primary") + '" href="/oauth/google/start">'
                + ("Reconnect" if google["state"] != "not_connected" else "Connect Google") + "</a>"
                + ("" if google["configured"] else
                   '<span class="muted small"> Google sign-in isn\'t set up on the server yet.</span>'))
    required = [t for t in takers.values() if not t.get("optional")]  # Granola is optional.
    on = [t["label"] for t in takers.values() if notetaker_ui.is_on(t)]
    refused = any(t["state"] == "refused" for t in required) and not any(notetaker_ui.is_on(t) for t in required)
    n_state = "attention" if refused else "done" if any(notetaker_ui.is_on(t) for t in required) else "todo"
    n_text = (f"Connected: {', '.join(on)}. VALR scores each recorded interview and plays it back here."
              if on else "Fathom (or a Fireflies history import): VALR learns from your recorded interviews, "
                         "scores each one and plays it back here.")
    steps = [("Connect Google", g_state,
              g_text.get(google["state"], "So VALR can see your interview invites and the recruiters' emails about them. Read-only."),
              g_action),
             ("Connect a notetaker", n_state, n_text, notetaker_ui.summary_buttons(takers)
              or '<a class="btn small" href="#notetakers">Notetakers</a>')]
    if view.get("mode") == "telegram":
        if tg.get("linked"):
            steps.append(("Connect Telegram", "done", "Connected: heads-ups arrive in your VALR chat.",
                          '<button class="btn small ghost" type="button" data-action="tglink">Link again</button>'
                          '<code class="mono" data-tg-code hidden></code>'))
        elif tg.get("configured"):
            steps.append(("Connect Telegram", "todo",
                          "VALR sends the heads-up there 30 minutes before each interview; you tap to get the call.",
                          '<button class="btn small primary" type="button" data-action="tglink">Open Telegram</button>'
                          '<code class="mono" data-tg-code hidden></code>'))
        else:
            steps.append(("Connect Telegram", "attention", "The VALR bot isn't installed on the server yet.",
                          '<details class="pop"><summary class="btn small">How to set it up</summary><p class="small">'
                          'Run the Telegram installer from the deploy folder with the bot token from '
                          '@BotFather. Then come back here and press "Open Telegram".</p></details>'))
    steps += [
        ("Confirm your phone number", "done" if s["phone_on_file"] else "todo",
         "VALR only ever calls the number on file, never anyone else.",
         '<details class="pop"><summary class="btn small">' + ("How VALR checks it" if s["phone_on_file"] else "How to add it")
         + '</summary><p class="small">VALR knows it is you by caller ID: a call from the owner phone number in '
           'the server settings gets your calendar, briefings and profile; every other caller '
           'gets the general assistant. ' + ("A number is on file. " if s["phone_on_file"] else
                                            "No number is on file yet: add it to the server settings and restart VALR. ")
         + f'To check it, call <a href="tel:{e(s["valr_number"])}">{e(number)}</a> and ask '
           '"what\'s on my calendar today?": VALR answers with your interviews only when it recognises you.</p></details>'),
        ("Make a test call", "done" if s["test_call_done"] else "todo",
         f"Call {number} from your phone and ask \"what's on my calendar today?\"",
         f'<a class="btn small{"" if s["test_call_done"] else " primary"}" href="tel:{e(s["valr_number"])}">'
         f'{"Call again" if s["test_call_done"] else "Call VALR"} <span class="num">{e(number)}</span></a>'),
    ]
    done = sum(1 for _, state, _, _ in steps if state == "done")
    items = "".join(f"""
<li class="step{' done' if state == 'done' else ''}">
  <span class="step-mark mono" aria-hidden="true">{'✓' if state == 'done' else f'0{i}'}</span>
  <div class="step-body"><p class="step-title">{e(title)} {chip(*STEP_STATE[state])}</p>
  <p class="muted small">{e(text)}</p></div>
  <div class="step-action">{action}</div>
</li>""" for i, (title, state, text, action) in enumerate(steps, 1))
    open_ = "" if done == len(steps) else " open"
    return f"""
<section class="section setup" id="setup" aria-labelledby="setup-title">
  <details class="setup-box"{open_}>
    <summary>{eyebrow("01", "Get set up")}<h2 id="setup-title">{'You are all set' if done == len(steps) else 'Finish setting up'}
      <span class="progress mono">{done}/{len(steps)}</span></h2></summary>
    <ol class="steps">{items}</ol>
  </details>
</section>"""


def notetaker_rows(view: dict[str, Any], clock: Clock) -> str:
    """valr-notetakers' cards: status, the connect form (the key is posted, never shown again),
    "Check for new recordings", the Fathom sharing hint and Granola's export upload."""
    from app.notetakers import ui as notetaker_ui
    takers = view["connections"].get("notetakers") if isinstance(view["connections"].get("notetakers"), dict) else {}
    states = {t["key"]: t for t in contracts.notetakers(view["connections"], attention.imported(view))}
    if takers:
        body = notetaker_ui.cards(takers)
    else:  # Status unavailable: what the imported recordings show.
        body = "<ul class=\"mini-rows\">" + "".join(
            f'<li class="mini-row"><span class="mini-name">{e(t["label"])}</span>{chip(*STATE[t["state"]])}</li>'
            for t in states.values()) + "</ul>"
    return row("notetakers", "Notetakers", "",
               '<p class="muted small">VALR reviews your recorded interviews to coach you. Fathom or Fireflies is '
               'enough; Granola is optional.</p>' + body)


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
    body = ('<p class="muted small" data-phone-status>Checking…</p>'
            '<p class="muted small">Share a call recording from Truecaller to the “Send to VALR” Shortcut: '
            'VALR transcribes it, adds it to Past interviews and the coach reviews it. The audio is stored '
            'encrypted and deleted a week after processing.</p>'
            '<div class="notif-token" data-phone-token hidden><code class="mono" data-phone-token-value></code>'
            '<button class="btn small" type="button" data-phone-copy>Copy</button>'
            '<p class="muted small">Copy it now: it won’t be shown again.</p></div>'
            '<details class="pop" id="phone-steps"><summary class="btn small ghost">Set up the iPhone Shortcut</summary>'
            f'<ol class="small notif-steps">{steps}</ol><p class="small">Upload address: '
            '<code class="mono" data-phone-url>/hooks/recording-upload</code>. Full steps: docs/phone-recordings.md.</p></details>')
    actions = ('<button class="btn small" type="button" data-phone-action="show">Show upload token</button>'
               '<button class="btn small ghost" type="button" data-phone-action="rotate">Rotate token</button>')
    return row("phone-recordings", "Phone call recordings", "", body, actions)


def alerts_row() -> str:
    """Telegram alert switches (app/notifications.py); static/valr-notify.js loads and saves them."""
    switches = "".join(
        f'<label class="notif-switch"><input type="checkbox" data-notif-switch="{key}" disabled> {e(label)}</label>'
        for key, label in (("immediate", "Immediate alerts (offers, interview invites, replies that need you)"),
                           ("digest", "Daily digest at 09:00 (rejections, new leads, stage changes)"),
                           ("quiet", "Quiet hours 23:00–08:00 (only interviews in the next 12 hours get through)")))
    return row("alerts", "Telegram alerts", "", f'<div class="notif-switches">{switches}</div>')


def reach_row(view: dict[str, Any]) -> str:
    """Telegram heads-up (the default) or an automatic call, stored on the server."""
    telegram = view.get("mode") == "telegram"
    reach = view.get("reach") or {}
    paused = (view.get("dials_paused") or {}).get("enabled")
    noun = "heads-ups" if telegram else "calls"
    how = ("A Telegram message 30 minutes before each interview. Tap 📞 for the briefing call; no reply, no call."
           if telegram else "VALR calls you 30 minutes before each interview, once. No retries.")
    default = "Telegram heads-up" if not reach.get("default_auto_dial") else "automatic call"
    source = "Your choice." if reach.get("source") == "settings" else f"The default ({default})."
    toggle = (f'<div class="seg" role="group" aria-label="How VALR reaches you">'
              f'<button type="button" data-action="reach" data-mode="telegram" aria-pressed="{"true" if telegram else "false"}">Telegram heads-up</button>'
              f'<button type="button" data-action="reach" data-mode="call" aria-pressed="{"false" if telegram else "true"}">Automatic call</button></div>')
    return row("calls", "How VALR reaches you",
               chip("Paused", "warn") if paused else chip("Telegram first" if telegram else "Automatic call", "info"),
               f'{toggle}<p class="muted small">{e(how)} {e(source)}</p>',
               f'<button class="btn small" type="button" data-action="dials" data-paused="{"false" if paused else "true"}" '
               f'aria-pressed="{"true" if paused else "false"}">{"Resume " + noun if paused else "Pause " + noun}</button>'
               '<button class="btn small" type="button" data-action="callnow">Call me now</button>')


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]]) -> str:
    s = view["setup"]
    telegram = view.get("mode") == "telegram"
    phone = row("phone", "Your phone", chip("On file", "ok") if s.get("phone_on_file") else chip("Missing", "bad"),
                f'<p class="muted small">VALR only ever calls the number on file (it is never shown here). '
                f'VALR\'s own number is <a href="tel:{e(s["valr_number"])}" class="num">{e(phone_label(s["valr_number"]))}</a>: '
                'call it any time and ask about your day.</p>'
                + ("" if s.get("test_call_done") else '<p class="small">Try it: call and ask "what\'s on my calendar today?"</p>'))
    prefs = reach_row(view)
    sp, lim = view["spend"], view["spend"]["interview_limits"]
    meters = "".join(meter(view["dials"][k]["used"], view["dials"][k]["limit"], title,
                           f"{'?' if view['dials'][k]['used'] is None else view['dials'][k]['used']} / {view['dials'][k]['limit']}")
                     for k, title in (("briefing", "Briefing calls today"), ("callback", "Research call-backs today")))
    meters += meter(sp["month_usd"], lim["month_usd"], "Research spend this month",
                    f"{money(sp['month_usd'])} of {money(lim['month_usd'])}")
    limits = row("limits", "Limits", "", f'<div class="meters">{meters}</div>'
                 '<p class="muted small">VALR stops for the day at a limit. <a href="/valr/system#spend">Spend details</a></p>')
    connected = sum(1 for x in (view["connections"]["google"].get("state") == "connected",
                                (view["connections"].get("telegram") or {}).get("linked") or not telegram,
                                any(t["state"] == "connected" for t in contracts.notetakers(view["connections"], attention.imported(view))
                                    if t["key"] not in contracts.OPTIONAL),
                                s.get("phone_on_file")) if x)
    issues = attention_list(items)
    return f"""{page_head("Settings", 'Connections <span class="dim">&amp; preferences.</span>',
                       f'{connected} of 4 essentials set up.')}
{f'<section class="card issues"><h2 class="card-title">Needs attention</h2>{issues}</section>' if issues else ''}
{setup(view)}
<section class="set-group" aria-labelledby="g-conn"><h2 id="g-conn" class="h-sm">Connections</h2>
  <ul class="set-list">{google_row(view, clock)}{calendar_row(view, clock)}{telegram_row(view, clock)}{notetaker_rows(view, clock)}{phone_recordings_row()}</ul></section>
<section class="set-group" aria-labelledby="g-calls"><h2 id="g-calls" class="h-sm">Calls</h2>
  <ul class="set-list">{prefs}{alerts_row()}{phone}{limits}</ul></section>
<p class="muted small"><a href="/valr/system">System details</a>: health, costs and recent research.</p>"""
