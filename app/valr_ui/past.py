"""Past interviews, filled in: company, interviewers and outcome from what VALR stores.

performance.json (the coach's scores) often lacks the company ("Unknown company"), never has
the interviewers, and knows the outcome only when the transcript said so. This fills them in,
in order of trust:

- company: the scorecard's own (unless it is a round label like "L2- Alex" or the owner's
  name), else the stored recording's company (Fathom's external invitees' email domain,
  app/notetakers/base.invitee_details), else the calendar title's, else the Gmail lead
  matched below;
- interviewers: the stored recording's external invitees;
- outcome: "Advanced" / "Not advanced" from the scorecard, else the matched lead's stage
  ("Interviewing", "Offer", "Rejected", "Ghosted", ...), else "Outcome unknown".

A lead matches by the invitees' domain, by a contact's email (compared as a one-way hash, the
recording never stores addresses) or by the company name. Pure functions; tests in
tests/test_valr_pages.py.
"""
import re
from typing import Any

from app.notetakers import base
from app.notetakers.store import names

# A lead's raw stage -> (outcome label, chip tone).
LEAD_OUTCOME = {
    "closed-lost": ("Rejected", "bad"), "rejected": ("Rejected", "bad"), "declined": ("Rejected", "bad"),
    "ghosted": ("Ghosted", "warn"), "withdrawn": ("Withdrawn", "muted"),
    "closed-won": ("Offer", "ok"), "offer": ("Offer", "ok"), "offered": ("Offer", "ok"),
    "interviewing": ("Interviewing", "info"), "interview": ("Interviewing", "info"), "onsite": ("Interviewing", "info"),
    "final": ("Interviewing", "info"), "screening": ("Screening", "info"), "screen": ("Screening", "info"),
    "applied": ("Applied", "muted"),
}
LEAD_STAGE = {"interviewing": ("Interviewing", "info"), "offer": ("Offer", "ok"), "screening": ("Screening", "info"),
              "applied": ("Applied", "muted"), "closed": ("Closed", "muted")}
FILLER = {"inc", "ltd", "llc", "pvt", "private", "limited", "software", "technologies", "technology", "labs",
          "ai", "hq", "the", "co", "corp", "company", "india"}


def key(name: str) -> str:
    """"Razorpay Software Private Limited" -> "razorpay": comparable company names."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in FILLER]
    return "".join(words)


def same_domain(a: str, b: str) -> bool:
    a, b = (a or "").lower().strip("."), (b or "").lower().strip(".")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def match_lead(company: str, row: dict[str, Any] | None, leads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The lead for this interview: invitee domain, then a contact's email, then the name."""
    domain = (row or {}).get("company_domain") or ""
    hashes = set(names(row, "invitee_hashes"))
    if domain:
        hit = next((l for l in leads if same_domain(domain, l.get("domain") or "")), None)
        if hit:
            return hit
    if hashes:
        hit = next((l for l in leads if any(c.get("email") and base.email_hash(c["email"]) in hashes
                                            for c in l.get("contacts") or [])), None)
        if hit:
            return hit
    wanted = key(company)
    if len(wanted) >= 3:
        for lead in leads:
            have = key(lead.get("company") or "")
            if lead.get("company") != "Unknown company" and len(have) >= 3 and (
                    have == wanted or (min(len(have), len(wanted)) >= 5 and (have.startswith(wanted) or wanted.startswith(have)))):
                return lead
    return None


def lead_outcome(lead: dict[str, Any]) -> tuple[str, str] | None:
    raw = str(lead.get("stage_raw") or "").lower()
    return LEAD_OUTCOME.get(raw) or LEAD_STAGE.get(str(lead.get("stage") or ""))


def enrich(interview: dict[str, Any], row: dict[str, Any] | None, leads: list[dict[str, Any]],
           owner: set[str] | None = None) -> dict[str, Any]:
    """One performance interview, with company, interviewers and an outcome chip filled in."""
    owner = base.owner_words() if owner is None else owner
    candidates = [interview.get("company") or "", (row or {}).get("company") or "",
                  (row or {}).get("event_company") or ""]
    company = next((c for c in candidates if c and base.is_company(c, owner)), "")
    lead = match_lead(company, row, leads)
    if not company and lead and lead.get("company") != "Unknown company":
        company = lead["company"]
    out = {**interview, "company": company, "interviewers": names(row)[:6]}
    if interview.get("outcome") not in ("advanced", "rejected") and lead:
        chip = lead_outcome(lead)
        if chip:
            out["outcome_chip"] = (*chip, f"From your leads: {lead.get('stage_label') or chip[0]}")
    return out


def all_leads(leads: dict[str, Any] | None) -> list[dict[str, Any]]:
    """contracts.leads(...) -> every lead, active and expired."""
    leads = leads or {}
    return list(leads.get("leads") or []) + list(leads.get("expired") or [])
