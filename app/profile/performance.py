"""Performance trends (schema v3; v2 plus impressions and the coach read): chart-ready series, badges, breakdowns, callouts.

Output file: VALR_PERFORMANCE_PATH (default data/performance.json). Schema:

{
  "schema_version": 3,                   # 3 adds interviews[].impression, "impressions",
                                         # "coach" (all optional for readers; v2 keys unchanged)
  "generated_at": "<ISO 8601 UTC>",
  "scale": {"min": 1, "max": 10, "na": null},
  "rubric": {"<dimension>": {"<subdimension>": "<what it measures>"}},
  "badge_rule": {"metric": "fitted_change", "threshold": 1.0, "min_points": 3,
                 "values": ["improving", "flat", "declining", "insufficient_data"]},
  "interviews": [{                       # sorted by date
      "interview_id": "<source>:<id>", "source", "id", "title", "date",
      "company", "company_stage", "round_type", "outcome", "outcome_evidence",
      "overall_score" (1-10, holistic), "overall_computed" (mean of dimension means),
      "dimension_scores": {"<dimension>": float|null},
      "scores": {"<dimension>": {"<subdimension>": {"score": int|null, "justification", "quote"}}},
      "do_next_time": [3 strings],
      "question_feedback": [{"question", "went_well", "improve", "model_answer_hint"}],
      "transcript_used": bool,
      "impression": IMPRESSION}],          # v3, app/profile/impressions.py
  "trends": {
      "overall": SERIES,
      "dimensions": {"<dimension>": SERIES},
      "subdimensions": {"<dimension>.<subdimension>": SERIES}},
  "by_round_type": {"<round_type>": BREAKDOWN},
  "by_company_stage": {"<stage>": BREAKDOWN},
  "callouts": {"most_improved": {"key", "fitted_change", "first", "last"} | null,
               "biggest_risk": {"key", "latest_ma3", "slope_per_30d", "badge"} | null},
  "recurring_weak_topics": [{"topic", "verdict": "improved|same|worse|mixed",
      "occurrences": [{"date", "interview_id", "status": "first_seen|improved|same|worse",
                       "note"}]}],
  "impressions": {...},                  # v3: impression vs outcome, app/profile/impressions.py
  "coach": COACH,                        # v3: the human summary, app/profile/coach.py
  "trend_text": "<= 150 tokens, plain language, also the Trend section of the evaluation brief"
}
SERIES    = {"points": [{"x": "YYYY-MM-DD", "interview_id", "company", "round_type",
                         "y": float, "ma3": float}],   # N/A interviews are omitted
             "n", "slope_per_30d", "fitted_change", "badge"}
BREAKDOWN = {"n", "overall": float|null, "dimensions": {"<dimension>": float|null}}

A long-format table of the same scores is written to VALR_PERFORMANCE_CSV
(default data/performance_scores.csv): date, interview_id, company, company_stage,
round_type, outcome, level (overall|dimension|subdimension), dimension,
subdimension, score, justification, quote.
"""
import csv
import io
import json
from datetime import datetime, timezone

from app.profile.evaluate import cap_tokens
from app.profile.llm import JsonLLM
from app.profile import coach, impressions
from app.profile.rubric import RUBRIC, SCHEMA_VERSION, mean, plain

BADGE_THRESHOLD = 1.0  # fitted points (on the 1-10 scale) across the window
MIN_POINTS = 3
TREND_TOKEN_LIMIT = 150


def _day(date: str) -> float:
    return datetime.fromisoformat(str(date).replace("Z", "+00:00")).timestamp() / 86400


def _x(date: str) -> str:
    return str(date)[:10]


def series(interviews: list[dict], value) -> dict:
    points = []
    for iv in interviews:
        y = value(iv)
        if y is None:
            continue
        points.append({"x": _x(iv["date"]), "interview_id": iv["interview_id"],
                       "company": iv.get("company"), "round_type": iv.get("round_type"),
                       "y": float(y)})
    for i, p in enumerate(points):
        window = [q["y"] for q in points[max(0, i - 2): i + 1]]
        p["ma3"] = round(sum(window) / len(window), 2)
    out = {"points": points, "n": len(points), "slope_per_30d": None, "fitted_change": None,
           "badge": "insufficient_data"}
    if len(points) >= 2:
        xs = [_day(p["x"]) for p in points]
        ys = [p["y"] for p in points]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var else 0.0
        out["slope_per_30d"] = round(slope * 30, 2)
        out["fitted_change"] = round(slope * (max(xs) - min(xs)), 2)
    if len(points) >= MIN_POINTS:
        change = out["fitted_change"]
        out["badge"] = ("improving" if change >= BADGE_THRESHOLD else
                        "declining" if change <= -BADGE_THRESHOLD else "flat")
    return out


def _breakdown(interviews: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for iv in interviews:
        groups.setdefault(iv.get(key) or "unknown", []).append(iv)
    return {name: {"n": len(ivs),
                   "overall": mean(iv["overall_score"] for iv in ivs),
                   "dimensions": {d: mean(iv["dimension_scores"][d] for iv in ivs)
                                  for d in RUBRIC}}
            for name, ivs in sorted(groups.items())}


def trends(interviews: list[dict]) -> dict:
    ivs = sorted(interviews, key=lambda iv: (iv["date"] or "", iv["interview_id"]))
    return {
        "overall": series(ivs, lambda iv: iv["overall_score"]),
        "dimensions": {d: series(ivs, lambda iv, d=d: iv["dimension_scores"][d]) for d in RUBRIC},
        "subdimensions": {f"{d}.{s}": series(ivs, lambda iv, d=d, s=s: iv["scores"][d][s]["score"])
                          for d, subs in RUBRIC.items() for s in subs},
    }


def callouts(trend: dict) -> dict:
    subs = {k: v for k, v in trend["subdimensions"].items() if v["n"] >= MIN_POINTS}
    improving = [(k, v) for k, v in subs.items() if v["badge"] == "improving"]
    most = max(improving, key=lambda kv: kv[1]["fitted_change"], default=None)
    risk = min(subs.items(), key=lambda kv: (kv[1]["points"][-1]["ma3"], kv[1]["slope_per_30d"]),
               default=None)
    return {
        "most_improved": None if most is None else {
            "key": most[0], "fitted_change": most[1]["fitted_change"],
            "first": most[1]["points"][0], "last": most[1]["points"][-1]},
        "biggest_risk": None if risk is None else {
            "key": risk[0], "latest_ma3": risk[1]["points"][-1]["ma3"],
            "slope_per_30d": risk[1]["slope_per_30d"], "badge": risk[1]["badge"]},
    }


TOPICS_SYSTEM = (
    "You track {candidate}'s recurring weak interview topics over time. Group the weak topics "
    "below into canonical topics (merge synonyms). Keep only topics that were weak at least "
    "once and came up in two or more interviews. For each occurrence in date order give a "
    "status: first_seen for the first, then improved / same / worse compared with the "
    "previous occurrence, with a one-line note. Verdict compares the last occurrence to the "
    "first. Use only the evidence given.")
_OCC = {"type": "object", "properties": {
    "date": {"type": "string"}, "interview_id": {"type": "string"},
    "status": {"type": "string", "enum": ["first_seen", "improved", "same", "worse"]},
    "note": {"type": "string"}},
    "required": ["date", "interview_id", "status", "note"], "additionalProperties": False}
TOPICS_SCHEMA = {"type": "object", "properties": {"topics": {"type": "array", "items": {
    "type": "object", "properties": {
        "topic": {"type": "string"},
        "verdict": {"type": "string", "enum": ["improved", "same", "worse", "mixed"]},
        "occurrences": {"type": "array", "items": _OCC}},
    "required": ["topic", "verdict", "occurrences"], "additionalProperties": False}}},
    "required": ["topics"], "additionalProperties": False}


def recurring_weak_topics(analyses: list[dict], scored: list[dict], llm: JsonLLM, model: str,
                          candidate: str) -> list[dict]:
    by_id = {s["interview_id"]: s for s in scored}
    evidence = []
    for a in sorted(analyses, key=lambda a: a.get("date") or ""):
        iid = f"{a.get('source')}:{a.get('id')}"
        s = by_id.get(iid, {})
        evidence.append({
            "date": _x(a.get("date")), "interview_id": iid, "company": a.get("company"),
            "weak": [{"topic": w["topic"], "why": w["why"]} for w in a.get("weak_answers") or []],
            "strong": [w["topic"] for w in a.get("strong_answers") or []],
            "topics": a.get("technical_topics") or [],
            "question_feedback": [{"q": q["question"], "improve": q["improve"]}
                                  for q in s.get("question_feedback") or []]})
    result = llm.json(model=model, system=TOPICS_SYSTEM.format(candidate=candidate),
                      user=json.dumps(evidence, ensure_ascii=False), schema=TOPICS_SCHEMA,
                      max_tokens=6000, effort="medium")
    return result["topics"]


def _label(key: str) -> str:
    return plain(key)


def _date(x: str) -> str:
    try:
        return datetime.fromisoformat(x).strftime("%d %b").lstrip("0")
    except ValueError:
        return x


DIRECTION = {"improving": "getting better", "flat": "holding steady", "declining": "slipping",
             "insufficient_data": "too early to tell"}


def trend_text(trend: dict, calls: dict, topics: list[dict]) -> str:
    """A plain-language trend line (no rubric names, few numbers); the coach headline
    replaces it when there is one (see `with_coach`)."""
    dims = trend["dimensions"]
    parts = []
    overall = trend["overall"]
    if overall["n"]:
        first, last = overall["points"][0], overall["points"][-1]
        parts.append(f"Across {overall['n']} interviews ({_date(first['x'])} to {_date(last['x'])}) "
                     f"your overall level is {DIRECTION[overall['badge']]}, around "
                     f"{round(last['ma3'])}/10 lately.")
    for badge, word in (("improving", "Getting better"), ("declining", "Slipping")):
        names = [_label(d) for d, v in dims.items() if v["badge"] == badge]
        if names:
            parts.append(f"{word}: {', '.join(names)}.")
    if calls.get("biggest_risk"):
        r = calls["biggest_risk"]
        parts.append(f"Weakest lately: {_label(r['key'])}.")
    if topics:
        repeat = [t for t in topics if t["verdict"] in ("same", "worse")][:2]
        if repeat:
            parts.append("Keeps coming up: " + "; ".join(t["topic"] for t in repeat) + ".")
    return cap_tokens(" ".join(parts), TREND_TOKEN_LIMIT)


def build(scored: list[dict], topics: list[dict]) -> dict:
    ivs = sorted(scored, key=lambda iv: (iv["date"] or "", iv["interview_id"]))
    trend = trends(ivs)
    calls = callouts(trend)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scale": {"min": 1, "max": 10, "na": None},
        "rubric": RUBRIC,
        "badge_rule": {"metric": "fitted_change", "threshold": BADGE_THRESHOLD,
                       "min_points": MIN_POINTS,
                       "values": ["improving", "flat", "declining", "insufficient_data"]},
        "interviews": ivs,
        "trends": trend,
        "by_round_type": _breakdown(ivs, "round_type"),
        "by_company_stage": _breakdown(ivs, "company_stage"),
        "callouts": calls,
        "recurring_weak_topics": topics,
        "impressions": impressions.summarize(ivs),
        "trend_text": trend_text(trend, calls, topics),
    }


def with_coach(perf: dict, llm: JsonLLM | None, model: str, candidate: str,
               comp: dict | None = None, previous: dict | None = None) -> dict:
    """Attach the coach read (cached by input hash; see app/profile/coach.py) and let its
    headline lead the trend text used by the voice brief."""
    read = coach.ensure(perf, llm, model, candidate, comp, previous=previous)
    perf = dict(perf)
    if read:
        perf["coach"] = read
        if read.get("headline"):
            first = (read.get("this_weeks_focus") or [{}])[0].get("drill", "")
            perf["trend_text"] = cap_tokens(
                f"{read['headline']}" + (f" This week: {first}." if first else ""), TREND_TOKEN_LIMIT)
    return perf


CSV_FIELDS = ["date", "interview_id", "company", "company_stage", "round_type", "outcome",
              "level", "dimension", "subdimension", "score", "justification", "quote"]


def to_csv(perf: dict) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for iv in perf["interviews"]:
        base = {k: iv.get(k) for k in ("interview_id", "company", "company_stage", "round_type",
                                       "outcome")} | {"date": _x(iv["date"])}
        writer.writerow(base | {"level": "overall", "score": iv["overall_score"]})
        for d in RUBRIC:
            writer.writerow(base | {"level": "dimension", "dimension": d,
                                    "score": iv["dimension_scores"][d]})
            for s, item in iv["scores"][d].items():
                writer.writerow(base | {"level": "subdimension", "dimension": d,
                                        "subdimension": s, "score": item["score"],
                                        "justification": item["justification"],
                                        "quote": item["quote"]})
    return buffer.getvalue()
