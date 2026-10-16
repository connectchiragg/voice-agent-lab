"""Server-rendered pieces of the VALR page for notetakers: the cards in "Get set up",
the recording placeholder of a past interview, and the scorecards' "Watch recording".

Every value is escaped; the API key is never part of the page (the form is empty and
posts to the server, see static/valr.js). The recording itself is loaded when a
detail view opens (`GET /valr/api/recording`), so the page never waits on a provider.
"""
import html
from typing import Any

STATE = {  # -> (label, tone)
    "connected": ("Connected", "ok"), "server": ("Connected · server key", "ok"),
    "refused": ("Key refused · reconnect", "bad"), "not_connected": ("Not connected", "muted"),
}
VIDEO = {"player": "Plays the recording right here", "link": "Opens the recording in Fireflies",
         "none": "Notes and transcript only (Granola keeps no recording)"}
ORDER = ("fathom", "fireflies", "granola")


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def chip(label: str, tone: str) -> str:
    return f'<span class="chip tone-{tone}">{e(label)}</span>'


def is_on(status: dict[str, Any]) -> bool:
    return status.get("state") in ("connected", "server")


def summary_buttons(notetakers: dict[str, dict[str, Any]]) -> str:
    """The checklist step's actions: one button per notetaker, each opening its form."""
    out = []
    for name in ORDER:
        s = notetakers.get(name)
        if not s or s.get("optional"):
            continue  # Optional connections (Granola) live only in the cards below.
        dot = "ok" if is_on(s) else "bad" if s["state"] == "refused" else "muted"
        out.append(f'<a class="btn small" href="#nt-{name}" data-connect-open="{name}">'
                   f'<span class="dot {dot}" aria-hidden="true"></span>{e(s["label"])}'
                   f'<span class="sr-only"> ({e(STATE.get(s["state"], ("", ""))[0])})</span></a>')
    return "".join(out)


def cards(notetakers: dict[str, dict[str, Any]]) -> str:
    if not notetakers:
        return '<p class="muted small">Notetaker status is unavailable right now.</p>'
    items = []
    for name in ORDER:
        s = notetakers.get(name)
        if not s:
            continue
        label, tone = STATE.get(s["state"], (s["state"], "muted"))
        if s.get("optional") and s["state"] == "not_connected":
            label, tone = "Optional", "muted"
        on = is_on(s)
        account = f'<span class="muted small">{e(s["account"])}</span>' if s.get("account") else ""
        connect_label = "Replace key" if s["state"] == "connected" else "Connect"
        buttons = [f'<button class="btn small{"" if on else " primary"}" type="button" '
                   f'data-connect-open="{name}" aria-controls="nt-form-{name}" aria-expanded="false">{connect_label}</button>']
        if on:
            sync_label = "Import past meetings" if name == "fireflies" else "Check for new recordings"
            buttons.append(f'<button class="btn small" type="button" data-action="ntsync" data-provider="{name}">'
                           f'{sync_label}</button>')
        if s["state"] in ("connected", "refused"):
            buttons.append(f'<button class="btn small ghost" type="button" data-action="ntdisconnect" '
                           f'data-provider="{name}">Disconnect</button>')
        store_note = ("" if s.get("can_store") else
                      '<p class="small tone-warn">The server has no VALR_TOKEN_KEY, so keys cannot be stored yet.</p>')
        form = f"""
  <form class="nt-form" id="nt-form-{name}" data-connect="{name}" hidden autocomplete="off">
    <label class="small" for="nt-key-{name}">{e(s["key_label"])}</label>
    <input id="nt-key-{name}" name="api_key" type="password" autocomplete="off" spellcheck="false"
           minlength="8" maxlength="512" required placeholder="Paste the key">
    <p class="muted small">{e(s["key_help"])} Stored encrypted on the server; never shown again.</p>{store_note}
    <div class="nt-row"><button class="btn small primary" type="submit">Save and test key</button>
    <button class="btn small ghost" type="button" data-connect-cancel="{name}">Cancel</button></div>
  </form>"""
        hint = ""
        if s.get("optional"):
            hint += '<p class="small nt-hint">API connect: needs a Granola Business or Enterprise plan.</p>'
        upload = ""
        if s.get("import"):
            upload = f"""
  <form class="nt-form nt-import" data-import="{name}">
    <label class="small" for="nt-file-{name}">No API key? Import Granola's export (CSV from Settings, JSON or Markdown)</label>
    <input id="nt-file-{name}" name="export" type="file" accept=".csv,.json,.md,.txt,text/csv,application/json,text/markdown">
    <div class="nt-row"><button class="btn small" type="submit">Import export</button></div>
  </form>"""
        items.append(f"""
<article class="nt-card" id="nt-{name}" data-provider="{name}">
  <header class="nt-head"><h3>{e(s["label"])}</h3>{chip(label, tone)}</header>
  {account}
  <p class="muted small">{e(s["gets"])}</p>
  <p class="small nt-video">{e(VIDEO.get(s["video"], ""))}</p>{hint}
  <div class="nt-row">{''.join(buttons)}</div>{form}{upload}
</article>""")
    return f'<div class="nt-grid" id="notetakers">{"".join(items)}</div>'


def recording_section(ref: str) -> str:
    """A past interview's recording; static/valr-player.js fills it when the detail view opens."""
    return (f'<section class="d-sec recording" data-recording-ref="{e(ref)}" aria-live="polite">'
            '<h3>Recording</h3><p class="muted small" data-recording-body>Looking for the recording in your '
            'notetakers… (needs JavaScript)</p></section>')


def watch_button(interview_id: str, providers: set[str]) -> str:
    provider = interview_id.partition(":")[0]
    if provider not in providers:
        return ""
    return (f'<button class="btn small" type="button" data-recording-id="{e(interview_id)}">'
            'Watch recording</button>')


DIALOG = """
<dialog class="drawer" id="recording-dialog" aria-labelledby="recording-title">
  <article class="drawer-inner">
    <header class="drawer-head"><div><p class="mono label">Past interview</p><h2 id="recording-title">Recording</h2></div>
      <button class="close" type="button" data-close aria-label="Close">✕</button></header>
    <section class="d-sec recording" aria-live="polite"><p class="muted small" data-recording-body>Loading…</p></section>
  </article>
</dialog>"""
