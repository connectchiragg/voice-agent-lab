"""Performance: trends first, then what they mean, then the detail folded away.

1. Trends: four to six small charts (app/valr_ui/charts.py), each under a one-line
   takeaway computed from the interviews (app/valr_ui/trends.py): overall score, skills,
   how interviewers react, talk balance, where interviews led, score by round type.
2. Insights, from the coach's read (valr-eval's performance.json schema 3, mapped by
   app/valr_view.coach_view): where you stand (two sentences), what's working, what's
   costing you, this week's focus as checkboxes, the salary scripts folded.
3. Folded: recent interviews, all the numbers in one table, post-call reports.
"""
import re
from typing import Any

from app.company_names import NOT_IDENTIFIED
from app.valr_page import OUTCOME, Clock, chip, e, score, short_day
from app.valr_ui import attention, charts, trends
from app.valr_ui.shell import page_head
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
            f'<footer class="mono small">{e(who)}{(" · " + e(where)) if where else ""}</footer></blockquote>')


def stand_card(c: dict[str, Any]) -> str:
    stale = ('<p class="muted small">From before your latest interview; it refreshes after the next review.</p>'
             if c["stale"] else "")
    return (f'<article class="card ins ins-stand" id="stand"><h3 class="ins-title">Where you stand</h3>'
            f'<p class="ins-lead">{e(sentences(c["where_you_stand"]))}</p>{stale}</article>')


def working_card(c: dict[str, Any]) -> str:
    if not c["working"]:
        return ""
    items = "".join(f'<li><p class="ins-point">{e(w["headline"])}</p>{quote_html(w["example"], 110)}</li>'
                    for w in c["working"][:3])
    return f'<article class="card ins ins-ok" id="working"><h3 class="ins-title">What\'s working</h3><ul class="ins-list">{items}</ul></article>'


def costing_cards(c: dict[str, Any]) -> str:
    out = []
    for x in c["costing"][:3]:
        say = x["model_rephrase"] or x["do_instead"]
        out.append(f"""<article class="card ins ins-cost">
  <p class="label">Costing you</p>
  <h4 class="ins-point">{e(x["headline"])}</h4>
  {f'<p class="small"><b>Why it matters.</b> {e(sentences(x["why_it_matters"], 1))}</p>' if x["why_it_matters"] else ''}
  {quote_html(x["example"], 120)}
  {f'<p class="say"><span class="mono">Say instead</span>{e(say)}</p>' if say else ''}
</article>""")
    if not out:
        return ""
    return (f'<section class="costing" aria-labelledby="cost-title"><h3 id="cost-title" class="ins-title">What\'s costing you</h3>'
            f'<div class="costs">{"".join(out)}</div></section>')


def focus_card(items: list[tuple[str, str]], key: str, note: str = "") -> str:
    if not items:
        return ""
    rows = "".join(f'<li><label><input type="checkbox" data-focus="{e(key)}:{i}">'
                   f'<span><span class="f-action">{e(head)}</span>{f"<span class=\"small muted\">{e(how)}</span>" if how else ""}</span>'
                   f'</label></li>' for i, (head, how) in enumerate(items[:3], 1))
    return (f'<article class="card ins ins-focus" id="focus"><h3 class="ins-title">This week\'s focus</h3>{note}'
            f'<ul class="focus-checks">{rows}</ul>'
            f'<p class="small muted">Ticks stay in this browser only.</p></article>')


def salary_card(c: dict[str, Any]) -> str:
    neg = c["negotiation"]
    if not (neg["read"] or neg["scripts"]):
        return ""
    scripts = "".join(f'<li><p class="ins-point">{e(x["moment"])}</p><p class="say"><span class="mono">Say</span>{e(x["say"])}</p></li>'
                      for x in neg["scripts"])
    return (f'<details class="card neg fold" id="salary"><summary><h3 class="ins-title">Salary conversations: what to say</h3></summary>'
            f'{f"<p class=\"muted\">{e(neg["read"])}</p>" if neg["read"] else ""}<ul class="scripts">{scripts}</ul>'
            '<p class="muted small">Scripts use placeholders on purpose: VALR never writes your numbers here.</p></details>')


def insights(perf: dict[str, Any]) -> str:
    c = perf.get("coach")
    if c:
        cards = (stand_card(c) + working_card(c)
                 + focus_card([(f["drill"], sentences(f["how"], 1)) for f in c["focus"]], str(c.get("generated_at") or "coach")))
        rest = costing_cards(c) + salary_card(c)
    else:
        latest = perf["interviews"][-1] if perf["interviews"] else {}
        note = (f'<p class="small muted">From your last interview. The coach\'s full read appears here after the next review.</p>')
        cards = focus_card([(f["action"], "") for f in perf.get("focus_this_week") or []], f"latest:{latest.get('id', '')}", note)
        rest = ""
    if not cards and not rest:
        return ""
    return (f'<section class="insights" aria-labelledby="ins-title"><h2 id="ins-title" class="h-md">What it means</h2>'
            f'<div class="ins-grid">{cards}</div>{rest}</section>')


def impression_html(imp: dict[str, Any] | None, open_: bool = False) -> str:
    """One interview from the interviewer's side: what they leaned into, the cues with their
    evidence, and the numbers folded."""
    if not imp:
        return ""
    if imp["coverage"] == "derived":
        share = f"You spoke {imp['talk_share'] * 100:.0f}% of the words." if imp["talk_share"] is not None else ""
        return f'<p class="muted small">The transcript is no longer available, so only the talk balance is known. {e(share)}</p>'
    parts = imp["components"]
    names = (("engagement", "Engagement"), ("positive", "Positive signals"), ("negative", "Warning signs"),
             ("next_steps", "Next steps"), ("balance", "Talk balance"))
    bars = "".join(f'<li><span class="sb-name">{e(n)}</span><span class="sb-track"><i class="{"neg" if k == "negative" else ""}" '
                   f'style="width:{(parts[k] or 0) * 10:.0f}%"></i></span><span class="sb-val num">{score(parts[k])}</span></li>'
                   for k, n in names if parts.get(k) is not None)
    cues = "".join(f'<li class="cue"><p class="q-head"><span class="chip tone-{c["tone"]}">{e(c["kind"])}</span>'
                   f'<span class="mono small muted">{("at " + e(c["at"])) if c["at"] else ""}</span></p>'
                   f'<p class="q-quote">“{e(c["quote"])}”</p>'
                   + (f'<p class="small muted">After you said: {e(c["after"])}</p>' if c["after"] else "") + "</li>"
                   for c in imp["cues"])
    facts = " · ".join(x for x in (
        f"You spoke {imp['talk_share'] * 100:.0f}% of the words" if imp["talk_share"] is not None else "",
        imp["next_step"],
        (f"ended at {imp['length_vs_slot'] * 100:.0f}% of the scheduled time" if imp["length_vs_slot"] is not None
         and imp["length_vs_slot"] < 0.75 else "")) if x)
    return f"""
<div class="imp">
  {f'<p><span class="chip tone-ok">Leaned in</span> {e(imp["leaned_in"])}</p>' if imp["leaned_in"] else ''}
  {f'<p><span class="chip tone-bad">Disengaged</span> {e(imp["disengaged"])}</p>' if imp["disengaged"] else ''}
  {f'<p class="small muted">{e(facts)}</p>' if facts else ''}
  {f'<details class="fold"{" open" if open_ and not (imp["leaned_in"] or imp["disengaged"]) else ""}><summary class="small">The cues, with quotes ({len(imp["cues"])})</summary><ul class="cues">{cues}</ul></details>' if cues else ''}
  {f'<details class="fold"><summary class="small">Impression score · {score(imp["score"])}/10</summary><ul class="score-bars">{bars}</ul></details>' if bars else ''}
</div>"""


REPORT_STATUS = {"sent": ("Sent on Telegram", "ok"), "pending_telegram": ("Waiting for Telegram", "warn"),
                 "queued": ("Queued", "info"), "processing": ("Scoring now", "info"), "failed": ("Failed", "bad")}


def post_call_html(pc: dict[str, Any] | None, clock: Clock) -> str:
    """Post-call reports: say when some wait for Telegram, and list the latest."""
    if not pc or not pc.get("recent"):
        return ""
    note = (f'<p class="verdict small">{pc["pending"]} post-call report{"s" if pc["pending"] != 1 else ""} '
            f'pending: <a href="/valr/settings#telegram">connect Telegram</a> and VALR sends '
            f'{"them" if pc["pending"] != 1 else "it"}.</p>' if pc.get("pending") else "")
    rows = "".join(f'<li><span>{e(r["title"] or "Interview")}</span><span>{chip(*REPORT_STATUS.get(r["status"], (r["status"], "muted")))} '
                   f'<span class="muted small">{clock.when(r["updated_at"]) if r.get("updated_at") else ""}</span></span></li>'
                   for r in pc["recent"])
    return (f'<section class="card post-call" id="reports"><h2 class="card-title">Post-call reports</h2>'
            f'<p class="muted small">After each recorded interview VALR scores it and sends you a short report on Telegram.</p>'
            f'{note}<ul class="kv-rows">{rows}</ul></section>')


REPORT_STATUS = {"sent": ("Sent on Telegram", "ok"), "pending_telegram": ("Waiting for Telegram", "warn"),
                 "queued": ("Queued", "info"), "processing": ("Scoring now", "info"), "failed": ("Failed", "bad")}


def pending_note(pc: dict[str, Any] | None) -> str:
    if not pc or not pc.get("pending"):
        return ""
    n = pc["pending"]
    return (f'<p class="verdict small">{n} post-call report{"s" if n != 1 else ""} pending: '
            f'<a href="/valr/settings#telegram">connect Telegram</a> and VALR sends {"them" if n != 1 else "it"}.</p>')


def post_call_html(pc: dict[str, Any] | None, clock: Clock) -> str:
    """The latest post-call reports, folded."""
    if not pc or not pc.get("recent"):
        return ""
    rows = "".join(f'<li><span>{e(r["title"] or "Interview")}</span><span>{chip(*REPORT_STATUS.get(r["status"], (r["status"], "muted")))} '
                   f'<span class="muted small">{clock.when(r["updated_at"]) if r.get("updated_at") else ""}</span></span></li>'
                   for r in pc["recent"])
    return (f'<details class="numbers" id="reports"><summary><h2 class="h-md">Post-call reports</h2>'
            f'<span class="muted small">a short report on Telegram after each recorded interview</span></summary>'
            f'<ul class="kv-rows">{rows}</ul></details>')


def recent_html(perf: dict[str, Any], clock: Clock) -> str:
    rows = "".join(
        f'<li><a href="/valr/interviews/past/{e(attention.past_slug(iv))}"><span class="cu-when mono num">{e(clock.short_date(iv["date"]) if iv["date"] else "–")}</span>'
        f'<span class="cu-what">{e(iv["company"] or NOT_IDENTIFIED)} <span class="muted small">{e(iv["round"])}</span></span>'
        f'<span class="cu-end"><span class="num">{score(iv["overall"])}</span>{chip(*OUTCOME[iv["outcome"]])}</span></a></li>'
        for iv in list(reversed(perf["interviews"]))[:5])
    return (f'<details class="numbers" id="recent"><summary><h2 class="h-md">Recent interviews</h2>'
            f'<span class="muted small">the last 5, each with its own page</span></summary>'
            f'<ul class="cu-list">{rows}</ul><p class="small"><a class="mono" href="/valr/interviews/past">All past interviews →</a></p></details>')


def numbers_html(perf: dict[str, Any]) -> str:
    """Every interview's numbers in one table (oldest last row first: newest on top)."""
    skills = [k for k in trends.SKILLS if any((iv.get("dimensions") or {}).get(k) is not None for iv in perf["interviews"])]
    head = ["Date", "Company", "Round", "Overall", *[label(k) for k in skills], "Reaction", "You talked", "Avg answer", "Outcome"]
    rows = []
    for iv in reversed(perf["interviews"]):
        imp = iv.get("impression") or {}
        cells = [short_day(iv["date"]) if iv["date"] else "–", iv["company"] or NOT_IDENTIFIED, iv["round"], score(iv["overall"]),
                 *[score((iv.get("dimensions") or {}).get(k)) for k in skills], score(imp.get("score")),
                 f'{imp["talk_share"] * 100:.0f}%' if imp.get("talk_share") is not None else "–",
                 f'{imp["answer_seconds"]:.0f} s' if imp.get("answer_seconds") is not None else "–",
                 OUTCOME[iv["outcome"]][0]]
        rows.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in cells) + "</tr>")
    th = "".join(f'<th scope="col">{e(h)}</th>' for h in head)
    return (f'<details class="numbers" id="numbers"><summary><h2 class="h-md">All the numbers</h2>'
            f'<span class="muted small">every interview, scores out of 10</span></summary>'
            f'<div class="table-scroll"><table class="num-table"><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')


def page(view: dict[str, Any], clock: Clock, leads: dict[str, Any] | None = None) -> str:
    perf = view["performance"]
    head = page_head("Performance", 'How your interviews <span class="dim">are going.</span>')
    if not perf["available"]:
        return head + ('<div class="empty"><p class="empty-title">Your scores appear after your first recorded interview.</p>'
                       '<p class="muted">Connect a notetaker; VALR reviews each recording, scores it out of 10 '
                       'and tells you what is improving and what to practise.</p>'
                       '<a class="btn small" href="/valr/settings#notetakers">Connect a notetaker</a></div>')
    labels = {k: label(k) for k in trends.SKILLS}
    t = trends.build(perf["interviews"], labels, leads)
    return f"""{head}
<section class="trends" aria-labelledby="trends-title">
  <h2 id="trends-title" class="h-md">Trends</h2>
  <div class="tc-grid">{charts.all_charts(t)}</div>
</section>
{insights(perf)}
{pending_note(view.get("post_call"))}
<div class="folds">
{recent_html(perf, clock)}
{numbers_html(perf)}
{post_call_html(view.get("post_call"), clock)}
</div>"""
