"""VALR candidate profile: resume parsing, PII exclusion, compensation handling, brief, script output."""
import json
import logging
import re
import stat
from datetime import date
from pathlib import Path

import pytest

from app.profile import profile as profiles
from app.profile.resume import parse_resume, scrub_contacts

FIXTURES = Path(__file__).parent / "fixtures" / "profile"
TODAY = date(2026, 10, 16)
COMP = {"current": {"fixed_lpa": 31, "variable_lpa": 4, "esop_units_per_year": 120,
                    "company_type": "private"},
        "target_lpa": {"min": 66, "max": 77}, "startup_floor_lpa": {"min": 52, "max": 59}}
COMP_NUMBERS = re.compile(r"(?<![\d.])(31|35|120|66|77|52|59)(?![\d.])")


@pytest.fixture
def parsed():
    return parse_resume((FIXTURES / "resume.txt").read_text(), today=TODAY)


def test_parses_roles_dates_and_sections(parsed):
    assert parsed["name"] == "Asha Rao"
    assert parsed["location"] == "Pune, India"
    assert [(r["organisation"], r["title"], r["start"], r["end"]) for r in parsed["roles"]] == [
        ("Acme Voice", "Founding Engineer, Voice AI", "Mar 2023", "Present"),
        ("Beta Data", "Backend Engineer", "Jan 2021", "Feb 2023")]
    assert parsed["roles"][0]["bullets"][1] == ("Built an agentic workflow platform; led "
                                                "knowledge transfer across the team.")
    assert parsed["years_of_experience"] == 5.8
    assert parsed["skills"]["Languages"] == ["Python", "Go", "SQL"]
    assert parsed["education"][0]["organisation"] == "NIT Trichy"
    assert parsed["open_source_and_talks"] == [
        "Created and open-sourced Widget, a Rust CLI for observability."]
    assert {"label": "GitHub", "url": "github.com/asharao"} in parsed["links"]


def test_profile_excludes_email_and_phone(parsed):
    profile = profiles.build_profile(parsed, COMP, today=TODAY)
    text = json.dumps(profile)
    assert "asha.rao@example.com" not in text and "@" not in text
    assert "98765" not in text and "43210" not in text


def test_scrub_keeps_ordinary_numbers():
    assert scrub_contacts("call +91 98765 43210 or a@b.co; 2016 - 2020, 400+ alerts") == \
        "call  or ; 2016 - 2020, 400+ alerts"


def test_assert_no_contacts_rejects_leaks(parsed):
    profile = profiles.build_profile(parsed, None, today=TODAY)
    profile["links"].append({"label": "x", "url": "someone@example.com"})
    with pytest.raises(ValueError):
        profiles.assert_no_contacts(profile)


def test_profile_fields_and_brief(parsed):
    profile = profiles.build_profile(parsed, COMP, today=TODAY)
    assert profile["current_role"]["organisation"] == "Acme Voice"
    assert "Voice AI at production scale" in profile["headline_strengths"]
    assert [a["organisation"] for a in profile["achievements"]] == ["Acme Voice", "Beta Data"]
    assert profile["compensation"]["target_lpa"] == {"min": 66.0, "max": 77.0}
    assert profile["compensation"]["sensitive"] is True
    brief = profile["brief"]
    assert "target 66-77" in brief and "floor 52-59" in brief  # the prompt needs the numbers
    assert profiles.estimate_tokens(brief) <= 260


def test_redacted_summary_has_no_comp_numbers(parsed):
    profile = profiles.build_profile(parsed, COMP, today=TODAY)
    summary = profiles.redacted_summary(profile)
    assert profiles.REDACTED in summary
    assert not COMP_NUMBERS.search(summary.split("Comp:")[1])


def test_bad_compensation_error_does_not_echo_values(parsed):
    with pytest.raises(ValueError) as err:
        profiles.build_profile(parsed, {"current": {"fixed_lpa": "31x"}}, today=TODAY)
    assert "31" not in str(err.value)


def test_redact_compensation_masks_owner_figures_only():
    comp = profiles.validate_compensation(COMP)
    text = "Said current is 31 LPA, expects 66-77 LPA and 120 ESOP units; offer 45 LPA; 31 minutes"
    out = profiles.redact_compensation(text, comp)
    assert "31 LPA" not in out and "66-77" not in out and "120 ESOP" not in out
    assert "45 LPA" in out and "31 minutes" in out


def test_save_profile_is_private(parsed, tmp_path):
    path = tmp_path / "data" / "profile.json"
    profiles.save_profile(profiles.build_profile(parsed, COMP, today=TODAY), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert profiles.load_profile(path)["name"] == "Asha Rao"


def test_build_profile_script_never_prints_or_logs_comp(tmp_path, monkeypatch, capsys, caplog):
    import scripts.build_profile as script
    comp_file = tmp_path / "comp.json"
    comp_file.write_text(json.dumps(COMP))
    monkeypatch.setattr(script, "extract_text", lambda _: (FIXTURES / "resume.txt").read_text())
    monkeypatch.setenv("VALR_ENV_FILE", str(tmp_path / "none.env"))
    caplog.set_level(logging.DEBUG)
    out = tmp_path / "profile.json"
    assert script.main(["--resume", "x.pdf", "--compensation", str(comp_file),
                        "--out", str(out)]) == 0
    printed = capsys.readouterr().out + caplog.text
    assert "compensation present" in printed
    assert not COMP_NUMBERS.search(printed.split("Brief")[1])
    assert json.loads(out.read_text())["compensation"]["current"]["fixed_lpa"] == 31.0


@pytest.mark.parametrize("text", [
    "He mentioned 120 ESOPs per year", "ESOP: 120 a year", "ESOPs of 120", "120 ESOP",
    "esop grant of 120", "RSUs 120", "120 stock options", "120 shares vest yearly"])
def test_redact_compensation_unitless_equity_forms(text):
    comp = profiles.validate_compensation(COMP)
    out = profiles.redact_compensation(text, comp)
    assert "120" not in out and profiles.REDACTED in out


def test_redact_compensation_leaves_unrelated_counts():
    comp = profiles.validate_compensation(COMP)
    text = "120 turns; ESOP policy discussed; 120 words per minute"
    assert profiles.redact_compensation(text, comp) == text
