"""One "needs attention" model: everything broken or about to break, each with one fix.

`items(view)` -> [{"key", "level": bad|warn|info, "title", "detail", "fix": {"label",
"href"} | {"label", "action", "data"}, "todo"}], worst first. `bad` and `warn` count
toward the badge; `info` is shown on Home and Settings only. `todo` marks the ones that
read as a task for the owner ("Reconnect Google in 2 days") rather than a fault.

`todos(view, attention, leads)` -> up to three things to do now, for Home.

Plain words only: no env vars, workflows or codes. Pure; tested in tests/test_valr_pages.py.
"""
import math
from datetime import datetime, timedelta
from typing import Any

from app.valr_ui import contracts, wording

GOOGLE_REFRESH_DAYS = 7  # Google expires testing-app refresh tokens after about a week.
LEVELS = {"bad": 0, "warn": 1, "info": 2}
CALL_NOW = {"label": "Call me now", "action": "callnow"}


def item(key: str, level: str, title: str, detail: str, fix: dict[str, Any] | None,
         todo: bool = False) -> dict[str, Any]:
    return {"key": key, "level": level, "title": title, "detail": detail, "fix": fix, "todo": todo}


def parse(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def who(block: dict[str, Any]) -> str:
    names = [wording.interview_label(s)["company"] for s in block.get("slots") or []]
    names = [n for n in dict.fromkeys(names) if n and n != "Unknown company"]
    return " and ".join(names[:2]) or "your interview"


def google_items(view: dict[str, Any]) -> list[dict[str, Any]]:
    g = (view.get("connections") or {}).get("google") or {}
    connect = {"label": "Reconnect Google", "href": "/oauth/google/start"}
    state = g.get("state")
    if state == "not_connected":
        return [item("google", "bad", "Google isn't connected",
                     "VALR can't see your interview invites until you connect it.",
                     {"label": "Connect Google", "href": "/oauth/google/start"})]
    if state == "reconnect_needed":
        return [item("google", "bad", "Google access expired",
                     "New invites won't show up until you reconnect.", connect)]
    if state == "connected" and g.get("expires_soon"):
        age = g.get("age_days")
        days = None if age is None else max(0, math.ceil(GOOGLE_REFRESH_DAYS - float(age)))
        when = "today" if days == 0 else "tomorrow" if days == 1 else \
            f"in {days} days" if days is not None else "soon"
        return [item("google_expiring", "warn", f"Reconnect Google {when}",
                     "Google asks for this about once a week. It takes ten seconds.", connect, todo=True)]
    return []


def block_items(view: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for block in view.get("blocks") or []:
        start = parse(block.get("start"))
        upcoming = bool(start and start > now)
        state = block.get("state")
        sent = block.get("heads_up") or {}
        name = who(block)
        if state == "heads_up_failed" or sent.get("outcome") == "failed":
            out.append(item(f"heads-up-{block['id']}", "bad", f"The heads-up for {name} wasn't sent",
                            "Telegram didn't take the message.",
                            CALL_NOW if upcoming else {"label": "Check Telegram", "href": "/valr/settings#telegram"}))
        elif state in ("unanswered", "missed"):
            out.append(item(f"missed-{block['id']}", "warn", f"You missed the briefing call for {name}",
                            "VALR doesn't call twice." + (" You can still get it now." if upcoming else ""),
                            CALL_NOW if upcoming else None))
        elif state == "refused":
            out.append(item(f"refused-{block['id']}", "bad", f"No briefing call for {name}: daily call limit",
                            "VALR stopped calling for today.", {"label": "See limits", "href": "/valr/settings#limits"}))
    return out


def limit_items(view: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, what in (("briefing", "Briefing calls"), ("callback", "Research call-backs")):
        d = (view.get("dials") or {}).get(key) or {}
        if d.get("used") is not None and d.get("limit") and d["used"] >= d["limit"]:
            out.append(item(f"dials-{key}", "warn", f"{what}: today's limit reached",
                            f"{d['used']} of {d['limit']} used. It resets at midnight UTC.",
                            {"label": "See limits", "href": "/valr/settings#limits"}))
    spend = view.get("spend") or {}
    lim = spend.get("interview_limits") or {}
    if lim.get("month_usd") and (spend.get("month_usd") or 0) >= 0.9 * lim["month_usd"]:
        out.append(item("budget", "warn", "Research budget almost used",
                        "New research stops at the monthly limit.", {"label": "See spend", "href": "/valr/system#spend"}))
    if lim.get("jobs_per_day") and (spend.get("today_jobs") or 0) >= lim["jobs_per_day"]:
        out.append(item("runs", "warn", "Research limit reached for today",
                        "Briefings that need fresh research wait until tomorrow.",
                        {"label": "See spend", "href": "/valr/system#spend"}))
    failed = [j for j in view.get("jobs") or [] if j.get("status") in ("failed", "error", "timeout")]
    if failed:
        out.append(item("research-failed", "warn", "Some research didn't finish",
                        f"{len(failed)} recent run{'s' if len(failed) != 1 else ''} failed.",
                        {"label": "See runs", "href": "/valr/system#runs"}))
    return out


def items(view: dict[str, Any]) -> list[dict[str, Any]]:
    now = parse(view.get("generated_at")) or datetime.now().astimezone()
    out: list[dict[str, Any]] = []
    if "timeline" in (view.get("errors") or []):
        out.append(item("timeline", "bad", "Couldn't load your interviews",
                        "VALR keeps working in the background; this page couldn't read them.",
                        {"label": "Try again", "action": "reload"}))
    out += google_items(view)
    cal = (view.get("connections") or {}).get("calendar") or {}
    if cal.get("last_error"):
        out.append(item("calendar", "warn", "The last calendar sync failed",
                        "New or moved interviews may be missing.", {"label": "Sync now", "action": "sync"}))
    tg = (view.get("connections") or {}).get("telegram") or {}
    if view.get("mode") == "telegram" and not tg.get("linked"):
        out.append(item("telegram", "bad", "Telegram isn't connected",
                        "No heads-ups can reach you before interviews.",
                        {"label": "Connect Telegram", "href": "/valr/settings#telegram"}))
    if not (view.get("setup") or {}).get("phone_on_file", True):
        out.append(item("phone", "bad", "No phone number on file",
                        "VALR has nowhere to call you.", {"label": "Fix", "href": "/valr/settings#phone"}))
    out += block_items(view, now)
    out += limit_items(view)
    if (view.get("dials_paused") or {}).get("enabled"):
        noun = "Heads-ups" if view.get("mode") == "telegram" else "Briefing calls"
        out.append(item("paused", "info", f"{noun} are paused", "VALR still researches every interview.",
                        {"label": "Resume", "action": "dials", "data": {"paused": "false"}}))
    if view.get("mode") != "telegram" and view.get("dial_env") is False:
        out.append(item("calls-off", "info", "Automatic briefing calls are off",
                        "You can still ask for a call any time.", CALL_NOW))
    takers = contracts.notetakers(view.get("connections") or {}, imported(view))
    if not any(t["state"] == "connected" for t in takers if t["key"] not in contracts.OPTIONAL):
        out.append(item("notetaker", "info", "Connect a notetaker",
                        "Connect one to get feedback after each interview.",
                        {"label": "Connect", "href": "/valr/settings#notetakers"}, todo=True))
    for t in takers:
        if t["state"] == "error" and t["key"] not in contracts.OPTIONAL:
            out.append(item(f"notetaker-{t['key']}", "warn", f"{t['label']} stopped syncing",
                            t["error"] or "Recordings aren't coming in.",
                            {"label": "Reconnect", "href": "/valr/settings#notetakers"}))
    return sorted(out, key=lambda i: LEVELS[i["level"]])


def imported(view: dict[str, Any]) -> set[str]:
    """Notetakers whose recordings already reached the performance data."""
    return {str(iv.get("source") or "").lower() for iv in (view.get("performance") or {}).get("interviews") or []} - {""}


def counted(attention: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in attention if i["level"] in ("bad", "warn")]


def next_slot(view: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(block, slot) of the next interview that hasn't started, or None."""
    now = parse(view.get("generated_at"))
    for block in view.get("blocks") or []:
        for slot in block.get("slots") or []:
            start = parse(slot.get("start"))
            if slot.get("status") != "cancelled" and start and (now is None or start > now):
                return block, slot
    return None


def todos(view: dict[str, Any], attention: list[dict[str, Any]],
          leads: dict[str, Any] | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """Up to three things to do now: prep for the next interview, the to-do attention items,
    leads waiting on a reply, the latest interview's feedback."""
    now = parse(view.get("generated_at"))
    out = []
    nxt = next_slot(view)
    if nxt:
        slot = nxt[1]
        start = parse(slot["start"])
        if now is None or start - now < timedelta(hours=48):
            name = wording.interview_label(slot)["company"]
            if slot.get("brief"):
                out.append({"title": f"Read your {name} briefing", "href": f"/valr/interviews/{slot['ref']}",
                            "kind": "prep"})
            prep = prep_lines(slot)
            if prep:
                done = prep_done(slot, prep)
                title = (f"{name} prep: all {len(prep)} done" if done == len(prep) else
                         f"Go through your {name} prep: {done} of {len(prep)} done" if done else
                         f"Go through your {name} prep: {len(prep)} item{'s' if len(prep) != 1 else ''}")
                out.append({"title": title, "href": f"/valr/interviews/{slot['ref']}#prep", "kind": "prep",
                            "done": done == len(prep)})
    out += [{"title": i["title"], "fix": i["fix"], "kind": "fix"} for i in attention if i.get("todo")]
    for lead in (leads or {}).get("leads") or []:
        if lead.get("waiting_on") == "you":  # app/leads.ball_in_court: a live lead, the ball in his court.
            out.append({"title": f"Reply to {lead['company']}", "href": f"/valr/leads?lead={lead['id']}#lead",
                        "kind": "lead", "sub": lead.get("waiting_reason") or "", "url": lead.get("url") or ""})
    past = (view.get("performance") or {}).get("interviews") or []
    if past and now is not None:
        latest = past[-1]
        when = parse(f"{latest['date']}T00:00:00+00:00") if latest.get("date") else None
        if when and now - when < timedelta(days=7):
            out.append({"title": f"Review feedback from {latest['company'] or 'your last interview'}",
                        "href": f"/valr/interviews/past/{past_slug(latest)}", "kind": "review"})
    return out[:limit]


def prep_lines(slot: dict[str, Any]) -> list[str]:
    return [p for p in (slot.get("detail") or {}).get("prep") or [] if wording.good_prep(p)]


def prep_done(slot: dict[str, Any], lines: list[str]) -> int:
    """How many of these prep lines are ticked (server-side, app/interview_store.prep_checks)."""
    from app.interview_store import prep_item
    ticked = set(slot.get("prep_done") or [])
    return sum(1 for line in lines if prep_item(line) in ticked)


def past_slug(interview: dict[str, Any]) -> str:
    """"fathom:1000" -> "fathom-1000": every character but a letter or digit becomes "-", the
    same rule as the old /valr#past-<provider>-<id> anchors, so those resolve one to one."""
    raw = str(interview.get("id") or interview.get("date") or "interview")
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in raw)[:80]
