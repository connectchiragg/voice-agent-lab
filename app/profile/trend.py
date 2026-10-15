"""Performance trend across interviews: dated 1-5 scores per dimension -> direction per dimension.

Scores come from the evaluator (one fixed rubric, see RUBRIC), so they are
comparable across interviews. The direction is computed here, not by the model:
a least-squares line over (days, score); its change across the window decides
improving / flat / declining.
"""
import csv
import io
import json
from datetime import datetime

DIMENSIONS = ["technical_depth", "problem_solving", "communication_clarity", "structure",
              "confidence", "handling_pushback", "compensation_handling"]
CHANGE_THRESHOLD = 0.5  # fitted points of change across the window
TREND_TOKEN_LIMIT = 150

RUBRIC = """Score each dimension 1-5 using these anchors (same rubric for every interview):
- technical_depth: 1 vague/buzzwords; 3 correct, some depth, misses trade-offs; 5 precise, trade-offs, numbers, failure modes.
- problem_solving: 1 stuck or jumps to answer; 3 reaches a workable solution with hints; 5 clarifies, decomposes, compares options, verifies.
- communication_clarity: 1 hard to follow; 3 clear with some rambling or filler; 5 concise, direct answer first, easy to follow.
- structure: 1 no structure; 3 partial (e.g. STAR without result); 5 consistently structured (context, action, result, impact).
- confidence: 1 hesitant, over-hedges; 3 steady with some hedging; 5 calm, owns decisions, admits gaps cleanly.
- handling_pushback: 1 defensive or folds; 3 engages but loses thread; 5 acknowledges, reasons, holds or updates with evidence. Null if no pushback occurred.
- compensation_handling: 1 discloses/anchors low or unprepared; 3 reasonable but reactive; 5 defers or anchors deliberately, asks for range, ties to value. Null if compensation never came up.
Give each score a one-line justification grounded in the transcript. Use null (not a guess) when a dimension had no evidence."""

_SCORE = {"type": "object",
          "properties": {"score": {"type": ["integer", "null"]},
                         "justification": {"type": "string"}},
          "required": ["score", "justification"], "additionalProperties": False}
SCORES_SCHEMA = {"type": "object", "properties": {d: _SCORE for d in DIMENSIONS},
                 "required": DIMENSIONS, "additionalProperties": False}


def _day(value: str) -> float:
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return when.timestamp() / 86400


def clamp_scores(scores: dict) -> dict:
    out = {}
    for dim in DIMENSIONS:
        item = scores.get(dim) or {}
        score = item.get("score")
        out[dim] = {"score": min(5, max(1, int(score))) if score is not None else None,
                    "justification": item.get("justification", "")}
    return out


def score_rows(analyses: list[dict]) -> list[dict]:
    """Dated long-format table: one row per interview x dimension (for a dashboard chart)."""
    rows = []
    for a in sorted(analyses, key=lambda a: a.get("date") or ""):
        for dim in DIMENSIONS:
            s = (a.get("scores") or {}).get(dim) or {}
            rows.append({"date": (a.get("date") or "")[:10], "company": a.get("company"),
                         "round_type": a.get("round_type"), "interview_id":
                         f"{a.get('source')}:{a.get('id')}", "dimension": dim,
                         "score": s.get("score"), "justification": s.get("justification", "")})
    return rows


def fit(points: list[tuple[float, float]]) -> float:
    """Fitted change (score points) from first to last date; 0 if all on one day."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    var = sum((x - mean_x) ** 2 for x, _ in points)
    if var == 0:
        return 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / var
    return slope * (max(x for x, _ in points) - min(x for x, _ in points))


def trends(rows: list[dict]) -> dict:
    out = {}
    for dim in DIMENSIONS:
        pts = [(r["date"], r["score"]) for r in rows
               if r["dimension"] == dim and r["score"] is not None and r["date"]]
        if len(pts) < 2:
            out[dim] = {"direction": "insufficient data", "n": len(pts)}
            continue
        change = fit([(_day(d), float(s)) for d, s in pts])
        direction = ("improving" if change >= CHANGE_THRESHOLD else
                     "declining" if change <= -CHANGE_THRESHOLD else "flat")
        low = min(pts, key=lambda p: (p[1], p[0]))
        high = max(pts, key=lambda p: (p[1], p[0]))
        out[dim] = {"direction": direction, "n": len(pts), "fitted_change": round(change, 2),
                    "first": {"date": pts[0][0], "score": pts[0][1]},
                    "last": {"date": pts[-1][0], "score": pts[-1][1]},
                    "low": {"date": low[0], "score": low[1]},
                    "high": {"date": high[0], "score": high[1]}}
    return out


def topics_over_time(analyses: list[dict]) -> dict:
    """Topic -> dates it came up (technical topics) and dates it was a weak spot."""
    seen: dict[str, dict] = {}
    for a in sorted(analyses, key=lambda a: a.get("date") or ""):
        day = (a.get("date") or "")[:10]
        for topic in a.get("technical_topics") or []:
            seen.setdefault(topic.lower(), {"came_up": [], "weak": []})["came_up"].append(day)
        for weak in a.get("weak_answers") or []:
            seen.setdefault(weak["topic"].lower(), {"came_up": [], "weak": []})["weak"].append(day)
    return {t: v for t, v in seen.items() if len(v["came_up"]) + len(v["weak"]) >= 2}


def to_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["date", "company", "round_type", "interview_id",
                                                "dimension", "score", "justification"])
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def table_json(rows: list[dict], trend: dict) -> str:
    return json.dumps({"rows": rows, "trends": trend}, indent=2, ensure_ascii=False)
