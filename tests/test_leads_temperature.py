"""The temperature spectrum and the ball in court, on archetypal situations.

Each archetype is a short, made-up history (fictional companies, example.com-style domains).
The table is the spec the owner reads in docs/leads.md: a change to the formula must keep
every situation in its band, and the scores spread across 0-100."""
from datetime import datetime, timedelta, timezone

import pytest

from app import leads
from app.leads import Contact, Touch

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
REC = Contact("rec@acme.example", "Riya Rao", "recruiter")


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def email(i, days, *, out=False, hint="", sentiment=0.3, cue="none", step="", people=(REC,), category="",
          asked=None, action="", thread="t1"):
    return Touch(f"gmail:{i}", "email", ago(days), "Acme", "acme.example", list(people), f"mail {i}", "",
                 "out" if out else "in", None if out else sentiment, cue, hint, step, thread=thread,
                 category=category, action_requested=asked, action=action)


def meet(i, days, hint="interviewing", title=""):
    return Touch(f"cal:{i}", "calendar", ago(days), "Acme", "acme.example", [REC], title or f"Acme round {i}", stage_hint=hint)


def score(i, days, value, cue="none"):
    return Touch(f"perf:{i}", "evaluation", ago(days), "Acme", "", [], "scored", cue=cue, score=value)


ACK = dict(people=(), category="application_update", sentiment=0.1, hint="applied", asked=False)

# (situation, touches, band, score, waiting_on)
ARCHETYPES = [
    ("Offer received 3 days ago, awaiting your decision",
     [meet(1, 15), score(1, 15, 8.1, "positive"),
      email(2, 3, hint="offer", sentiment=0.9, cue="positive", category="offer", asked=True, action="decide on the offer")],
     "hot", 99, "you"),
    ("Interviewing, next interview in 3 days",
     [email(1, 9, hint="screening", sentiment=0.5), meet(2, 6), score(2, 6, 7.4, "positive"),
      email(3, 2, hint="interviewing", sentiment=0.8, cue="positive", step="Panel on Tuesday"), meet(4, -3)],
     "hot", 86, "nobody"),
    ("Final round tomorrow after two strong rounds",
     [meet(1, 12), score(1, 12, 8.0, "positive"), meet(2, 5), score(2, 5, 8.4, "positive"),
      email(3, 1, hint="interviewing", sentiment=0.8, cue="positive"), meet(4, -1, title="Acme · Final round")],
     "hot", 94, "nobody"),
    ("References requested after the onsite",
     [meet(1, 9), meet(2, 6, title="Acme onsite"), score(2, 6, 7.8, "positive"),
      email(3, 2, hint="interviewing", sentiment=0.7, cue="positive", step="Share two references", asked=True,
            action="share two references")],
     "hot", 92, "you"),
    ("Compensation talk with HR booked after the rounds",
     [meet(1, 10), score(1, 10, 7.0), email(2, 3, hint="interviewing", sentiment=0.6),
      meet(3, -4, title="Acme HR round: compensation discussion")],
     "hot", 93, "nobody"),
    ("First technical round in 2 days",
     [email(1, 5, hint="screening", sentiment=0.5), meet(2, -2, title="Acme technical interview")],
     "hot", 78, "nobody"),
    ("Interviewed 2 days ago, waiting for feedback",
     [email(1, 8, hint="screening", sentiment=0.5), meet(2, 2), score(2, 2, 7.5)],
     "warm", 66, "them"),
    ("Take-home sent 3 days ago, not submitted",
     [email(1, 7, hint="screening", sentiment=0.5), email(2, 3, hint="interviewing", sentiment=0.4,
                                                          category="assessment", asked=True, action="submit the take-home")],
     "warm", 60, "you"),
    ("Recruiter replied yesterday asking for your availability",
     [email(1, 4, out=True, hint="applied"),
      email(2, 1, hint="screening", sentiment=0.6, category="scheduling", asked=True, action="share your availability")],
     "warm", 58, "you"),
    ("Recruiter asks your expected CTC before a first screen",
     [email(1, 1, hint="screening", sentiment=0.5, category="scheduling", asked=True, action="share your expected CTC")],
     "warm", 55, "you"),
    ("Recruiter screen booked in 2 days",
     [email(1, 3, hint="screening", sentiment=0.5, category="scheduling", asked=True, action="pick a slot"),
      email(2, 2.5, out=True), meet(3, -2, "screening")],
     "warm", 68, "nobody"),
    ("You answered the recruiter's question yesterday",
     [email(1, 3, hint="screening", sentiment=0.5, asked=True, action="share your notice period"), email(2, 1, out=True)],
     "warm", 53, "them"),
    ("Applied 2 days ago, no reply",
     [email(1, 2, **ACK)],
     "cool", 44, "them"),
    ("A recruiter reached out 4 days ago (sourced)",
     [email(1, 4, hint="sourced", sentiment=0.5, category="recruiter_outreach")],
     "cool", 37, "them"),
    ("Interviewed 2 weeks ago, then silence",
     [email(1, 20, hint="screening", sentiment=0.4), meet(2, 14), score(2, 14, 7.0)],
     "cool", 46, "them"),
    ("Applied 20 days ago, no reply",
     [email(1, 20, **ACK)],
     "cold", 21, "nobody"),
    ("A recruiter question left unanswered for 15 days",
     [email(1, 15, hint="screening", sentiment=0.5, asked=True, action="share your resume")],
     "cool", 32, "nobody"),
    ("Interviewing, then 25 days of silence (ghosted)",
     [meet(1, 25), score(1, 25, 6.5), email(2, 25, asked=True, action="reply with feedback")],
     "cold", 15, "nobody"),
    ("Rejected",
     [email(1, 12, hint="applied"), email(2, 5, hint="closed-lost", sentiment=-0.4, cue="negative", category="rejection")],
     "closed", 10, "nobody"),
]


@pytest.mark.parametrize("situation,touches,band,score,waiting", ARCHETYPES, ids=[a[0] for a in ARCHETYPES])
def test_archetypes_land_in_their_band(situation, touches, band, score, waiting):
    lead = leads.build(touches, NOW)[0]
    assert lead.band == band, (situation, lead.temperature, lead.parts)
    assert abs(lead.temperature - score) <= 2, (situation, lead.temperature, lead.parts)
    assert lead.waiting_on == waiting, (situation, lead.waiting_reason)


def test_the_hot_tiers_keep_their_order():
    """offer > final stages > a later round with momentum > an early round, whatever else."""
    by = {a[0]: leads.build(a[1], NOW)[0] for a in ARCHETYPES}
    offer = by["Offer received 3 days ago, awaiting your decision"]
    final = by["Final round tomorrow after two strong rounds"]
    late = by["Interviewing, next interview in 3 days"]
    early = by["First technical round in 2 days"]
    assert offer.temperature > final.temperature > late.temperature > early.temperature >= 75
    assert [l.parts["tier"]["name"] for l in (offer, final, late, early)] == ["offer", "final", "late", "early"]
    assert final.stage == "final" and by["References requested after the onsite"].stage == "final"
    assert by["Recruiter asks your expected CTC before a first screen"].stage == "screening"  # Too early to mean an offer.
    # A weak, quiet offer still beats the best possible final round; a stale final is not Hot.
    weak = leads.build([email(1, 12, hint="offer", sentiment=-0.5)], NOW)[0]
    assert weak.stage == "offer" and 95 <= weak.temperature < 100 and weak.temperature > final.temperature
    stale = leads.build([meet(1, 18, title="Acme final round")], NOW)[0]
    assert stale.stage == "final" and stale.band != "hot"
    for lead in by.values():
        tier = lead.parts["tier"]
        if tier:
            assert tier["low"] <= lead.temperature <= tier["high"]


def test_archetypes_spread_across_the_scale():
    scores = sorted(leads.build(t, NOW)[0].temperature for _, t, band, _, _ in ARCHETYPES if band != "closed")
    assert scores[0] < 20 and scores[-1] > 85
    assert {leads.band_of(s) for s in scores} == {"hot", "warm", "cool", "cold"}


def test_band_edges():
    assert [leads.band_of(t) for t in (0, 24, 25, 49, 50, 74, 75, 100)] == \
        ["cold", "cold", "cool", "cool", "warm", "warm", "hot", "hot"]
    assert leads.band_of(90, "closed-won") == leads.band_of(3, "closed-lost") == "closed"
    assert leads.band_of(90, "ghosted") == "hot"  # The band follows the score; ghosted is capped at 24.


def test_status_live_closed_expired():
    rejected = leads.build(ARCHETYPES[-1][1], NOW)[0]
    assert rejected.status == "closed"
    old = leads.build([email(1, 31, hint="closed-lost", cue="negative")], NOW)[0]
    assert old.status == "expired"  # Expired wins over closed: hidden everywhere.
    assert leads.build(ARCHETYPES[0][1], NOW)[0].status == "active"


def test_counts_agree():
    built = [leads.build(t, NOW)[0] for _, t, _, _, _ in ARCHETYPES]
    c = leads.counts(built)
    assert c["live"] + c["closed"] + c["expired"] == len(built)
    assert sum(c["bands"].values()) == c["live"]
    assert list(c["bands"]) == ["hot", "warm", "cool", "cold"]
    assert c["waiting_on_you"] == sum(1 for l in built if l.waiting_on == "you" and l.status == "active")


# Ball in court, beyond the archetypes above.

def court(touches):
    lead = leads.build(touches, NOW)[0]
    return lead.waiting_on, lead.waiting_reason


def test_recruiter_asks_for_availability():
    who, why = court([email(1, 2, hint="screening", sentiment=0.6, category="scheduling", asked=True,
                            action="share your availability")])
    assert who == "you" and why == "Recruiter: share your availability (2 days ago)"


def test_your_reply_or_a_booked_event_hands_the_ball_back():
    asked = email(1, 3, hint="screening", sentiment=0.6, asked=True, action="share your availability")
    assert court([asked, email(2, 2, out=True)])[0] == "them"
    assert court([asked, email(2, 2, out=True, thread="other")])[0] == "you"  # Another thread is not a reply.
    assert court([asked, meet(3, -2, "screening")])[0] == "nobody"  # He booked it: nothing owed.


def test_automated_and_closing_mail_never_waits_on_you():
    assert court([email(1, 1, **ACK)])[0] == "them"  # An ATS acknowledgement: waiting on them.
    noreply = email(1, 1, people=(), sentiment=0.2, hint="screening", category="other", asked=True, action="reply")
    assert court([noreply])[0] != "you"
    rejection = email(1, 1, hint="closed-lost", cue="negative", category="rejection", asked=True, action="reply")
    assert court([rejection])[0] == "nobody"
    alert = email(1, 1, category="job_board_alert", asked=True, action="apply now")
    assert court([alert])[0] != "you"


def test_a_scheduling_link_from_a_tool_still_counts():
    link = email(1, 1, people=(), hint="interviewing", sentiment=0.5, category="scheduling", asked=True,
                 action="pick an interview slot")
    assert court([email(0, 5, hint="screening", sentiment=0.5), link])[0] == "you"


def test_older_rows_use_the_stored_next_step():
    assert court([email(1, 2, hint="screening", sentiment=0.5, step="Reply with availability")])[0] == "you"
    assert court([email(1, 2, hint="screening", sentiment=0.5, step="Team will review and get back")])[0] == "them"
    assert court([email(1, 2, hint="screening", sentiment=0.5, step="")])[0] == "them"


def test_waiting_on_them_ends_after_three_weeks():
    who, why = court([email(1, 6, hint="screening", sentiment=0.5), email(2, 3, out=True)])
    assert who == "them" and why == "Waiting on Acme for 3 days"


def test_cold_closed_and_expired_wait_on_nobody():
    cold = leads.build([email(1, 13, hint="applied", sentiment=-0.5, cue="negative", asked=True, action="reply")], NOW)[0]
    assert cold.band == "cold" and cold.waiting_on == "nobody"
    assert court([email(1, 40, asked=True, action="reply")])[0] == "nobody"


def test_an_interview_on_the_calendar_does_not_answer_an_offer():
    offer = email(2, 1, hint="offer", sentiment=0.9, cue="positive", category="offer", asked=True, action="decide on the offer")
    assert court([meet(1, 10), offer, meet(3, -2)])[0] == "you"
