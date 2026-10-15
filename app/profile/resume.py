"""Resume PDF -> structured fields. Deterministic: no LLM, no network.

Contact details (email, phone) are scrubbed from the text before parsing, so
they cannot reach the stored profile by any path.
"""
import re
from datetime import date
from pathlib import Path

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)")
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATES = re.compile(rf"^({MONTH}\s+\d{{4}})\s*[-–]\s*({MONTH}\s+\d{{4}}|Present)$", re.I)
LOCATION = re.compile(r"^[A-Z][A-Za-z .]+, [A-Z][A-Za-z ]+$")
SECTIONS = ("WORK EXPERIENCE", "EXPERIENCE", "OPEN SOURCE & COMMUNITY", "SKILLS", "EDUCATION")
BULLET = "•"
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def extract_text(pdf: Path) -> str:
    from pypdf import PdfReader
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)


def scrub_contacts(text: str) -> str:
    def phone(m: re.Match) -> str:
        return "" if sum(c.isdigit() for c in m.group()) >= 10 else m.group()
    return PHONE.sub(phone, EMAIL.sub("", text))


def _month(value: str) -> date | None:
    if value.lower() == "present":
        return None
    name, year = value.split()
    return date(int(year), MONTHS[name[:3].lower()], 1)


def _sections(lines: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"HEADER": []}
    current = "HEADER"
    for line in lines:
        if line.upper() == line and line in SECTIONS:
            current = line
            out[current] = []
        else:
            out[current].append(line)
    return out


def _entries(lines: list[str]) -> list[dict]:
    """Company / location / title / dates headers followed by bullets."""
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        if (i + 3 < len(lines) and LOCATION.match(lines[i + 1])
                and DATES.match(lines[i + 3])):
            start, end = DATES.match(lines[i + 3]).groups()
            entries.append({"organisation": lines[i], "location": lines[i + 1],
                            "title": lines[i + 2], "start": start, "end": end, "bullets": []})
            i += 4
            continue
        if entries:
            _add_bullet_text(entries[-1]["bullets"], lines[i])
        i += 1
    return entries


def _add_bullet_text(bullets: list[str], line: str) -> None:
    if line.startswith(BULLET) or not bullets:
        bullets.append(line.lstrip(BULLET).strip())
    else:
        bullets[-1] = f"{bullets[-1]} {line}"


def _bullets(lines: list[str]) -> list[str]:
    bullets: list[str] = []
    for line in lines:
        _add_bullet_text(bullets, line)
    return [b for b in bullets if b]


def _skills(lines: list[str]) -> dict[str, list[str]]:
    skills: dict[str, list[str]] = {}
    last = None
    for line in lines:
        if ":" in line:
            last, _, items = line.partition(":")
            skills[last.strip()] = []
        elif last is None:
            continue
        else:
            items = line
        skills[last.strip()] += [s.strip() for s in items.split(",") if s.strip()]
    return skills


def _header(lines: list[str]) -> dict:
    name = lines[0] if lines else ""
    location = ""
    links = []
    for line in lines[1:]:
        for part in (p.strip() for p in line.split("·")):
            if not part:
                continue
            if LOCATION.match(part) and not location:
                location = part
            elif ":" in part:
                label, _, value = part.partition(":")
                links.append({"label": label.strip(), "url": value.strip()})
    return {"name": name.title() if name.isupper() else name, "location": location,
            "links": links}


def years_of_experience(roles: list[dict], today: date) -> float:
    starts = [_month(r["start"]) for r in roles]
    return round((today - min(starts)).days / 365.25, 1) if starts else 0.0


def parse_resume(text: str, today: date | None = None) -> dict:
    today = today or date.today()
    lines = [ln.strip() for ln in scrub_contacts(text).splitlines()]
    lines = [ln for ln in lines if ln and ln not in {"·"}]
    sections = _sections(lines)
    roles = _entries(sections.get("WORK EXPERIENCE") or sections.get("EXPERIENCE") or [])
    education = [{k: e[k] for k in ("organisation", "location", "title", "start", "end")}
                 for e in _entries(sections.get("EDUCATION", []))]
    return {
        **_header(sections["HEADER"]),
        "roles": roles,
        "years_of_experience": years_of_experience(roles, today),
        "open_source_and_talks": _bullets(sections.get("OPEN SOURCE & COMMUNITY", [])),
        "skills": _skills(sections.get("SKILLS", [])),
        "education": education,
    }
