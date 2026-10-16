"""A realistic, fictional cockpit view built with app/valr_view's real builders.

Used by the dashboard tests and to preview the page locally without Postgres or
Temporal. All people, companies, numbers and links are made up.
"""
from datetime import datetime, timedelta, timezone

from app import research_jobs, valr_view
from app.interview_types import Person, SlotEvent


def _event(key, title, company, start, minutes, attendees):
    return SlotEvent(key, title, start.isoformat(), (start + timedelta(minutes=minutes)).isoformat(),
                     attendees=attendees, company=company, company_domain=company.lower() + ".example",
                     organizer_email="talent@" + company.lower() + ".example",
                     organizer_name="Priya (Talent Acquisition)")


def _part(summary, claims, ok=True):
    return {"ok": ok, "findings": {"summary": summary, "claims": [
        {"claim": c, "source_url": u} for c, u in claims]}, "evidence": "", "seen_urls": []}


def _round():
    part = _part("A 60-minute system design round: 45 minutes on one problem, then your questions. "
                 "Senior candidates are expected to drive the design and size it.", [
                     ("Glassdoor reports a 'design a metrics pipeline' prompt.",
                      "https://www.glassdoor.example/Interview/Juniper"),
                     ("Interviewers score trade-offs and capacity estimates explicitly.",
                      "https://www.glassdoor.example/Interview/Juniper-2")])
    part["findings"]["likely_questions"] = [
        "Design ingestion for sensor data from 10,000 robots at 50 Hz.",
        "How would you backfill a day of data after an outage without double counting?",
        "Batch or streaming for fleet health alerts? Defend it.",
        "Walk us through partitioning a 5 TB Postgres table."]
    part["findings"]["prep"] = ["Size 10k robots x 50 Hz x 200 B before drawing boxes",
                                "Have one exactly-once vs at-least-once story ready",
                                "Prepare a question about their Kafka migration"]
    return part


def _company():
    part = _part("Series C warehouse-robotics company, 400 people; raised $90M in May.", [
        ("Series C, $90M, May 2026.", "https://news.example/juniper-series-c"),
        ("Senior backend base $180k-$215k (US); India roles 45-60 LPA.",
         "https://levels.example/companies/juniper/salaries"),
        ("Hybrid, three office days in Bengaluru.", "https://careers.juniper.example/bengaluru")])
    part["findings"]["pay_range"] = {"min": 45, "max": 60, "unit": "LPA", "currency": "INR",
                                     "source_url": "https://levels.example/companies/juniper/salaries"}
    return part


DIMENSIONS = [
    ("technical_depth", "Technical depth", [("tradeoffs", "Trade-offs"), ("numbers", "Numbers & sizing")]),
    ("problem_solving", "Problem solving", [("clarify", "Clarifying"), ("decompose", "Decomposition")]),
    ("communication", "Communication", [("answer_first", "Answer first"), ("concise", "Concision")]),
    ("structure", "Structure", [("star", "STAR stories"), ("signposting", "Signposting")]),
    ("confidence", "Confidence", [("ownership", "Owning decisions")]),
    ("pushback", "Handling pushback", [("hold_update", "Hold or update")]),
    ("pay", "Pay conversations", [("defer", "Deferring the number")]),
]


def scorecards(now: datetime) -> dict:
    """The profile agent's upcoming scorecard file, in the shape app/valr_view expects (1-10)."""
    base = {"technical_depth": 6, "problem_solving": 6, "communication": 4.5, "structure": 4,
            "confidence": 6, "pushback": 6.5, "pay": 4}
    slope = {"technical_depth": 0.45, "problem_solving": 0.35, "communication": 0.6, "structure": 0.65,
             "confidence": 0.05, "pushback": -0.4, "pay": 0.3}
    companies = [("Lumen", "Phone screen"), ("Orbit", "System design"), ("Kite", "Coding"),
                 ("Juniper", "Hiring manager"), ("Vela", "System design"), ("Tessel", "Coding"),
                 ("Nimbus", "System design")]
    interviews = []
    for i, (company, round_) in enumerate(companies):
        scores = {k: round(max(1, min(10, base[k] + slope[k] * i + (0.8 if (i + len(k)) % 3 == 0 else -0.3))), 1)
                  for k in base}
        subs = {sub: round(max(1, min(10, scores[k] + (0.6 if j else -0.5))), 1)
                for k, _, ss in DIMENSIONS for j, (sub, _) in enumerate(ss)}
        interviews.append({
            "id": f"fathom:{1000 + i}", "date": (now - timedelta(days=70 - 10 * i)).date().isoformat(),
            "company": company, "round": round_, "scores": scores, "sub_scores": subs,
            "badges": (["Best clarity yet"] if i == 6 else []) + (["Sized it first"] if i in (4, 6) else []),
            "questions": [
                {"question": "Design a rate limiter for a public API", "score": round(5 + i * 0.5, 1),
                 "feedback": "Good token-bucket choice; sized the Redis memory only when asked.",
                 "better": "Lead with the numbers: 50k req/s, 1 KB per key, then pick the algorithm.",
                 "strong": i > 3},
                {"question": "Tell me about a time you disagreed with your manager", "score": round(7 - i * 0.3, 1),
                 "feedback": "Clear situation, but the result was missing and you conceded quickly.",
                 "better": "Close with the outcome and what you would hold firm on next time.",
                 "strong": False},
                {"question": "What are your salary expectations?", "score": round(4 + i * 0.4, 1),
                 "feedback": "Deferred politely and asked for their range." if i > 2
                 else "Gave a number before hearing their band.",
                 "better": "Ask for the band first; tie your range to scope.", "strong": i > 2},
            ],
        })
    return {"generated_at": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dimensions": [{"key": k, "label": label, "subs": [{"key": s, "label": l} for s, l in ss]}
                           for k, label, ss in DIMENSIONS],
            "interviews": interviews,
            "badges": [{"label": "3 rounds in a row: answer first", "detail": "Communication up 2.4 since August"},
                       {"label": "Sizing habit", "detail": "Estimated capacity unprompted in 2 of the last 3 designs"}],
            "focus_this_week": [
                {"action": "Rehearse a 60-second capacity estimate out loud, twice a day",
                 "why": "Sizing questions decided two of your last four design rounds."},
                {"action": "When challenged, restate their concern before you answer",
                 "why": "Handling pushback has dipped three rounds running."},
                {"action": "Close every story with the result and a number",
                 "why": "STAR answers lose points at the 'result' step."}]}


def sample_view(now: datetime | None = None, paused: bool = False, scenario: str = "full") -> dict:
    """scenario: full (a busy week), empty (first run: nothing connected, no data) or error
    (the calendar store and Temporal are down)."""
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    first = now + timedelta(hours=2, minutes=40)
    tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=30)
    events = [
        _event("ev-juniper-1", "Juniper Robotics: Senior Backend, system design", "Juniper", first, 60, [
            Person("me@example.com", "Chirag", is_self=True),
            Person("maya.lin@juniper.example", "Maya Lin", "accepted"),
            Person("dev.k@juniper.example", "Dev Kapoor", "accepted"),
            Person("talent@juniper.example", "Priya (Talent Acquisition)", organizer=True)]),
        _event("ev-juniper-2", "Juniper Robotics: hiring manager chat", "Juniper", first + timedelta(minutes=75), 45, [
            Person("sam.ortiz@juniper.example", "Sam Ortiz", "needsAction")]),
        _event("ev-nimbus", "Nimbus Pay: Staff Engineer, coding round (call +1 415 555 0134)", "Nimbus",
               tomorrow, 60, [Person("lee@nimbus.example", "Lee Park", "accepted"),
                              Person("noreply@greenhouse.io", "Greenhouse")]),
    ]
    slots = {
        "ev-juniper-1": {
            "key": "ev-juniper-1", "company_key": "juniper", "status": "done",
            "updated_at": now - timedelta(minutes=20),
            "spoken": ("Your first one is Juniper Robotics at the top of the hour: a system design round "
                       "with Maya Lin and Dev Kapoor. Maya leads the fleet telemetry platform and wrote "
                       "about moving it from batch to streaming, so expect a design question on ingesting "
                       "robot sensor data at scale. Dev's talks are about Postgres partitioning. Lead with "
                       "the event-driven pipeline you built, give numbers, and name trade-offs before they "
                       "ask. The posted band for this level is 180 to 215 thousand base."),
            "notes": [
                {"fact": "Maya Lin moved telemetry from nightly batch to Kafka streams in 2025.",
                 "source_ids": ["S1"], "sensitive": False, "private": False},
                {"fact": "Recruiter said the round is 45 min design + 15 min your questions.",
                 "source_ids": ["S2"], "sensitive": False, "private": True},
                {"fact": "Posted base band: $180k-$215k; your stated expectation sits in range.",
                 "source_ids": ["S3"], "sensitive": True, "private": False},
                {"fact": "Contact the coordinator at priya@juniper.example if the link fails.",
                 "source_ids": [], "sensitive": False, "private": True},
            ],
            "sources": [{"id": "S1", "url": "https://engineering.juniper.example/blog/streaming-telemetry"},
                        {"id": "S2", "url": "https://mail.google.com/mail/u/0/#inbox/18f2c"},
                        {"id": "S3", "url": "https://levels.example/companies/juniper/salaries"}],
            "parts": {
                "private": _part("Two recruiter emails: a design round, then the hiring manager. They "
                                 "liked your Temporal work.", [
                                     ("Round is 45 minutes of design plus questions.",
                                      "https://mail.google.com/mail/u/0/#inbox/18f2c"),
                                     ("Your last Juniper call (Fathom) stalled on capacity estimates.",
                                      "https://fathom.video/calls/123")]),
                "round": _round(),
                "interviewers_by_email": {"dev.k@juniper.example,maya.lin@juniper.example": _part(
                    "Maya leads telemetry; Dev runs the data platform.", [
                        ("Maya Lin: 'Streaming telemetry at fleet scale' (2025).",
                         "https://engineering.juniper.example/blog/streaming-telemetry"),
                        ("Dev Kapoor spoke on Postgres partitioning at PGConf.",
                         "https://pgconf.example/talks/kapoor")])},
                "company": _company(),
            },
        },
        "ev-juniper-2": {"key": "ev-juniper-2", "company_key": "juniper", "status": "partial",
                        "updated_at": now - timedelta(minutes=18), "spoken": (
                            "Then at a quarter past, Sam Ortiz, the hiring manager. Little public about "
                            "Sam; expect motivation and team-fit questions."),
                        "notes": [], "sources": [],
                        "parts": {"interviewers_by_email": {}}},
    }
    jobs = {
        "ev-juniper-1": [{"event_key": "ev-juniper-1", "status": "done", "created_at": now - timedelta(minutes=24),
                         "duration_s": 212.0, "spend_usd": 0.27, "detail": "cache: company"}],
        "ev-juniper-2": [{"event_key": "ev-juniper-2", "status": "running", "created_at": now - timedelta(minutes=3),
                         "duration_s": None, "spend_usd": 0.04, "detail": ""}],
        "ev-nimbus": [],
    }
    recorded = {valr_view.group_blocks(events)[0].block_id: {"state": "waiting",
                                                             "label": "your interviews with Juniper"}}
    blocks = valr_view.build_blocks(events, recorded, slots, jobs, {}, {"me@example.com"}, now, paused)
    evaluation = {"generated_at": (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "brief": {
        "top_strengths": ["Clear event-driven architecture stories with real numbers",
                          "Calm, methodical debugging narration",
                          "Good questions about team process"],
        "recurring_weak_topics": ["Capacity estimates (QPS, storage) under time pressure",
                                  "Consistency models beyond 'eventual'",
                                  "Answering pushback on cost trade-offs"],
        "prep_checklist": ["Rehearse a 60-second capacity estimate", "Prepare two failure-mode stories",
                           "State the answer first, then the reasoning", "Have the comp range ready; defer the number"],
        "communication": "Answers improve when you lead with the conclusion.",
        "negotiation_notes": "Deferred well twice; once anchored early.",
        "trend": "Structure and clarity improved steadily since August; pushback handling dipped in the last two rounds.",
    }}
    jobs_recent = [
        {"job_id": "iv-1", "caller": "a1b2c3d4e5f6", "topic": "Juniper Robotics: Senior Backend, system design",
         "status": "done", "promised_minutes": 0, "created_at": now - timedelta(minutes=24), "duration_s": 212.0,
         "spend_usd": 0.27, "spoken": "", "notes": [], "sources": [], "cap_hits": {"searches": 2},
         "detail": "cache: company", "kind": "interview"},
        {"job_id": "cache-1", "caller": "a1b2c3d4e5f6", "topic": "Nimbus Pay: Staff Engineer", "status": "cached",
         "promised_minutes": 0, "created_at": now - timedelta(hours=5), "duration_s": 0, "spend_usd": 0,
         "spoken": "", "notes": [], "sources": [], "cap_hits": {}, "detail": "slot research reused (inputs unchanged)",
         "kind": "interview"},
        {"job_id": "j-1", "caller": "f6e5d4c3b2a1", "topic": "Best monsoon treks near Coorg", "status": "done",
         "promised_minutes": 4, "created_at": now - timedelta(days=1, hours=2), "duration_s": 171.0,
         "spend_usd": 0.12, "spoken": "Three treks stand out: Tadiandamol, Brahmagiri and Kotebetta.",
         "notes": [{"fact": "Brahmagiri needs a forest permit.", "source_ids": ["S1"], "sensitive": False}],
         "sources": [{"id": "S1", "url": "https://karnatakatourism.org/brahmagiri"}],
         "cap_hits": {"page_chars": 1}, "detail": "", "kind": "topic"},
    ]
    view = {
        "generated_at": valr_view.iso(now), "zone": "Asia/Kolkata", "errors": [],
        "spend": {"today_usd": 0.43, "month_usd": 11.82, "today_jobs": 4, "month_jobs": 61,
                  "today_cached": 1, "promise_minutes": 4,
                  "limits": valr_view.limits_view(research_jobs.Limits()),
                  "interview_limits": valr_view.limits_view(research_jobs.Limits(30, 30, 40.0, 0.40))},
        "jobs": [valr_view.job_view(job) for job in jobs_recent],
        "dials_paused": {"enabled": paused, "updated_at": None}, "dial_env": True,
        "blocks": blocks,
        "connections": {
            "google": {"state": "connected", "account": "owner@example.com", "age_days": 6.2,
                       "expires_soon": True, "last_ok_at": valr_view.iso(now), "configured": True},
            "fathom": {"configured": True},
            "calendar": valr_view.calendar_view({"channel_id": "c", "channel_expires": now + timedelta(days=5),
                                                 "source": "google", "last_sync_at": now - timedelta(minutes=7),
                                                 "last_error": None}, now),
        },
        "dials": {"briefing": {"used": 2, "limit": 12}, "callback": {"used": 1, "limit": 5}},
        "performance": valr_view.performance(None, evaluation, scorecards(now)),
        "pay_target": {"min": 50, "max": 65, "unit": "LPA", "currency": "INR"},
        "setup": {"phone_on_file": True, "test_call_done": True, "valr_number": "+12288888007"},
    }
    if scenario == "empty":
        view.update(blocks=[], jobs=[], pay_target=None,
                    performance=valr_view.performance(None, None, None),
                    setup={"phone_on_file": False, "test_call_done": False, "valr_number": "+12288888007"},
                    dials={"briefing": {"used": 0, "limit": 12}, "callback": {"used": 0, "limit": 5}})
        view["spend"].update(today_usd=0.0, month_usd=0.0, today_jobs=0, month_jobs=0, today_cached=0)
        view["connections"] = {
            "google": {"state": "not_connected", "account": "", "age_days": None, "expires_soon": False,
                       "last_ok_at": None, "configured": True},
            "fathom": {"configured": False},
            "calendar": valr_view.calendar_view({}, now)}
    elif scenario == "error":
        view.update(blocks=[], errors=["timeline"],
                    dials={"briefing": {"used": None, "limit": 12}, "callback": {"used": None, "limit": 5}})
        view["connections"]["google"]["state"] = "reconnect_needed"
        view["connections"]["calendar"]["last_error"] = "HTTP 401 from Google Calendar"
    view["next_call"] = valr_view.next_call(view["blocks"], now, paused)
    return view
