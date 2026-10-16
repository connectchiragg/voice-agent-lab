"""The frame around every VALR page: the top bar (navigation, the notification bell, one
"Call me" button with "Talk in browser" under its arrow, light/dark, the account menu),
the page header, the mobile tab bar, and the small shared pieces (icons, menus, empty
states).

Server-rendered; static/valr.js (countdowns, actions, prep ticks, quiet refresh),
static/valr-app.js (theme, menus and sheets, globe, leads), static/valr-notify.js (the bell,
Home's "New" strip, read on click) and static/valr-player.js (past interviews) enhance it. Styles:
static/valr-ds.css (the design system) and static/leads.css (bands and the globe stage).
"""
import hashlib
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from app.valr_page import FONTS, ICON, Clock, e

NAV = [("home", "Home", "/valr"), ("interviews", "Interviews", "/valr/interviews"),
       ("leads", "Leads", "/valr/leads"), ("progress", "Progress", "/valr/progress"),
       ("settings", "Settings", "/valr/settings")]
NAV_ICON = {"home": "house", "interviews": "calendar", "leads": "globe", "progress": "trending-up",
            "settings": "settings"}

# Lucide icons (ISC licence, lucide.dev): 24x24, 2px round strokes in currentColor.
ICONS = {
    "house": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "calendar": '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "trending-up": '<path d="M22 7 13.5 15.5 8.5 10.5 2 17"/><path d="M16 7h6v6"/>',
    "settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    "phone": '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "check-circle": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "mail": '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "log-out": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "file-text": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "volume": '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    "award": '<circle cx="12" cy="8" r="6"/><path d="M15.477 12.89 17 22l-5-3-5 3 1.523-9.11"/>',
    "user-plus": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M22 11h-6"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "circle-x": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "arrow-up-right": '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    "sparkles": '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.287 1.288L3 12l5.8 1.9a2 2 0 0 1 1.288 1.287L12 21l1.9-5.8a2 2 0 0 1 1.287-1.288L21 12l-5.8-1.9a2 2 0 0 1-1.288-1.287Z"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "list-checks": '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
    "lightbulb": '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "rupee": '<path d="M6 3h12"/><path d="M6 8h12"/><path d="m6 13 8.5 8"/><path d="M6 13h3"/><path d="M9 13c6.667 0 6.667-10 0-10"/>',
    "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/>',
    "video": '<path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
}


STATIC = Path(__file__).resolve().parent.parent.parent / "static"


@cache
def asset(name: str) -> str:
    """/static/<name>?v=<content hash>, so a deploy never serves yesterday's CSS or JS."""
    try:
        digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={digest}"


def icon(name: str, size: int = 18) -> str:
    return (f'<svg class="i i-{name}" viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true" focusable="false">{ICONS[name]}</svg>')


def fix_control(fix: dict[str, Any] | None, small: bool = True) -> str:
    """The one-click fix: a link, or a button that valr.js posts (with the CSRF token)."""
    if not fix:
        return ""
    cls = "btn sm" if small else "btn"
    if fix.get("href"):
        return f'<a class="{cls}" href="{e(fix["href"])}">{e(fix["label"])}</a>'
    data = "".join(f' data-{e(k)}="{e(v)}"' for k, v in (fix.get("data") or {}).items())
    return f'<button class="{cls}" type="button" data-action="{e(fix["action"])}"{data}>{e(fix["label"])}</button>'


def alert_strip(items: list[dict[str, Any]]) -> str:
    """Only what is broken right now, one line each, with its fix. Silence = healthy. "bad"
    items, plus the broken connections that are only "warn" (a failed calendar sync, a
    notetaker that stopped syncing): the bell no longer pins them, so they show here."""
    bad = [i for i in items if i["level"] == "bad" or i["key"] == "calendar" or i["key"].startswith("notetaker-")]
    if not bad:
        return ""
    rows = "".join(f'<li class="alert">{icon("alert", 16)}<span class="alert-text"><b>{e(i["title"])}</b> '
                   f'<span class="alert-detail">{e(i["detail"])}</span></span>{fix_control(i["fix"])}</li>' for i in bad)
    return f'<ul class="alerts" role="status">{rows}</ul>'


def menu(items: list[str], label: str = "More actions", cls: str = "", align: str = "end") -> str:
    """A small ⋯ menu (a <details>; static/valr-app.js closes it on Escape or a click outside).
    `items` are ready-made elements from menu_item()."""
    items = [i for i in items if i]
    if not items:
        return ""
    return (f'<details class="menu {e(cls)}" data-menu><summary class="icon-btn" aria-label="{e(label)}">{icon("more", 18)}</summary>'
            f'<div class="menu-pop align-{align}" role="menu">{"".join(items)}</div></details>')


def menu_item(label: str, icon_name: str = "", href: str = "", attrs: str = "", external: bool = False) -> str:
    glyph = icon(icon_name, 16) if icon_name else ""
    if href:
        target = ' target="_blank" rel="noopener noreferrer"' if external else ""
        return f'<a class="menu-item" role="menuitem" href="{e(href)}"{target}{attrs}>{glyph}<span>{e(label)}</span></a>'
    return f'<button class="menu-item" role="menuitem" type="button"{attrs}>{glyph}<span>{e(label)}</span></button>'


def sheet(sheet_id: str, title: str, body: str) -> str:
    """A small modal sheet (<dialog>), opened by any [data-sheet="<id>"] (static/valr-app.js)."""
    return (f'<dialog class="sheet" id="{e(sheet_id)}" aria-labelledby="{e(sheet_id)}-title">'
            f'<div class="sheet-head"><h2 class="sheet-title" id="{e(sheet_id)}-title">{e(title)}</h2>'
            f'<button class="icon-btn" type="button" data-sheet-close aria-label="Close">{icon("x", 18)}</button></div>'
            f'<div class="sheet-body">{body}</div></dialog>')


ILLUSTRATIONS = {  # Small line drawings for empty states (static/valr-ds.css: .ill-*).
    "calendar": ('<svg class="ill" viewBox="0 0 120 88" aria-hidden="true"><rect x="22" y="14" width="76" height="62" rx="10" class="ill-a"/>'
                 '<path d="M22 32h76" class="ill-line"/><path d="M42 8v12M78 8v12" class="ill-line"/>'
                 '<circle cx="46" cy="48" r="4" class="ill-dot"/><circle cx="60" cy="48" r="4" class="ill-dot"/>'
                 '<circle cx="74" cy="48" r="4" class="ill-b"/><circle cx="46" cy="62" r="4" class="ill-dot"/></svg>'),
    "inbox": ('<svg class="ill" viewBox="0 0 120 88" aria-hidden="true"><path d="M20 48 34 18h52l14 30v22a8 8 0 0 1-8 8H28a8 8 0 0 1-8-8z" class="ill-a"/>'
              '<path d="M20 48h24l6 10h20l6-10h24" class="ill-line"/><circle cx="60" cy="34" r="6" class="ill-b"/></svg>'),
    "orbit": ('<svg class="ill" viewBox="0 0 120 88" aria-hidden="true"><circle cx="60" cy="44" r="26" class="ill-a"/>'
              '<ellipse cx="60" cy="44" rx="46" ry="14" class="ill-line" transform="rotate(-16 60 44)"/>'
              '<circle cx="99" cy="33" r="5" class="ill-b"/><circle cx="50" cy="38" r="3" class="ill-dot"/><circle cx="68" cy="52" r="3" class="ill-dot"/></svg>'),
    "check": ('<svg class="ill" viewBox="0 0 120 88" aria-hidden="true"><circle cx="60" cy="44" r="28" class="ill-a"/>'
              '<path d="m47 45 9 9 18-20" class="ill-line strong"/><circle cx="96" cy="20" r="4" class="ill-b"/><circle cx="24" cy="66" r="3" class="ill-dot"/></svg>'),
    "chart": ('<svg class="ill" viewBox="0 0 120 88" aria-hidden="true"><rect x="18" y="12" width="84" height="64" rx="10" class="ill-a"/>'
              '<path d="M30 60 48 46l12 8 24-22" class="ill-line strong"/><circle cx="84" cy="32" r="5" class="ill-b"/></svg>'),
    "video": ('<svg class="ill" viewBox="0 0 120 88" aria-hidden="true"><rect x="16" y="18" width="68" height="52" rx="10" class="ill-a"/>'
              '<path d="m84 36 20-12v40L84 52" class="ill-line"/><path d="m44 34 14 10-14 10z" class="ill-b"/></svg>'),
}


def empty_state(art: str, title: str, body: str = "", action: str = "", cls: str = "") -> str:
    """One line, one action, one small drawing. `title` and `body` are text; `action` is HTML."""
    return (f'<div class="empty {e(cls)}">{ILLUSTRATIONS.get(art, "")}<p class="empty-title">{e(title)}</p>'
            + (f'<p class="empty-body">{e(body)}</p>' if body else "")
            + (f'<div class="empty-action">{action}</div>' if action else "") + "</div>")


def theme_attr(theme: str | None) -> str:
    return f' data-theme="{theme}"' if theme in ("light", "dark") else ""


THEME_BOOT = ("<script>try{var t=localStorage.getItem('valr-theme');var d=document.documentElement;"
              "if(!d.dataset.theme&&(t==='light'||t==='dark'))d.dataset.theme=t;}catch(_){}</script>")


def pwa_head() -> str:
    from app.valr_ui import pwa  # The Home Screen app: manifest, iOS metas, icon, valr-pwa.js.
    return pwa.head()


def head(title: str, theme: str | None, csrf: str = "") -> str:
    """The <head> shared by the app, the sign-in pages and the error page (left open)."""
    token = f'<meta name="csrf-token" content="{e(csrf)}">' if csrf else ""
    return f"""<!doctype html>
<html lang="en"{theme_attr(theme)}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">{token}
<meta name="theme-color" content="#f6f7f9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0c0f" media="(prefers-color-scheme: dark)">
<title>{e(title)} · VALR</title>
<link rel="icon" href="{ICON}">{THEME_BOOT}{FONTS}
<link rel="stylesheet" href="{asset('valr-ds.css')}">{pwa_head()}"""


def call_button() -> str:
    """The one call action: "Call me" (VALR rings your phone now), with "Talk in browser" under ▾."""
    return f"""<div class="split">
  <button class="btn primary split-main" type="button" data-action="callnow">{icon('phone', 16)}<span class="split-label">Call me</span></button>
  <details class="menu split-more" data-menu><summary class="btn primary split-caret" aria-label="More ways to talk">{icon('chevron-down', 16)}</summary>
    <div class="menu-pop align-end" role="menu">{menu_item('Talk in browser', 'mic', '/call')}</div></details>
</div>"""


# The notification bell (data and API: app/notifications.py, valr-notify).

KIND_TONE = {"offer": "ok", "interview": "accent", "reply_needed": "warn", "take_home": "warn", "rejection": "bad",
             "reply": "info", "update": "info", "recording": "info", "calendar": "accent"}


def ago(iso: str, now: datetime) -> str:
    """"just now", "12 min ago", "2 h ago", "3 d ago" (static/valr-notify.js says the same)."""
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    minutes = max(0, round((now - moment).total_seconds() / 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = round(minutes / 60)
    return f"{hours} h ago" if hours < 24 else f"{round(hours / 24)} d ago"


def note_item(n: dict[str, Any], now: datetime, tag: str = "li") -> str:
    """One notification; the same markup static/valr-notify.js builds. A click marks it read."""
    meta = " · ".join(x for x in (n.get("company") or "", n.get("label") or "", ago(n.get("created_at") or "", now)) if x)
    gmail = (f'<a class="notif-gmail" href="{e(n["gmail_url"])}" target="_blank" rel="noopener noreferrer" '
             f'data-notif-item="{e(n["id"])}" data-notif-stay>Open in Gmail</a>' if n.get("gmail_url") else "")
    unread = not n.get("read")
    return (f'<{tag} class="notif-item{" unread" if unread else ""} notif-{e(n.get("importance") or "digest")} kind-{e(n.get("kind") or "")}" data-notif-id="{e(n["id"])}">'
            f'<a class="notif-main" href="{e(n.get("link") or "/valr/leads")}" data-notif-item="{e(n["id"])}">'
            f'<span class="notif-dot" aria-hidden="true"></span><span class="notif-title">{e(n["title"])}'
            f'{"<span class=\"sr-only\"> (unread)</span>" if unread else ""}</span>'
            f'<span class="notif-meta">{e(meta)}</span>'
            + (f'<span class="notif-body">{e(n["body"])}</span>' if n.get("body") else "")
            + f'</a>{gmail}</{tag}>')


def bell(notes: dict[str, Any], items: list[dict[str, Any]], clock: Clock) -> str:
    """The bell: a red badge with the unread count; its panel (a drawer on desktop, full screen
    on a phone) lists the notifications by day: new events only (emails, call recordings,
    calendar changes). Broken connections are not here: Home's alert line and Settings show
    them. `items` is unused (kept for the callers). Opening the panel
    reads nothing; clicking an item does. static/valr-notify.js loads the full list on open."""
    unread = int(notes.get("unread") or 0)
    first = "".join(note_item(n, clock.now) for n in (notes.get("items") or [])[:12])
    body = (f'<ul class="notif-items">{first}</ul>' if first else
            '<p class="notif-empty">You\'re all caught up.</p>')
    label = f"Notifications, {unread} unread" if unread else "Notifications"
    return f"""<details class="notif-bell{' has-unread' if unread else ''}" data-popover data-notif>
  <summary class="icon-btn" aria-label="{e(label)}">{icon('bell', 18)}<span class="notif-count num" data-notif-count>{unread or ''}</span></summary>
  <div class="notif-pop" role="region" aria-label="Notifications">
    <div class="notif-head"><h2 class="notif-heading">Notifications</h2>
      <button class="btn ghost sm" type="button" data-notif-all>Mark all read</button>
      <button class="icon-btn" type="button" data-notif-close aria-label="Close notifications">{icon('x', 18)}</button></div>
    <div class="notif-list" data-notif-list>{body}</div>
  </div>
</details>"""


def layout(view: dict[str, Any], email: str, csrf: str, active: str, title: str, body: str,
           theme: str | None = None, bell: str = "", lead_dot: bool = False) -> str:
    from app.auth.pages import account_menu  # Here: app.auth.pages imports this module.
    dot = '<span class="nav-dot" data-lead-dot title="Unread email"><span class="sr-only"> (unread email)</span></span>'
    mark = lambda key: dot if key == "leads" and lead_dot else ""  # noqa: E731
    nav = "".join(f'<a href="{href}"{" aria-current=\"page\"" if key == active else ""}>{e(label)}{mark(key)}</a>'
                  for key, label, href in NAV)
    tabs = "".join(f'<a href="{href}"{" aria-current=\"page\"" if key == active else ""}><span class="tab-icon">{icon(NAV_ICON[key], 22)}{mark(key)}</span>'
                   f'<span>{e(label)}</span></a>' for key, label, href in NAV)
    return f"""{head(title, theme, csrf)}
<link rel="stylesheet" href="{asset('leads.css')}">
<script type="module" src="{asset('valr.js')}"></script>
<script type="module" src="{asset('valr-app.js')}"></script>
<script type="module" src="{asset('valr-notify.js')}"></script>
<script type="module" src="{asset('valr-player.js')}"></script>
</head><body class="app page-{e(active)}">
<a class="skip" href="#main">Skip to content</a>
<header class="topbar">
  <div class="topbar-in">
    <a class="brand" href="/valr" aria-label="VALR home"><span class="brand-mark" aria-hidden="true"></span><span class="brand-word">valr</span></a>
    <nav class="nav" aria-label="Main">{nav}</nav>
    <div class="topbar-tools">
      {bell}
      {call_button()}
      <button class="icon-btn theme-btn" type="button" data-theme-toggle aria-label="Switch between light and dark">{icon('sun', 18)}{icon('moon', 18)}</button>
      {account_menu(email)}
    </div>
  </div>
</header>
<main id="main" class="main" tabindex="-1">
{body}
</main>
<nav class="tabbar" aria-label="Main">{tabs}</nav>
<div class="toast" role="status" aria-live="polite" hidden></div>
</body></html>"""


def page_head(title: str, eyebrow: str = "", actions: str = "", crumb: str = "", sub: str = "") -> str:
    """title, eyebrow and sub are HTML: callers escape what they put in. crumb: "href|label"."""
    back = ""
    if crumb:
        href, label = crumb.split("|", 1)
        back = f'<a class="crumb" href="{e(href)}">{icon("chevron-left", 16)}{e(label)}</a>'
    return f"""
<header class="page-head">
  {back}
  {f'<p class="eyebrow">{eyebrow}</p>' if eyebrow else ''}
  <div class="page-head-row"><h1 class="page-title">{title}</h1>{f'<div class="page-actions">{actions}</div>' if actions else ''}</div>
  {f'<div class="page-sub">{sub}</div>' if sub else ''}
</header>"""
