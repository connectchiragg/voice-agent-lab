"""Candidate profile: resume fields + owner-provided compensation, and a short prompt brief.

Compensation is SENSITIVE. It is stored only in the profile file (0600) and in
the brief that the briefing prompt reads. Anything printed or logged goes
through `profile_brief(..., redact_compensation=True)` or `redacted_summary`.
"""
import json
import re
from datetime import date
from pathlib import Path

from app.profile.config import write_private
from app.profile.resume import EMAIL, PHONE

SCHEMA_VERSION = 1
REDACTED = "[redacted]"
NUMBER = re.compile(r"\d")
# Theme -> evidence pattern. A theme is a headline strength only when the resume shows it.
STRENGTH_THEMES = [
    ("Voice AI at production scale",
     r"Voice AI.*(clients|calls)"),
    ("Founding-engineer 0-to-1 delivery", r"Founding Engineer|from scratch"),
    ("Agentic systems, AI-native workflows", r"agentic|AI-native"),
    ("Observability and reliability", r"observability|alerts|on-call"),
    ("Data platforms and RAG", r"RAG|analytics platform|ETL"),
    ("Technical leadership and mentoring", r"[Ll]ed |mentor|knowledge transfer"),
]


def _month_year(value: str) -> str:
    return value if value == "Present" else value[:3] + " " + value.split()[-1]


def _short(text: str, words: int) -> str:
    parts = text.split()
    return " ".join(parts[:words]).rstrip(",;:") + ("…" if len(parts) > words else "")


def achievements(roles: list[dict]) -> list[dict]:
    """Bullets that carry a number, newest role first."""
    return [{"organisation": r["organisation"], "text": b}
            for r in roles for b in r["bullets"] if NUMBER.search(b)]


def headline_strengths(parsed: dict) -> list[str]:
    corpus = " ".join([r["title"] for r in parsed["roles"]]
                      + [b for r in parsed["roles"] for b in r["bullets"]]
                      + parsed["open_source_and_talks"])
    return [theme for theme, pattern in STRENGTH_THEMES if re.search(pattern, corpus)]


def validate_compensation(comp: dict) -> dict:
    """Expect {current: {fixed_lpa, variable_lpa, esop_units_per_year, company_type},
    target_lpa: {min, max}, startup_floor_lpa: {min, max}}. Raises without echoing values."""
    try:
        current = comp["current"]
        out = {
            "sensitive": True, "currency": "INR", "unit": "LPA (lakh per annum)",
            "current": {"fixed_lpa": float(current["fixed_lpa"]),
                        "variable_lpa": float(current["variable_lpa"]),
                        "esop_units_per_year": int(current["esop_units_per_year"]),
                        "company_type": str(current.get("company_type", "private"))},
            "target_lpa": {"min": float(comp["target_lpa"]["min"]),
                           "max": float(comp["target_lpa"]["max"])},
            "startup_floor_lpa": {"min": float(comp["startup_floor_lpa"]["min"]),
                                  "max": float(comp["startup_floor_lpa"]["max"])},
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"compensation file is missing or has a bad field: {type(exc).__name__}"
                         ) from None
    out["note"] = comp.get("note", "Floor applies only to cash-constrained startups.")
    return out


def build_profile(parsed: dict, compensation: dict | None, today: date | None = None) -> dict:
    roles = [{k: r[k] for k in ("organisation", "location", "title", "start", "end")}
             | {"highlights": r["bullets"]} for r in parsed["roles"]]
    profile = {
        "schema_version": SCHEMA_VERSION,
        "generated_on": (today or date.today()).isoformat(),
        "name": parsed["name"],
        "location": parsed["location"] or "Bengaluru, India",
        "links": parsed["links"],
        "years_of_experience": parsed["years_of_experience"],
        "current_role": roles[0] if roles else None,
        "roles": roles,
        "headline_strengths": headline_strengths(parsed),
        "achievements": achievements(parsed["roles"]),
        "skills": parsed["skills"],
        "open_source_and_talks": parsed["open_source_and_talks"],
        "education": parsed["education"],
        "compensation": validate_compensation(compensation) if compensation else None,
    }
    assert_no_contacts(profile)
    profile["brief"] = profile_brief(profile)
    return profile


def assert_no_contacts(profile: dict) -> None:
    text = json.dumps(profile)
    if EMAIL.search(text) or any(sum(c.isdigit() for c in m.group()) >= 10
                                 for m in PHONE.finditer(text)):
        raise ValueError("profile contains an email or phone number")


def _lpa(value: float) -> str:
    return f"{value:g}"


def compensation_line(comp: dict | None, redact: bool) -> str:
    if not comp:
        return "Comp: not provided."
    if redact:
        return f"Comp: {REDACTED}."
    c = comp["current"]
    return (f"Comp (INR LPA, private): now {_lpa(c['fixed_lpa'])} fixed + "
            f"{_lpa(c['variable_lpa'])} var + {c['esop_units_per_year']} ESOP units/yr; "
            f"target {_lpa(comp['target_lpa']['min'])}-{_lpa(comp['target_lpa']['max'])}; "
            f"cash-tight startup floor {_lpa(comp['startup_floor_lpa']['min'])}-"
            f"{_lpa(comp['startup_floor_lpa']['max'])}.")


def proof_point(text: str, words: int = 11) -> str:
    """The numeric clauses of an achievement, e.g. '100+ live clients, 1M+ monthly calls'."""
    clauses = [c.strip() for c in re.split(r"[:;,]| while | using | and (?=\d)", text.rstrip(".")) if c.strip()]
    numeric = [c for c in clauses if NUMBER.search(c)] or clauses[:1]
    return _short(", ".join(numeric), words)


def profile_brief(profile: dict, redact_compensation: bool = False) -> str:
    """~200-token summary for the briefing prompt (o200k tokens, as read by gpt-oss)."""
    roles = profile["roles"]
    now = roles[0] if roles else None
    lines = [f"{profile['name']}, {profile['location'].split(',')[0]}, "
             f"~{profile['years_of_experience']:.0f} yrs."]
    if now:
        lines.append(f"Now {now['title']}, {now['organisation']} (since "
                     f"{_month_year(now['start'])}); before "
                     + ", ".join(r["organisation"] for r in roles[1:]) + ".")
    lines.append("Strengths: " + "; ".join(profile["headline_strengths"][:4]) + ".")
    lines.append("Proof: " + "; ".join(proof_point(a["text"], 10)
                                        for a in profile["achievements"][:4]) + ".")
    top_skills = [s for group in profile["skills"].values() for s in group[:2]][:6]
    lines.append("Skills: " + ", ".join(top_skills) + ".")
    if profile["open_source_and_talks"]:
        lines.append("Community: " + "; ".join(
            _short(t, 6) for t in profile["open_source_and_talks"][:2]))
    if profile["education"]:
        e = profile["education"][0]
        lines.append(f"{e['title']}, {e['organisation']} {e['end'].split()[-1]}.")
    lines.append(compensation_line(profile["compensation"], redact_compensation))
    return "\n".join(lines)


def redacted_summary(profile: dict) -> str:
    """What scripts may print: the brief with compensation redacted."""
    return profile_brief(profile, redact_compensation=True)


def comp_figures(comp: dict | None) -> set[str]:
    if not comp:
        return set()
    c = comp["current"]
    values = {c["fixed_lpa"], c["variable_lpa"], c["fixed_lpa"] + c["variable_lpa"],
              c["esop_units_per_year"], *comp["target_lpa"].values(),
              *comp["startup_floor_lpa"].values()}
    return {_lpa(float(v)) for v in values}


EQUITY = r"ESOPs?|RSUs?|options|stock options|shares|equity|stock"


def redact_compensation(text: str, comp: dict | None) -> str:
    """Mask the owner's own comp figures when they appear as amounts (e.g. 'N LPA', 'N-M L',
    'N ESOP units') in text stored outside profile.json or printed."""
    figures = comp_figures(comp)
    if not figures or not text:
        return text
    number = "|".join(sorted((re.escape(f) for f in figures), key=len, reverse=True))
    amount = re.compile(
        rf"(?<![\d.])(?:₹\s*|INR\s*|Rs\.?\s*)?({number})(?:\s*(?:-|–|to)\s*({number}))?"
        rf"(?=\s*(?:LPA|L\b|lakhs?|lacs?|{EQUITY}|units|k\b|CTC|fixed|variable))",
        re.I)
    # Unit-less equity forms where the word comes first: "ESOPs: N", "ESOP grant of N".
    equity_first = re.compile(
        rf"(\b(?:{EQUITY})\b(?:\s+(?:grant|pool|allocation|count))?\s*(?:of|:|=|-|–|is|was)?\s*)"
        rf"(?<![\d.])(?:{number})(?![\d.])", re.I)
    return equity_first.sub(lambda m: m.group(1) + REDACTED, amount.sub(REDACTED, text))


def save_profile(profile: dict, path: Path) -> None:
    write_private(path, json.dumps(profile, indent=2, ensure_ascii=False) + "\n")


def load_profile(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def estimate_tokens(text: str) -> int:
    """Rough count for the briefing model's budget. VALR briefs are read by gpt-oss on Groq
    (o200k tokenizer): measured ~3.6 chars/token on this text. Claude's tokenizer counts
    about 1.7x more for the same text."""
    return max(1, round(len(text) / 3.6))
