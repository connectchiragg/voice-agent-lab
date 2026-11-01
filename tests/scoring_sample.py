"""Made-up interviews for the rubric scorer's evals (scripts/eval_scoring.py) and tests.

Each pair asks the same questions; only the candidate's answers differ (weak vs strong), so
a scorer that tracks performance must rank the strong one higher. "long" hides the strong
system-design answers in the middle of a long call (small talk and process chat around
them): a scorer that drops the middle of a transcript misses them.
"""
CANDIDATE = "Sam Rivera"
INTERVIEWER = "Priya Nair"

SD_QUESTIONS = [
    "Let's design a system that records phone calls for a voice agent and makes them searchable. Where would you start?",
    "How do you get the audio from the call into storage without losing anything if a worker dies?",
    "Transcription is slow and costs money. How would you handle that at ten thousand calls a day?",
    "How would search work over the transcripts?",
    "What would break first as you grow 10x?",
]
SD_WEAK = [
    "Um, so, I guess I'd use, like, microservices. Basically a service for calls and a service for, you know, storage. And a database.",
    "We could just write it to S3. I mean, S3 is reliable, so it should be fine. Maybe a queue, I'm not sure.",
    "Uh, we'd use a transcription API. It's basically fine, they scale. Maybe cache it?",
    "Elasticsearch, I think. You put the text in and search it.",
    "Hmm, probably the database? You know, we'd add more servers. Kubernetes handles scaling.",
]
SD_STRONG = [
    "First the requirements: ten thousand calls a day, average six minutes, searchable within five minutes of hang-up, kept ninety days. That's about sixty thousand audio minutes a day, so roughly 30 GB of Opus audio. I'll walk the pipeline: capture, durable storage, transcription, indexing, search.",
    "The media server writes 10-second chunks to object storage as the call runs, each with a sequence number, and records a manifest row in Postgres. A worker that dies loses at most the chunk in flight; on restart it resumes from the manifest. At hang-up an idempotent job keyed by call id stitches the chunks. The trade-off is more small objects, which I'd compact daily.",
    "Batch transcription is about a third the cost of streaming, and the search SLA is five minutes, so I'd batch: a queue with per-tenant fairness, a worker pool sized for the peak hour, about 1,200 calls in the busiest hour. If the vendor rate-limits us we back off and the queue absorbs it. I'd track p95 time-to-searchable as the SLO.",
    "Postgres full-text search is enough to start: 3.6 million transcripts over ninety days, partitioned by month, a GIN index per partition. I'd only move to OpenSearch if we need fuzzy or semantic search; then I'd add embeddings for the semantic part. Every query is filtered by tenant first, for isolation and speed.",
    "At 10x the first thing to break is the stitching and transcription queue at peak, not storage. I'd watch queue age; the fix is autoscaling workers on queue depth. Second is index write amplification; monthly partitions and batching index updates handle that. I'd load-test the peak hour before it happens.",
]

BEH_QUESTIONS = [
    "Tell me about a time you disagreed with a teammate on a technical decision.",
    "Tell me about a project that failed. What did you learn?",
    "Why do you want to work here?",
]
BEH_WEAK = [
    "Yeah, so, there was this one time, um, we disagreed about a database. It was, like, a long discussion and eventually we kind of went with their idea. It was fine I guess.",
    "Hmm, I don't really have failures that come to mind. I mean, sometimes deadlines slip, but that's normal, you know, basically everyone has that.",
    "I've heard good things, and, you know, it seems like a cool company. I'm looking for a change.",
]
BEH_STRONG = [
    "At my last company my lead wanted to move our job queue to Kafka; I thought Postgres with SKIP LOCKED was enough for our 200 jobs a second. I wrote a one-page comparison and ran a load test at 5x peak: Postgres held p99 at 40 ms. We kept Postgres, saved about two months of migration, and agreed on the throughput level at which we'd revisit.",
    "I led a rewrite of our call-routing service that I estimated at six weeks; it took fourteen. I'd skipped a spike on the legacy edge cases. We shipped late but without an outage. Since then I time-box a spike on the riskiest unknown before any estimate, and on my next project we landed within ten percent.",
    "Three reasons. You're building voice agents for clinics, and I've spent two years on real-time voice pipelines. Your post on cutting turn latency to 600 ms is the problem I enjoy most. And at your stage one backend engineer owns whole systems, which is how I work best.",
]

COMP_QUESTIONS = [
    "Thanks for joining. Can you walk me through what you're working on now?",
    "Great. What are your salary expectations for this role?",
    "And what are you making currently?",
    "Are you interviewing anywhere else?",
]
COMP_LEAKY = [
    "I build backend services for a voice AI product: call routing and the agent's tools.",
    "Um, I don't know, maybe 25 lakhs? I'm flexible, whatever works for you.",
    "Right now I make 18 lakhs fixed. So anything above that is fine really.",
    "Not really, no. This is the main one.",
]
COMP_STRONG = [
    "I build backend services for a voice AI product: call routing and the agent's tools. Last quarter I cut our p95 tool latency from 900 to 300 ms.",
    "I'd like to understand the full scope first. Could you share the band you've budgeted for this level? I'm sure we can find a number that works if the role is the right fit.",
    "I'd rather not anchor on my current package; my focus is the market range for this role and the value I'd bring. What's the range for the level?",
    "Yes, I'm in later stages with two other companies, so timing matters to me, but this role is my first choice for the product.",
]

SMALL_TALK = [
    (INTERVIEWER, "Before we get going, how has your week been? Busy?"),
    (CANDIDATE, "Busy but good, thanks. We shipped a release on Tuesday so things are calmer now."),
    (INTERVIEWER, "Nice. Let me tell you a bit about the team. We're eight engineers, split between the voice runtime and the platform side. We do two-week sprints, a planning meeting on Mondays, and demos every other Friday. Most of us are remote, with an office we use once a month for planning."),
    (CANDIDATE, "That sounds good. How do on-call rotations work?"),
    (INTERVIEWER, "One week in eight, with a secondary. Pages are rare, maybe two a week, mostly vendor issues. We write a short review for anything customer-facing and go through them in the Friday demo slot."),
    (CANDIDATE, "Makes sense. And how do you decide what goes into a sprint?"),
    (INTERVIEWER, "The product lead brings priorities, engineers size them, and we keep about twenty percent for tech debt. We also have a rotating 'interrupt' person who handles support tickets so the rest can focus. Our tooling is GitHub, Linear, and a lot of Notion docs."),
    (CANDIDATE, "Okay. I like having the interrupt rotation, we did something similar."),
]


def turns(questions: list[str], answers: list[str], filler: int = 0, start: float = 0.0) -> list[dict]:
    """Interleaved turns with timestamps (about 2.5 words a second), `filler` rounds of small
    talk before and after the questions."""
    lines: list[tuple[str, str]] = []
    lines += SMALL_TALK * filler
    for q, a in zip(questions, answers):
        lines += [(INTERVIEWER, q), (CANDIDATE, a)]
    lines += SMALL_TALK * filler
    lines.append((INTERVIEWER, "That's all from me. Do you have any questions?"))
    lines.append((CANDIDATE, "Thanks, no, I think you covered it."))
    out, t = [], start
    for speaker, text in lines:
        out.append({"speaker": speaker, "text": text, "start": round(t, 1)})
        t += max(len(text.split()) / 2.5, 2.0)
    return out


def meeting(case_id: str, title: str, turn_list: list[dict], date: str = "2026-10-20T10:00:00Z") -> dict:
    return {"source": "eval", "id": case_id, "title": title, "date": date,
            "duration": int(turn_list[-1]["start"]) + 10, "turns": turn_list}


def cases() -> dict[str, dict]:
    """case id -> meeting. Pairs: sd_*, beh_*, comp_*; plus long_sd_strong."""
    return {
        "sd_weak": meeting("sd_weak", "Acme Voice - system design", turns(SD_QUESTIONS, SD_WEAK)),
        "sd_strong": meeting("sd_strong", "Acme Voice - system design", turns(SD_QUESTIONS, SD_STRONG)),
        "beh_weak": meeting("beh_weak", "Acme Voice - hiring manager", turns(BEH_QUESTIONS, BEH_WEAK)),
        "beh_strong": meeting("beh_strong", "Acme Voice - hiring manager", turns(BEH_QUESTIONS, BEH_STRONG)),
        "comp_leaky": meeting("comp_leaky", "Acme Voice - recruiter screen", turns(COMP_QUESTIONS, COMP_LEAKY)),
        "comp_strong": meeting("comp_strong", "Acme Voice - recruiter screen", turns(COMP_QUESTIONS, COMP_STRONG)),
        "long_sd_strong": meeting("long_sd_strong", "Acme Voice - system design",
                                  turns(SD_QUESTIONS, SD_STRONG, filler=14)),
    }


# What a scorer that tracks performance must get right. Pairs: strong minus weak, at least.
PAIRS = [("sd_strong", "sd_weak", "technical"), ("beh_strong", "beh_weak", "behavioural"),
         ("comp_strong", "comp_leaky", "negotiation")]
MIN_OVERALL_GAP = 2
MIN_DIMENSION_GAP = 2
