"""Shared pieces of the VALR pages: escaping, the owner's clock ("11:15 AM IST"), chips,
links, meters, status labels and the error page.

The pages themselves live in app/valr_ui/ (Home, Interviews, Leads, Performance, Settings,
System); the single-page cockpit that used to be rendered here was retired when they landed.
Every value is escaped here.
"""
import html
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


INTERVIEW_STATUS = {  # -> (label, tone)
    "scheduled": ("Research scheduled", "muted"), "researching": ("Researching now", "info"),
    "ready": ("Briefing ready", "ok"), "calling": ("Calling you now", "accent"),
    "done": ("Done", "muted"), "cancelled": ("Cancelled", "bad"),
}


CALL_STATUS = {
    "scheduled": ("Scheduled", "info"), "waiting": ("Scheduled", "info"),
    "waiting_for_call_end": ("After your current call", "info"),
    "waiting_for_research": ("Waiting for research", "info"), "calling": ("Calling you", "accent"),
    "briefed": ("You were briefed", "ok"), "unanswered": ("Missed: no answer", "warn"),
    "missed": ("Too late to call", "warn"), "refused": ("Skipped: daily call limit", "bad"),
    "cancelled": ("Cancelled", "muted"), "paused": ("Skipped while paused", "warn"),
    "heads_up_sent": ("Heads-up sent", "ok"), "heads_up_failed": ("Heads-up not sent", "bad"),
    "dial_scheduled": ("Call scheduled", "info"),
}


HEADS_UP = {  # Heads-up outcome -> (label, tone); "{at}" is the call time.
    "sent": ("Heads-up sent · no reply, no call", "info"), "called": ("Called at {at}", "ok"),
    "calling": ("Calling by {at}", "accent"), "later": ("Calling at {at}", "accent"),
    "skipped": ("Skipped", "muted"),
}


PERSON = {
    "researched": ("Researched", "ok"), "skipped_hr": ("Recruiter, not researched", "muted"),
    "skipped_classifier": ("Not an interviewer", "muted"), "pending": ("Not researched yet", "info"),
    "declined": ("Declined", "muted"),
}


def e(value: object) -> str:
    return html.escape("" if value is None else str(value))


class Clock:
    """Times in the owner's zone, always as "11:15 AM IST"."""

    def __init__(self, zone: str, now: datetime) -> None:
        self.zone = ZoneInfo(zone or "UTC")
        self.now = now.astimezone(self.zone)
        self.abbr = self.now.strftime("%Z")

    def local(self, text: str) -> datetime:
        return datetime.fromisoformat(text).astimezone(self.zone)

    def time(self, text: str | None, zone: bool = True) -> str:
        if not text:
            return "–"
        moment = self.local(text)
        label = moment.strftime("%I:%M %p").lstrip("0") + (f" {moment.strftime('%Z')}" if zone else "")
        return f'<time datetime="{e(text)}">{e(label)}</time>'

    def day(self, text: str) -> str:
        moment = self.local(text).date()
        if moment == self.now.date():
            return "Today"
        if moment == self.now.date() + timedelta(days=1):
            return "Tomorrow"
        return self.local(text).strftime("%a %d %b").replace(" 0", " ")

    def when(self, text: str | None) -> str:
        """"Today, 11:15 AM IST" / "Sat 17 Oct, 9:30 AM IST"."""
        return "–" if not text else f"{e(self.day(text))}, {self.time(text)}"

    def short_date(self, text: str) -> str:
        return datetime.fromisoformat(text).strftime("%d %b").lstrip("0")


def countdown(text: str | None, short: bool = False) -> str:
    if not text:
        return ""
    attr = " data-short" if short else ""
    return f'<span class="rel" data-countdown="{e(text)}"{attr}></span>'


def chip(label: str, tone: str, title: str = "") -> str:
    hint = f' title="{e(title)}"' if title else ""
    return f'<span class="chip tone-{tone}"{hint}>{e(label)}</span>'


def link(url: str, text: str | None = None) -> str:
    if not url.startswith(("https://", "http://")):
        return e(url)
    label = text or url.split("://", 1)[1].split("/", 1)[0].removeprefix("www.")
    return (f'<a class="src" href="{e(url)}" rel="noopener noreferrer nofollow" target="_blank" '
            f'title="{e(url[:200])}">{e(label)} ↗</a>')


def money(usd: float | None) -> str:
    if usd is None:
        return "–"
    return f"${usd:,.4f}" if 0 < usd < 0.01 else f"${usd:,.2f}"


def phone_label(number: str) -> str:
    """VALR's own number, grouped for reading: +1 228 888 8007."""
    digits = number.lstrip("+")
    if number.startswith("+1") and len(digits) == 11:
        return f"+1 {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return number


def eyebrow(number: str, text: str) -> str:
    return f'<p class="eyebrow"><span class="eb-num">{e(number)}</span><span class="eb-rule"></span>{e(text)}</p>'


def meter(used: float | None, limit: float | None, label: str, detail: str) -> str:
    share = 0.0 if not limit or used is None else min(used / limit, 1)
    tone = "bad" if share >= 0.9 else "warn" if share >= 0.7 else "ok"
    return (f'<div class="meter"><div class="meter-head"><span>{e(label)}</span>'
            f'<span class="num">{detail}</span></div>'
            f'<div class="bar" role="meter" aria-label="{e(label)}" aria-valuemin="0" aria-valuemax="100" '
            f'aria-valuenow="{share * 100:.0f}"><i class="tone-{tone}" style="width:{share * 100:.1f}%"></i></div></div>')


def status_label(slot: dict[str, Any], clock: Clock) -> str:
    label, tone = INTERVIEW_STATUS.get(slot["status"], (slot["status"], "muted"))
    if slot["status"] == "scheduled" and slot.get("research_due"):
        label = f"Research at {clock.local(slot['research_due']).strftime('%I:%M %p').lstrip('0')}"
    return chip(label, tone)


def found_block(data: dict[str, Any] | None, empty: str) -> str:
    if not data or not (data.get("summary") or data.get("claims")):
        return f'<p class="muted small">{e(empty)}</p>' if empty else ""
    claims = "".join(f"<li>{e(c['claim'])} {link(c['url']) if c['url'] else ''}</li>" for c in data["claims"])
    return ((f"<p>{e(data['summary'])}</p>" if data["summary"] else "")
            + (f'<ul class="facts">{claims}</ul>' if claims else ""))


BADGE = {"improving": ("Improving", "ok", "↗"), "flat": ("Steady", "info", "→"),
         "declining": ("Slipping", "bad", "↘"), "insufficient_data": ("Too early to tell", "muted", "·")}


VERDICT = {"improved": ("Improved", "ok"), "same": ("No change yet", "warn"), "worse": ("Getting worse", "bad"),
           "mixed": ("Mixed", "info")}


OUTCOME = {"advanced": ("Advanced", "ok"), "rejected": ("Didn't move on", "bad"), "unknown": ("Awaiting result", "muted")}


def badge_chip(badge: str) -> str:
    text, tone, arrow = BADGE.get(badge, BADGE["insufficient_data"])
    return chip(f"{arrow} {text}", tone)


def score(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}"


def short_day(date: str) -> str:
    try:
        return datetime.fromisoformat(date).strftime("%d %b").lstrip("0")
    except ValueError:
        return date


def mail_source(url: str) -> str:
    """Where a message link opens: "LinkedIn" for linkedin.com threads, else "Gmail"."""
    from urllib.parse import urlsplit
    host = (urlsplit(url or "").hostname or "").lower()
    return "LinkedIn" if host == "linkedin.com" or host.endswith(".linkedin.com") else "Gmail"


# The faces (Newsreader, Source Sans 3) are self-hosted in static/fonts (static/valr-ds.css
# @font-face); preload the two every page paints first, so text doesn't reflow.
FONTS = ('<link rel="preload" href="/static/fonts/source-sans-3-latin-wght-normal.woff2" as="font" type="font/woff2" crossorigin>'
         '<link rel="preload" href="/static/fonts/newsreader-latin-opsz-normal.woff2" as="font" type="font/woff2" crossorigin>')

# The Signal V on a porcelain tile (an ink tile in dark browser chrome): scripts/make_icons.py.
ICON = ('<link rel="icon" href="/static/icons/favicon.svg" type="image/svg+xml">'
        '<link rel="icon" href="/static/icons/favicon-32.png" sizes="32x32" type="image/png">')


def error_page(message: str) -> str:
    """When the data store is down: a page, not a JSON error."""
    from app.valr_ui.shell import head
    return f"""{head("Interview copilot", None)}</head><body class="app solo-page">
<main class="solo"><section class="solo-card">
<p class="brand"><span class="brand-mark" aria-hidden="true"></span><span class="brand-word">valr</span></p>
<h1 class="solo-title">VALR can't read its data right now</h1>
<p class="solo-text">{e(message)} Calls and research keep running in the background. Try again in a minute.</p>
<a class="btn primary" href="/valr">Try again</a></section></main></body></html>"""
