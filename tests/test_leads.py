"""The lead tracker's pure logic: merging, stages, expiry, temperature and the graph."""
from datetime import timedelta

from app import leads
from app.leads import Contact, Touch
from tests.leads_sample import NOW, sample_touches


def by_company(built):
    return {lead.company: lead for lead in built}


def test_company_keys_and_domains():
    assert leads.company_key("Acme Technologies Pvt. Ltd.") == "acme"
    assert leads.company_key("Juniper Robotics Inc") == "juniper robotics"
    assert leads.company_domain("careers.acme.example") == "acme.example"
    assert leads.company_domain("mail.acme.co.in") == "acme.co.in"
    assert leads.company_domain("gmail.com") == "" and leads.company_domain("hire.lever.co") == ""
    assert leads.domain_company("careers.acme-labs.example") == "Acme Labs"


def test_sample_merges_into_one_lead_per_company():
    built = by_company(leads.build(sample_touches(), NOW))
    juniper = built["Juniper Robotics"]
    # The calendar title said "Juniper", the score only "Juniper": merged by domain and name.
    assert {t.id for t in juniper.touches} == {"gmail:j1", "gmail:j2", "cal:j3", "perf:j4", "gmail:j5", "cal:j6"}
    assert juniper.role == "Senior Backend Engineer"
    assert {c.role for c in juniper.contacts.values()} == {"recruiter", "interviewer"}
    # A recording with no company joins Quartz through its attendee's email.
    assert "fathom:q2" in {t.id for t in built["Quartz"].touches}
    # The agency recruiter's two clients stay two leads.
    assert "Vela" in built and "Kite" in built


def test_recordings_and_scores_never_start_a_lead():
    orphan = [Touch("fathom:z", "recording", NOW - timedelta(days=1), "Zeta", "zeta.example",
                    [Contact("ann@zeta.example", "Ann Lee")], "Recording")]
    assert leads.build(orphan, NOW) == []
    assert orphan[0].lead_id == ""


def test_fuzzy_company_and_person_names():
    a = Touch("gmail:a", "email", NOW - timedelta(days=3), "Acme Robotics", "", [Contact("", "Priya Nair")], stage_hint="sourced")
    b = Touch("cal:b", "calendar", NOW - timedelta(days=2), "Acme Robotic", "", [], stage_hint="interviewing")
    c = Touch("gmail:c", "email", NOW - timedelta(days=1), "", "", [Contact("", "Priya  Nair")], stage_hint="")
    built = leads.build([a, b, c], NOW)
    assert len(built) == 1 and built[0].stage == "interviewing" and len(built[0].touches) == 3


def test_stages():
    built = by_company(leads.build(sample_touches(), NOW))
    assert built["Juniper Robotics"].stage == "interviewing"
    assert built["Orbit Labs"].stage == "offer"
    assert built["Tessel"].stage == "closed-lost"
    assert built["Lumen"].stage == "ghosted"  # 25 days quiet, not closed.
    assert built["Nimbus"].stage == "screening"
    reopened = [Touch("e1", "email", NOW - timedelta(days=5), "Ono", stage_hint="closed-lost"),
                Touch("e2", "email", NOW - timedelta(days=1), "Ono", stage_hint="interviewing")]
    assert leads.build(reopened, NOW)[0].stage == "interviewing"
    won = [Touch("e1", "email", NOW - timedelta(days=5), "Ono", stage_hint="closed-won"),
           Touch("e2", "email", NOW - timedelta(days=1), "Ono", stage_hint="applied")]
    assert leads.build(won, NOW)[0].stage == "closed-won"


def test_expiry_after_30_quiet_days_and_revival():
    built = by_company(leads.build(sample_touches(), NOW))
    assert built["Harbor"].status == "expired"
    assert built["Lumen"].status == "active"
    touches = sample_touches() + [Touch("gmail:h2", "email", NOW - timedelta(hours=2), "Harbor", "harbor.example",
                                        [Contact("pia@harbor.example", "Pia Novak")], stage_hint="screening")]
    assert by_company(leads.build(touches, NOW))["Harbor"].status == "active"


def test_a_scheduled_interview_keeps_a_lead_active():
    future = [Touch("cal:f", "calendar", NOW + timedelta(days=3), "Fern", "fern.example", [], "Fern interview",
                    stage_hint="interviewing"),
              Touch("gmail:f0", "email", NOW - timedelta(days=45), "Fern", "fern.example", [])]
    lead = leads.build(future, NOW)[0]
    assert lead.status == "active" and lead.stage == "interviewing"
    assert lead.next_step == "Interview: Fern interview" and lead.next_step_at == NOW + timedelta(days=3)


def test_temperature_parts_add_up_and_rank():
    built = leads.build(sample_touches(), NOW)
    for lead in built:
        p = lead.parts
        raw = sum(p[k]["points"] for k in ("recency", "stage", "momentum", "quality"))
        assert 0 <= lead.temperature <= 100
        if p["cap"] is None:
            assert abs(raw - lead.temperature) <= 1.0
        else:
            assert lead.temperature <= p["cap"]
    named = by_company(built)
    assert named["Juniper Robotics"].temperature > named["Nimbus"].temperature > named["Lumen"].temperature
    assert named["Orbit Labs"].temperature >= 60
    assert named["Tessel"].temperature <= 10
    assert built[0].temperature == max(l.temperature for l in built)  # Hottest first.


def test_temperature_formula_by_hand():
    touch = Touch("gmail:a", "email", NOW - timedelta(days=7), "Opal", "opal.example", [], direction="in",
                  sentiment=1.0, cue="positive", stage_hint="screening")
    lead = leads.build([touch], NOW)[0]
    # recency 35 x 0.5 = 17.5; stage 25 x 0.55 = 13.75; momentum 12 x 1/5 = 2.4;
    # quality 4 (no score) + 6 (sentiment +1) + 6 (positive) = 16 -> 49.65
    assert lead.temperature == 50
    assert lead.parts["recency"]["points"] == 17.5 and lead.parts["quality"]["points"] == 16.0


def test_graph_links_contacts_and_shared_people():
    view = leads.graph(leads.build(sample_touches(), NOW), NOW)
    ids = {l["company"]: l["id"] for l in view["leads"]}
    shared = [k for k in view["links"] if k["kind"] == "shared"]
    assert {"source": min(ids["Vela"], ids["Kite"]), "target": max(ids["Vela"], ids["Kite"]),
            "kind": "shared", "weight": 1} in shared
    nina = next(c for c in view["contacts"] if c["name"] == "Nina Shah")
    assert set(nina["leads"]) == {ids["Vela"], ids["Kite"]}
    juniper = next(l for l in view["leads"] if l["company"] == "Juniper Robotics")
    assert juniper["timeline"][0]["kind"] == "calendar"  # Newest first: the booked panel.
    assert all(t["url"] == "" or t["url"].startswith("https://") for t in juniper["timeline"])
    assert set(view["stages"]) >= {"sourced", "offer", "closed-lost", "ghosted"}
