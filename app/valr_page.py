"""The VALR page at /valr: server-rendered HTML from app/valr_view.collect().

Written for someone who did not build VALR: plain words (no slots, blocks, workflows
or researcher codes), every time in the owner's zone as "11:15 AM IST", the next
call and the three main actions first, set-up steps until everything is connected,
one detail view per interview, performance, and the technical details folded away
under "System".

Readable without JavaScript (the interview details are anchored sections then);
static/valr.js adds the drawer, countdowns, the chart, the actions and a quiet
refresh. Every value is escaped here; chart data travels as JSON in a
non-executable script tag with "<" escaped.
"""
import html
import json
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
HEADS_UP = {  # Telegram heads-up outcome -> (label, tone); "{at}" is the call time.
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


# Header and hero.

def header(view: dict[str, Any], clock: Clock, email: str) -> str:
    return f"""
<header class="topbar">
  <a class="brand" href="#top"><span class="brand-dot" aria-hidden="true"></span>valr<span class="brand-sub">/ interview copilot</span></a>
  <nav class="nav" aria-label="Sections">
    <a href="#upcoming">Interviews</a><a href="#performance">Performance</a><a href="#system">System</a>
  </nav>
  <p class="tz mono" title="Signed in as {e(email)}"><span class="live-dot" aria-hidden="true"></span>
    Times in {e(clock.abbr)} · updated <span data-updated>{clock.time(view['generated_at'], zone=False)}</span></p>
</header>"""


def telegram_mode(view: dict[str, Any]) -> bool:
    return view.get("mode") == "telegram"


def heads_up_card(view: dict[str, Any], clock: Clock) -> str:
    nxt = view.get("next_call")
    tg = view["connections"].get("telegram") or {}
    if view["dials_paused"]["enabled"]:
        return ('<div class="feature tone-warn-bar"><p class="mono label"><span class="dot warn"></span>Heads-ups paused</p>'
                '<p class="feature-big">Paused</p><p class="muted">VALR keeps researching but won\'t message you before '
                'interviews. "Call me now" still works.</p>'
                '<button class="btn" type="button" data-action="dials" data-paused="false">Resume heads-ups</button></div>')
    if tg.get("linked"):
        warn = '<p class="small muted"><span class="dot live"></span> Telegram connected</p>'
    elif tg.get("configured"):
        warn = ('<p class="small tone-warn">Telegram isn\'t connected yet, so no heads-up can be sent. '
                'Send the bot this code:</p><p><button class="btn small" type="button" data-action="tgcode">'
                'Show /start code</button><code class="mono" data-tg-code hidden></code></p>')
    else:
        warn = ('<p class="small tone-warn">Telegram isn\'t set up yet, so no heads-up can be sent '
                '(the owner runs deploy/install-telegram.sh with the bot token).</p>')
    if not nxt:
        return ('<div class="feature"><p class="mono label">Next heads-up</p><p class="feature-big">None yet</p>'
                '<p class="muted">No interviews in the next 8 days. VALR messages you on Telegram 30 minutes '
                f'before each one; tap 📞 to get the briefing call.</p>{warn}</div>')
    block = next((b for b in view["blocks"] if b["id"] == nxt["block"]), None)
    n = len(block["slots"]) if block else 1
    who = ", ".join(dict.fromkeys(s["company"] or s["title"] for s in block["slots"])) if block else nxt["label"]
    return f"""
<div class="feature">
  <p class="mono label"><span class="dot live"></span>Next heads-up on Telegram</p>
  <p class="feature-big num">Heads-up at {clock.time(nxt['at'])}</p>
  <p class="feature-rel">{e(clock.day(nxt['at']))} · {countdown(nxt['at'])}</p>
  <p>Before your {'interview' if n == 1 else f'{n} back-to-back interviews'} with <b>{e(who)}</b>.</p>
  <p class="muted small">Tap 📞 Call me now or ⏰ In 10 min for the briefing call. No reply, no call.</p>{warn}
</div>"""


def next_call_card(view: dict[str, Any], clock: Clock) -> str:
    if telegram_mode(view):
        return heads_up_card(view, clock)
    nxt = view.get("next_call")
    paused = view["dials_paused"]["enabled"]
    if not view.get("dial_env"):
        return ('<div class="feature"><p class="mono label">Briefing calls</p><p class="feature-big">Off</p>'
                '<p class="muted">Calls are switched off on the server. VALR still researches each interview; '
                'you can read the briefings below.</p></div>')
    if paused:
        return ('<div class="feature tone-warn-bar"><p class="mono label"><span class="dot warn"></span>Briefing calls paused</p>'
                '<p class="feature-big">Paused</p><p class="muted">VALR keeps researching but won\'t call you before '
                'interviews. Calls you ask for yourself still come through.</p>'
                '<button class="btn" type="button" data-action="dials" data-paused="false">Resume calls</button></div>')
    if not nxt:
        return ('<div class="feature"><p class="mono label">Next briefing call</p><p class="feature-big">None yet</p>'
                '<p class="muted">No interviews in the next 8 days. When an invite lands on your calendar, '
                'VALR plans a call for 30 minutes before it.</p></div>')
    block = next((b for b in view["blocks"] if b["id"] == nxt["block"]), None)
    n = len(block["slots"]) if block else 1
    who = ", ".join(dict.fromkeys(s["company"] or s["title"] for s in block["slots"])) if block else nxt["label"]
    retry = "One call, no retries: if you miss it, call VALR or tap \"Call me now\"."
    return f"""
<div class="feature">
  <p class="mono label"><span class="dot live"></span>Next briefing call</p>
  <p class="feature-big num">{clock.time(nxt['at'])}</p>
  <p class="feature-rel">{e(clock.day(nxt['at']))} · {countdown(nxt['at'])}</p>
  <p>Before your {'interview' if n == 1 else f'{n} back-to-back interviews'} with <b>{e(who)}</b>.</p>
  <p class="muted small">{retry}</p>
</div>"""


def hero(view: dict[str, Any], clock: Clock) -> str:
    number = view["setup"]["valr_number"]
    paused = view["dials_paused"]["enabled"]
    noun = "heads-ups" if telegram_mode(view) else "calls"
    pause = ("" if not view.get("dial_env") and not telegram_mode(view) else
             f'<button class="btn" type="button" data-action="dials" data-paused="{"false" if paused else "true"}" '
             f'aria-pressed="{"true" if paused else "false"}">{"Resume " + noun if paused else "Pause " + noun}</button>')
    lede = ("VALR messages you on Telegram 30 minutes before each interview; tap to get a briefing call: "
            "who you're meeting, what this round is like, the pay band, and what to practise."
            if telegram_mode(view) else
            "VALR calls you 30 minutes before each interview with a researched briefing: who you're meeting, "
            "what this round is like, the pay band, and what to practise.")
    return f"""
<section class="hero" id="top" aria-labelledby="hero-title">
  <div class="hero-copy">
    {eyebrow("00", "Interview copilot")}
    <h1 id="hero-title">Walk into every interview <span class="dim">already briefed<span class="accent">.</span></span></h1>
    <p class="lede">{lede}</p>
    <div class="actions">
      <a class="btn primary" href="tel:{e(number)}"><span aria-hidden="true">☎</span> Call VALR now <span class="btn-sub num">{e(phone_label(number))}</span></a>
      {pause}
      <button class="btn" type="button" data-action="callnow">Call me now</button>
      <a class="btn" href="/call">Talk in the browser</a>
      <button class="btn" type="button" data-action="sync">Sync calendar</button>
    </div>
  </div>
  {next_call_card(view, clock)}
</section>"""


# Set-up checklist.

def setup(view: dict[str, Any]) -> str:
    google = view["connections"]["google"]
    tg = view["connections"].get("telegram") or {}
    s = view["setup"]
    number = phone_label(s["valr_number"])
    g_done = google["state"] == "connected" and not google["expires_soon"]
    g_text = {"reconnect_needed": "Google access expired. Reconnect so VALR can see new invites.",
              "connected": "Connected. Google asks you to reconnect about once a week while the app is in testing."}
    steps = [
        ("Connect Google", g_done,
         g_text.get(google["state"], "So VALR can see your interview invites and the recruiters' emails about them. Read-only."),
         ('<a class="btn small" href="/oauth/google/start">'
          + ("Reconnect" if google["state"] != "not_connected" else "Connect Google") + "</a>")
         if google["configured"] and not g_done else ""),
        ("Connect Fathom", view["connections"]["fathom"]["configured"],
         "So VALR can learn from your past interview recordings and score how you're doing.",
         '<span class="muted small">Add your Fathom API key to the server settings (FATHOM_API_KEY).</span>'
         if not view["connections"]["fathom"]["configured"] else ""),
        *([("Connect Telegram", tg.get("linked", False),
            "VALR sends the heads-up there 30 minutes before each interview; you tap to get the call.",
            ('<button class="btn small" type="button" data-action="tgcode">Show /start code</button>'
             '<code class="mono" data-tg-code hidden></code>') if tg.get("configured")
            else '<span class="muted small">Run deploy/install-telegram.sh with the bot token first.</span>')]
          if telegram_mode(view) and not tg.get("linked") else
          [("Connect Telegram", True, "Connected: heads-ups arrive in your VALR chat.", "")]
          if telegram_mode(view) else []),
        ("Confirm your phone number", s["phone_on_file"],
         "VALR only ever calls the number on file, never anyone else.",
         '<span class="muted small">Set it in the server settings (VALR_OWNER_PHONE).</span>'
         if not s["phone_on_file"] else ""),
        ("Make a test call", s["test_call_done"],
         f"Call {number} from your phone and ask \"what's on my calendar today?\"",
         f'<a class="btn small" href="tel:{e(s["valr_number"])}">Call now</a>' if not s["test_call_done"] else ""),
    ]
    done = sum(1 for _, ok, _, _ in steps if ok)
    items = "".join(f"""
<li class="step{' done' if ok else ''}">
  <span class="step-mark mono" aria-hidden="true">{'✓' if ok else f'0{i}'}</span>
  <div class="step-body"><p class="step-title">{e(title)} <span class="sr-only">{'done' if ok else 'to do'}</span></p>
  <p class="muted small">{e(text)}</p></div>
  <div class="step-action">{action}</div>
</li>""" for i, (title, ok, text, action) in enumerate(steps, 1))
    open_ = "" if done == len(steps) else " open"
    return f"""
<section class="section setup" id="setup" aria-labelledby="setup-title">
  <details class="setup-box"{open_}>
    <summary>{eyebrow("01", "Get set up")}<h2 id="setup-title">{'You are all set' if done == len(steps) else 'Finish setting up'}
      <span class="progress mono">{done}/{len(steps)}</span></h2></summary>
    <ol class="steps">{items}</ol>
  </details>
</section>"""


# Upcoming interviews.

def people_line(slot: dict[str, Any]) -> str:
    names = [p["name"] for p in slot["interviewers"] if p["status"] not in ("skipped_hr", "declined")]
    if not names:
        return ""
    shown = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    return f'<span class="iv-people small">With {e(shown)}</span>'


def status_label(slot: dict[str, Any], clock: Clock) -> str:
    label, tone = INTERVIEW_STATUS.get(slot["status"], (slot["status"], "muted"))
    if slot["status"] == "scheduled" and slot.get("research_due"):
        label = f"Research at {clock.local(slot['research_due']).strftime('%I:%M %p').lstrip('0')}"
    return chip(label, tone)


def interview_row(slot: dict[str, Any], clock: Clock) -> str:
    return f"""
<li class="iv" data-ref="{e(slot['ref'])}">
  <a class="iv-link" href="#iv-{e(slot['ref'])}" data-open="{e(slot['ref'])}">
    <span class="iv-time num">{clock.time(slot['start'])}</span>
    <span class="iv-main">
      <span class="iv-company">{e(slot['company'] or slot['title'])}</span>
      <span class="iv-title muted small">{e(slot['title']) if slot['company'] else ''}</span>
      {people_line(slot)}
    </span>
    <span class="iv-side">{status_label(slot, clock)}<span class="iv-open mono">Open briefing →</span></span>
  </a>
</li>"""


def upcoming(view: dict[str, Any], clock: Clock) -> str:
    google = view["connections"]["google"]["state"]
    blocks = view.get("blocks") or []
    if "timeline" in view.get("errors", []):
        body = ('<div class="empty error" role="alert"><p class="empty-title">We couldn\'t load your interviews just now.</p>'
                '<p class="muted">VALR keeps researching and calling in the background; this page only failed to read them.</p>'
                '<button class="btn small" type="button" data-action="reload">Try again</button></div>')
    elif not blocks and google != "connected":
        body = ('<div class="empty"><p class="empty-title">Connect your Google Calendar to see your interviews here.</p>'
                '<p class="muted">VALR reads invites only; it never changes your calendar.</p>'
                '<a class="btn small" href="/oauth/google/start">Connect Google</a></div>')
    elif not blocks:
        body = ('<div class="empty"><p class="empty-title">No interviews in the next 8 days.</p>'
                '<p class="muted">New invites show up here within a minute of landing on your calendar.</p></div>')
    else:
        days: dict[str, list[str]] = {}
        for block in blocks:
            day = f"{clock.day(block['start'])} · {clock.local(block['start']).strftime('%a %d %b')}"
            label, tone = CALL_STATUS.get(block["state"], (block["state"], "muted"))
            if block["paused"] and block["state"] in ("scheduled", "waiting"):
                label, tone = ("Paused: no heads-up" if telegram_mode(view) else "Paused: no call"), "warn"
            sent = block.get("heads_up")
            if sent:
                label, tone = HEADS_UP.get(sent["outcome"], (sent["outcome"], "muted"))
                label = label.format(at=clock.local(sent["call_at"]).strftime("%I:%M %p").lstrip("0")
                                     if sent.get("call_at") else "–")
            n = len(block["slots"])
            covers = f" · covers {n} back-to-back interviews" if n > 1 else ""
            what = "Telegram heads-up" if telegram_mode(view) else "Briefing call"
            days.setdefault(day, []).append(f"""
<div class="group">
  <p class="call-line"><span class="call-icon" aria-hidden="true">☎</span>
    <span class="call-what">{what} at <b>{clock.time(block['call_at'])}</b>{covers}</span>
    <span class="muted small">{countdown(block['call_at'], short=True)}</span>{chip(label, tone)}</p>
  <ul class="ivs">{''.join(interview_row(s, clock) for s in block['slots'])}</ul>
</div>""")
        body = "".join(f'<div class="day"><h3 class="day-title mono">{e(day)}</h3>{"".join(items)}</div>'
                       for day, items in days.items())
    return f"""
<section class="section" id="upcoming" aria-labelledby="upcoming-title">
  {eyebrow("02", "Upcoming")}
  <h2 id="upcoming-title" class="section-title">Your interviews <span class="dim">this week.</span></h2>
  {body}
</section>"""


# One interview, in detail.

def found_block(data: dict[str, Any] | None, empty: str) -> str:
    if not data or not (data.get("summary") or data.get("claims")):
        return f'<p class="muted small">{e(empty)}</p>' if empty else ""
    claims = "".join(f"<li>{e(c['claim'])} {link(c['url']) if c['url'] else ''}</li>" for c in data["claims"])
    return ((f"<p>{e(data['summary'])}</p>" if data["summary"] else "")
            + (f'<ul class="facts">{claims}</ul>' if claims else ""))


def pay_snapshot(pay: dict[str, Any], target: dict[str, Any] | None) -> str:
    market = pay.get("range")
    rows = ""
    verdict = ""
    numbers = (market and target and market.get("unit") == target.get("unit")
               and all(isinstance(x, (int, float)) for x in (market.get("min"), market.get("max"),
                                                             target.get("min"), target.get("max"))))
    if numbers:
        low = min(market["min"], target["min"])
        high = max(market["max"], target["max"])
        span = (high - low) or 1

        def bar(name: str, r: dict[str, Any], cls: str) -> str:
            left = (r["min"] - low) / span * 100
            width = max((r["max"] - r["min"]) / span * 100, 2)
            return (f'<div class="range-row"><span class="range-name">{e(name)}</span><span class="range-track">'
                    f'<i class="{cls}" style="left:{left:.1f}%;width:{width:.1f}%"></i></span>'
                    f'<span class="range-val num">{e(r["min"])}–{e(r["max"])} {e(r["unit"])}</span></div>')
        rows = bar("Their band", market, "market") + bar("Your target", target, "target")
        if target["min"] > market["max"]:
            verdict = "Your target is above their band: ask early whether the role has room, and lead with scope."
        elif target["max"] < market["min"]:
            verdict = "Their band is above your target: let them name a number first."
        elif target["max"] > market["max"]:
            verdict = "Your target overlaps the top of their band: anchor on scope and impact, not the number."
        else:
            verdict = "Your target sits inside their band: defer the number and ask for their range first."
    elif market:
        rows = f'<p class="num">Their band: <b>{e(market.get("min"))}–{e(market.get("max"))} {e(market.get("unit"))}</b></p>'
    claims = "".join(f"<li>{e(c['claim'])} {link(c['url']) if c['url'] else ''}</li>" for c in pay.get("claims") or [])
    if not rows and not claims:
        return '<p class="muted small">No pay information found for this company yet.</p>'
    source = link(market["source_url"], "source") if market and market.get("source_url") else ""
    return (f'<div class="ranges">{rows}</div>' + (f'<p class="verdict">{e(verdict)} {source}</p>' if verdict else "")
            + (f'<ul class="facts">{claims}</ul>' if claims else "")
            + '<p class="muted small">Only you can see this page.</p>')


def detail(slot: dict[str, Any], view: dict[str, Any], clock: Clock) -> str:
    d = slot["detail"]
    brief = slot.get("brief")
    if brief:
        quality = {"partial": "Some research came back thin, so this one is shorter.",
                   "apologized": "VALR couldn't find anything reliable for this one."}.get(brief.get("quality") or "", "")
        brief_html = (f'<blockquote class="spoken">{e(brief["spoken"])}</blockquote>'
                      + (f'<p class="muted small">{e(quality)}</p>' if quality else ""))
        notes = "".join(
            f"<li>{e(n['fact'])}"
            + (" " + chip("sensitive", "warn", "VALR only says this if you ask") if n["sensitive"] else "")
            + (" " + chip("from your inbox", "info") if n["private"] else "")
            + "".join(f" {link(u)}" for u in n["sources"]) + "</li>" for n in brief["notes"])
    else:
        brief_html = (f'<p class="muted">The briefing is written after research, around '
                      f'{clock.time(slot.get("research_due"))}. Check back then, or refresh the research now.</p>')
        notes = ""
    people = "".join(
        f'<li class="person"><span class="avatar" aria-hidden="true">{e((p["name"][:1] or "?").upper())}</span>'
        f'<span class="who">{e(p["name"])}</span>'
        + (f'<a class="mono small email" href="mailto:{e(p["email"])}">{e(p["email"])}</a>' if p.get("email") else "")
        + f'{chip(*PERSON.get(p["status"], (p["status"], "muted")))}</li>'
        for p in slot["interviewers"]) or '<li class="muted small">No one else is on the invite.</li>'
    questions = "".join(f"<li>{e(q)}</li>" for q in d["likely_questions"])
    perf = view["performance"]
    prep = d["prep"] or [f["action"] for f in perf.get("focus_this_week") or []]
    prep_source = "For this round" if d["prep"] else "From your recent interviews"
    checklist = "".join(f'<li><label><input type="checkbox" data-check="{e(slot["ref"])}:{i}"> <span>{e(x)}</span></label></li>'
                        for i, x in enumerate(prep)) or '<li class="muted small">Nothing yet.</li>'
    ref = e(slot["ref"])
    return f"""
<dialog class="drawer" id="iv-{ref}" aria-labelledby="t-{ref}">
  <article class="drawer-inner">
    <header class="drawer-head">
      <div>
        <p class="mono label">{e(clock.day(slot['start']))} · {clock.time(slot['start'], zone=False)} – {clock.time(slot['end'])}</p>
        <h2 id="t-{ref}">{e(slot['company'] or slot['title'])}</h2>
        <p class="muted">{e(slot['title']) if slot['company'] else ''}</p>
        <p>{status_label(slot, clock)}</p>
      </div>
      <button class="close" type="button" data-close aria-label="Close">✕</button>
    </header>
    <section class="d-sec"><h3>The briefing</h3><p class="muted small">What VALR will tell you on the call.</p>{brief_html}</section>
    <section class="d-sec"><h3>Who you're meeting</h3><ul class="people">{people}</ul>{found_block(d['people'], '')}</section>
    <section class="d-sec"><h3>What to expect in this round</h3>{found_block(d['round'], 'Nothing found about this round yet.')}</section>
    <section class="d-sec"><h3>Likely questions</h3>{f'<ol class="questions">{questions}</ol>' if questions else '<p class="muted small">VALR lists likely questions once it has researched this round.</p>'}</section>
    <section class="d-sec"><h3>Pay snapshot</h3>{pay_snapshot(d['pay'], view.get('pay_target'))}</section>
    <section class="d-sec"><h3>Prep checklist</h3><p class="muted small">{e(prep_source)} · ticks stay on this device</p><ul class="checklist">{checklist}</ul></section>
    <section class="d-sec"><h3>From your emails &amp; past rounds</h3>{found_block(d['from_you'], 'Nothing in your inbox or recordings about this company yet.')}</section>
    <section class="d-sec"><h3>The company</h3>{found_block(d['company'], 'Nothing found about the company yet.')}</section>
    {f'<section class="d-sec"><h3>Notes for follow-up questions</h3><ul class="facts">{notes}</ul></section>' if notes else ''}
    <footer class="drawer-foot">
      <button class="btn small" type="button" data-action="refresh" data-ref="{ref}">Refresh research</button>
      <span class="muted small">Runs everything again now (a few minutes). Last updated {clock.when(slot['updated_at'])}.</span>
    </footer>
  </article>
</dialog>"""


# Performance.

BADGE = {"improving": ("Improving", "ok", "↗"), "flat": ("Steady", "info", "→"),
         "declining": ("Slipping", "bad", "↘"), "insufficient_data": ("Too early to tell", "muted", "·")}
VERDICT = {"improved": ("Improved", "ok"), "same": ("No change yet", "warn"), "worse": ("Getting worse", "bad"),
           "mixed": ("Mixed", "info")}
OUTCOME = {"advanced": ("Advanced", "ok"), "rejected": ("Not advanced", "bad"), "unknown": ("Outcome unknown", "muted")}


def badge_chip(badge: str) -> str:
    text, tone, arrow = BADGE.get(badge, BADGE["insufficient_data"])
    return chip(f"{arrow} {text}", tone)


def score(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}"


def performance(view: dict[str, Any], clock: Clock) -> str:
    perf = view["performance"]
    head = f"""{eyebrow("03", "Performance")}
  <h2 id="perf-title" class="section-title">How your interviews <span class="dim">are trending.</span></h2>"""
    if not perf["available"]:
        return f"""
<section class="section" id="performance" aria-labelledby="perf-title">{head}
  <div class="empty"><p class="empty-title">Your scores appear after your first recorded interview.</p>
  <p class="muted">Connect Fathom and record your interviews; VALR scores each one on the same rubric (out of 10)
  and shows what is improving and what to practise.</p></div>
</section>"""
    overall = perf["series"]["overall"]
    last = overall["points"][-1]["ma3"] if overall["points"] else None
    most, risk = perf.get("most_improved"), perf.get("biggest_risk")
    most_html = (f'<p class="tile-mid">{e(most["label"])}</p>'
                 + (chip(f"↗ +{most['change']:.1f}", "ok") if most.get("change") is not None else "")
                 + (f'<p class="muted small">{score(most["first"])} → {score(most["last"])} out of 10</p>'
                    if most.get("first") is not None and most.get("last") is not None else "")) if most else \
        '<p class="tile-mid">–</p><p class="muted small">Needs 3 interviews on the same skill.</p>'
    risk_html = (f'<p class="tile-mid">{e(risk["label"])}</p>'
                 f'<p class="muted small">Recent average {score(risk["latest"])}/10'
                 + (f", {risk['per_30d']:+.1f} a month" if risk.get("per_30d") is not None else "") + "</p>") if risk else \
        '<p class="tile-mid">Nothing slipping</p>'
    tiles = f"""
<div class="tiles">
  <div class="tile"><p class="mono label">Overall · average of last 3</p>
    <p class="tile-big num">{score(last)}<span class="of">/10</span></p>{badge_chip(overall['badge'])}</div>
  <div class="tile"><p class="mono label">Most improved</p>{most_html}</div>
  <div class="tile risk"><p class="mono label">Biggest risk</p>{risk_html}</div>
</div>"""
    focus = "".join(f'<li><span class="f-num mono">0{i}</span><div><p class="f-action">{e(f["action"])}</p>'
                    f'<p class="muted small">{e(f["why"])}</p></div></li>'
                    for i, f in enumerate(perf.get("focus_this_week") or [], 1))
    focus_html = (f'<div class="focus"><h3 class="sub-title">Focus this week</h3><ol class="focus-list">{focus}</ol></div>'
                  if focus else "")
    dim_buttons = "".join(
        f'<button type="button" class="pick" data-dim="{e(d["key"])}" aria-pressed="false">{e(d["label"])} '
        f'<span class="pick-badge" aria-hidden="true">{BADGE.get(perf["series"].get(d["key"], {}).get("badge", ""), BADGE["insufficient_data"])[2]}</span></button>'
        for d in perf["dimensions"] if perf["series"].get(d["key"], {}).get("n"))
    weak = "".join(f'<li class="weak"><span class="weak-topic">{e(t["topic"])}</span>{chip(*VERDICT[t["verdict"]])}'
                   f'<span class="muted small">{t["seen"]} interview{"s" if t["seen"] != 1 else ""}'
                   f'{(" · " + e(t["last_note"])) if t["last_note"] else ""}</span></li>'
                   for t in perf.get("recurring_weak_topics") or [])
    rounds = "".join(f'<li><span>{e(r["round"])}</span><span class="num">{score(r["overall"])}<span class="muted">/10 · {e(r["n"])}</span></span></li>'
                     for r in perf.get("by_round_type") or [])
    labels = {d["key"]: d["label"] for d in perf["dimensions"]}
    cards = []
    for card in reversed(perf["interviews"]):
        bars = "".join(f'<li><span class="sb-name">{e(labels.get(k, k))}</span><span class="sb-track">'
                       f'<i style="width:{v * 10:.0f}%"></i></span><span class="sb-val num">{v:.1f}</span></li>'
                       if v is not None else
                       f'<li class="na"><span class="sb-name">{e(labels.get(k, k))}</span><span class="sb-track"></span>'
                       f'<span class="sb-val num">n/a</span></li>'
                       for k, v in card["dimensions"].items())
        qs = "".join(
            f'<li class="q"><p class="q-text">{e(q["question"])}</p>'
            + (f'<p class="small q-good"><span class="mono">Went well</span> {e(q["went_well"])}</p>' if q["went_well"] else "")
            + (f'<p class="small q-improve"><span class="mono">Improve</span> {e(q["improve"])}</p>' if q["improve"] else "")
            + (f'<p class="small q-better"><span class="mono">Try</span> {e(q["hint"])}</p>' if q["hint"] else "")
            + "</li>" for q in card["questions"])
        nxt = "".join(f"<li>{e(x)}</li>" for x in card["do_next_time"])
        stage = f" · {e(card['stage'])}" if card["stage"] else ""
        cards.append(f"""
<article class="card-score">
  <p class="mono label">{e(clock.short_date(card['date']) if card['date'] else '')} · {e(card['round'])}</p>
  <h4>{e(card['company'] or 'Interview')}<span class="muted small">{stage}</span></h4>
  <p class="score-big num">{score(card['overall'])}<span class="of">/10</span></p>
  <div class="card-badges">{chip(*OUTCOME[card['outcome']], card['outcome_evidence'])}</div>
  <ul class="score-bars">{bars}</ul>
  {f'<details class="qs"><summary>Next time ({len(card["do_next_time"])})</summary><ul class="facts">{nxt}</ul></details>' if nxt else ''}
  {f'<details class="qs"><summary>Question feedback ({len(card["questions"])})</summary><ul>{qs}</ul></details>' if qs else ''}
</article>""")
    data = json.dumps({"series": perf["series"],
                       "dimensions": [{"key": d["key"], "label": d["label"],
                                       "subs": [{"key": x["key"], "label": x["label"]} for x in d["subs"]
                                                if perf["series"].get(x["key"], {}).get("n")]}
                                      for d in perf["dimensions"]]}).replace("<", "\\u003c")
    trend_text = f'<p class="trend-text">{e(perf["trend_text"])}</p>' if perf.get("trend_text") else ""
    side = ""
    if weak or rounds:
        side = (f'<div class="two">'
                + (f'<div><h3 class="sub-title">Topics that keep coming up</h3><ul class="weak-list">{weak}</ul></div>' if weak else "")
                + (f'<div><h3 class="sub-title">By round type</h3><ul class="kv-rows">{rounds}</ul></div>' if rounds else "")
                + "</div>")
    return f"""
<section class="section" id="performance" aria-labelledby="perf-title">{head}
  {tiles}
  {focus_html}
  <div class="chart-box">
    <div class="chart-head"><h3 class="sub-title">Trend</h3><p class="muted small">Dots are single interviews, the line is the
      average of the last 3, out of 10. Skills a round didn't test are left out.</p></div>
    <div class="picker" role="group" aria-label="Choose what to chart"><button type="button" class="pick" data-dim="overall" aria-pressed="true">Overall</button>{dim_buttons}</div>
    <div class="picker subs" role="group" aria-label="Choose a skill" hidden></div>
    <div class="chart-wrap"><div id="perf-chart" class="chart" role="img" aria-label="Interview scores over time">
      <noscript><p class="muted">The chart needs JavaScript; the scorecards below have the numbers.</p></noscript></div></div>
    <p class="chart-status small" aria-live="polite"></p>
  </div>
  {trend_text}
  {side}
  <h3 class="sub-title">Scorecards</h3>
  <div class="cards-row">{''.join(cards)}</div>
  <script type="application/json" id="perf-data">{data}</script>
</section>"""


# System: costs, budgets, connections and runs, folded away.

def telegram_line(tg: dict[str, Any], clock: Clock) -> str:
    if not tg.get("configured"):
        return "not set up"
    if not tg.get("linked"):
        return "bot ready · not connected (send the /start code)"
    return "connected" + (f" · since {clock.when(tg['linked_at'])}" if tg.get("linked_at") else "")


def system(view: dict[str, Any], clock: Clock) -> str:
    s, ilim = view["spend"], view["spend"]["interview_limits"]
    dials = "".join(meter(view["dials"][k]["used"], view["dials"][k]["limit"], title,
                          f"{'?' if view['dials'][k]['used'] is None else view['dials'][k]['used']} / {view['dials'][k]['limit']}")
                    for k, title in (("briefing", "Briefing calls today"), ("callback", "Research callbacks today")))
    c = view["connections"]
    g, cal = c["google"], c["calendar"]
    channel = {"active": "live updates on", "renewing": "live updates renewing",
               "none": "no live updates (checks every 6 hours)", "off": "live updates off"}.get(cal.get("channel", ""), "unknown")
    age = f" · token {g['age_days']} days old" if g.get("age_days") is not None else ""
    until = f" · until {clock.when(cal.get('expires'))}" if cal.get("expires") else ""
    synced = f" · last sync {clock.when(cal.get('last_sync_at'))}" if cal.get("last_sync_at") else ""
    error = (f'<li class="tone-bad"><span>Last calendar error</span><span>{e(cal["last_error"])}</span></li>'
             if cal.get("last_error") else "")
    conn = f"""
<ul class="kv">
  <li><span>Google account</span><span>{e(g['account']) or '–'} · {e(g['state'].replace('_', ' '))}{age}</span></li>
  <li><span>Calendar</span><span>{e(channel)}{until}{synced}</span></li>{error}
  <li><span>Fathom</span><span>{'configured' if c['fathom']['configured'] else 'not configured'}</span></li>
  <li><span>Telegram</span><span>{telegram_line(c.get('telegram') or {}, clock)}</span></li>
</ul>"""
    runs = []
    for job in view["jobs"]:
        tone = {"done": "ok", "cached": "info", "partial": "warn", "running": "info", "booked": "info"}.get(job["status"], "bad")
        status = {"cached": "reused earlier research", "booked": "queued"}.get(job["status"], job["status"])
        duration = "–" if job["duration_s"] is None else f"{job['duration_s']:.0f} s"
        runs.append(f'<li class="run"><span class="run-topic">{e(job["topic"])}</span>{chip(status, tone)}'
                    f'<span class="muted small num">{clock.when(job["created_at"])} · {duration} · {money(job["spend_usd"])}</span></li>')
    return f"""
<section class="section" id="system" aria-labelledby="system-title">
  <details class="system">
    <summary>{eyebrow("04", "System")}<h2 id="system-title" class="section-title small-title">Costs, limits and connections
      <span class="dim">for when something looks off.</span></h2></summary>
    <div class="sys-grid">
      <div class="sys-card"><h3>Research spend</h3>
        {meter(s['month_usd'], ilim['month_usd'], 'This month', f"{money(s['month_usd'])} of {money(ilim['month_usd'])}")}
        {meter(s['today_jobs'], ilim['jobs_per_day'], 'Research runs today', f"{s['today_jobs']} of {ilim['jobs_per_day']}")}
        <p class="muted small">Today {money(s['today_usd'])}, {s['today_cached']} reused for free. Each interview's research is capped at {money(ilim['job_usd'])}.</p></div>
      <div class="sys-card"><h3>Calls</h3>{dials}<p class="muted small">VALR stops dialling for the day at the limit.</p></div>
      <div class="sys-card wide"><h3>Connections</h3>{conn}</div>
      <div class="sys-card wide"><h3>Recent research</h3><ul class="runs">{''.join(runs) or '<li class="muted small">Nothing yet.</li>'}</ul></div>
    </div>
  </details>
</section>"""


FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Space+Grotesk:wght@400;500;600&display=swap">')
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' "
        "fill='%2305070c'/%3E%3Ccircle cx='16' cy='16' r='5' fill='%233b82ff'/%3E%3C/svg%3E")


def page(view: dict[str, Any], email: str, csrf: str) -> str:
    clock = Clock(view.get("zone") or "UTC", datetime.fromisoformat(view["generated_at"]))
    details = "".join(detail(slot, view, clock) for block in view.get("blocks") or [] for slot in block["slots"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="csrf-token" content="{e(csrf)}">
<meta name="theme-color" content="#05070c">
<title>VALR · Interview copilot</title>
<link rel="icon" href="{ICON}">{FONTS}
<link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/valr.css">
<script type="module" src="/static/valr.js"></script>
</head><body>
<a class="sr-only skip" href="#upcoming">Skip to your interviews</a>
{header(view, clock, email)}
<main class="wrap">
  {hero(view, clock)}
  {setup(view)}
  {upcoming(view, clock)}
  {performance(view, clock)}
  {system(view, clock)}
  <footer class="foot mono">valr · interview copilot <span class="muted">· signed in as {e(email)} · attendee emails and phone numbers are never shown</span></footer>
</main>
{details}
<div class="toast" role="status" aria-live="polite" hidden></div>
</body></html>"""


def error_page(message: str) -> str:
    """When the data store is down: a page, not a JSON error."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>VALR · Interview copilot</title><link rel="icon" href="{ICON}">{FONTS}
<link rel="stylesheet" href="/static/theme.css"><link rel="stylesheet" href="/static/valr.css"></head><body>
<main class="wrap"><section class="hero solo"><div class="hero-copy">{eyebrow("!!", "Temporarily unavailable")}
<h1>VALR can't read its data <span class="dim">right now<span class="accent">.</span></span></h1>
<p class="lede">{e(message)} Calls and research keep running in the background. Try again in a minute.</p>
<div class="actions"><a class="btn primary" href="/valr">Try again</a></div></div></section></main></body></html>"""
