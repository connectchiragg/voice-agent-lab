"""Fictional data for the parts the other branches plug in, until they land: the leads
(valr-leads), the coach narrative and impression cues (valr-eval) and recordings and
notetaker status (valr-notetakers). Same companies as tests/valr_sample.py; every email
is at an .example domain; no real people or companies."""
from datetime import datetime, timedelta, timezone

LEADS = [  # (company, role, stage, waiting on you, days since activity, next step, location, lat, lng)
    ("Juniper", "Senior Backend Engineer", "interviewing", False, 0, "System design round today", "Bengaluru", 12.97, 77.59),
    ("Nimbus", "Staff Engineer, Payments", "interviewing", False, 1, "Coding round tomorrow", "Remote (India)", 19.08, 72.88),
    ("Kite", "Platform Engineer", "screening", True, 3, "Reply with your availability", "Pune", 18.52, 73.86),
    ("Lumen", "Backend Engineer", "applied", False, 6, "Waiting for the recruiter", "Hyderabad", 17.39, 78.49),
    ("Orbit", "Senior Engineer, Data", "offer", True, 2, "Offer call: ask for the band in writing", "Bengaluru", 12.93, 77.62),
    ("Vela", "AI Engineer", "screening", False, 4, "Recruiter screen on Monday", "Remote", 28.61, 77.21),
    ("Tessel", "Voice AI Engineer", "closed", False, 12, "", "Chennai", 13.08, 80.27),
    ("Quanta", "Senior Software Engineer", "applied", False, 9, "Applied via referral", "Gurugram", 28.46, 77.03),
    ("Fernway", "Backend Lead", "interviewing", False, 2, "Hiring manager chat on Thursday", "Bengaluru", 12.99, 77.70),
    ("Marlo", "Founding Engineer", "screening", True, 1, "Founder asked for a time this week", "Remote", 15.30, 74.12),
    ("Brightline", "Engineer II", "closed", False, 20, "", "Noida", 28.54, 77.39),
    ("Cobalt Labs", "Senior Backend Engineer", "applied", False, 5, "", "Mumbai", 19.12, 72.85),
    ("Harbor", "Staff Engineer", "interviewing", False, 3, "Take-home due Sunday", "Remote (APAC)", 1.35, 103.82),
    ("Pinecrest", "Platform Engineer", "applied", False, 7, "", "Kochi", 9.93, 76.27),
]


def sample_leads(now: datetime | None = None) -> dict:
    """The JSON /valr/api/leads is expected to return (see app/valr_ui/contracts.py)."""
    now = now or datetime.now(timezone.utc)
    leads = []
    for i, (company, role, stage, waiting, days, step, place, lat, lng) in enumerate(LEADS):
        slug = company.lower().replace(" ", "")
        leads.append({"id": f"lead-{i + 1}", "company": company, "role": role, "stage": stage,
                      "waiting_on_you": waiting, "last_activity_at": (now - timedelta(days=days)).isoformat(),
                      "next_step": step, "source": "Gmail", "location": place, "lat": lat, "lng": lng,
                      "contacts": [{"name": "Recruiting team", "email": f"talent@{slug}.example"}],
                      "summary": f"Fictional lead: {role} at {company}."})
    return {"generated_at": now.isoformat(), "connected": True, "leads": leads}


COACH = {
    "headline": "Your technical answers are landing; pushback is where rounds slip.",
    "narrative": [
        "Across your last seven interviews you lead with the answer far more often, and your system "
        "design rounds now size the problem before drawing boxes. That is the biggest change since August.",
        "The pattern to fix: when an interviewer challenges a design choice, you concede within a "
        "minute. Restate their concern, say what you'd trade, and hold the parts you believe in.",
    ],
    "strengths": ["Answer first, then the reasoning", "Capacity estimates without being asked",
                  "Clear stories with a number at the end"],
    "work_on": [{"action": "Hold your position under pushback", "why": "Conceded quickly at Vela and Nimbus."},
                {"action": "Ask for their band before giving a number", "why": "Gave a figure first in two screens."}],
}
IMPRESSION = {
    "summary": "Came across as sharp and well prepared; a little quick to agree when challenged.",
    "cues": [{"cue": "Sized the system unprompted in the first five minutes", "tone": "positive"},
             {"cue": "Interviewer pushed on consistency; the answer changed within a minute", "tone": "negative",
              "quote": "Sure, we could do it the other way too."},
             {"cue": "Asked a specific question about their streaming migration", "tone": "positive"}],
}


def with_extras(perf: dict) -> dict:
    """performance.json plus the coach, impression cues and recordings."""
    perf = {**perf, "coach": COACH, "interviews": [dict(iv) for iv in perf["interviews"]]}
    for i, iv in enumerate(perf["interviews"]):
        iv["recording"] = {"provider": "fathom", "url": f"https://fathom.video/share/sample-{i}",
                           "title": f"{iv['company']} interview"}
        if i == len(perf["interviews"]) - 2:  # One with the inline player (a made-up URL).
            iv["recording"]["embed_url"] = f"https://fathom.video/embed/sample-{i}"
        if i >= len(perf["interviews"]) - 2:
            iv["impression"] = IMPRESSION
    return perf


NOTETAKERS = {"fathom": {"connected": True, "account": "owner@example.com"},
              "fireflies": {"connected": False}, "granola": {"connected": False}}
