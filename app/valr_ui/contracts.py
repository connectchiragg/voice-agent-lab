"""The shapes the other branches plug into the pages, normalised in one place.

Each function takes whatever the producing branch writes, tolerates missing or extra
fields, scrubs free text (phone numbers and stray emails hidden; contact emails are
kept, the owner sees them) and returns a small, stable dict the pages render.

- Leads (valr-leads, GET /valr/api/leads, static/leads-globe.js):
  {"leads": [{"id", "company", "role", "stage", "last_activity_at", "next_step",
  "waiting_on_you", "source", "location", "lat", "lng", "url", "contacts": [{"name", "email"}],
  "summary"}], "generated_at", "connected"}; `stage` is one of STAGES (anything else: "other").
- Coach (valr-eval, performance.json top level): "coach": {"headline", "narrative" (text or
  a list of paragraphs), "strengths": [text], "work_on": [text | {"action", "why"}]}.
- Impression cues (valr-eval, per interview): "impression": {"summary", "cues": [{"cue",
  "tone": positive|negative|neutral, "quote"}]} (or a plain list of cues).
- Recording (valr-notetakers, per interview): "recording": {"provider", "url", "embed_url",
  "title"}; the embed is used only from an allow-listed https host.
- Notetakers (valr-notetakers, view["connections"]["notetakers"]): {"fathom" | "fireflies" |
  "granola": {"connected", "account", "last_import_at", "error"}}.
"""
import re
from typing import Any
from urllib.parse import urlsplit

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(\d{3}\)\s?\d{3}[-.\s]\d{4}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
                   r"|\b\d{5}\s?\d{5}\b")
STAGES = [("applied", "Applied"), ("screening", "Screening"), ("interviewing", "Interviewing"),
          ("offer", "Offer"), ("closed", "Closed"), ("other", "Other")]
STAGE_ALIASES = {"new": "applied", "lead": "applied", "screen": "screening", "recruiter": "screening",
                 "interview": "interviewing", "onsite": "interviewing", "final": "interviewing",
                 "offered": "offer", "rejected": "closed", "declined": "closed", "withdrawn": "closed",
                 "ghosted": "closed", "archived": "closed"}
EMBED_HOSTS = {"fathom.video", "app.fireflies.ai", "share.fireflies.ai", "app.granola.ai",
               "notes.granola.ai"}
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
                    "email": email if EMAIL.fullmatch(email) else ""})
    return out[:6]


def leads(raw: Any) -> dict[str, Any]:
    """valr-leads' JSON -> {"available", "leads": [...], "stages": [{key, label, count}]}."""
    raw = raw if isinstance(raw, dict) else {}
    rows = []
    for i, lead in enumerate(raw.get("leads") or []):
        if not isinstance(lead, dict):
            continue
        rows.append({
            "id": re.sub(r"[^\w-]", "-", str(lead.get("id") or f"lead-{i}"))[:64],
            "company": text(lead.get("company"), 80) or "Unknown company",
            "role": text(lead.get("role") or lead.get("title"), 120),
            "stage": stage_key(lead.get("stage") or lead.get("status")),
            "last_activity_at": lead.get("last_activity_at") or lead.get("last_contact_at")
            or lead.get("updated_at"),
            "next_step": text(lead.get("next_step"), 200),
            "waiting_on_you": bool(lead.get("waiting_on_you") or lead.get("needs_reply")),
            "source": text(lead.get("source"), 40),
            "location": text(lead.get("location"), 80),
            "lat": number(lead.get("lat")), "lng": number(lead.get("lng")),
            "url": https(lead.get("url") or lead.get("thread_url")),
            "contacts": contacts(lead.get("contacts")),
            "summary": text(lead.get("summary"), 400),
        })
    counts = {key: sum(1 for r in rows if r["stage"] == key) for key, _ in STAGES}
    return {"available": bool(raw) and raw.get("connected", True) is not False,
            "generated_at": raw.get("generated_at"), "leads": rows,
            "stages": [{"key": k, "label": label, "count": counts[k]} for k, label in STAGES
                       if counts[k] or k != "other"]}


def paragraphs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text(p, 1200) for p in value if str(p or "").strip()][:6]
    return [text(p, 1200) for p in re.split(r"\n\s*\n", str(value or "")) if p.strip()][:6]


def coach(raw: Any) -> dict[str, Any] | None:
    """valr-eval's coach narrative, or None until it lands."""
    if isinstance(raw, str):
        raw = {"narrative": raw}
    if not isinstance(raw, dict):
        return None
    work = []
    for item in raw.get("work_on") or []:
        if isinstance(item, dict) and str(item.get("action") or "").strip():
            work.append({"action": text(item["action"], 200), "why": text(item.get("why"), 300)})
        elif isinstance(item, str) and item.strip():
            work.append({"action": text(item, 200), "why": ""})
    out = {"headline": text(raw.get("headline"), 200),
           "narrative": paragraphs(raw.get("narrative") or raw.get("summary")),
           "strengths": [text(s, 200) for s in raw.get("strengths") or [] if isinstance(s, str) and s.strip()][:4],
           "work_on": work[:4], "generated_at": raw.get("generated_at")}
    return out if out["headline"] or out["narrative"] or out["strengths"] or out["work_on"] else None


def impression(raw: Any) -> dict[str, Any] | None:
    """How the interviewer likely read him: a short summary and a few cues."""
    if isinstance(raw, list):
        raw = {"cues": raw}
    if not isinstance(raw, dict):
        return None
    cues = []
    for cue in raw.get("cues") or []:
        if isinstance(cue, str) and cue.strip():
            cues.append({"cue": text(cue, 240), "tone": "neutral", "quote": ""})
        elif isinstance(cue, dict) and str(cue.get("cue") or cue.get("text") or "").strip():
            tone = str(cue.get("tone") or cue.get("valence") or "neutral").lower()
            cues.append({"cue": text(cue.get("cue") or cue.get("text"), 240),
                         "tone": tone if tone in ("positive", "negative", "neutral") else "neutral",
                         "quote": text(cue.get("quote"), 240)})
    out = {"summary": text(raw.get("summary"), 600), "cues": cues[:8]}
    return out if out["summary"] or out["cues"] else None


def recording(raw: Any, interview: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A link to the recording, and an embed URL only from a known notetaker host."""
    raw = raw if isinstance(raw, dict) else {}
    url = https(raw.get("url") or (interview or {}).get("recording_url"))
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
    """Each notetaker's real state: valr-notetakers' status when present; else Fathom counts as
    connected when its recordings are being imported (its key lives on the worker, not here)."""
    given = connections.get("notetakers") if isinstance(connections.get("notetakers"), dict) else {}
    out = []
    for key, label in NOTETAKERS:
        row = given.get(key) if isinstance(given.get(key), dict) else None
        if row is not None:
            state = "error" if row.get("error") else "connected" if row.get("connected") else "not_connected"
            out.append({"key": key, "label": label, "state": state, "account": account(row.get("account")),
                        "last_import_at": row.get("last_import_at"), "error": text(row.get("error"), 160)})
            continue
        known = key in imported_from or (key == "fathom" and (connections.get("fathom") or {}).get("configured"))
        out.append({"key": key, "label": label, "state": "connected" if known else "unknown",
                    "account": "", "last_import_at": None, "error": ""})
    return out
