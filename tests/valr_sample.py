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


RUBRIC = {  # Same keys as app/profile/rubric.py (valr-profile); descriptions shortened.
    "technical": {"system_design": "components, scaling, numbers", "coding_problem_solving": "clarifies, decomposes, tests",
                  "cs_fundamentals": "data structures, networking, databases", "domain_depth": "voice AI and agents in production",
                  "tradeoff_reasoning": "names options, compares, justifies"},
    "communication": {"conciseness_answer_first": "leads with the answer", "structure": "signposted, ordered answers",
                      "clarity": "easy to follow", "listening_clarifying": "answers the question asked",
                      "pace_fillers": "steady pace, few fillers"},
    "behavioural": {"star_stories": "situation-task-action-result", "ownership_impact": "owns decisions, numbers",
                    "motivation_why_us": "specific reasons", "pushback_curveballs": "composed under pushback"},
    "process": {"time_management": "paces the round", "questions_at_end": "sharp questions"},
    "negotiation": {"anchoring": "asks for the band first", "information_control": "keeps current pay private",
                    "competing_offers": "uses leverage without figures"},
}
BASE = {"technical": 6.0, "communication": 4.8, "behavioural": 6.2, "process": 5.5, "negotiation": 4.5}
SLOPE = {"technical": 0.35, "communication": 0.5, "behavioural": -0.25, "process": 0.1, "negotiation": 0.3}
ROUNDS = [("Lumen", "seed", "screen"), ("Orbit", "series_b", "system_design"), ("Kite", "series_a", "technical"),
          ("Juniper", "series_c", "hiring_manager"), ("Vela", "series_b", "system_design"),
          ("Tessel", "public", "technical"), ("Nimbus", "series_c", "system_design")]


def _series(interviews: list, value) -> dict:
    """The SERIES shape of app/profile/performance.py, computed the same way."""
    points = [{"x": iv["date"], "interview_id": iv["interview_id"], "company": iv["company"],
               "round_type": iv["round_type"], "y": float(value(iv))} for iv in interviews if value(iv) is not None]
    for i, point in enumerate(points):
        window = [q["y"] for q in points[max(0, i - 2):i + 1]]
        point["ma3"] = round(sum(window) / len(window), 2)
    out = {"points": points, "n": len(points), "slope_per_30d": None, "fitted_change": None,
           "badge": "insufficient_data"}
    if len(points) >= 2:
        xs = [datetime.fromisoformat(p["x"]).timestamp() / 86400 for p in points]
        ys = [p["y"] for p in points]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.0
        out["slope_per_30d"], out["fitted_change"] = round(slope * 30, 2), round(slope * (max(xs) - min(xs)), 2)
    if len(points) >= 3:
        c = out["fitted_change"]
        out["badge"] = "improving" if c >= 1 else "declining" if c <= -1 else "flat"
    return out


def _impression(i: int, round_type: str) -> dict:
    """A made-up interviewer impression (schema 3); the first interview has no transcript."""
    if i == 0:
        return {"coverage": "derived", "score": None, "components": {"balance": 8.0},
                "metrics": {"candidate_talk_share": 0.7}, "cues": [], "next_step": "none",
                "leaned_in": "", "disengaged": "", "source_hash": ""}
    warm = i % 2 == 1
    cues = [
        {"group": "engagement", "kind": "follow_up_probe", "polarity": "+", "strength": 2,
         "quote": "Interesting. How did you keep the queue from growing when the consumer fell behind?",
         "timestamp": "14:05", "after": "You sized the ingest at 100 MB/s before picking a queue.", "origin": "llm"},
        {"group": "engagement", "kind": "flat_acknowledgement", "polarity": "-", "strength": 1,
         "quote": "Okay. Let's move to the next one.", "timestamp": "31:40",
         "after": "A four-minute answer about a past migration with no result.", "origin": "llm"},
        {"group": "positive", "kind": "future_framing", "polarity": "+", "strength": 2,
         "quote": "You'd own the realtime pipeline from day one.", "timestamp": "48:12",
         "after": "You described the barge-in classifier you shipped.", "origin": "llm"},
        {"group": "next_step", "kind": "concrete_next_step", "polarity": "+", "strength": 3,
         "quote": "The recruiter will set up the system design round for next week.", "timestamp": "55:30",
         "after": "", "origin": "llm"},
    ] if warm else [
        {"group": "negative", "kind": "rushing", "polarity": "-", "strength": 2,
         "quote": "In the interest of time, let's skip ahead.", "timestamp": "22:10",
         "after": "A long preamble before the design.", "origin": "llm"},
        {"group": "negative", "kind": "brush_off_close", "polarity": "-", "strength": 2,
         "quote": "Thanks for your time, we'll get back to you.", "timestamp": "44:02", "after": "", "origin": "measured"},
    ]
    parts = ({"engagement": 7.4, "positive": 6.3, "negative": 1.5, "next_steps": 10.0, "balance": 9.0} if warm else
             {"engagement": 3.1, "positive": 0.0, "negative": 6.3, "next_steps": 1.0, "balance": 5.5})
    return {"coverage": "transcript", "score": 8.1 if warm else 3.0, "components": parts,
            "metrics": {"candidate_talk_share": 0.62 if warm else 0.84, "follow_ups": 5 if warm else 0,
                        "length_vs_slot": 1.0 if warm else 0.7},
            "cues": cues, "next_step": "concrete" if warm else "brush_off",
            "leaned_in": "They dug in when you put numbers on the design." if warm else "",
            "disengaged": "" if warm else "They moved on after a long preamble.", "source_hash": f"h{i}"}


def _impressions(interviews: list) -> dict:
    return {"n_scored": len(interviews) - 1, "n_derived_only": 1,
            "by_outcome": {"advanced": {"n": 3, "mean": 7.2}, "rejected": {"n": 2, "mean": 3.4},
                           "unknown": {"n": 1, "mean": 5.0}},
            "r_with_rubric_overall": 0.41, "point_biserial_advanced": 0.78,
            "note": "Correlation of impression with advancing: r=0.78 (n=5)."}


def _coach(interviews: list) -> dict:
    last = interviews[-1]
    quote = lambda text, speaker="you", at="": {"id": "Q1", "quote": text, "date": last["date"],  # noqa: E731
                                                "company": last["company"], "speaker": speaker, "timestamp": at,
                                                "context": ""}
    return {
        "input_hash": "sample", "model": "claude-sonnet-5-5", "generated_at": "2026-10-15T08:00:00Z",
        "where_you_stand": ("You are at the bar for senior platform roles and moving up: your system design answers "
                            "now start with numbers. What still costs you offers is folding under pushback."),
        "whats_working": [{"headline": "You size the problem first",
                           "detail": "Interviewers ask sharper follow-ups once you put numbers on the table.",
                           "example": quote("Ten thousand robots at fifty hertz is about a hundred megabytes a second.")}],
        "whats_costing_you": [
            {"headline": "You give ground too fast when challenged",
             "plain_meaning": "Holding your design when an interviewer pushes back, or changing it for a reason.",
             "why_it_matters": "Interviewers push to see judgement. Folding in a minute reads as guessing.",
             "example": quote("Yeah, you're right, we can just drop the queue then."),
             "what_happened": "The interviewer questioned the queue; you removed it without saying why.",
             "do_instead": "Restate their concern, then keep or change the design with one reason.",
             "model_rephrase": "Good point on cost. I'd still keep the queue: it absorbs bursts. If cost matters more, we batch writes."},
            {"headline": "Your salary answer comes too early",
             "plain_meaning": "Revealing your current or expected salary before they share their range.",
             "why_it_matters": "The first number sets the range, and theirs is usually higher.",
             "example": quote("Currently I'm at [amount], so I'd expect something above that."),
             "what_happened": "The recruiter asked once and you answered with your current pay.",
             "do_instead": "Ask for their band first, then answer with a range tied to scope.",
             "model_rephrase": "I'd love to hear the band you've set for this level first."}],
        "this_weeks_focus": [
            {"drill": "Pushback reps", "how": "Ask a friend to challenge three of your designs.",
             "done_when": "You restate and answer with a reason every time."},
            {"drill": "Salary script out loud", "how": "Say the band-first line ten times.", "done_when": "It feels natural."},
            {"drill": "60-second answers", "how": "Record five answers with a timer.", "done_when": "All under 60 seconds."}],
        "negotiation": {"read": "You stay friendly but give the first number.",
                        "scripts": [{"moment": "Asked for current salary",
                                     "say": "I'd rather focus on this role. What band have you set for it?"},
                                    {"moment": "A low offer",
                                     "say": "Thank you. That's below [your target]; given the scope, can we get closer?"}]},
        "interviewers": {"leaned_in_when": "Interviewers leaned in when you put numbers on a design.",
                         "disengaged_when": "They disengaged after long preambles.",
                         "examples": [quote("Interesting. How did you keep the queue from growing?", "interviewer", "14:05")]},
        "headline": "At the bar and rising; hold your ground under pushback and ask for the band first.",
        "numbers": {}}


def performance_file(now: datetime) -> dict:
    """A made-up performance.json (schema 3), the exact shape the profile importer writes."""
    interviews = []
    for i, (company, stage, round_type) in enumerate(ROUNDS):
        scores = {}
        for d, subs in RUBRIC.items():
            scores[d] = {}
            for j, sub in enumerate(subs):
                tested = not (d == "negotiation" and round_type not in ("hiring_manager", "screen", "founder")) \
                    and not (sub == "coding_problem_solving" and round_type == "hiring_manager")
                value = max(1, min(10, round(BASE[d] + SLOPE[d] * i + ((i + j) % 3 - 1) * 0.6))) if tested else None
                scores[d][sub] = {"score": value, "justification": "Fictional sample justification.",
                                  "quote": "a short made-up quote" if value else ""}
        dims = {d: (round(sum(v["score"] for v in subs.values() if v["score"]) /
                          len([v for v in subs.values() if v["score"]]), 2)
                    if any(v["score"] for v in subs.values()) else None) for d, subs in scores.items()}
        known = [v for v in dims.values() if v is not None]
        interviews.append({
            "interview_id": f"fathom:{1000 + i}", "source": "fathom", "id": str(1000 + i),
            "title": f"{company} interview", "date": (now - timedelta(days=70 - 10 * i)).date().isoformat(),
            "company": company, "company_stage": stage, "round_type": round_type,
            "outcome": ["advanced", "rejected", "advanced", "unknown", "advanced", "rejected", "unknown"][i],
            "outcome_evidence": "Fictional: recruiter email about next steps.",
            "overall_score": round(sum(known) / len(known) + 0.2, 1), "overall_computed": round(sum(known) / len(known), 2),
            "dimension_scores": dims, "scores": scores,
            "do_next_time": ["Size the system out loud in the first five minutes",
                             "When challenged, restate their concern before answering",
                             "Ask for their band before giving a number"] if i == 6 else
                            ["Lead with the answer, then the reasoning", "Close stories with a number",
                             "Ask one question about how the team measures success"],
            "question_feedback": [
                {"question": "Design ingestion for telemetry from 10,000 robots", "went_well": "Clear components and data flow.",
                 "improve": "Sized storage only when asked.", "model_answer_hint": "Start with 10k x 50 Hz x 200 B = 100 MB/s, then pick Kafka."},
                {"question": "Tell me about a time you disagreed with your manager", "went_well": "Concrete situation.",
                 "improve": "No result; conceded quickly.", "model_answer_hint": "End with the outcome and what you would hold firm on."},
                {"question": "What are your salary expectations?", "went_well": "Stayed calm and friendly.",
                 "improve": "Gave a number before hearing their band." if i < 3 else "Good deferral; could tie the range to scope.",
                 "model_answer_hint": "Ask for the band first; tie your range to the role's scope."},
            ][: 2 + i % 2],
            "transcript_used": True,
            "impression": _impression(i, round_type)})
    trends = {"overall": _series(interviews, lambda iv: iv["overall_score"]),
              "dimensions": {d: _series(interviews, lambda iv, d=d: iv["dimension_scores"][d]) for d in RUBRIC},
              "subdimensions": {f"{d}.{s}": _series(interviews, lambda iv, d=d, s=s: iv["scores"][d][s]["score"])
                                for d, subs in RUBRIC.items() for s in subs}}
    return {"schema_version": 3, "coach": _coach(interviews), "impressions": _impressions(interviews),
            "generated_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scale": {"min": 1, "max": 10, "na": None}, "rubric": RUBRIC,
            "badge_rule": {"metric": "fitted_change", "threshold": 1.0, "min_points": 3,
                           "values": ["improving", "flat", "declining", "insufficient_data"]},
            "interviews": interviews, "trends": trends,
            "by_round_type": {r: {"n": sum(1 for iv in interviews if iv["round_type"] == r),
                                  "overall": round(sum(iv["overall_score"] for iv in interviews if iv["round_type"] == r)
                                                   / sum(1 for iv in interviews if iv["round_type"] == r), 2),
                                  "dimensions": {}} for r in sorted({iv["round_type"] for iv in interviews})},
            "by_company_stage": {},
            "callouts": {"most_improved": {"key": "communication.conciseness_answer_first", "fitted_change": 2.6,
                                           "first": 4.0, "last": 7.0},
                         "biggest_risk": {"key": "behavioural.pushback_curveballs", "latest_ma3": 4.3,
                                          "slope_per_30d": -0.6, "badge": "declining"}},
            "recurring_weak_topics": [
                {"topic": "Capacity estimates under time pressure", "verdict": "improved",
                 "occurrences": [{"date": "x", "interview_id": "fathom:1001", "status": "first_seen", "note": ""},
                                 {"date": "y", "interview_id": "fathom:1006", "status": "improved",
                                  "note": "Sized it unprompted at Nimbus."}]},
                {"topic": "Holding a position under pushback", "verdict": "worse",
                 "occurrences": [{"date": "x", "interview_id": "fathom:1003", "status": "first_seen", "note": ""},
                                 {"date": "y", "interview_id": "fathom:1005", "status": "worse",
                                  "note": "Conceded on consistency within a minute."}]},
                {"topic": "Giving a number before hearing the band", "verdict": "mixed",
                 "occurrences": [{"date": "x", "interview_id": "fathom:1000", "status": "first_seen", "note": ""}]}],
            "trend_text": ("Technical answers and answer-first communication improved steadily since August; "
                           "pushback handling slipped over the last three rounds.")}


def notetakers_view(states: dict) -> dict:
    """The dashboard's notetaker status (app/notetakers.status_view's shape), no key anywhere."""
    from app import notetakers
    return {name: {"label": info["label"], "state": states.get(name, "not_connected"),
                   "account": "", "connected_at": None, "last_ok_at": None, "can_store": True,
                   "optional": bool(info.get("optional")), "share_hint": info.get("share_hint", ""),
                   "share_help": info.get("share_help", ""),
                   **{k: info[k] for k in ("key_label", "key_help", "gets", "video", "import")}}
            for name, info in notetakers.INFO.items()}


def sample_recording(found: bool = True) -> dict:
    """A made-up /valr/api/recording answer: a Fathom embed and key moments."""
    if not found:
        return {"ok": True, "found": False}
    share = "https://fathom.video/calls/424242"
    moments = [(95, "Walk me through how you'd ingest telemetry from 10,000 robots?"),
               (610, "How would you backfill a day of data without double counting?"),
               (1490, "Tell me about a time you pushed back on a deadline?"),
               (2710, "What questions do you have for us?")]
    return {"ok": True, "found": True, "provider": "fathom", "label": "Fathom",
            "title": "Orbit Labs: Backend Engineer, technical round", "start": None,
            "share_url": share, "embed_url": "https://fathom.video/embed/sampletoken123",
            "moments": [{"label": label, "seconds": t, "clock": f"{t // 60}:{t % 60:02d}",
                         "url": f"{share}?timestamp={t}"} for t, label in moments], "note": ""}


def sample_view(now: datetime | None = None, paused: bool = False, scenario: str = "full",
                extras: bool = False) -> dict:
    """scenario: full (a busy week), empty (first run: nothing connected, no data) or error
    (the calendar store and Temporal are down). extras: add the other branches' data
    (coach, impression cues, recordings, notetakers; tests/valr_sample_extras.py)."""
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    first = now + timedelta(hours=2, minutes=40)
    tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=30)
    events = [
        _event("ev-juniper-1", "Juniper Robotics: Senior Backend, system design", "Juniper", first, 60, [
            Person("me@example.com", "Me", is_self=True),
            Person("maya.lin@juniper.example", "Maya Lin", "accepted"),
            Person("dev.k@juniper.example", "Dev Kapoor", "accepted"),
            Person("talent@juniper.example", "Priya (Talent Acquisition)", organizer=True)]),
        _event("ev-juniper-2", "Juniper Robotics: hiring manager chat", "Juniper", first + timedelta(minutes=75), 45, [
            Person("sam.ortiz@juniper.example", "Sam Ortiz", "needsAction")]),
        _event("ev-nimbus", "Nimbus Pay: Staff Engineer, coding round (call +1 415 555 0134)", "Nimbus",
               tomorrow, 60, [Person("lee@nimbus.example", "Lee Park", "accepted"),
                              Person("noreply@greenhouse.io", "Greenhouse")]),
        _event("ev-orbit", "Orbit Labs: Backend Engineer, technical round", "Orbit", now - timedelta(hours=5), 60, [
            Person("me@example.com", "Chirag", is_self=True),
            Person("ana.ruiz@orbit.example", "Ana Ruiz", "accepted")]),
    ]
    slots = {
        "ev-orbit": {"key": "ev-orbit", "company_key": "orbit", "status": "done",
                     "updated_at": now - timedelta(hours=6), "spoken": "Orbit Labs at 10: a technical round with Ana Ruiz.",
                     "notes": [], "sources": [], "parts": {"interviewers_by_email": {}}},
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
    plans = valr_view.group_blocks(events)
    recorded = {next(b.block_id for b in plans if "ev-juniper-1" in b.slot_keys): {
                    "state": "waiting", "label": "your interviews with Juniper"},
                next(b.block_id for b in plans if "ev-orbit" in b.slot_keys): {
                    "state": "briefed", "label": "your interview with Orbit"}}
    blocks = valr_view.build_blocks(events, recorded, slots, jobs, {}, {"me@example.com"}, now, paused)
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
            "notetakers": notetakers_view({"fathom": "connected", "fireflies": "not_connected",
                                           "granola": "not_connected"}),
            "calendar": valr_view.calendar_view({"channel_id": "c", "channel_expires": now + timedelta(days=5),
                                                 "source": "google", "last_sync_at": now - timedelta(minutes=7),
                                                 "last_error": None}, now),
        },
        "dials": {"briefing": {"used": 2, "limit": 12}, "callback": {"used": 1, "limit": 5}},
        "performance": valr_view.performance(_extras(performance_file(now)) if extras else performance_file(now)),
        "post_call": {"pending": 1, "queued": 0, "failed": 0, "recent": [
            {"title": "Nimbus · system design", "status": "pending_telegram",
             "updated_at": (now - timedelta(hours=3)).isoformat()},
            {"title": "Tessel · technical", "status": "sent", "updated_at": (now - timedelta(days=4)).isoformat()}]},
        "pay_target": {"min": 50, "max": 65, "unit": "LPA", "currency": "INR"},
        "setup": {"phone_on_file": True, "test_call_done": True, "valr_number": "+12288888007"},
    }
    if scenario == "empty":
        view.update(blocks=[], jobs=[], pay_target=None,
                    performance=valr_view.performance(None),
                    setup={"phone_on_file": False, "test_call_done": False, "valr_number": "+12288888007"},
                    dials={"briefing": {"used": 0, "limit": 12}, "callback": {"used": 0, "limit": 5}})
        view["spend"].update(today_usd=0.0, month_usd=0.0, today_jobs=0, month_jobs=0, today_cached=0)
        view["connections"] = {
            "google": {"state": "not_connected", "account": "", "age_days": None, "expires_soon": False,
                       "last_ok_at": None, "configured": True},
            "fathom": {"configured": False},
            "notetakers": notetakers_view({}),
            "calendar": valr_view.calendar_view({}, now)}
    elif scenario == "error":
        view.update(blocks=[], errors=["timeline"],
                    dials={"briefing": {"used": None, "limit": 12}, "callback": {"used": None, "limit": 5}})
        view["connections"]["google"]["state"] = "reconnect_needed"
        view["connections"]["calendar"]["last_error"] = "HTTP 401 from Google Calendar"
    if extras and scenario == "full":
        from tests.valr_sample_extras import NOTETAKERS
        view["connections"]["notetakers"] = NOTETAKERS
        view["connections"]["telegram"] = {"configured": True, "linked": True, "username": "sample_owner",
                                           "linked_at": valr_view.iso(now - timedelta(days=2))}
    view["next_call"] = valr_view.next_call(view["blocks"], now, paused)
    return view


def _extras(perf: dict) -> dict:
    from tests.valr_sample_extras import with_extras
    return with_extras(perf)
