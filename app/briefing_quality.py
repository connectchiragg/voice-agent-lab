"""Shared, dependency-free filters for generated and older interview briefs."""
import re

UNCERTAIN_PERSON = re.compile(
    r"(?:does not|doesn't|cannot|could not) confirm|(?:identity|backgrounds?).{0,30}"
    r"(?:unverified|not verified|unconfirmed)|may be (?:a )?different (?:person|interviewer)", re.I)
LISTING_ONLY = re.compile(r"(?:no|without|holds no).{0,25}(?:transcript|summary|discussion)|"
                          r"(?:only|automated) (?:a )?(?:recording )?listing", re.I)
LOGISTICS_NOISE = re.compile(r"\b(?:invite|invitation) (?:was )?sent\b|\b(?:moved|rescheduled) to\b", re.I)


def useful_fact(text: str, category: str = "") -> bool:
    """Missing financial evidence is useful; speculative people and empty listings aren't."""
    if LOGISTICS_NOISE.search(text):
        return False
    if category == "interviewer" and UNCERTAIN_PERSON.search(text):
        return False
    if category == "previous_round" and LISTING_ONLY.search(text):
        return False
    return True


def concise_speech(text: str) -> str:
    """Drop research-status narration, retaining factual substance and role-specific advice."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(s for s in sentences if not LISTING_ONLY.search(s) and not UNCERTAIN_PERSON.search(s))
