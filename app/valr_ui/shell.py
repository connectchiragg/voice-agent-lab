"""The frame around every VALR page: top bar, navigation, the needs-attention badge,
light/dark, a slim banner for anything broken, the footer and the mobile tab bar.

Server-rendered; static/valr.js (countdowns, actions, refresh, focus ticks) and
static/valr-app.js (theme, globe, filters, the attention popover) and static/valr-player.js
(past interviews: the recording player and key moments) enhance it.
"""
import hashlib
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from app.valr_page import FONTS, ICON, Clock, e
from app.valr_ui import attention as attn

NAV = [("home", "Home", "/valr"), ("interviews", "Interviews", "/valr/interviews"),
       ("leads", "Leads", "/valr/leads"), ("performance", "Performance", "/valr/performance"),
       ("settings", "Settings", "/valr/settings")]
ICONS = {  # 24x24 strokes, drawn with currentColor.
    "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 9.5V20h13V9.5"/>',
    "interviews": '<rect x="3.5" y="5" width="17" height="15"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "leads": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c3 3.2 3 13.8 0 17M12 3.5c-3 3.2-3 13.8 0 17"/>',
    "performance": '<path d="M4 19h16"/><path d="M6 15l4-4 3 3 5-6"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>',
    "call": '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a1 1 0 0 1-1 1A16 16 0 0 1 4 5a1 1 0 0 1 1-1z"/>',
    "telegram": '<path d="M21 4 3 11l6 2 2 6 3-4 5 4z"/><path d="m9 13 8-6"/>',
    "theme": '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 0 0 16z" fill="currentColor"/>',
    "alert": '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17v.5"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
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
            f'stroke="currentColor" stroke-width="1.6" stroke-linecap="square" aria-hidden="true">'
            f'{ICONS[name]}</svg>')


def fix_control(fix: dict[str, Any] | None, small: bool = True) -> str:
    """The one-click fix: a link, or a button that valr.js posts (with the CSRF token)."""
    if not fix:
        return ""
    cls = "btn small" if small else "btn"
    if fix.get("href"):
        return f'<a class="{cls}" href="{e(fix["href"])}">{e(fix["label"])}</a>'
    data = "".join(f' data-{e(k)}="{e(v)}"' for k, v in (fix.get("data") or {}).items())
    return f'<button class="{cls}" type="button" data-action="{e(fix["action"])}"{data}>{e(fix["label"])}</button>'


def attention_list(items: list[dict[str, Any]], cls: str = "att-list") -> str:
    if not items:
        return ""
    rows = "".join(
        f'<li class="att lvl-{e(i["level"])}"><span class="att-dot" aria-hidden="true"></span>'
        f'<span class="att-text"><span class="att-title">{e(i["title"])}</span>'
        f'<span class="att-detail">{e(i["detail"])}</span></span>{fix_control(i["fix"])}</li>'
        for i in items)
    return f'<ul class="{cls}">{rows}</ul>'


def badge(items: list[dict[str, Any]]) -> str:
    """The top-bar badge: a count of what needs attention, opening the list (works without JS)."""
    counted = attn.counted(items)
    worst = counted[0]["level"] if counted else "ok"
    label = (f"{len(counted)} need{'s' if len(counted) == 1 else ''} attention" if counted
             else "All good")
    body = (attention_list(items) if items else
            '<p class="att-empty">Nothing needs you right now.</p>')
    return f"""
<details class="att-badge lvl-{worst}" data-popover>
  <summary aria-label="{e(label)}">{icon('alert' if counted else 'check', 16)}<span class="att-count num">{len(counted) if counted else ''}</span><span class="att-label">{e(label)}</span></summary>
  <div class="att-pop" role="region" aria-label="Needs attention">
    <p class="label">Needs attention</p>{body}
    <a class="att-more" href="/valr/system">System details →</a>
  </div>
</details>"""


def banner(items: list[dict[str, Any]]) -> str:
    """On every page but Home: only what is broken (bad), one line each."""
    bad = [i for i in items if i["level"] == "bad"]
    if not bad:
        return ""
    return f'<div class="banner" role="status"><div class="banner-in">{attention_list(bad, "att-list compact")}</div></div>'


def theme_attr(theme: str | None) -> str:
    return f' data-theme="{theme}"' if theme in ("light", "dark") else ""


THEME_BOOT = ("<script>try{var t=localStorage.getItem('valr-theme');var d=document.documentElement;"
              "if(!d.dataset.theme&&(t==='light'||t==='dark'))d.dataset.theme=t;}catch(_){}</script>")


def layout(view: dict[str, Any], email: str, csrf: str, active: str, title: str, body: str,
           items: list[dict[str, Any]], theme: str | None = None) -> str:
    from app.auth.pages import account_menu  # Here: app.auth.pages imports this module.
    clock = Clock(view.get("zone") or "UTC", datetime.fromisoformat(view["generated_at"]))
    nav = "".join(f'<a href="{href}"{" aria-current=\"page\"" if key == active else ""}>{e(label)}</a>'
                  for key, label, href in NAV)
    tabs = "".join(f'<a href="{href}"{" aria-current=\"page\"" if key == active else ""}>{icon(key, 20)}'
                   f'<span>{e(label)}</span></a>' for key, label, href in NAV)
    return f"""<!doctype html>
<html lang="en"{theme_attr(theme)}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="csrf-token" content="{e(csrf)}">
<meta name="theme-color" content="#05070c">
<title>{e(title)} · VALR</title>
<link rel="icon" href="{ICON}">{THEME_BOOT}{FONTS}
<link rel="stylesheet" href="{asset('theme.css')}"><link rel="stylesheet" href="{asset('valr.css')}">
<link rel="stylesheet" href="{asset('valr-app.css')}"><link rel="stylesheet" href="{asset('valr-auth.css')}">
<link rel="stylesheet" href="{asset('valr-perf.css')}">
<script type="module" src="{asset('valr.js')}"></script>
<script type="module" src="{asset('valr-app.js')}"></script>
<script type="module" src="{asset('valr-player.js')}"></script>
</head><body class="app page-{e(active)}">
<a class="sr-only skip" href="#main">Skip to content</a>
<header class="appbar">
  <div class="appbar-in">
    <a class="brand" href="/valr"><span class="brand-dot" aria-hidden="true"></span>valr</a>
    <nav class="nav" aria-label="Main">{nav}</nav>
    <div class="appbar-tools">
      {badge(items)}
      <a class="btn small talk-btn" href="/call">{icon('mic', 16)}<span>Talk</span></a>
      <button class="icon-btn" type="button" data-theme-toggle aria-label="Switch light or dark">{icon('theme', 18)}</button>
      {account_menu(email)}
    </div>
  </div>
</header>
{banner(items) if active not in ("home", "settings") else ""}
<main id="main" class="main">
{body}
</main>
<footer class="foot-bar mono">
  <span><span class="live-dot" aria-hidden="true"></span> Times in {e(clock.abbr)} · updated <span data-updated>{clock.time(view['generated_at'], zone=False)}</span></span>
  <span class="foot-links"><a href="/valr/system">System</a><a href="/call">Talk in the browser</a></span>
  <span class="muted">Signed in as {e(email)} · only you can see this</span>
</footer>
<nav class="tabs" aria-label="Main">{tabs}</nav>
<div class="toast" role="status" aria-live="polite" hidden></div>
</body></html>"""


def page_head(eyebrow: str, title: str, lede: str = "", actions: str = "", crumb: str = "") -> str:
    """eyebrow, title and lede are HTML: callers escape what they put in."""
    back = f'<a class="crumb mono" href="{e(crumb.split("|")[0])}">← {e(crumb.split("|")[1])}</a>' if crumb else ""
    return f"""
<header class="page-head">
  {back}
  <p class="eyebrow"><span class="eb-rule"></span>{eyebrow}</p>
  <div class="page-head-row"><h1>{title}</h1>{f'<div class="page-actions">{actions}</div>' if actions else ''}</div>
  {f'<p class="lede">{lede}</p>' if lede else ''}
</header>"""
