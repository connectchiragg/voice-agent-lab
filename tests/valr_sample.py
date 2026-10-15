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


def sample_view(now: datetime | None = None, paused: bool = False) -> dict:
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
                "round": _part("System design on telemetry ingestion is their standard senior round.", [
                    ("Glassdoor reports a 'design a metrics pipeline' prompt.",
                     "https://www.glassdoor.example/Interview/Juniper")]),
                "interviewers_by_email": {"dev.k@juniper.example,maya.lin@juniper.example": _part(
                    "Maya leads telemetry; Dev runs the data platform.", [
                        ("Maya Lin: 'Streaming telemetry at fleet scale' (2025).",
                         "https://engineering.juniper.example/blog/streaming-telemetry"),
                        ("Dev Kapoor spoke on Postgres partitioning at PGConf.",
                         "https://pgconf.example/talks/kapoor")])},
                "company": _part("Series C warehouse-robotics company, 400 people; raised $90M in May.", [
                    ("Series C, $90M, May 2026.", "https://news.example/juniper-series-c"),
                    ("Senior backend base $180k-$215k.", "https://levels.example/companies/juniper/salaries")]),
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
    dims = list(valr_view.DIMENSION_LABELS)
    base = {"technical_depth": 3, "problem_solving": 3, "communication_clarity": 2, "structure": 2,
            "confidence": 3, "handling_pushback": 3, "compensation_handling": 2}
    slope = {"technical_depth": 0.25, "problem_solving": 0.2, "communication_clarity": 0.35,
             "structure": 0.3, "confidence": 0.0, "handling_pushback": -0.2, "compensation_handling": 0.15}
    rows = []
    for i, company in enumerate(["Lumen", "Orbit", "Kite", "Juniper", "Vela", "Tessel", "Nimbus"]):
        day = (now - timedelta(days=70 - 10 * i)).date().isoformat()
        for dim in dims:
            score = max(1, min(5, round(base[dim] + slope[dim] * i + (0.4 if (i + len(dim)) % 3 == 0 else 0))))
            rows.append({"date": day, "company": company, "round_type": "technical", "dimension": dim,
                         "score": score})
    trends = {d: {"direction": "improving" if slope[d] >= 0.15 else "declining" if slope[d] < 0 else "flat",
                  "n": 7, "fitted_change": round(slope[d] * 6, 2), "last": {"score": rows[-7 + dims.index(d)]["score"]}}
              for d in dims}
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
        "performance": valr_view.performance({"rows": rows, "trends": trends}, evaluation),
    }
    view["next_call"] = valr_view.next_call(view["blocks"], now, paused)
    return view
