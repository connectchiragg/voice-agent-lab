"""The Performance page's trend infographics: small inline SVG drawn on the server.

No chart library and no JavaScript: each chart is a <figure> with its name, a one-line
takeaway (app/valr_ui/trends.py), the picture (role="img", labelled with the takeaway),
native hover titles on every dot, and a visually hidden table of the same numbers for
screen readers. Colours come from the theme's CSS variables (static/valr-perf.css), so the
charts follow the light and dark themes; the SVG scales to the card's width.
"""
from typing import Any

from app.valr_page import e, short_day
from app.valr_ui import trends

W = 360
PAD_L, PAD_R, PAD_T, PAD_B = 30, 12, 10, 22
SHAPES = {"Screen": "circle", "Technical": "square", "Hiring manager": "diamond", "Founder": "triangle", "Other": "ring"}


def x_at(i: int, n: int) -> float:
    if n <= 1:
        return (PAD_L + W - PAD_R) / 2
    return PAD_L + i * (W - PAD_L - PAD_R) / (n - 1)


def y_at(value: float, lo: float, hi: float, height: int) -> float:
    span = (hi - lo) or 1
    return PAD_T + (hi - value) / span * (height - PAD_T - PAD_B)


def marker(shape: str, cx: float, cy: float, cls: str = "dot") -> str:
    r = 4.5
    if shape == "square":
        return f'<rect class="{cls}" x="{cx - r:.1f}" y="{cy - r:.1f}" width="{2 * r}" height="{2 * r}"/>'
    if shape == "diamond":
        d = r * 1.3
        return f'<path class="{cls}" d="M{cx:.1f},{cy - d:.1f}L{cx + d:.1f},{cy:.1f}L{cx:.1f},{cy + d:.1f}L{cx - d:.1f},{cy:.1f}Z"/>'
    if shape == "triangle":
        d = r * 1.25
        return f'<path class="{cls}" d="M{cx:.1f},{cy - d:.1f}L{cx + d:.1f},{cy + d * 0.8:.1f}L{cx - d:.1f},{cy + d * 0.8:.1f}Z"/>'
    if shape == "ring":
        return f'<circle class="{cls} ring" cx="{cx:.1f}" cy="{cy:.1f}" r="{r - 0.5}"/>'
    return f'<circle class="{cls}" cx="{cx:.1f}" cy="{cy:.1f}" r="{r}"/>'


def path(coords: list[tuple[float, float]]) -> str:
    return "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in coords)


def svg(name: str, takeaway: str, height: int, inner: str) -> str:
    return (f'<svg class="tc-svg" viewBox="0 0 {W} {height}" role="img" aria-label="{e(name)}: {e(takeaway)}">'
            f'<title>{e(name)}</title>{inner}</svg>')


def grid(lo: float, hi: float, height: int, ticks: list[tuple[float, str]]) -> str:
    out = []
    for value, text in ticks:
        y = y_at(value, lo, hi, height)
        out.append(f'<line class="tc-grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text class="tc-axis" x="{PAD_L - 6}" y="{y + 3:.1f}" text-anchor="end">{e(text)}</text>')
    return "".join(out)


def dates(pts: list[dict[str, Any]], height: int) -> str:
    if not pts:
        return ""
    first, last = short_day(pts[0]["date"]), short_day(pts[-1]["date"])
    y = height - 6
    out = f'<text class="tc-axis" x="{PAD_L}" y="{y}" text-anchor="start">{e(first)}</text>'
    if len(pts) > 1:
        out += f'<text class="tc-axis" x="{W - PAD_R}" y="{y}" text-anchor="end">{e(last)}</text>'
    return out


def tip(p: dict[str, Any], value: str) -> str:
    bits = [short_day(p["date"]) if p.get("date") else "", p.get("company") or "", p.get("round") or "", value]
    return " · ".join(b for b in bits if b)


def table(caption: str, head: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    th = "".join(f'<th scope="col">{e(h)}</th>' for h in head)
    body = "".join("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="sr-only"><caption>{e(caption)}</caption><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def figure(key: str, name: str, takeaway: str, body: str, data: str = "", wide: bool = False) -> str:
    return (f'<figure class="tc{" tc-wide" if wide else ""}" id="chart-{e(key)}" aria-labelledby="tc-{e(key)}-name tc-{e(key)}-take">'
            f'<figcaption><p class="label" id="tc-{e(key)}-name">{e(name)}</p>'
            f'<p class="tc-take" id="tc-{e(key)}-take">{e(takeaway)}</p></figcaption>{body}{data}</figure>')


def empty(text: str) -> str:
    return f'<div class="tc-empty"><p class="muted small">{e(text)}</p></div>'


def few_note(n: int) -> str:
    return "" if n >= trends.MIN_POINTS else f'<p class="tc-note small muted">Needs {trends.MIN_POINTS}+ interviews to show a trend.</p>'


# ---------- the charts ----------

def overall_chart(data: dict[str, Any]) -> str:
    name, pts = "Overall score", data["points"]
    if not pts:
        return figure("overall", name, data["takeaway"], empty("Your scores appear after your first reviewed interview."))
    h = 160
    n = len(pts)
    inner = grid(0, 10, h, [(0, "0"), (5, "5"), (10, "10")])
    if n >= trends.MIN_POINTS:
        inner += f'<path class="tc-avg" d="{path([(x_at(i, n), y_at(p["avg"], 0, 10, h)) for i, p in enumerate(pts)])}"/>'
    for i, p in enumerate(pts):
        inner += (f'<g class="tc-pt"><title>{e(tip(p, f"{p["y"]:.1f} out of 10"))}</title>'
                  f'{marker(SHAPES[p["round"]], x_at(i, n), y_at(p["y"], 0, 10, h))}</g>')
    inner += dates(pts, h)
    used = [r for r in trends.ROUND_ORDER if any(p["round"] == r for p in pts)]
    legend = "".join(f'<li><svg viewBox="0 0 12 12" aria-hidden="true">{marker(SHAPES[r], 6, 6)}</svg>{e(r)}</li>' for r in used)
    legend += ('<li><svg viewBox="0 0 16 12" aria-hidden="true"><path class="tc-avg" d="M1,6L15,6"/></svg>Average of 3</li>'
               if n >= trends.MIN_POINTS else "")
    body = svg(name, data["takeaway"], h, inner) + f'<ul class="tc-legend small">{legend}</ul>' + few_note(n)
    rows = [[p["date"], p["company"], p["round"], f'{p["y"]:.1f}', f'{p["avg"]:.1f}'] for p in pts]
    return figure("overall", name, data["takeaway"], body,
                  table("Overall score per interview, out of 10", ["Date", "Company", "Round", "Score", "Average of 3"], rows))


def sparkline(pts: list[dict[str, Any]]) -> str:
    sw, sh, pad = 120, 28, 4
    ys = [p["y"] for p in pts]
    lo, hi = min(ys), max(ys)
    if hi - lo < 2:
        mid = (hi + lo) / 2
        lo, hi = mid - 1, mid + 1
    n = len(pts)
    coords = [(pad + (i * (sw - 2 * pad) / (n - 1) if n > 1 else (sw - 2 * pad) / 2),
               pad + (hi - y) / (hi - lo) * (sh - 2 * pad)) for i, y in enumerate(ys)]
    line = f'<path class="tc-spark" d="{path(coords)}"/>' if n > 1 else ""
    x, y = coords[-1]
    return (f'<svg class="sk-spark" viewBox="0 0 {sw} {sh}" aria-hidden="true">{line}'
            f'<circle class="tc-spark-end" cx="{x:.1f}" cy="{y:.1f}" r="3"/></svg>')


def delta(t: dict[str, Any]) -> str:
    if t["dir"] is None:
        return '<span class="sk-delta muted">needs 3+</span>'
    change = t["change"]
    if t["dir"] == "up":
        return f'<span class="sk-delta up"><span aria-hidden="true">↑</span> +{change:.1f}<span class="sr-only"> climbing</span></span>'
    if t["dir"] == "down":
        return f'<span class="sk-delta down"><span aria-hidden="true">↓</span> −{abs(change):.1f}<span class="sr-only"> slipping</span></span>'
    return f'<span class="sk-delta flat"><span aria-hidden="true">→</span> steady</span>'


def skills_chart(data: dict[str, Any]) -> str:
    name, rows = "Skills", data["rows"]
    if not rows:
        return figure("skills", name, data["takeaway"], empty("Skill scores appear after your first reviewed interview."))
    items = "".join(f'<li><span class="sk-name">{e(r["label"])}</span>{sparkline(r["points"])}'
                    f'<span class="sk-last num">{r["latest"]:.1f}</span>{delta(r["trend"])}</li>' for r in rows)
    body = (f'<ul class="sk" role="list">{items}</ul>'
            f'<p class="tc-note small muted">Last {trends.SPARK_POINTS} interviews per skill; the number is the latest score '
            f'out of 10, the arrow the change over the last {trends.TREND_WINDOW}.</p>')
    data_rows = [[r["label"], f'{r["latest"]:.1f}', "–" if r["trend"]["change"] is None else f'{r["trend"]["change"]:+.1f}',
                  ", ".join(f'{p["y"]:.1f}' for p in r["points"])] for r in rows]
    return figure("skills", name, data["takeaway"], body,
                  table("Skill scores, out of 10", ["Skill", "Latest", "Change", "Recent scores, oldest first"], data_rows))


def reactions_chart(data: dict[str, Any]) -> str:
    name, pts = "How interviewers react", data["points"]
    if not pts:
        return figure("reactions", name, data["takeaway"],
                      empty("Appears once an interview has a transcript VALR can read."))
    h, n = 130, len(pts)
    inner = grid(0, 10, h, [(0, "0"), (5, "5"), (10, "10")])
    coords = [(x_at(i, n), y_at(p["y"], 0, 10, h)) for i, p in enumerate(pts)]
    if n > 1:
        inner += f'<path class="tc-line" d="{path(coords)}"/>'
    for p, (x, y) in zip(pts, coords):
        inner += f'<g class="tc-pt"><title>{e(tip(p, f"reaction {p["y"]:.1f} out of 10"))}</title>{marker("circle", x, y)}</g>'
    inner += dates(pts, h)
    body = svg(name, data["takeaway"], h, inner) + few_note(n)
    total = data["warm"] + data["flat"]
    if total:
        warm_pct = round(data["warm"] / total * 100)
        body += (f'<div class="wf" aria-hidden="true"><span class="wf-warm" style="width:{warm_pct}%"></span>'
                 f'<span class="wf-flat" style="width:{100 - warm_pct}%"></span></div>'
                 f'<p class="wf-key small"><span><i class="k-warm"></i>Warm {data["warm"]}</span>'
                 f'<span><i class="k-flat"></i>Flat {data["flat"]}</span>'
                 f'<span class="muted">acknowledgements, last {data["counted"]} interview{"s" if data["counted"] != 1 else ""}</span></p>')
    rows = [[p["date"], p["company"], f'{p["y"]:.1f}'] for p in pts]
    if total:
        rows.append(["Last few interviews", "Warm vs flat acknowledgements", f'{data["warm"]} warm, {data["flat"]} flat'])
    return figure("reactions", name, data["takeaway"], body,
                  table("Interviewer reaction score per interview, out of 10", ["Date", "Company", "Reaction"], rows))


def band_chart(pts: list[dict[str, Any]], lo: float, hi: float, band: tuple[float, float], ticks: list[tuple[float, str]],
               fmt, label: str, title: str) -> str:
    h, n = 104, len(pts)
    top, bottom = y_at(band[1], lo, hi, h), y_at(band[0], lo, hi, h)
    inner = f'<rect class="tc-band" x="{PAD_L}" y="{top:.1f}" width="{W - PAD_L - PAD_R}" height="{bottom - top:.1f}"/>'
    inner += grid(lo, hi, h, ticks)
    coords = [(x_at(i, n), y_at(min(max(p["y"], lo), hi), lo, hi, h)) for i, p in enumerate(pts)]
    if n > 1:
        inner += f'<path class="tc-line faint" d="{path(coords)}"/>'
    for p, (x, y) in zip(pts, coords):
        inside = band[0] <= p["y"] <= band[1]
        inner += (f'<g class="tc-pt"><title>{e(tip(p, fmt(p["y"]) + ("" if inside else " (outside the healthy range)")))}</title>'
                  f'{marker("circle", x, y, "dot in" if inside else "dot out")}</g>')
    inner += dates(pts, h)
    return svg(title, label, h, inner)


def talk_chart(data: dict[str, Any]) -> str:
    name, share, answers = "Talk balance", data["share"], data["answers"]
    if not share and not answers:
        return figure("talk", name, data["takeaway"], empty("Appears once an interview has a transcript VALR can read."))
    body = ""
    if share:
        body += '<p class="tc-sub small">How much of the conversation was you <span class="tc-key">shaded: healthy, 50–72%</span></p>' + band_chart(
            [{**p, "y": p["y"] * 100} for p in share], 0, 100, (trends.TALK_BAND[0] * 100, trends.TALK_BAND[1] * 100),
            [(0, "0%"), (100, "100%")], lambda v: f"{v:.0f}% of the words", "healthy 50–72%", "Your share of the talking")
    if answers:
        top = max(120, -(-max(p["y"] for p in answers) // 30) * 30)
        body += '<p class="tc-sub small">Average answer length <span class="tc-key">shaded: healthy, under 90 s</span></p>' + band_chart(
            answers, 0, top, (0, trends.ANSWER_LIMIT), [(0, "0s"), (top, f"{top:.0f}s")],
            lambda v: f"{v:.0f} seconds per answer", "healthy under 90 s", "Average answer length")
    rows = [[p["date"], p["company"], f'{p["y"] * 100:.0f}%', ""] for p in share]
    by_date = {(p["date"], p["company"]): p for p in answers}
    for r in rows:
        a = by_date.get((r[0], r[1]))
        r[3] = f'{a["y"]:.0f} s' if a else "–"
    return figure("talk", name, data["takeaway"], body,
                  table("Talk balance per interview", ["Date", "Company", "Your share of the words", "Average answer"], rows))


def bars(rows: list[tuple[str, float, str]], top: float, cls: str = "") -> str:
    items = "".join(f'<li><span class="hb-name">{e(label)}</span><span class="hb-track"><i class="{cls}" '
                    f'style="width:{(value / top * 100) if top else 0:.0f}%"></i></span><span class="hb-val num">{e(text)}</span></li>'
                    for label, value, text in rows)
    return f'<ul class="hb" role="list">{items}</ul>'


def funnel_chart(data: dict[str, Any], leads_note: bool) -> str:
    name, steps = "Where interviews led", data["steps"]
    if not steps[0][1]:
        return figure("funnel", name, data["takeaway"], empty("Your first interview starts this chart."))
    body = bars([(label, value, str(value)) for label, value in steps], steps[0][1], "funnel")
    extra = " · ".join(x for x in (f"No word yet: {data['unknown']}" if data["unknown"] else "",
                                   f"Didn't move on: {data['rejected']}" if data["rejected"] else "") if x)
    if extra:
        body += f'<p class="tc-note small muted">{e(extra)}</p>'
    if leads_note:
        body += '<p class="tc-note small muted">Offers appear here when the leads tracker is connected.</p>'
    return figure("funnel", name, data["takeaway"], body,
                  table("Where interviews led", ["Step", "Count"], [[label, str(v)] for label, v in steps]))


def rounds_chart(data: dict[str, Any]) -> str:
    rows = data["rows"]
    if len(rows) < 2:
        return ""
    body = bars([(r["round"], r["avg"], f'{r["avg"]:.1f} · {r["n"]}') for r in rows], 10)
    body += '<p class="tc-note small muted">Average score out of 10 · number of interviews.</p>'
    return figure("rounds", "Score by round type", data["takeaway"], body,
                  table("Average score by round type", ["Round", "Average out of 10", "Interviews"],
                        [[r["round"], f'{r["avg"]:.1f}', str(r["n"])] for r in rows]))


def all_charts(t: dict[str, Any]) -> str:
    return "".join((overall_chart(t["overall"]), skills_chart(t["skills"]), reactions_chart(t["reactions"]),
                    talk_chart(t["talk"]), funnel_chart(t["funnel"], len(t["funnel"]["steps"]) < 3),
                    rounds_chart(t["rounds"])))
