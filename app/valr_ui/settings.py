"""Settings: one short page, every row the same pattern (status, one line of what it's for,
the actions), no folds.

0. Finish setting up: only until done; every step can be finished here, without SSH.
1. Profile: avatar, name and the masked phone → Edit profile (/valr/profile).
2. Connections: Google (both syncs, Sync now, Reconnect, Disconnect) and the notetakers.
3. Calls & reminders: your phone (add, change, remove), the heads-up or the automatic call
   (with Pause for a day, a week or until resumed), VALR's own number.
4. Notifications: the alerts (immediate, the daily digest push, quiet hours, their times and
   a sample), this device's phone notifications and every subscribed device.
5. Call recordings: the Mac app, the iPhone shortcut and the upload token (on or off).
6. Data & account: the month's research spend, export, delete, sign-in and sign out everywhere.

Statuses come from the real state. static/valr-settings.js runs the forms and confirms;
static/valr-pwa.js the device card; static/valr-notify.js the alert switches and recordings.
"""
from typing import Any

from app import owner
from app.valr_page import Clock, chip, e, money, phone_label
from app.valr_ui import attention, contracts, pwa
from app.valr_ui.shell import asset, icon, page_head

STATE = {"connected": ("Connected", "ok"), "not_connected": ("Not connected", "warn"),
         "error": ("Needs a fix", "bad"), "unknown": ("Not checked yet", "muted")}
STEP_STATE = {"done": ("Done", "ok"), "todo": ("To do", "info"), "attention": ("Needs a fix", "warn")}
GOOGLE_ERRORS = {
    "cancelled": "You cancelled on Google's screen, so nothing changed.",
    "failed": "Google didn't confirm the connection. Try again.",
    "expired": "That link expired. Start again with Connect.",
    "account": "That was a different Google account. Pick the one VALR is set up for.",
    "unavailable": "Google sign-in isn't available on this server right now.",
}


def row(anchor: str, title: str, glyph: str, status: str, body: str, action: str = "", media: str = "") -> str:
    """One settings row. `media` replaces the icon tile (a logo or an avatar)."""
    lead = media or f'<span class="set-icon" aria-hidden="true">{icon(glyph, 18)}</span>'
    return f"""
<li class="set-row" id="{anchor}">
  {lead}
  <div class="set-main"><div class="set-title-row"><h3 class="set-title">{e(title)}</h3>{status}</div><div class="set-body">{body}</div></div>
  {f'<div class="set-action">{action}</div>' if action else ''}
</li>"""


def section(anchor: str, title: str, rows: str) -> str:
    return (f'<section class="section set-section" aria-labelledby="g-{anchor}"><div class="section-head">'
            f'<h2 id="g-{anchor}" class="section-title">{e(title)}</h2></div>'
            f'<ul class="set-list card">{rows}</ul></section>')


def confirm_attrs(title: str, body: str, ok: str) -> str:
    """valr-settings.js asks first ([data-confirm]) in a small sheet, then runs the action."""
    return f' data-confirm="{e(title)}" data-confirm-body="{e(body)}" data-confirm-ok="{e(ok)}"'


def avatar(size: str = "", label: str = "") -> str:
    """The owner's photo, else his initials (the same in the top bar)."""
    name = owner.name()
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
    cls = f"avatar-face {size}".strip()
    if owner.photo_etag():
        return (f'<img class="{cls}" src="/valr/profile/photo?v={e(owner.photo_etag())}" alt="{e(label)}" '
                f'width="64" height="64" data-avatar>')
    return f'<span class="{cls} initials" aria-hidden="true" data-avatar>{e(initials)}</span>'


# 0. Finish setting up.

def steps(view: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    from app.notetakers import ui as notetaker_ui
    google = view["connections"]["google"]
    takers = (view.get("connections") or {}).get("notetakers") or {}
    push = (view.get("connections") or {}).get("push") or {}
    s = view["setup"]
    g_done = google["state"] == "connected"
    g_state = "done" if g_done else "attention" if google["state"] != "not_connected" else "todo"
    g_action = ("" if g_done else '<a class="btn sm primary" href="/oauth/google/start">'
                + ("Reconnect" if google["state"] != "not_connected" else "Connect Google") + "</a>")
    required = [t for t in takers.values() if not t.get("optional")]
    refused = any(t["state"] == "refused" for t in required) and not any(notetaker_ui.is_on(t) for t in required)
    n_state = "attention" if refused else "done" if any(notetaker_ui.is_on(t) for t in required) else "todo"
    devices = len(push.get("devices") or [])
    talk = ('<a class="btn sm" href="/call">Talk in browser</a>'
            + ('<button class="btn sm primary" type="button" data-action="callnow">Call my phone</button>'
               if s["phone_on_file"] else ""))
    return [
        ("Connect Google", g_state, "So VALR can see your interview invites and job emails. Read-only.", g_action),
        ("Connect a notetaker", n_state, "Fathom or Fireflies: VALR reviews each recorded interview and plays it back here.",
         "" if n_state == "done" else (notetaker_ui.summary_buttons(takers) or '<a class="btn sm" href="#notetakers">Notetakers</a>')),
        ("Add your phone", "done" if s["phone_on_file"] else "todo",
         "VALR only ever calls the number you add here.",
         "" if s["phone_on_file"] else '<button class="btn sm primary" type="button" data-phone-edit>Add phone</button>'),
        ("Turn on phone notifications", "done" if devices else "todo",
         "Heads-ups before interviews and alerts reach your lock screen.",
         "" if devices else '<a class="btn sm" href="#phone-push">Set up</a>'),
        ("Make a test call", "done" if s["test_call_done"] else "todo",
         "Ask VALR \"what's on my calendar today?\"", "" if s["test_call_done"] else talk),
    ]


def setup(view: dict[str, Any]) -> str:
    """The set-up checklist; gone once every step is done. Granola is optional, never a step."""
    todo = steps(view)
    done = sum(1 for _, state, _, _ in todo if state == "done")
    if done == len(todo):
        return ""
    items = "".join(f"""
<li class="step{' done' if state == 'done' else ''}">
  <span class="step-mark" aria-hidden="true">{icon('check', 14) if state == 'done' else i}</span>
  <div class="step-body"><p class="step-title">{e(title)}</p><p class="muted">{e(text)}</p></div>
  <div class="step-action">{action if state != 'done' and action else chip(*STEP_STATE[state])}</div>
</li>""" for i, (title, state, text, action) in enumerate(todo, 1))
    pct = round(done / len(todo) * 100)
    return f"""
<section class="card setup" id="setup" aria-labelledby="setup-title">
  <div class="card-head"><h2 class="card-title" id="setup-title">Finish setting up</h2><span class="card-meta">{done} of {len(todo)} done</span></div>
  <div class="progress-bar" role="progressbar" aria-label="Set-up progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pct}"><i style="width:{pct}%"></i></div>
  <ol class="steps">{items}</ol>
</section>"""


# 1. Profile.

def profile_row(view: dict[str, Any]) -> str:
    me = owner.view()
    phone = me["phone"]["masked"] or "No phone yet"
    name = me["display_name"] or "Add your name"
    return row("profile", name, "users", "", f'<p class="num">{e(phone)} · {e(me["timezone"])}</p>',
               '<a class="btn sm" href="/valr/profile">Edit profile</a>',
               media=f'<span class="set-media">{avatar()}</span>')


# 2. Connections.

def synced(clock: Clock, label: str, when: str | None) -> str:
    return f"{label} synced {clock.when(when)}" if when else f"{label} not synced yet"


def google_row(view: dict[str, Any], clock: Clock) -> str:
    g = view["connections"]["google"]
    cal = view["connections"].get("calendar") or {}
    gmail = view["connections"].get("gmail") or {}
    state = g.get("state")
    query = view.get("_query") or {}
    problem = GOOGLE_ERRORS.get(query.get("google_error", ""), "")
    note = f'<p class="field-help bad" role="alert">{e(problem)}</p>' if problem else ""
    disconnect = ('<button class="btn sm ghost" type="button" data-settings-action="google-disconnect"'
                  + confirm_attrs("Disconnect Google?", "VALR stops seeing new invites and job emails. Leads and "
                                  "interviews already found stay. You can connect again any time.", "Disconnect")
                  + '>Disconnect</button>')
    if not g.get("configured"):
        return row("google", "Google", "mail", chip("Not available", "muted"),
                   '<p class="muted">Google sign-in isn\'t set up on this server yet.</p>' + note)
    if state == "connected":
        expiring = [i for i in attention.google_items(view) if i["key"] == "google_expiring"]
        status = chip("Connected", "ok") + (f' {chip(expiring[0]["title"], "warn")}' if expiring else "")
        lines = [synced(clock, "Calendar", cal.get("last_sync_at")), synced(clock, "Gmail", gmail.get("last_sync_at"))]
        failed = '<p class="field-help warn">The last calendar sync failed: try Sync now.</p>' if cal.get("last_error") else ""
        body = (f'<p>{e(g.get("account") or "Your Google account")} · read-only: calendar invites and job emails.</p>'
                f'<p class="muted">{" · ".join(lines)}</p>{failed}{note}')
        action = ('<button class="btn sm" type="button" data-settings-action="google-sync">Sync now</button>'
                  '<a class="btn sm ghost" href="/oauth/google/start">Reconnect</a>' + disconnect)
        return row("google", "Google", "mail", status, body, action)
    if state == "reconnect_needed":
        return row("google", "Google", "mail", chip("Access expired", "bad"),
                   '<p>New invites and emails won\'t show up until you reconnect.</p>' + note,
                   '<a class="btn sm primary" href="/oauth/google/start">Reconnect Google</a>' + disconnect)
    status = chip("Not connected", "warn") if state == "not_connected" else chip("Not checked yet", "muted")
    return row("google", "Google", "mail", status,
               '<p>So VALR can see your interview invites and job emails. Read-only.</p>' + note,
               '<a class="btn sm primary" href="/oauth/google/start">Connect Google</a>')


def notetaker_row(view: dict[str, Any], clock: Clock) -> str:
    """The three notetaker cards (app/notetakers/ui.py): logo, state, account, last sync, the
    actions; the API key is posted, never shown again."""
    from app.notetakers import ui as notetaker_ui
    takers = view["connections"].get("notetakers") if isinstance(view["connections"].get("notetakers"), dict) else {}
    if takers:
        body = notetaker_ui.cards(takers, clock)
    else:  # Status unavailable: what the imported recordings show.
        states = contracts.notetakers(view["connections"], attention.imported(view))
        body = '<ul class="mini-rows">' + "".join(
            f'<li class="mini-row"><span>{e(t["label"])}</span>{chip(*STATE[t["state"]])}</li>' for t in states) + "</ul>"
    return row("notetakers", "Notetakers", "video", "",
               '<p>VALR reviews your recorded interviews to coach you. Fathom or Fireflies is enough; '
               'Granola is optional.</p>' + body)


# 3. Calls & reminders.

PHONE_FORM = """
<form class="inline-form" data-phone-form hidden autocomplete="off" novalidate>
  <label class="field-label" for="phone-input">Your mobile number</label>
  <div class="field-row"><input class="input" id="phone-input" name="phone" type="tel" inputmode="tel" autocomplete="tel"
    maxlength="24" placeholder="+91 98765 43210" aria-describedby="phone-help phone-error" required>
    <button class="btn primary" type="submit">Save</button></div>
  <p class="field-help" id="phone-help">With the country code. A 10-digit Indian mobile works too.</p>
  <p class="field-help bad" id="phone-error" data-phone-error role="alert" hidden></p>
  <div><button class="btn sm ghost" type="button" data-phone-cancel>Cancel</button></div>
</form>"""


def phone_row(view: dict[str, Any]) -> str:
    me = owner.view()["phone"]
    used = '<p class="muted">Used for Call my phone, automatic calls, and recognising you when you call VALR.</p>'
    if not me["set"]:
        return row("phone", "Your phone", "phone", chip("Not added", "warn"),
                   '<p>Add your phone so VALR can call you.</p>' + PHONE_FORM,
                   '<button class="btn sm primary" type="button" data-phone-edit>Add phone</button>')
    source = ' <span class="muted">· from server settings</span>' if me["source"] == "env" else ""
    remove = ('<button class="btn sm ghost" type="button" data-settings-action="phone-remove"'
              + confirm_attrs("Remove your phone?", "VALR won't be able to call you, and calls from this number "
                              "won't be recognised as you. Automatic calls switch to heads-ups. Talk in browser "
                              "still works.", "Remove")
              + '>Remove</button>')
    return row("phone", "Your phone", "phone", chip("On file", "ok"),
               f'<p class="num big-num">{e(me["masked"])}{source}</p>{used}{PHONE_FORM}',
               '<button class="btn sm" type="button" data-phone-edit>Change</button>' + remove)


def pause_label(view: dict[str, Any], clock: Clock) -> str:
    until = (view.get("dials_paused") or {}).get("until")
    if not until:
        return "Paused"
    moment = clock.local(until)
    return f"Paused until {moment:%a} {moment.day} {moment:%b}"


def reach_row(view: dict[str, Any], clock: Clock) -> str:
    """The heads-up (default) or an automatic call, stored on the server; and Pause."""
    heads_up = view.get("mode") != "call"
    reach = view.get("reach") or {}
    paused = (view.get("dials_paused") or {}).get("enabled")
    phone = owner.phone()
    push = (view.get("connections") or {}).get("push") or {}
    how = ("A notification on your phone 30 minutes before each interview. Tap it for the briefing call; "
           "no tap, no call." if heads_up else "VALR calls you 30 minutes before each interview, once. No retries.")
    default = "heads-up" if not reach.get("default_auto_dial") else "automatic call"
    source = "Your choice." if reach.get("source") == "settings" else f"The default ({default})."
    call_off = ' disabled aria-describedby="reach-needs-phone"' if not phone and heads_up else ""
    toggle = (f'<div class="seg" role="group" aria-label="How VALR reminds you">'
              f'<button type="button" data-action="reach" data-mode="heads_up" aria-pressed="{"true" if heads_up else "false"}">Heads-up</button>'
              f'<button type="button" data-action="reach" data-mode="call" aria-pressed="{"false" if heads_up else "true"}"{call_off}>Automatic call</button></div>')
    warn = ""
    if heads_up and push.get("configured") and not push.get("devices"):
        warn = ('<p class="field-help warn">Phone notifications are off, so heads-ups only reach the bell. '
                '<a href="#phone-push">Enable them</a>.</p>')
    if not phone:
        warn += '<p class="field-help" id="reach-needs-phone">Automatic calls need your phone: <a href="#phone">add it</a>.</p>'
    if paused:
        action = ('<button class="btn sm" type="button" data-action="dials" data-paused="false" aria-pressed="true">'
                  'Resume</button>')
    else:
        options = "".join(f'<button class="menu-item" role="menuitem" type="button" data-action="dials" data-paused="true"'
                          f' data-days="{days}"><span>{e(label)}</span></button>'
                          for days, label in (("1", "For 1 day"), ("7", "For 1 week"), ("", "Until I resume")))
        action = (f'<details class="menu pause-menu" data-menu><summary class="btn sm">{icon("pause", 14)}Pause…</summary>'
                  f'<div class="menu-pop align-end" role="menu">{options}</div></details>')
    return row("calls", "Before each interview", "clock", chip(pause_label(view, clock), "warn") if paused else "",
               f'{toggle}<p class="muted">{e(how)} {e(source)}</p>{warn}', action)


def number_row(view: dict[str, Any]) -> str:
    number = view["setup"]["valr_number"]
    note = ("A US number: calling it from India costs international rates. Talk in browser is free."
            if number.startswith("+1") else "Call it any time and ask about your day.")
    return row("dial-in", "VALR's number", "phone", "",
               f'<p><a class="num big-num" href="tel:{e(number)}">{e(phone_label(number))}</a></p><p class="muted">{e(note)}</p>',
               f'<button class="btn sm" type="button" data-copy="{e(number)}">Copy</button>'
               '<a class="btn sm ghost" href="/call">Talk in browser</a>')


# 4. Notifications.

def hours(selected: str, name: str, label: str) -> str:
    whole = [f"{h:02d}:00" for h in range(24)]
    options = "".join(f'<option value="{v}"{" selected" if selected == v else ""}>'
                      f'{(int(v[:2]) % 12) or 12}:00 {"AM" if int(v[:2]) < 12 else "PM"}</option>' for v in whole)
    if selected and selected not in whole:
        options = f'<option value="{e(selected)}" selected>{e(selected)}</option>' + options
    return (f'<label class="time-pick"><span class="sr-only">{e(label)}</span>'
            f'<select class="input sm" data-notif-time="{name}" disabled>{options}</select></label>')


def alerts_row(view: dict[str, Any]) -> str:
    """The alert switches and times (app/notifications.py); static/valr-notify.js loads and saves them."""
    times = view.get("notify") or {}
    digest_at = times.get("digest_at", "09:00")
    start, end = times.get("quiet_start", "23:00"), times.get("quiet_end", "08:00")

    def switch(key: str, title: str, help_: str, extra: str = "") -> str:
        return (f'<div class="notif-line"><label class="switch notif-switch"><input type="checkbox" data-notif-switch="{key}" disabled>'
                f'<span class="switch-ui" aria-hidden="true"></span><span><span class="switch-title">{e(title)}</span>'
                f'<span class="field-help">{e(help_)}</span></span></label>{extra}</div>')
    body = ('<div class="notif-switches">'
            + switch("immediate", "Immediate alerts", "Offers, interview invites, replies that need you, ready reviews "
                     "and calendar changes in the next 48 hours: one push each.")
            + switch("digest", "Daily digest", "Rejections, other replies and later calendar changes, in one push.",
                     f'<div class="time-row">at {hours(digest_at, "digest_at", "Digest time")}</div>')
            + switch("quiet", "Quiet hours", "Pushes wait until the end, except an interview in the next 12 hours.",
                     f'<div class="time-row">{hours(start, "quiet_start", "Quiet hours start")} to '
                     f'{hours(end, "quiet_end", "Quiet hours end")}</div>')
            + '</div><p class="muted">Everything also stays in the bell for 30 days. Times are in your time zone '
            f'({e(owner.zone_name())}).</p>')
    return row("alerts", "Alerts", "bell", "", body,
               '<button class="btn sm" type="button" data-settings-action="sample-digest">Send a sample digest</button>')


# 5. Call recordings.

IPHONE_STEPS = [
    "During a call, tap More → Call Recording. iPhone announces it to both people; tell the other person first.",
    "After the call, iPhone saves the recording in Notes.",
]
MAC_STEPS = [
    "Open the download and drag VALR to Applications.",
    "The first time, right-click VALR and choose Open.",
    "Paste your upload token (below).",
    "Allow Full Disk Access when it asks.",
    "Click Start syncing.",
]


def mac_download() -> str:
    """VALR for Mac's Download button, version, size and SHA-256 (app/mac_release.py)."""
    from app import mac_release
    release = mac_release.info()
    if release is None:
        return '<p class="field-help">The Mac app isn\'t in this build yet.</p>'
    return (f'<div class="rec-download"><a class="btn sm primary" href="/valr/downloads/mac" download>'
            f'{icon("download", 16)}Download for Mac</a>'
            f'<span class="field-help num">DMG · version {e(release["version"])} · {e(release["size_label"])} · macOS 14 or later</span></div>'
            f'<p class="field-help rec-sha">SHA-256 <code class="token">{e(release["sha256"])}</code></p>')


def phone_recordings_row(host: str = "") -> str:
    """Call recordings: iPhone call recording (via the Mac app) or Truecaller (via the Shortcut),
    the shared upload token (show once, rotate, turn off), and the last upload. static/valr-notify.js
    fills the status from /valr/api/phone/status and runs the token buttons."""
    iphone = "".join(f"<li>{e(step)}</li>" for step in IPHONE_STEPS) + (
        '<li>It reaches VALR automatically through the Mac app (below), or tap Share → Send to VALR on the iPhone '
        '(<a href="/valr/settings/phone-help">iPhone shortcut steps</a>).</li>')
    mac = "".join(f"<li>{e(step)}</li>" for step in MAC_STEPS)
    address = f"https://{host}/hooks/recording-upload" if host else "/hooks/recording-upload"
    body = (
        '<p>Recordings come from iPhone call recording (through VALR for Mac) or Truecaller (through the Shortcut). '
        'VALR transcribes each call, adds it to Past interviews and the coach reviews it. '
        'The audio is stored encrypted and deleted a week later.</p>'
        '<section class="rec-part" aria-labelledby="rec-status"><h4 class="sub-title" id="rec-status">Status</h4>'
        '<p class="muted" data-phone-status>Checking…</p></section>'
        '<section class="rec-part" aria-labelledby="rec-iphone"><h4 class="sub-title" id="rec-iphone">Record calls on iPhone</h4>'
        f'<ol class="notif-steps">{iphone}</ol></section>'
        '<section class="rec-part" aria-labelledby="rec-mac"><h4 class="sub-title" id="rec-mac">VALR for Mac</h4>'
        f'{mac_download()}<ol class="notif-steps">{mac}</ol>'
        '<ul class="rec-notes field-help"><li>Your Mac must be on and signed into the same iCloud with Notes sync on.</li>'
        '<li>Only call recordings are sent; note text never leaves your Mac.</li></ul></section>'
        '<section class="rec-part" aria-labelledby="rec-token"><h4 class="sub-title" id="rec-token">Upload token</h4>'
        '<p class="field-help">The Mac app and the iPhone shortcut share the same token. Rotating it means pasting the new one into both.</p>'
        '<div class="rec-buttons" data-phone-on><button class="btn sm" type="button" data-phone-action="show">Show upload token</button>'
        '<button class="btn sm ghost" type="button" data-phone-action="rotate"'
        + confirm_attrs("Rotate the upload token?", "VALR for Mac and the iPhone Shortcut stop sending until you paste "
                        "the new token into both.", "Rotate") + '>Rotate token</button>'
        '<button class="btn sm ghost" type="button" data-phone-action="revoke"'
        + confirm_attrs("Turn off uploads?", "The token stops working at once. Recordings already in Past interviews "
                        "stay. Turn uploads on again for a new token.", "Turn off") + '>Turn off uploads</button></div>'
        '<div class="rec-buttons" data-phone-off hidden><button class="btn sm primary" type="button" data-phone-action="rotate">'
        'Turn uploads on</button></div>'
        '<div class="notif-token" data-phone-token hidden><code class="token" data-phone-token-value></code>'
        '<button class="btn sm" type="button" data-phone-copy>Copy</button>'
        '<p class="field-help">Copy it now: it won’t be shown again.</p></div>'
        f'<p class="field-help">Upload address: <code class="token" data-phone-url>{e(address)}</code></p></section>')
    return row("phone-recordings", "Phone call recordings", "mic", "", body)


# 6. Data & account.

def data_row(view: dict[str, Any]) -> str:
    spend = view.get("spend") or {}
    cap = (spend.get("interview_limits") or {}).get("month_usd")
    line = (f'Research spend this month: <span class="num">{money(spend.get("month_usd"))}</span>'
            + (f' of <span class="num">{money(cap)}</span>' if cap else "") + ".")
    delete = ('<button class="btn sm danger" type="button" data-settings-action="data-delete"'
              + confirm_attrs("Delete all your VALR data?", "Interviews, briefings, leads, notifications, recordings, "
                              "transcripts, scorecards and settings are deleted, Google is disconnected and every "
                              "device is signed out. This can't be undone. Export first if you want a copy.",
                              "Delete everything")
              + ' data-confirm-type="DELETE">Delete my data</button>')
    return row("data", "Your data", "file-text", "",
               f'<p>{line}</p><p class="muted">The export is a zip of JSON files: everything VALR keeps about your '
               'search, without audio, keys or tokens.</p>',
               '<a class="btn sm" href="/valr/api/export" download>Export my data</a>' + delete)


def account_row(email: str) -> str:
    who = f'<p>Signed in with Google as <b class="email">{e(email)}</b>.</p>' if email else ""
    return row("account", "Account", "log-out", "",
               who + '<p class="muted">Signing out everywhere ends every session, the iPhone app included.</p>',
               '<button class="btn sm" type="button" data-settings-action="logout-all"'
               + confirm_attrs("Sign out of all devices?", "Every browser and the iPhone app must sign in again.",
                               "Sign out everywhere")
               + '>Sign out of all devices</button>')


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]]) -> str:
    email = str(view.get("_email") or "")
    connected = (view.get("_query") or {}).get("connected") == "google"
    toast = '<p class="sr-only" data-toast-on-load="Google connected.">Google connected.</p>' if connected else ""
    return f"""{page_head("Settings")}{toast}
{setup(view)}
{section("profile", "Profile", profile_row(view))}
{section("conn", "Connections", google_row(view, clock) + notetaker_row(view, clock))}
{section("calls", "Calls & reminders", phone_row(view) + reach_row(view, clock) + number_row(view))}
{section("notify", "Notifications", alerts_row(view) + pwa.card())}
{section("rec", "Call recordings", phone_recordings_row(str(view.get("_host") or "")))}
{section("data", "Data & account", data_row(view) + account_row(email))}
<script type="module" src="{asset('valr-settings.js')}"></script>"""
