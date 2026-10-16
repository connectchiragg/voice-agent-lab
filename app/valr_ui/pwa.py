"""The Home Screen app's two UI pieces, kept here so the shell and Settings each change by one line:

- `head()`: the manifest, the iOS Home Screen metas, the apple-touch-icon and static/valr-pwa.js
  (which registers /sw.js on every page and runs the Settings card).
- `card()`: Settings → "Phone notifications". The server renders every state; valr-pwa.js
  shows the one that fits this device: install steps on iPhone Safari, "Enable notifications"
  in the installed app, then "On for this iPhone" with Send test and Turn off. Also where
  alerts go (Telegram + phone push, phone push only, Telegram only) and how many devices.
"""
from app.valr_page import e

STEPS = [
    "In Safari, tap the Share button (the square with an arrow, at the bottom of the screen).",
    "Scroll down, tap “Add to Home Screen”, then tap Add.",
    "Open VALR from the new Home Screen icon, sign in if asked, and come back to Settings.",
]
DELIVERY = [("both", "Telegram + phone push"), ("push", "Phone push only"), ("telegram", "Telegram only")]


def head() -> str:
    from app.valr_ui.shell import asset  # Here: shell imports this module lazily.
    return ('<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">'
            '<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="black">'
            '<meta name="apple-mobile-web-app-title" content="VALR">'
            '<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">'
            f'<script type="module" src="{asset("valr-pwa.js")}"></script>')


def card() -> str:
    """Settings › Reminders › "Phone notifications", in the design system's row (app/valr_ui/settings.row)."""
    from app.valr_ui.shell import icon
    steps = "".join(f"<li>{e(step)}</li>" for step in STEPS)
    modes = "".join(
        f'<label class="seg-option"><input type="radio" name="pwa-delivery" value="{key}" data-pwa-delivery'
        f'{" checked" if key == "both" else ""} disabled><span>{e(label)}</span></label>' for key, label in DELIVERY)
    views = f"""
<p class="muted" data-pwa-view="loading">Checking this device…</p>
<div data-pwa-view="install" hidden>
  <p>To get alerts on your lock screen, add VALR to your Home Screen first (iPhone, iOS 16.4 or later):</p>
  <ol class="notif-steps">{steps}</ol>
</div>
<p data-pwa-view="update" hidden>This iPhone needs iOS 16.4 or later for notifications from a Home Screen app.
  Update in Settings → General → Software Update.</p>
<p class="muted" data-pwa-view="unsupported" hidden>This browser can't receive push notifications. Use the VALR app on your iPhone's Home Screen.</p>
<p class="muted" data-pwa-view="server" hidden>Phone notifications aren't available yet.</p>
<div class="pwa-actions" data-pwa-view="enable" hidden>
  <p>Get a notification for every job email and update, even when VALR is closed.</p>
  <button class="btn sm primary" type="button" data-pwa-enable>Enable notifications</button>
</div>
<p class="muted" data-pwa-view="denied" hidden>Notifications are turned off for VALR. On iPhone: Settings → Notifications →
  VALR → Allow Notifications, then come back here.</p>
<div class="pwa-actions" data-pwa-view="on" hidden>
  <p><span class="chip tone-ok" data-pwa-on-label>On for this iPhone</span></p>
  <button class="btn sm" type="button" data-pwa-test>Send a test</button>
  <button class="btn sm ghost" type="button" data-pwa-off>Turn off</button>
</div>"""
    return f"""
<li class="set-row" id="phone-push">
  <span class="set-icon" aria-hidden="true">{icon("bell", 18)}</span>
  <div class="set-main"><div class="set-title-row"><h3 class="set-title">Phone notifications</h3></div>
    <div class="set-body" data-pwa-card>{views}
      <p class="muted" data-pwa-devices></p>
      <fieldset class="seg-field" data-pwa-modes><legend class="field-label">Where alerts go</legend>
        <div class="seg">{modes}</div></fieldset>
      <p class="field-help">Quiet hours hold phone notifications too. “Phone push only” still uses Telegram until a
        phone is set up, so nothing is lost meanwhile.</p>
    </div></div>
</li>"""
