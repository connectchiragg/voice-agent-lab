"""The shapes the other branches plug into the pages, normalised in one place.

Each function takes what the producing branch actually returns, tolerates missing or extra
fields, scrubs free text (phone numbers and stray emails hidden; contact emails are kept,
the owner sees them) and returns a small, stable dict the pages render.

- Leads (valr-leads, app/leads_web.collect = GET /valr/api/leads): {"leads": [{"id",
  "company", "role", "domain", "stage" (sourced | applied | screening | interviewing | offer |
  closed-won | closed-lost | ghosted), "stage_manual", "status" (active | expired),
  "temperature" 0-100, "parts" {recency, stage, momentum, quality: {points, max, why}, cap},
  "activity", "touches", "last_touch_at", "next_step", "next_step_at", "contacts": [{"id",
  "name", "email", "role"}], "timeline": [{"kind", "at", "title", "summary", "direction",
  "cue", "url", "detail_url"}]}], "contacts", "links", "stages", "generated_at", "gmail":
  {"enabled", "watch", "last_sync_at", ...}}. The pages group stages into STAGES (closed-won,
  closed-lost and ghosted are "closed"; the exact one stays in `stage_label`); the raw JSON is
  kept for the 3D globe (static/leads-globe.js), which reads it as is.
- Coach and impressions (valr-eval, performance.json schema 3): mapped by
  app/valr_view.coach_view and impression_view (not here).
- Recording (valr-notetakers, a stored row of app/notetakers/store.py): {"provider",
  "share_url" | "url", "embed_url", "title"}; the embed is used only from an allow-listed
  https host.
- Notetakers (valr-notetakers, view["connections"]["notetakers"] = notetakers.status_view):
  {"fathom" | "fireflies" | "granola": {"label", "state": connected | server | refused |
  not_connected, "account", "connected_at", "last_ok_at", "optional", ...}}.
"""
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(\d{3}\)\s?\d{3}[-.\s]\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
                   r"|\b\d{5}\s?\d{5}\b")
STAGES = [("sourced", "Sourced"), ("applied", "Applied"), ("screening", "Screening"),
          ("interviewing", "Interviewing"), ("offer", "Offer"), ("closed", "Closed"), ("other", "Other")]
STAGE_ALIASES = {"new": "sourced", "lead": "sourced", "screen": "screening", "recruiter": "screening",
                 "interview": "interviewing", "onsite": "interviewing", "final": "interviewing",
                 "offered": "offer", "closed-won": "closed", "closed-lost": "closed", "ghosted": "closed",
                 "rejected": "closed", "declined": "closed", "withdrawn": "closed", "archived": "closed"}
STAGE_LABELS = {"closed-won": "Won", "closed-lost": "Not moving forward", "ghosted": "Gone quiet"}
WAITING_DAYS = 10  # Their email is the latest touch and is this recent: the ball is in his court.
EMBED_HOSTS = {"fathom.video", "app.fireflies.ai", "share.fireflies.ai", "app.granola.ai",
               "notes.granola.ai"}
NOTETAKER_STATE = {"connected": "connected", "server": "connected", "refused": "error",
                   "not_connected": "not_connected"}
NOTETAKERS = [("fathom", "Fathom"), ("fireflies", "Fireflies"), ("granola", "Granola")]
OPTIONAL = {"granola"}  # Owner decision 2026-10-16: never an issue or a set-up step.


def text(value: Any, limit: int = 600) -> str:
    raw = "" if value is None else str(value)
    return PHONE.sub("[phone hidden]", EMAIL.sub("[email hidden]", raw)).strip()[:limit]


def https(url: Any) -> str:
    value = str(url or "").strip()
    return value if value.startswith("https://") else ""


def stage_key(value: Any) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    key = STAGE_ALIASES.get(key, key)
    return key if key in dict(STAGES) else "other"


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def contacts(values: Any) -> list[dict[str, str]]:
    out = []
    for c in values or []:
        if not isinstance(c, dict):
            continue
        email = str(c.get("email") or "").strip().lower()
        out.append({"name": text(c.get("name"), 80) or email.split("@", 1)[0],
                    "email": email if EMAIL.fullmatch(email) else "",
                    "role": str(c.get("role") or "other").replace("_", " ")[:20]})
    return out[:6]


def iso_age_days(value: Any, now: datetime) -> float | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds() / 86400


def touches(lead: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in lead.get("timeline") or [] if isinstance(t, dict)]


def waiting_on_you(lead: dict[str, Any], stage: str, now: datetime) -> bool:
    """The latest past touch is their email, recent, on a live lead: he owes a reply."""
    if stage == "closed" or lead.get("status", "active") != "active":
        return False
    if lead.get("waiting_on_you") or lead.get("needs_reply"):
        return True
    past = [t for t in touches(lead) if (iso_age_days(t.get("at"), now) or -1) >= 0]
    latest = max(past, key=lambda t: str(t.get("at")), default=None)
    if not latest or latest.get("kind") != "email" or latest.get("direction") != "in":
        return False
    age = iso_age_days(latest.get("at"), now)
    return age is not None and age <= WAITING_DAYS


def timeline_rows(lead: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    rows = []
    for t in touches(lead)[:limit]:
        detail = str(t.get("detail_url") or "")
        rows.append({"kind": str(t.get("kind") or ""), "at": t.get("at"), "title": text(t.get("title"), 140),
                     "summary": text(t.get("summary"), 280), "you": t.get("direction") == "out",
                     "cue": t.get("cue") if t.get("cue") in ("positive", "negative") else "",
                     "url": https(t.get("url")), "detail_url": new_detail_url(detail)})
    return rows


def new_detail_url(url: str) -> str:
    """Links stored before the redesign (/valr#past-<provider>-<id>, /valr#performance)."""
    if url.startswith("/valr#past-"):
        return "/valr/interviews/past/" + url.removeprefix("/valr#past-")
    if url == "/valr#performance":
        return "/valr/performance"
    return url if url.startswith("/") else ""


def temperature_parts(raw: Any) -> list[dict[str, Any]]:
    parts = raw if isinstance(raw, dict) else {}
    out = []
    for key in ("recency", "stage", "momentum", "quality"):
        p = parts.get(key)
        if isinstance(p, dict) and number(p.get("points")) is not None:
            out.append({"name": key.capitalize(), "points": number(p["points"]), "max": number(p.get("max")) or 0,
                        "why": text(p.get("why"), 160)})
    return out


def leads(raw: Any, now: datetime | None = None) -> dict[str, Any]:
    """valr-leads' JSON -> {"available", "enabled", "leads", "expired", "stages", "raw", "gmail"}."""
    raw = raw if isinstance(raw, dict) else {}
    now = now or datetime.now(timezone.utc)
    rows = []
    for i, lead in enumerate(raw.get("leads") or []):
        if not isinstance(lead, dict):
            continue
        raw_stage = str(lead.get("stage") or lead.get("status") or "").strip().lower()
        stage = stage_key(raw_stage)
        latest_email = next((t for t in touches(lead) if t.get("kind") == "email" and https(t.get("url"))), {})
        latest = next(iter(touches(lead)), {})
        temp = number(lead.get("temperature"))
        rows.append({
            "id": re.sub(r"[^\w-]", "-", str(lead.get("id") or f"lead-{i}"))[:64],
            "company": text(lead.get("company"), 80) or "Unknown company",
            "role": text(lead.get("role") or lead.get("title"), 120),
            "stage": stage, "stage_raw": raw_stage,
            "stage_label": STAGE_LABELS.get(raw_stage) or dict(STAGES).get(stage, "Other"),
            "stage_manual": bool(lead.get("stage_manual")),
            "status": "expired" if lead.get("status") == "expired" else "active",
            "temperature": int(round(temp)) if temp is not None else None,
            "parts": temperature_parts(lead.get("parts")),
            "last_activity_at": lead.get("last_touch_at") or lead.get("last_activity_at") or lead.get("updated_at"),
            "next_step": text(lead.get("next_step"), 200), "next_step_at": lead.get("next_step_at"),
            "waiting_on_you": waiting_on_you(lead, stage, now),
            "source": text(lead.get("source"), 40) or ("Gmail" if latest_email else ""),
            "domain": text(lead.get("domain"), 80), "location": text(lead.get("location"), 80),
            "lat": number(lead.get("lat")), "lng": number(lead.get("lng")),
            "url": https(lead.get("url") or lead.get("thread_url") or latest_email.get("url")),
            "contacts": contacts(lead.get("contacts")),
            "summary": text(lead.get("summary") or latest.get("summary"), 400),
            "timeline": timeline_rows(lead),
        })
    rows.sort(key=lambda r: (-(r["temperature"] if r["temperature"] is not None else -1), r["company"].lower()))
    active = [r for r in rows if r["status"] == "active"]
    counts = {key: sum(1 for r in active if r["stage"] == key) for key, _ in STAGES}
    gmail = raw.get("gmail") if isinstance(raw.get("gmail"), dict) else {}
    return {"available": isinstance(raw.get("leads"), list) and raw.get("connected", True) is not False,
            "enabled": bool(gmail.get("enabled", raw.get("connected", bool(raw)))),
            "generated_at": raw.get("generated_at"), "leads": active,
            "expired": [r for r in rows if r["status"] == "expired"],
            "stages": [{"key": k, "label": label, "count": counts[k]} for k, label in STAGES
                       if counts[k] or k != "other"],
            "raw_stages": [s for s in raw.get("stages") or [] if isinstance(s, str)],
            "gmail": {"enabled": bool(gmail.get("enabled")), "watch": str(gmail.get("watch") or "off"),
                      "last_sync_at": gmail.get("last_sync_at"),
                      "backfill_done_at": gmail.get("backfill_done_at"),
                      "last_error": text(gmail.get("last_error"), 120)},
            "raw": raw if isinstance(raw.get("leads"), list) else None}


def recording(raw: Any, interview: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A link to the recording, and an embed URL only from a known notetaker host."""
    raw = raw if isinstance(raw, dict) else {}
    url = https(raw.get("url") or raw.get("share_url") or (interview or {}).get("recording_url"))
    embed = https(raw.get("embed_url"))
    if embed and (urlsplit(embed).hostname or "") not in EMBED_HOSTS:
        embed = ""
    if not (url or embed):
        return None
    provider = text(raw.get("provider") or (interview or {}).get("source"), 30).lower()
    return {"provider": dict(NOTETAKERS).get(provider, provider.capitalize() or "Recording"),
            "url": url, "embed_url": embed, "title": text(raw.get("title"), 120)}


def account(value: Any) -> str:
    """The owner's own account label: his email address is fine to show him."""
    raw = str(value or "").strip()
    return raw[:80] if EMAIL.fullmatch(raw) else text(raw, 80)


def notetakers(connections: dict[str, Any], imported_from: set[str]) -> list[dict[str, Any]]:
    """Each notetaker's real state from valr-notetakers' status_view; without it, Fathom counts
    as connected when its recordings are being imported (its key lives on the worker)."""
    given = connections.get("notetakers") if isinstance(connections.get("notetakers"), dict) else {}
    out = []
    for key, label in NOTETAKERS:
        row = given.get(key) if isinstance(given.get(key), dict) else None
        if row is not None:
            if "state" in row:
                state = NOTETAKER_STATE.get(str(row["state"]), "unknown")
            else:  # An older shape: {connected, error}.
                state = "error" if row.get("error") else "connected" if row.get("connected") else "not_connected"
            error = text(row.get("error"), 160) or ("The key was refused. Reconnect it." if row.get("state") == "refused" else "")
            out.append({"key": key, "label": label, "state": state, "account": account(row.get("account")),
                        "last_import_at": row.get("last_ok_at") or row.get("last_import_at"), "error": error,
                        "server": row.get("state") == "server"})
            continue
        known = key in imported_from or (key == "fathom" and (connections.get("fathom") or {}).get("configured"))
        out.append({"key": key, "label": label, "state": "connected" if known else "unknown",
                    "account": "", "last_import_at": None, "error": "", "server": False})
    return out
