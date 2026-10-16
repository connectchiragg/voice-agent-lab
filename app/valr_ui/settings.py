"""Settings: connections (Google, calendar, Telegram, notetakers), the phone, call
preferences (pause, how VALR reaches you) and today's limits. Statuses come from the
real state; no server or env-var talk on this page."""
from typing import Any

from app.valr_page import Clock, chip, e, meter, money, phone_label
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


def notetaker_rows(view: dict[str, Any], clock: Clock) -> str:
    rows = []
    given = view["connections"].get("notetakers") if isinstance(view["connections"].get("notetakers"), dict) else {}
    for t in contracts.notetakers(view["connections"], attention.imported(view)):
        label, tone = STATE[t["state"]]
        if t["key"] in contracts.OPTIONAL and t["state"] != "connected":
            label, tone = "Optional", "muted"
        detail = []
        if t["account"]:
            detail.append(e(t["account"]))
        if t["last_import_at"]:
            detail.append(f"last recording {clock.when(t['last_import_at'])}")
        if t["error"]:
            detail.append(e(t["error"]))
        raw = given.get(t["key"]) if isinstance(given.get(t["key"]), dict) else {}
        url = str(raw.get("connect_url") or "")
        action = (f'<a class="btn small" href="{e(url)}">{"Reconnect" if t["state"] == "connected" else "Connect"}</a>'
                  if url.startswith(("/", "https://")) else "")
        rows.append(f'<li class="mini-row"><span class="mini-name">{e(t["label"])}</span>{chip(label, tone)}'
                    f'<span class="muted small">{" · ".join(detail)}</span>{action}</li>')
    return row("notetakers", "Notetakers", "",
               '<p class="muted small">VALR reviews your recorded interviews to coach you. Any one is enough.</p>'
               f'<ul class="mini-rows">{"".join(rows)}</ul>')


def page(view: dict[str, Any], clock: Clock, items: list[dict[str, Any]]) -> str:
    s = view["setup"]
    telegram = view.get("mode") == "telegram"
    paused = (view.get("dials_paused") or {}).get("enabled")
    noun = "heads-ups" if telegram else "calls"
    phone = row("phone", "Your phone", chip("On file", "ok") if s.get("phone_on_file") else chip("Missing", "bad"),
                f'<p class="muted small">VALR only ever calls the number on file (it is never shown here). '
                f'VALR\'s own number is <a href="tel:{e(s["valr_number"])}" class="num">{e(phone_label(s["valr_number"]))}</a>: '
                'call it any time and ask about your day.</p>'
                + ("" if s.get("test_call_done") else '<p class="small">Try it: call and ask "what\'s on my calendar today?"</p>'))
    how = ("A Telegram message 30 minutes before each interview. Tap 📞 for the briefing call; no reply, no call."
           if telegram else "VALR calls you 30 minutes before each interview, once.")
    prefs = row("calls", "How VALR reaches you",
                chip("Paused", "warn") if paused else chip("Telegram first" if telegram else "Automatic call", "info"),
                f'<p class="muted small">{e(how)}</p><p class="muted small">Switching between a heads-up and an '
                'automatic call is a server choice for now.</p>',
                f'<button class="btn small" type="button" data-action="dials" data-paused="{"false" if paused else "true"}" '
                f'aria-pressed="{"true" if paused else "false"}">{"Resume " + noun if paused else "Pause " + noun}</button>'
                '<button class="btn small" type="button" data-action="callnow">Call me now</button>')
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
<section class="set-group" aria-labelledby="g-conn"><h2 id="g-conn" class="h-sm">Connections</h2>
  <ul class="set-list">{google_row(view, clock)}{calendar_row(view, clock)}{telegram_row(view, clock)}{notetaker_rows(view, clock)}</ul></section>
<section class="set-group" aria-labelledby="g-calls"><h2 id="g-calls" class="h-sm">Calls</h2>
  <ul class="set-list">{prefs}{phone}{limits}</ul></section>
<p class="muted small"><a href="/valr/system">System details</a>: health, costs and recent research.</p>"""
