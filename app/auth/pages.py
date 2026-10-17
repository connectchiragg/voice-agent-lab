"""The sign-in pages (/login, /private) and the account menu in the app's top bar.

Same design system as the app (static/valr-ds.css: tokens for light and dark, Instrument
Serif and Inter); the sign-in stage is in static/valr-auth.css.
"""
from urllib.parse import quote

from app.valr_page import e
from app.valr_ui.shell import asset, head, icon

ERRORS = {
    "expired": "That sign-in link expired or was already used. Please try again.",
    "cancelled": "Sign-in was cancelled.",
    "failed": "Google didn't confirm the sign-in. Please try again.",
    "config": "Sign-in isn't set up on this server yet (Google client).",
    "db": "VALR can't reach its database right now. Try again in a minute.",
    "off": "Sign-in with Google is turned off here; this server uses Cloudflare Access.",
}
GOOGLE_G = (  # Google's "G" mark, in its four colours.
    '<svg class="g-mark" viewBox="0 0 48 48" width="18" height="18" aria-hidden="true">'
    '<path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>'
    '<path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
    '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>'
    '<path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>')


# The sign-in art: VALR's mark, the Signal V (scripts/make_icons.py), large on its porcelain tile.
MARK_ART = ('<svg class="auth-mark" viewBox="0 0 100 100" aria-hidden="true">'
            '<path class="m-ink" d="M28 9.8Q30 8.2 32 9.8C34 12.64 35 16.28 35 19.4L35 50.6C35 53.72 34 57.36 32 60.2Q30 61.8 28 60.2C26 57.36 25 53.72 25 50.6L25 19.4C25 16.28 26 12.64 28 9.8ZM68 9.8Q70 8.2 72 9.8C74 12.64 75 16.28 75 19.4L75 50.6C75 53.72 74 57.36 72 60.2Q70 61.8 68 60.2C66 57.36 65 53.72 65 50.6L65 19.4C65 16.28 66 12.64 68 9.8Z"/>'
            '<path class="m-accent" d="M48 39.8Q50 38.2 52 39.8C54 42.64 55 46.28 55 49.4L55 80.6C55 83.72 54 87.36 52 90.2Q50 91.8 48 90.2C46 87.36 45 83.72 45 80.6L45 49.4C45 46.28 46 42.64 48 39.8Z"/></svg>')


def frame(title: str, body: str, theme: str | None) -> str:
    return f"""{head(title, theme)}
<link rel="stylesheet" href="{asset('valr-auth.css')}">
</head><body class="auth-page">
<main class="auth-main">
<div class="auth-art"><div class="auth-tile">{MARK_ART}</div></div>
<section class="auth-card">
<p class="brand"><span class="brand-mark" aria-hidden="true"></span><span class="brand-word">valr</span></p>
{body}
</section>
</main>
</body></html>"""


def login_page(next_path: str, error: str = "", signed_out: bool = False,
               theme: str | None = None) -> str:
    start = "/login/google" + (f"?next={quote(next_path, safe='/')}" if next_path != "/valr" else "")
    notice = ""
    if error in ERRORS:
        notice = f'<p class="auth-note bad" role="alert">{e(ERRORS[error])}</p>'
    elif signed_out:
        notice = '<p class="auth-note ok" role="status">You\'re signed out.</p>'
    return frame("Sign in", f"""
<p class="eyebrow">Interview copilot</p>
<h1 class="auth-title">Sign in to VALR</h1>
<p class="auth-pitch">Research, a briefing and a call before every interview round.</p>
{notice}
<a class="btn gbtn" href="{e(start)}">{GOOGLE_G}<span>Sign in with Google</span></a>
<p class="auth-privacy">VALR is private: only its owner's Google account gets in. Signing in
shares your name and email address with VALR and nothing else. It doesn't ask for Gmail or
Calendar here.</p>""", theme)


def private_page(theme: str | None = None) -> str:
    return frame("Private", """
<p class="eyebrow">Private</p>
<h1 class="auth-title">VALR is private</h1>
<p class="auth-pitch">It holds one person's interview prep, so only its owner can sign in.
The Google account you picked isn't on the list, and VALR kept nothing about it.</p>
<a class="btn gbtn" href="/login/google">""" + GOOGLE_G + """<span>Use another account</span></a>
<p class="auth-privacy"><a href="/login">Back to sign-in</a></p>""", theme)


def account_menu(email: str) -> str:
    """The top bar's account control: an initial and Sign out (the app is owner-only, so no
    address on show)."""
    initial = (email[:1] or "?").upper()
    return f"""<details class="menu acct" data-menu>
  <summary class="acct-btn" aria-label="Account"><span class="acct-avatar" aria-hidden="true">{e(initial)}</span></summary>
  <div class="menu-pop align-end" role="menu">
    <form method="post" action="/logout"><button class="menu-item" role="menuitem" type="submit">{icon('log-out', 16)}<span>Sign out</span></button></form>
  </div>
</details>"""
