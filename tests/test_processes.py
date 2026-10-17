"""Interviews → Processes: deriving each hiring process from the leads' touchpoints (the
fizzle rule, the outcomes), the pattern stats, and the owner-only API. Fictional companies."""
from datetime import datetime, timedelta, timezone

from app.valr_ui import processes
from tests.test_valr_pages import GOOD, client  # noqa: F401 - the pages' client fixture.
from tests.valr_sample_extras import sample_leads

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def touch(days_ago, kind="email", hint="", title="", direction="in", summary="Private email text."):
    return {"kind": kind, "at": (NOW - timedelta(days=days_ago)).isoformat(), "title": title or f"{kind} {days_ago}",
            "stage_hint": hint, "direction": direction, "summary": summary, "cue": "none"}


def lead(*touches, company="Aster", stage="", manual=False, lead_id=None):
    return {"id": lead_id or company.lower(), "company": company, "stage": stage, "stage_manual": manual,
            "timeline": list(reversed(touches))}  # The JSON is newest first.


def one(*touches, **options):
    process = processes.derive(lead(*touches, **options), NOW)
    assert process is not None
    return process


def test_a_process_runs_from_first_contact_through_its_rounds_to_an_offer():
    p = one(touch(30, hint="applied"), touch(24, "email", "interviewing", "Interview invite: round one"),
            touch(20, "calendar", "interviewing", "Round one"), touch(20.1, "evaluation", "interviewing"),
            touch(12, "calendar", "interviewing", "System design"), touch(5, "email", "offer", "Your offer"))
    assert p.start == NOW - timedelta(days=30) and p.status == "offer" and p.end == NOW - timedelta(days=5)
    assert len(p.rounds) == 2 and p.first_interview == NOW - timedelta(days=20.1)  # Same day: one round.
    assert p.duration_days(NOW) == 25
    assert [e.kind for e in p.events][:2] == ["email", "invite"]


def test_rejections_and_withdrawals_are_told_apart():
    assert one(touch(10), touch(3, hint="closed-lost", title="Update on your application")).status == "rejected"
    assert one(touch(10), touch(3, hint="closed-lost", direction="out", title="Re: next steps")).status == "withdrew"
    assert one(touch(10), touch(3, hint="closed-lost", title="Withdrawing my application")).status == "withdrew"
    # A scorecard's verdict only shows in the tracker's stage: it still ends the process.
    assert one(touch(10), touch(6, "evaluation"), stage="closed-lost").status == "rejected"


def test_a_new_round_after_a_rejection_reopens_the_process():
    p = one(touch(40), touch(30, hint="closed-lost"), touch(10, "calendar", "interviewing"))
    assert p.status == "active" and p.end is None


def test_a_process_fizzles_out_after_21_quiet_days_with_nothing_scheduled():
    quiet = one(touch(50), touch(22))
    assert quiet.status == "fizzled" and quiet.end == NOW - timedelta(days=22)
    assert quiet.fizzle_at == NOW - timedelta(days=1)  # Declared 21 days after the last word.
    assert one(touch(50), touch(20)).status == "active"  # 20 days: not yet.
    booked = lead(touch(50), touch(30), {"kind": "calendar", "at": (NOW + timedelta(days=2)).isoformat(),
                                        "title": "Round two", "stage_hint": "interviewing"})
    assert processes.derive(booked, NOW).status == "active"  # Something is still scheduled.
    assert one(touch(5), stage="closed-won", manual=True).status == "offer"  # His own stage wins.


def test_agencies_without_an_employer_and_undated_leads_are_not_processes():
    assert processes.derive({**lead(touch(3)), "agency_only": True}, NOW) is None
    assert processes.derive(lead({"kind": "email", "at": "not a date"}), NOW) is None


def raw(*leads_):
    return {"leads": list(leads_)}


def test_patterns_are_medians_and_say_when_there_is_not_enough_data():
    many = [lead(touch(40), touch(40 - d, "calendar", "interviewing"), touch(40 - d - 5, hint="closed-lost"),
                 company=f"Co{d}") for d in (2, 4, 6, 8)]
    data = processes.timeline(raw(*many, lead(touch(60), company="Quiet"), lead(touch(2), company="Fresh")), NOW)
    by = {p["key"]: p for p in data["patterns"]}
    assert by["first_interview"]["enough"] and by["first_interview"]["value"] == "5 days"  # Median of 2, 4, 6, 8.
    assert by["rejection"]["value"] == "10 days" and by["after_interview"]["value"] == "5 days"
    funnel = by["funnel"]
    assert funnel["enough"] and [s["share"] for s in funnel["stages"]] == [67, 0, 0]
    assert data["counts"] == {"active": 1, "offer": 0, "rejected": 4, "withdrew": 0, "fizzled": 1}
    thin = {p["key"]: p for p in processes.timeline(raw(many[0]), NOW)["patterns"]}
    assert not thin["rejection"]["enough"] and thin["rejection"]["detail"].startswith("Needs more data: 1 of 3")
    assert not thin["funnel"]["enough"]


def test_the_timeline_carries_titles_companies_and_kinds_but_never_email_text():
    data = processes.timeline(sample_leads(NOW), NOW)
    assert data["processes"] and data["events"]
    text = str(data)
    assert "Private email text" not in text and "summary" not in text
    for leads_ in sample_leads(NOW)["leads"]:
        for t in leads_["timeline"]:
            if t["summary"]:
                assert t["summary"] not in text
    assert set(data["events"][0]) == {"at", "process", "company", "kind", "label", "title"}
    assert [e["at"] for e in data["events"]] == sorted(e["at"] for e in data["events"])


def test_the_api_and_the_tab_are_the_owners_only(client):  # noqa: F811
    assert client.get("/valr/api/processes").status_code == 403
    assert client.get("/valr/api/processes", headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    assert client.get("/valr/interviews/processes", headers={"Cf-Access-Jwt-Assertion": "guest"}).status_code == 403
    response = client.get("/valr/api/processes", headers=GOOD)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert {"processes", "events", "patterns", "counts", "range"} <= set(response.json())
    page = client.get("/valr/interviews/processes", headers=GOOD).text
    assert 'href="/valr/interviews/processes" aria-current="page"' in page
    assert 'id="processes-data"' in page and "data-proc-play" in page and "/static/valr-processes.js" in page
    assert 'href="/valr/interviews/processes"' in client.get("/valr/interviews", headers=GOOD).text


def test_no_leads_is_an_empty_state(client):  # noqa: F811
    from app.valr_ui import contracts
    client.box["leads"] = lambda: contracts.leads(None)
    page = client.get("/valr/interviews/processes", headers=GOOD).text
    assert "No hiring processes yet." in page and "data-proc-play" not in page
