"""Profile (/valr/profile, from the avatar menu): the photo, the names VALR uses, the time zone,
the phone (managed in Settings) and the account.

Everything saves to app/owner.py's `owner_profile` and takes effect without a deploy: the next
call's greeting, transcripts, reminders and quiet hours read it. static/valr-settings.js runs
the form (inline errors per field), the square photo crop and Sign out of all devices.
"""
from typing import Any

from app import owner
from app.valr_page import Clock, e
from app.valr_ui.settings import avatar, confirm_attrs
from app.valr_ui.shell import asset, icon, page_head

COMMON_ZONES = ("Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Europe/London", "Europe/Berlin",
                "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney")


def zone_select(current: str) -> str:
    zones = owner.zones()
    common = [z for z in COMMON_ZONES if z in zones]
    rest = [z for z in zones if z not in common]
    if current and current not in zones:
        common.insert(0, current)

    def options(items: list[str]) -> str:
        return "".join(f'<option value="{e(z)}"{" selected" if z == current else ""}>{e(z.replace("_", " "))}</option>'
                       for z in items)
    return (f'<select class="input" id="pf-timezone" name="timezone" aria-describedby="pf-timezone-help pf-timezone-error">'
            f'<optgroup label="Common">{options(common)}</optgroup><optgroup label="All time zones">{options(rest)}</optgroup>'
            '</select>')


def field(name: str, label: str, value: str, help_: str, placeholder: str = "", required: bool = True) -> str:
    return f"""
<div class="pf-field">
  <label class="field-label" for="pf-{name}">{e(label)}</label>
  <input class="input" id="pf-{name}" name="{name}" value="{e(value)}" maxlength="60" autocomplete="off"
    placeholder="{e(placeholder)}" aria-describedby="pf-{name}-help pf-{name}-error"{' required' if required else ''}>
  <p class="field-help" id="pf-{name}-help">{e(help_)}</p>
  <p class="field-help bad" id="pf-{name}-error" data-error-for="{name}" role="alert" hidden></p>
</div>"""


def photo_card() -> str:
    has = bool(owner.photo_etag())
    remove = ('<button class="btn sm ghost" type="button" data-photo-remove'
              + confirm_attrs("Remove your photo?", "Your initials show instead, here and in the top bar.", "Remove")
              + '>Remove photo</button>') if has else ""
    return f"""
<section class="card pf-photo" aria-labelledby="pf-photo-title">
  <h2 class="card-title" id="pf-photo-title">Photo</h2>
  <div class="pf-photo-row">{avatar("xl", "Your photo")}
    <div class="pf-photo-actions">
      <button class="btn sm" type="button" data-photo-pick>{"Change photo" if has else "Upload photo"}</button>{remove}
      <p class="field-help">JPEG, PNG or WebP. You'll crop it to a square; VALR keeps a 512 px copy without its
        location data.</p>
      <input type="file" accept="image/jpeg,image/png,image/webp" data-photo-file hidden>
    </div></div>
</section>
<dialog class="sheet crop-sheet" data-crop-sheet aria-labelledby="crop-title">
  <div class="sheet-head"><h2 class="sheet-title" id="crop-title">Crop your photo</h2>
    <button class="icon-btn" type="button" data-crop-cancel aria-label="Close">{icon("x", 18)}</button></div>
  <div class="sheet-body crop-body">
    <canvas width="560" height="560" tabindex="0" aria-label="Crop area: drag, or use the arrow keys, to move the photo"></canvas>
    <label class="crop-zoom"><span class="field-label">Zoom</span><input type="range" min="1" max="3" step="0.01" value="1" data-crop-zoom></label>
    <p class="field-help bad" data-crop-status role="alert"></p>
    <div class="confirm-actions"><button class="btn" type="button" data-crop-cancel>Cancel</button>
      <button class="btn primary" type="button" data-crop-save>Save photo</button></div>
  </div>
</dialog>"""


def details_card() -> str:
    me = owner.view()
    return f"""
<section class="card" aria-labelledby="pf-details-title">
  <h2 class="card-title" id="pf-details-title">How VALR knows you</h2>
  <form class="pf-form" data-profile-form novalidate>
    {field("display_name", "Display name", me["display_name"], "What VALR calls you on calls and here.", "Your name")}
    {field("recording_name", "Name in recordings", me["recording_name"],
           "How you appear in Fathom, Fireflies and phone transcripts, so VALR can tell you from the interviewer.",
           "As your notetaker shows it")}
    {field("name_pronunciation", "How to say your name", me["name_pronunciation"],
           "Optional. A respelling for VALR's voice, like “Chi-rag”. Letters, spaces and hyphens.", "", required=False)}
    <div class="pf-field">
      <label class="field-label" for="pf-timezone">Time zone</label>
      {zone_select(me["timezone"])}
      <p class="field-help" id="pf-timezone-help">Reminders, quiet hours, the daily digest and every time VALR shows.</p>
      <p class="field-help bad" id="pf-timezone-error" data-error-for="timezone" role="alert" hidden></p>
    </div>
    <div class="pf-save"><button class="btn primary" type="submit">Save</button></div>
  </form>
</section>"""


def phone_card() -> str:
    phone = owner.view()["phone"]
    shown = phone["masked"] or "Not added"
    return f"""
<section class="card" aria-labelledby="pf-phone-title">
  <div class="card-head"><h2 class="card-title" id="pf-phone-title">Phone</h2>
    <a class="btn sm" href="/valr/settings#phone">{"Manage" if phone["set"] else "Add phone"}</a></div>
  <p class="num big-num">{e(shown)}</p>
  <p class="muted">VALR calls this number and knows it's you when you call. Change or remove it in Settings.</p>
</section>"""


def account_card(email: str) -> str:
    return f"""
<section class="card" aria-labelledby="pf-account-title">
  <h2 class="card-title" id="pf-account-title">Account</h2>
  <p>Signed in with Google as <b class="email">{e(email)}</b>.</p>
  <p class="muted">The sign-in email comes from Google and can't be changed here.</p>
  <div class="pf-account-actions">
    <form method="post" action="/logout"><button class="btn sm" type="submit">{icon("log-out", 14)}Sign out</button></form>
    <button class="btn sm ghost" type="button" data-settings-action="logout-all"{confirm_attrs(
        "Sign out of all devices?", "Every browser and the iPhone app must sign in again.", "Sign out everywhere")}>Sign out of all devices</button>
  </div>
</section>"""


def page(view: dict[str, Any], clock: Clock, email: str) -> str:
    return f"""{page_head("Profile", crumb="/valr/settings|Settings")}
<div class="pf-grid">
  <div class="pf-col">{photo_card()}{details_card()}</div>
  <div class="pf-col">{phone_card()}{account_card(email)}</div>
</div>
<script type="module" src="{asset('valr-settings.js')}"></script>"""
