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
    steps = "".join(f"<li>{e(step)}</li>" for step in STEPS)
    modes = "".join(
        f'<label class="notif-switch"><input type="radio" name="pwa-delivery" value="{key}" data-pwa-delivery'
        f'{" checked" if key == "both" else ""} disabled> {e(label)}</label>' for key, label in DELIVERY)
    views = f"""
<p class="muted small" data-pwa-view="loading">Checking this device…</p>
<div data-pwa-view="install" hidden>
  <p class="small">To get alerts on your lock screen, add VALR to your Home Screen first (iPhone, iOS 16.4 or later):</p>
  <ol class="small notif-steps">{steps}</ol>
</div>
<p class="small" data-pwa-view="update" hidden>This iPhone needs iOS 16.4 or later for notifications from a Home Screen app.
  Update in Settings → General → Software Update.</p>
<p class="small" data-pwa-view="unsupported" hidden>This browser can't receive push notifications. Use the VALR app on your iPhone's Home Screen.</p>
<p class="small" data-pwa-view="server" hidden>Phone push isn't set up on the server yet.</p>
<div data-pwa-view="enable" hidden>
  <p class="small">Get a notification for every job email and update, even when VALR is closed.</p>
  <button class="btn small" type="button" data-pwa-enable>Enable notifications</button>
</div>
<p class="small" data-pwa-view="denied" hidden>Notifications are turned off for VALR. On iPhone: Settings → Notifications →
  VALR → Allow Notifications, then come back here.</p>
<div data-pwa-view="on" hidden>
  <p class="small"><strong data-pwa-on-label>On for this iPhone</strong></p>
  <button class="btn small" type="button" data-pwa-test>Send test</button>
  <button class="btn small ghost" type="button" data-pwa-off>Turn off</button>
</div>"""
    return f"""
<li class="set-row" id="phone-push">
  <div class="set-main"><h3 class="set-title">Phone notifications</h3>
    <div class="set-body" data-pwa-card>{views}
      <p class="muted small" data-pwa-devices></p>
      <fieldset class="notif-switches" data-pwa-modes><legend class="small">Where alerts go</legend>{modes}</fieldset>
      <p class="muted small">Quiet hours (Telegram alerts above) hold phone pushes too. “Phone push only” still uses
        Telegram until a device is subscribed, so nothing is lost while you set up.</p>
    </div></div>
</li>"""
