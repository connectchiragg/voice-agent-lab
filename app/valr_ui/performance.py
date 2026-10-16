"""Progress (renamed from Performance): how the interviews are going, for a coach's eye.

1. Trends: small charts (app/valr_ui/charts.py), each under a one-line takeaway computed
   from the interviews (app/valr_ui/trends.py). Fewer than three scored interviews: one
   line ("Trends appear after 3 recorded interviews") and the latest "Next time" instead.
2. What it means, from the coach's read (valr-eval's performance.json schema 3, mapped by
   app/valr_view.coach_view): where you stand, what's working, this week's focus as ticks,
   what's costing you with "Say instead", and the salary scripts folded.
3. Every number as a CSV download under ⋯. Post-call delivery lives in Settings › Advanced.
"""
import csv
import io
import re
from typing import Any
from urllib.parse import quote

from app.company_names import NOT_IDENTIFIED
from app.valr_page import OUTCOME, Clock, e, score, short_day
from app.valr_ui import charts, trends
from app.valr_ui.shell import empty_state, icon, menu, menu_item, page_head
from app.valr_view import label

SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“])")


def sentences(text: str, limit: int = 2) -> str:
    """The first `limit` sentences of the text."""
    return " ".join(SENTENCE.split((text or "").strip())[:limit])


def clip(text: str, limit: int = 140) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def quote_html(q: dict[str, Any] | None, limit: int = 140) -> str:
    """A real quote, kept short, with who said it, where and when."""
    if not q:
        return ""
    who = "You" if q["speaker"] == "you" else "Interviewer"
    where = " · ".join(x for x in (q["company"], short_day(q["date"]) if q["date"] else "") if x)
    return (f'<blockquote class="cq cq-{e(q["speaker"])}"><p>“{e(clip(q["quote"], limit))}”</p>'
            f'<footer>{e(who)}{(" · " + e(where)) if where else ""}</footer></blockquote>')


def stand_card(c: dict[str, Any]) -> str:
    stale = '<p class="muted">From before your latest interview; it refreshes after the next review.</p>' if c["stale"] else ""
    return (f'<article class="card ins ins-stand" id="stand"><h3 class="ins-title">Where you stand</h3>'
            f'<p class="ins-lead">{e(sentences(c["where_you_stand"]))}</p>{stale}</article>')


def working_card(c: dict[str, Any]) -> str:
    if not c["working"]:
        return ""
    items = "".join(f'<li><p class="ins-point">{icon("check", 16)}{e(w["headline"])}</p>{quote_html(w["example"], 110)}</li>'
                    for w in c["working"][:3])
    return f'<article class="card ins ins-ok" id="working"><h3 class="ins-title">What\'s working</h3><ul class="ins-list">{items}</ul></article>'


def costing_cards(c: dict[str, Any]) -> str:
    out = []
    for x in c["costing"][:3]:
        say = x["model_rephrase"] or x["do_instead"]
        out.append(f"""<article class="card ins ins-cost">
  <h4 class="ins-point">{e(x["headline"])}</h4>
  {f'<p class="ins-why">{e(sentences(x["why_it_matters"], 1))}</p>' if x["why_it_matters"] else ''}
  {quote_html(x["example"], 120)}
  {f'<p class="say"><span class="say-tag">Say instead</span>{e(say)}</p>' if say else ''}
</article>""")
    if not out:
        return ""
    return (f'<section class="costing" aria-labelledby="cost-title"><h3 id="cost-title" class="section-title">What\'s costing you</h3>'
            f'<div class="costs">{"".join(out)}</div></section>')


def focus_card(items: list[tuple[str, str]], key: str, note: str = "") -> str:
    if not items:
        return ""
    rows = "".join(f'<li><label class="check"><input type="checkbox" data-focus="{e(key)}:{i}">'
                   f'<span><span class="f-action">{e(head)}</span>{f"<span class=\"f-how\">{e(how)}</span>" if how else ""}</span>'
                   f'</label></li>' for i, (head, how) in enumerate(items[:3], 1))
    return (f'<article class="card ins ins-focus" id="focus"><h3 class="ins-title">This week\'s focus</h3>{note}'
            f'<ul class="checklist">{rows}</ul></article>')


def salary_card(c: dict[str, Any]) -> str:
    neg = c["negotiation"]
    if not (neg["read"] or neg["scripts"]):
        return ""
    scripts = "".join(f'<li><p class="ins-point">{e(x["moment"])}</p><p class="say"><span class="say-tag">Say</span>{e(x["say"])}</p></li>'
                      for x in neg["scripts"])
    return (f'<details class="fold card" id="salary"><summary><span class="fold-title">Salary conversations: what to say</span>'
            f'{icon("chevron-down", 18)}</summary><div class="fold-body">'
            f'{f"<p class=\"muted\">{e(neg["read"])}</p>" if neg["read"] else ""}<ul class="scripts">{scripts}</ul></div></details>')


def insights(perf: dict[str, Any]) -> str:
    c = perf.get("coach")
    if c:
        cards = (stand_card(c) + working_card(c)
                 + focus_card([(f["drill"], sentences(f["how"], 1)) for f in c["focus"]], str(c.get("generated_at") or "coach")))
        rest = costing_cards(c) + salary_card(c)
    else:
        latest = perf["interviews"][-1] if perf["interviews"] else {}
        note = '<p class="muted">From your last interview. The coach\'s full read appears here after the next review.</p>'
        cards = focus_card([(f["action"], "") for f in perf.get("focus_this_week") or []], f"latest:{latest.get('id', '')}", note)
        rest = ""
    if not cards and not rest:
        return ""
    return (f'<section class="section insights" aria-labelledby="ins-title"><div class="section-head">'
            f'<h2 id="ins-title" class="section-title">What it means</h2></div>'
            f'<div class="ins-grid">{cards}</div>{rest}</section>')


def csv_href(perf: dict[str, Any]) -> str:
    """Every interview's numbers as a CSV (newest first), as a data: link for the ⋯ menu."""
    skills = [k for k in trends.SKILLS if any((iv.get("dimensions") or {}).get(k) is not None for iv in perf["interviews"])]
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["Date", "Company", "Round", "Overall", *[label(k) for k in skills], "Reaction", "You talked",
                  "Average answer (s)", "Outcome"])
    for iv in reversed(perf["interviews"]):
        imp = iv.get("impression") or {}
        out.writerow([iv["date"], iv["company"] or NOT_IDENTIFIED, iv["round"], score(iv["overall"]),
                      *[score((iv.get("dimensions") or {}).get(k)) for k in skills], score(imp.get("score")),
                      f'{imp["talk_share"] * 100:.0f}%' if imp.get("talk_share") is not None else "",
                      f'{imp["answer_seconds"]:.0f}' if imp.get("answer_seconds") is not None else "",
                      OUTCOME[iv["outcome"]][0]])
    return "data:text/csv;charset=utf-8," + quote(buf.getvalue())


def early_note(perf: dict[str, Any], scored: int) -> str:
    """Before three scored interviews: say when trends appear, and show the latest "Next time"."""
    latest = perf["interviews"][-1] if perf["interviews"] else {}
    tips = "".join(f"<li>{e(x)}</li>" for x in latest.get("do_next_time") or [])
    body = (f'<p class="early-title">Trends appear after 3 recorded interviews. You have {scored}.</p>'
            + (f'<p class="muted">Meanwhile, from your {e(latest.get("company") or "last")} interview:</p>'
               f'<ol class="points numbered">{tips}</ol>' if tips else ""))
    return f'<section class="card early" id="trends-title">{body}</section>'


def page(view: dict[str, Any], clock: Clock, leads: dict[str, Any] | None = None) -> str:
    perf = view["performance"]
    if not perf["available"]:
        return page_head("Progress") + empty_state(
            "chart", "Your progress appears after your first recorded interview.",
            "Connect a notetaker; VALR reviews each recording and tells you what is improving and what to practise.",
            '<a class="btn primary" href="/valr/settings#notetakers">Connect a notetaker</a>')
    head = page_head("Progress", actions=menu([menu_item("Download all the numbers", "download", csv_href(perf),
                                                         attrs=' download="valr-progress.csv"')]))
    scored = sum(1 for iv in perf["interviews"] if iv.get("overall") is not None)
    if scored < trends.MIN_POINTS:
        return head + early_note(perf, scored) + insights(perf)
    labels = {k: label(k) for k in trends.SKILLS}
    t = trends.build(perf["interviews"], labels, leads)
    return f"""{head}
<section class="section trends" aria-labelledby="trends-title">
  <h2 id="trends-title" class="sr-only">Trends</h2>
  <div class="tc-grid">{charts.all_charts(t)}</div>
</section>
{insights(perf)}"""
