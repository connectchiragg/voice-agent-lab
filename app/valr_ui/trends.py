"""The Performance page's trend math and one-line takeaways (pure, no HTML).

Every takeaway is computed from the interviews with fixed rules, never written by hand:

- A trend looks at the last 5 interviews that have the number (TREND_WINDOW) and fits a
  straight line through them; its rise from the first to the last of them is the change.
- The change counts as "climbing" at +0.5 or more and "slipping" at -0.5 or less (scores
  out of 10, THRESHOLD); anything in between is "steady". Talk share and answer length use
  their own units (5 percentage points, 10 seconds).
- Fewer than 3 interviews: no trend; the page says it needs 3+ (MIN_POINTS).
- The talk balance is judged on the average of the last 3 interviews against the healthy
  band: 50-72% of the words, answers under 90 seconds.

The wording lives in TEMPLATES so a test can pin every sentence.
"""
from typing import Any, Callable

TREND_WINDOW = 5
MIN_POINTS = 3
THRESHOLD = 0.5
SPARK_POINTS = 8  # The skills sparklines show the last 8 interviews that tested the skill.
TALK_BAND = (0.50, 0.72)
ANSWER_LIMIT = 90  # seconds
SKILLS = ("technical", "communication", "behavioural", "process", "negotiation")
ROUND_GROUPS = {"screen": "Screen", "technical": "Technical", "system_design": "Technical",
                "hiring_manager": "Hiring manager", "founder": "Founder"}
ROUND_ORDER = ("Screen", "Technical", "Hiring manager", "Founder", "Other")

TEMPLATES = {
    "few": "Needs {need}+ interviews to show a trend.",
    "overall_up": "Your overall score is climbing: up {change} over your last {n} interviews.",
    "overall_down": "Your overall score is slipping: down {change} over your last {n} interviews.",
    "overall_flat": "Your overall score is holding steady at about {level} out of 10.",
    "skills_both": "{up} is climbing; {down} is slipping.",
    "skills_up": "{up} is climbing; the rest are holding steady.",
    "skills_down": "{down} is slipping; nothing is climbing yet.",
    "skills_flat": "All your skills are holding steady.",
    "react_up": "Interviewers are warming to you",
    "react_down": "Interviewers are cooling on you",
    "react_flat": "Interviewer reactions are steady",
    "warm_most": "most of their reactions were warm ({warm} of {total}).",
    "flat_most": "most of their reactions were flat ({flat} of {total}).",
    "warm_even": "warm and flat reactions are about even.",
    "talk_high": "You're talking a lot: {share}% of the words lately (aim for 50–72%)",
    "talk_low": "You're talking less than ideal: {share}% of the words lately (aim for 50–72%)",
    "talk_ok": "Your talk balance is healthy: {share}% of the words lately",
    "answers_long": "and answers run long, about {secs} seconds each.",
    "answers_ok": "and answers stay under 90 seconds.",
    "funnel": "{advanced} of {n} interviews moved you to the next round",
    "funnel_offers": "; {offers} offer{s} so far",
    "funnel_no_offers": "; no offers yet",
    "funnel_unknown": " No word yet on {unknown}.",
    "rounds_spread": "You score best in {best} rounds ({hi}) and lowest in {worst} rounds ({lo}).",
    "rounds_even": "Your scores are about the same across round types.",
}


def fitted_change(values: list[float]) -> float:
    """The rise of a straight line fitted through the values (evenly spaced), first to last."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x, mean_y = (n - 1) / 2, sum(values) / n
    var = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values)) / var
    return round(slope * (n - 1), 2)


def trend(values: list[float], threshold: float = THRESHOLD, window: int = TREND_WINDOW) -> dict[str, Any]:
    """{"dir": "up" | "down" | "flat" | None, "change", "n"} over the last `window` values."""
    recent = [v for v in values if v is not None][-window:]
    if len(recent) < MIN_POINTS:
        return {"dir": None, "change": None, "n": len(recent)}
    change = fitted_change(recent)
    direction = "up" if change >= threshold else "down" if change <= -threshold else "flat"
    return {"dir": direction, "change": change, "n": len(recent)}


def rolling(values: list[float], size: int = 3) -> list[float]:
    """The average of each value and the (up to) two before it."""
    return [round(sum(values[max(0, i - size + 1):i + 1]) / len(values[max(0, i - size + 1):i + 1]), 2)
            for i in range(len(values))]


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def round_group(key: str) -> str:
    return ROUND_GROUPS.get(str(key or ""), "Other")


def points(interviews: list[dict[str, Any]], value: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
    """The interviews (oldest first) that have this number: {date, company, round, y}."""
    out = []
    for iv in interviews:
        y = value(iv)
        if isinstance(y, (int, float)) and not isinstance(y, bool):
            out.append({"date": iv.get("date") or "", "company": iv.get("company") or "",
                        "round": round_group(iv.get("round_key")), "y": float(y)})
    return out


def _imp(key: str) -> Callable[[dict[str, Any]], Any]:
    return lambda iv: (iv.get("impression") or {}).get(key)


def _num(value: float) -> str:
    return f"{abs(value):.1f}"


# ---------- the six charts ----------

def overall(interviews: list[dict[str, Any]]) -> dict[str, Any]:
    pts = points(interviews, lambda iv: iv.get("overall"))
    ys = [p["y"] for p in pts]
    for p, avg in zip(pts, rolling(ys)):
        p["avg"] = avg
    t = trend(ys)
    if t["dir"] is None:
        text = TEMPLATES["few"].format(need=MIN_POINTS)
    elif t["dir"] == "flat":
        text = TEMPLATES["overall_flat"].format(level=_num(mean(ys[-TREND_WINDOW:])))
    else:
        text = TEMPLATES[f"overall_{t['dir']}"].format(change=_num(t["change"]), n=t["n"])
    return {"points": pts, "trend": t, "takeaway": text}


def skills(interviews: list[dict[str, Any]], labels: dict[str, str]) -> dict[str, Any]:
    rows = []
    for key in SKILLS:
        pts = points(interviews, lambda iv, k=key: (iv.get("dimensions") or {}).get(k))
        if not pts:
            continue
        rows.append({"key": key, "label": labels.get(key, key.capitalize()), "points": pts[-SPARK_POINTS:],
                     "latest": pts[-1]["y"], "trend": trend([p["y"] for p in pts])})
    return {"rows": rows, "takeaway": skills_takeaway(rows)}


def skills_takeaway(rows: list[dict[str, Any]]) -> str:
    known = [r for r in rows if r["trend"]["dir"] is not None]
    if not known:
        return TEMPLATES["few"].format(need=MIN_POINTS)
    ups = sorted((r for r in known if r["trend"]["dir"] == "up"), key=lambda r: -r["trend"]["change"])
    downs = sorted((r for r in known if r["trend"]["dir"] == "down"), key=lambda r: r["trend"]["change"])
    if ups and downs:
        return TEMPLATES["skills_both"].format(up=ups[0]["label"], down=downs[0]["label"])
    if ups:
        return TEMPLATES["skills_up"].format(up=ups[0]["label"])
    if downs:
        return TEMPLATES["skills_down"].format(down=downs[0]["label"])
    return TEMPLATES["skills_flat"]


def reactions(interviews: list[dict[str, Any]]) -> dict[str, Any]:
    pts = points(interviews, _imp("score"))
    t = trend([p["y"] for p in pts])
    recent = [iv["impression"] for iv in interviews if (iv.get("impression") or {}).get("warm") is not None
              and (iv.get("impression") or {}).get("flat") is not None][-TREND_WINDOW:]
    warm, flat = sum(i["warm"] for i in recent), sum(i["flat"] for i in recent)
    if t["dir"] is None:
        text = TEMPLATES["few"].format(need=MIN_POINTS).replace("interviews", "interviews with a transcript")
    else:
        text = TEMPLATES[f"react_{t['dir']}"]
        total = warm + flat
        if total:
            share = warm / total
            tail = (TEMPLATES["warm_most"] if share >= 0.6 else TEMPLATES["flat_most"] if share <= 0.4
                    else TEMPLATES["warm_even"]).format(warm=warm, flat=flat, total=total)
            text = f"{text}, and {tail}"
        else:
            text += "."
    return {"points": pts, "trend": t, "warm": warm, "flat": flat, "counted": len(recent), "takeaway": text}


def talk(interviews: list[dict[str, Any]]) -> dict[str, Any]:
    share = points(interviews, _imp("talk_share"))
    answers = points(interviews, _imp("answer_seconds"))
    recent_share = mean([p["y"] for p in share[-3:]])
    recent_secs = mean([p["y"] for p in answers[-3:]])
    if recent_share is None:
        return {"share": share, "answers": answers, "takeaway": "Needs an interview with a transcript."}
    pct = round(recent_share * 100)
    key = "talk_high" if recent_share > TALK_BAND[1] else "talk_low" if recent_share < TALK_BAND[0] else "talk_ok"
    text = TEMPLATES[key].format(share=pct)
    if recent_secs is None:
        text += "."
    else:
        text += ", " + (TEMPLATES["answers_long"].format(secs=round(recent_secs)) if recent_secs > ANSWER_LIMIT
                        else TEMPLATES["answers_ok"])
    return {"share": share, "answers": answers, "recent_share": recent_share, "recent_secs": recent_secs,
            "takeaway": text}


def funnel(interviews: list[dict[str, Any]], offers: int | None) -> dict[str, Any]:
    n = len(interviews)
    advanced = sum(1 for iv in interviews if iv.get("outcome") == "advanced")
    rejected = sum(1 for iv in interviews if iv.get("outcome") == "rejected")
    unknown = n - advanced - rejected
    steps = [("Interviews", n), ("Moved to the next round", advanced)]
    if offers is not None:
        steps.append(("Offers", offers))
    if not n:
        text = "Your first interview starts this chart."
    else:
        text = TEMPLATES["funnel"].format(advanced=advanced, n=n)
        if offers is not None:
            text += (TEMPLATES["funnel_offers"].format(offers=offers, s="" if offers == 1 else "s") if offers
                     else TEMPLATES["funnel_no_offers"])
        text += "." + (TEMPLATES["funnel_unknown"].format(unknown=unknown) if unknown else "")
    return {"steps": steps, "rejected": rejected, "unknown": unknown, "takeaway": text}


def by_round(interviews: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[float]] = {}
    for p in points(interviews, lambda iv: iv.get("overall")):
        groups.setdefault(p["round"], []).append(p["y"])
    rows = [{"round": r, "avg": round(mean(groups[r]), 1), "n": len(groups[r])} for r in ROUND_ORDER if r in groups]
    if len(rows) < 2:
        return {"rows": rows, "takeaway": ""}
    best = max(rows, key=lambda r: r["avg"])
    worst = min(rows, key=lambda r: r["avg"])
    text = (TEMPLATES["rounds_even"] if best["avg"] - worst["avg"] < THRESHOLD else
            TEMPLATES["rounds_spread"].format(best=best["round"].lower(), hi=f"{best['avg']:.1f}",
                                              worst=worst["round"].lower(), lo=f"{worst['avg']:.1f}"))
    return {"rows": rows, "takeaway": text}


def offers_from_leads(leads: dict[str, Any] | None) -> int | None:
    """Offers from the leads tracker (live leads at "offer", closed ones won); None without it."""
    if not leads or not leads.get("available"):
        return None
    return (sum(1 for r in leads.get("leads") or [] if r.get("stage") == "offer")
            + sum(1 for r in leads.get("closed") or [] if r.get("stage_raw") == "closed-won"))


def build(interviews: list[dict[str, Any]], labels: dict[str, str], leads: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"overall": overall(interviews), "skills": skills(interviews, labels), "reactions": reactions(interviews),
            "talk": talk(interviews), "funnel": funnel(interviews, offers_from_leads(leads)),
            "rounds": by_round(interviews)}
