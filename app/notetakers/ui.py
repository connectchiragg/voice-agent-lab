"""Server-rendered pieces of the VALR page for notetakers: the Settings cards (logo, state,
account, last sync, Sync now, Replace key, Disconnect, the API-key help link),
the recording placeholder of a past interview, and the scorecards' "Watch recording".

Every value is escaped; the API key is never part of the page (the form is empty and
posts to the server, see static/valr.js). The recording itself is loaded when a
detail view opens (`GET /valr/api/recording`), so the page never waits on a provider.
"""
import html
from pathlib import Path
from typing import Any

STATE = {  # -> (label, tone)
    "connected": ("Connected", "ok"), "server": ("Connected · server key", "ok"),
    "refused": ("Key stopped working", "bad"), "not_connected": ("Not connected", "muted"),
}
VIDEO = {"player": "Plays the recording right here", "link": "Opens the recording in Fireflies",
         "none": "Notes and transcript only (Granola keeps no recording)"}
ORDER = ("fathom", "fireflies", "granola")
BRANDS = Path(__file__).resolve().parent.parent.parent / "static" / "brands"


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def chip(label: str, tone: str) -> str:
    return f'<span class="chip tone-{tone}">{e(label)}</span>'


def is_on(status: dict[str, Any]) -> bool:
    return status.get("state") in ("connected", "server")


def logo(name: str, label: str) -> str:
    """The provider's own mark (static/brands/<name>.svg, sources in each file) in a fixed
    neutral tile, only to label the connection; a lettermark when the file is missing."""
    if (BRANDS / f"{name}.svg").is_file():
        return (f'<span class="nt-logo" aria-hidden="true"><img src="/static/brands/{e(name)}.svg" alt="" '
                f'width="24" height="24" loading="lazy" decoding="async"></span>')
    return f'<span class="nt-logo letter" aria-hidden="true">{e(label[:1])}</span>'


def when(clock: Any, iso: str | None) -> str:
    if not iso:
        return ""
    return clock.when(iso) if clock is not None else e(iso[:10])


def summary_buttons(notetakers: dict[str, dict[str, Any]]) -> str:
    """The checklist step's actions: one button per notetaker, each opening its form."""
    out = []
    for name in ORDER:
        s = notetakers.get(name)
        if not s or s.get("optional"):
            continue  # Optional connections (Granola) live only in the cards below.
        dot = "ok" if is_on(s) else "bad" if s["state"] == "refused" else "muted"
        out.append(f'<a class="btn sm" href="#nt-{name}" data-connect-open="{name}">'
                   f'<span class="dot {dot}" aria-hidden="true"></span>{e(s["label"])}'
                   f'<span class="sr-only"> ({e(STATE.get(s["state"], ("", ""))[0])})</span></a>')
    return "".join(out)


def meta_lines(name: str, s: dict[str, Any], clock: Any) -> str:
    lines = []
    if s["state"] == "refused":
        last = when(clock, s.get("last_ok_at"))
        lines.append(f'<p class="nt-meta bad">The key was refused.{f" Last worked {last}." if last else ""}</p>')
    elif is_on(s):
        synced = when(clock, s.get("last_ok_at"))
        count = int(s.get("new_7d") or 0)
        parts = [f"Last sync {synced}" if synced else "Not synced yet",
                 f"{count} new this week" if count else "nothing new this week"]
        lines.append(f'<p class="nt-meta">{" · ".join(parts)}</p>')
        hook = s.get("webhook")
        if hook is not None:
            if hook.get("configured"):
                last = when(clock, hook.get("last_at"))
                lines.append(f'<p class="nt-meta">New recordings arrive by webhook{f" (last {last})" if last else ""}.</p>')
            else:
                lines.append('<p class="nt-meta warn">The webhook isn\'t set up: use Sync now after each interview.</p>')
    return "".join(lines)


def card(name: str, s: dict[str, Any], clock: Any = None) -> str:
    label, tone = STATE.get(s["state"], (s["state"], "muted"))
    if s.get("optional") and s["state"] == "not_connected":
        label, tone = "Optional", "muted"
    state = s["state"]
    account = f'<p class="nt-account">{e(s["account"])}</p>' if s.get("account") else ""
    connect_label = {"connected": "Replace key", "refused": "Replace key"}.get(state, "Connect")
    primary = state in ("not_connected", "refused")
    buttons = []
    if is_on(s):
        buttons.append(f'<button class="btn sm" type="button" data-action="ntsync" data-provider="{name}">Sync now</button>')
    if state != "server":
        buttons.append(f'<button class="btn sm{" primary" if primary else ""}" type="button" '
                       f'data-connect-open="{name}" aria-controls="nt-form-{name}" aria-expanded="false">{connect_label}</button>')
    if state in ("connected", "refused"):
        what = e(s["label"])
        buttons.append(
            f'<button class="btn sm ghost" type="button" data-action="ntdisconnect" data-provider="{name}" '
            f'data-confirm="Disconnect {what}?" data-confirm-body="VALR stops getting new recordings from {what}. '
            f'Recordings already in Past interviews stay; remove them one by one with Hide. The key isn&#x27;t kept." '
            f'data-confirm-ok="Disconnect">Disconnect</button>')
    managed = ('<p class="nt-meta">Managed in server settings: the key comes from the server\'s environment.</p>'
               if state == "server" else "")
    can_store = s.get("can_store")
    store_note = ("" if can_store else
                  '<p class="field-help warn">The server has no VALR_TOKEN_KEY, so keys can\'t be stored yet.</p>')
    disabled = "" if can_store else " disabled"
    form = f"""
  <form class="nt-form" id="nt-form-{name}" data-connect="{name}" hidden autocomplete="off">
    <label class="field-label" for="nt-key-{name}">{e(s["key_label"])}</label>
    <input class="input sm" id="nt-key-{name}" name="api_key" type="password" autocomplete="off" spellcheck="false"
           minlength="8" maxlength="512" required placeholder="Paste the key"{disabled}>
    <p class="field-help">{e(s["key_help"])} Stored encrypted on the server; never shown again.</p>{store_note}
    <div class="nt-row"><button class="btn sm primary" type="submit"{disabled}>Save and test key</button>
    <button class="btn sm ghost" type="button" data-connect-cancel="{name}">Cancel</button></div>
  </form>"""
    hint = ('<p class="field-help nt-hint">API connect needs a Granola Business or Enterprise plan.</p>'
            if s.get("optional") else "")
    help_link = (f'<a class="nt-help" href="{e(s["key_url"])}" target="_blank" rel="noopener noreferrer">'
                 f'How to get an API key<span aria-hidden="true"> ↗</span><span class="sr-only"> (opens {e(s["label"])}\'s docs)</span></a>'
                 if s.get("key_url") and state != "server" else "")
    upload = ""
    if s.get("import"):
        upload = f"""
  <details class="nt-import-fold"><summary>Import a file instead</summary>
  <form class="nt-form nt-import" data-import="{name}">
    <label class="field-label" for="nt-file-{name}">Granola's export (CSV from Settings, JSON or Markdown)</label>
    <input id="nt-file-{name}" name="export" type="file" accept=".csv,.json,.md,.txt,text/csv,application/json,text/markdown">
    <div class="nt-row"><button class="btn sm" type="submit">Import</button></div>
  </form></details>"""
    return f"""
<article class="nt-card state-{e(state)}" id="nt-{name}" data-provider="{name}">
  <header class="nt-head">{logo(name, s["label"])}<h4 class="nt-name">{e(s["label"])}</h4>{chip(label, tone)}</header>
  {account}{meta_lines(name, s, clock)}{managed}
  <p class="nt-gets">{e(s["gets"])}</p>
  <p class="field-help nt-video">{e(VIDEO.get(s["video"], ""))}</p>{hint}
  <div class="nt-row">{''.join(buttons)}</div>{help_link}{form}{upload}
</article>"""


def cards(notetakers: dict[str, dict[str, Any]], clock: Any = None) -> str:
    if not notetakers:
        return '<p class="muted">Notetaker status is unavailable right now.</p>'
    items = [card(name, notetakers[name], clock) for name in ORDER if notetakers.get(name)]
    return f'<div class="nt-grid" id="notetaker-cards">{"".join(items)}</div>'


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
