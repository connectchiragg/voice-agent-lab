"""Performance: the coach's words first (valr-eval's performance.json schema 3, mapped by
app/valr_view.coach_view): where you stand, what's working, what's costing you, this week's
focus and the salary scripts; then the post-call reports; then the numbers, folded away
(three tiles, the trend chart, recurring topics, impression by outcome) and the past rounds.

The chart markup (#perf-chart, .picker, #perf-data) is the one static/valr.js draws into.
"""
from app.company_names import NOT_IDENTIFIED
import json
from typing import Any

from app.valr_page import BADGE, OUTCOME, VERDICT, Clock, badge_chip, chip, e, score, short_day
from app.valr_ui import attention
from app.valr_ui.shell import page_head


def quote_html(q: dict[str, Any] | None) -> str:
    """A real quote with who said it, where and when."""
    if not q:
        return ""
    who = "You" if q["speaker"] == "you" else "Interviewer"
    where = " · ".join(x for x in (q["company"], short_day(q["date"]) if q["date"] else "", q["at"]) if x)
    return (f'<blockquote class="cq cq-{e(q["speaker"])}"><p>“{e(q["quote"])}”</p>'
            f'<footer class="mono small">{e(who)}{(" · " + e(where)) if where else ""}</footer></blockquote>')


def coach_html(c: dict[str, Any]) -> str:
    """The coach's read: plain language first; quotes as evidence; scripts without numbers."""
    working = "".join(f'<li><p class="c-head">{e(w["headline"])}</p><p class="muted">{e(w["detail"])}</p>'
                      f'{quote_html(w["example"])}</li>' for w in c["working"])
    costing = "".join(f"""
<article class="cost">
  <p class="label">Costing you · {i}</p>
  <h4>{e(x["headline"])}</h4>
  {f'<p class="muted">{e(x["plain_meaning"])}</p>' if x["plain_meaning"] else ''}
  {f'<p><b>Why it matters.</b> {e(x["why_it_matters"])}</p>' if x["why_it_matters"] else ''}
  {quote_html(x["example"])}
  {f'<p class="small muted">{e(x["what_happened"])}</p>' if x["what_happened"] else ''}
  {f'<p><b>Do instead.</b> {e(x["do_instead"])}</p>' if x["do_instead"] else ''}
  {f'<p class="say"><span class="mono">Say this instead</span>{e(x["model_rephrase"])}</p>' if x["model_rephrase"] else ''}
</article>""" for i, x in enumerate(c["costing"], 1))
    drills = "".join(f'<li><span class="f-num mono">0{i}</span><div><p class="f-action">{e(f["drill"])}</p>'
                     f'<p class="small">{e(f["how"])}</p>'
                     + (f'<p class="muted small">Done when: {e(f["done_when"])}</p>' if f["done_when"] else "")
                     + '</div></li>' for i, f in enumerate(c["focus"], 1))
    iv = c["interviewers"]
    reacted = ""
    if iv["leaned_in"] or iv["disengaged"]:
        reacted = ('<div class="react"><h3 class="h-sm">How interviewers reacted</h3>'
                   + (f'<p><span class="chip tone-ok">Leaned in</span> {e(iv["leaned_in"])}</p>' if iv["leaned_in"] else "")
                   + (f'<p><span class="chip tone-bad">Disengaged</span> {e(iv["disengaged"])}</p>' if iv["disengaged"] else "")
                   + "".join(quote_html(q) for q in iv["examples"]) + "</div>")
    neg = c["negotiation"]
    scripts = "".join(f'<li><p class="c-head">{e(x["moment"])}</p><p class="say"><span class="mono">Say</span>{e(x["say"])}</p></li>'
                      for x in neg["scripts"])
    negotiation = (f'<details class="neg card" id="salary" open><summary><h3 class="h-sm">Salary conversations: what to say</h3></summary>'
                   f'<p class="muted">{e(neg["read"])}</p><ul class="scripts">{scripts}</ul>'
                   '<p class="muted small">Scripts use placeholders on purpose: VALR never writes your numbers here.</p></details>'
                   if neg["read"] or scripts else "")
    stale = ('<p class="muted small">This read is from before your latest interview; it refreshes after the next review.</p>'
             if c["stale"] else "")
    return f"""
<section class="coach card" aria-labelledby="coach-title">
  <p class="label">Your coach</p>
  <h2 id="coach-title" class="coach-head">Where you stand</h2>
  <p class="coach-text stand">{e(c["where_you_stand"])}</p>{stale}
  <div class="coach-cols">
    {f'<div><h3 class="h-sm">What\'s working</h3><ul class="working">{working}</ul></div>' if working else ''}
    {f'<div>{reacted}</div>' if reacted else ''}
  </div>
</section>
{f'<section class="costing" aria-labelledby="cost-title"><h2 id="cost-title" class="h-md">What\'s costing you</h2><div class="costs">{costing}</div></section>' if costing else ''}
{f'<section class="card focus" id="focus" aria-labelledby="focus-title"><h2 id="focus-title" class="card-title">This week\'s focus</h2><ol class="focus-list">{drills}</ol></section>' if drills else ''}
{negotiation}"""


def focus_block(perf: dict[str, Any]) -> str:
    """Before the coach's first read: the rubric's focus items."""
    focus = perf.get("focus_this_week") or []
    if not focus:
        return ""
    items = "".join(f'<li><span class="f-num mono">0{i}</span><div><p class="f-action">{e(f["action"])}</p>'
                    f'<p class="muted small">{e(f["why"])}</p></div></li>' for i, f in enumerate(focus, 1))
    return f"""
<section class="coach card" aria-labelledby="coach-title">
  <p class="label">Your coach</p>
  <h2 id="coach-title" class="coach-head">Focus this week</h2>
  <p class="muted small">The coach's full write-up appears here after the next review.</p>
  <ol class="focus-list">{items}</ol>
</section>"""


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


def impressions_overview(imp: dict[str, Any] | None) -> str:
    if not imp:
        return ""
    by = imp["by_outcome"]
    rows = "".join(f'<li><span>{e(OUTCOME[k][0])}</span><span class="num">{score(by[k]["mean"])}'
                   f'<span class="muted">/10 · {e(by[k]["n"])}</span></span></li>' for k in ("advanced", "rejected", "unknown")
                   if by[k]["n"])
    return (f'<div><h3 class="sub-title">Interviewer impression by outcome</h3><ul class="kv-rows">{rows}</ul>'
            + (f'<p class="muted small">Tracks your rubric score with r = {imp["r_with_score"]:.2f} '
               f'over {imp["n"]} interviews with transcripts.</p>' if imp["r_with_score"] is not None else "")
            + (f'<p class="muted small">{e(imp["note"])}</p>' if imp["note"] else "") + "</div>")


def page(view: dict[str, Any], clock: Clock) -> str:
    perf = view["performance"]
    head = page_head("Performance", 'How your interviews <span class="dim">are going.</span>')
    if not perf["available"]:
        return head + ('<div class="empty"><p class="empty-title">Your scores appear after your first recorded interview.</p>'
                       '<p class="muted">Connect a notetaker; VALR reviews each recording on the same rubric (out of 10) '
                       'and tells you what is improving and what to practise.</p>'
                       '<a class="btn small" href="/valr/settings#notetakers">Connect a notetaker</a></div>')
    overall = perf["series"]["overall"]
    last = overall["points"][-1]["ma3"] if overall["points"] else None
    most, risk = perf.get("most_improved"), perf.get("biggest_risk")
    tiles = f"""
<div class="tiles">
  <div class="tile"><p class="label">Overall · last 3 interviews</p><p class="tile-big num">{score(last)}<span class="of">/10</span></p>{badge_chip(overall['badge'])}</div>
  <div class="tile"><p class="label">Most improved</p><p class="tile-mid">{e(most['label']) if most else '–'}</p>
    {chip(f"↗ +{most['change']:.1f}", "ok") if most and most.get('change') is not None else '<p class="muted small">Needs 3 interviews on the same skill.</p>' if not most else ''}</div>
  <div class="tile risk"><p class="label">Watch out for</p><p class="tile-mid">{e(risk['label']) if risk else 'Nothing slipping'}</p>
    {f'<p class="muted small">Recent average {score(risk["latest"])}/10</p>' if risk else ''}</div>
</div>"""
    dim_buttons = "".join(
        f'<button type="button" class="pick" data-dim="{e(d["key"])}" aria-pressed="false">{e(d["label"])} '
        f'<span class="pick-badge" aria-hidden="true">{BADGE.get(perf["series"].get(d["key"], {}).get("badge", ""), BADGE["insufficient_data"])[2]}</span></button>'
        for d in perf["dimensions"] if perf["series"].get(d["key"], {}).get("n"))
    data = json.dumps({"series": perf["series"],
                       "dimensions": [{"key": d["key"], "label": d["label"],
                                       "subs": [{"key": x["key"], "label": x["label"]} for x in d["subs"]
                                                if perf["series"].get(x["key"], {}).get("n")]}
                                      for d in perf["dimensions"]]}).replace("<", "\\u003c")
    weak = "".join(f'<li class="weak"><span class="weak-topic">{e(t["topic"])}</span>{chip(*VERDICT[t["verdict"]])}'
                   f'<span class="muted small">{t["seen"]} interview{"s" if t["seen"] != 1 else ""}'
                   f'{(" · " + e(t["last_note"])) if t["last_note"] else ""}</span></li>'
                   for t in perf.get("recurring_weak_topics") or [])
    rounds = "".join(f'<li><span>{e(r["round"])}</span><span class="num">{score(r["overall"])}<span class="muted">/10 · {e(r["n"])}</span></span></li>'
                     for r in perf.get("by_round_type") or [])
    recent = "".join(
        f'<li><a href="/valr/interviews/past/{e(attention.past_slug(iv))}"><span class="cu-when mono num">{e(clock.short_date(iv["date"]) if iv["date"] else "–")}</span>'
        f'<span class="cu-what">{e(iv["company"] or NOT_IDENTIFIED)} <span class="muted small">{e(iv["round"])}</span></span>'
        f'<span class="cu-end"><span class="num">{score(iv["overall"])}</span>{chip(*OUTCOME[iv["outcome"]])}</span></a></li>'
        for iv in list(reversed(perf["interviews"]))[:5])
    coach = perf.get("coach")
    trend = f'<p class="trend-text">{e(perf["trend_text"])}</p>' if perf.get("trend_text") else ""
    return f"""{head}
{coach_html(coach) if coach else focus_block(perf)}
{post_call_html(view.get("post_call"), clock)}
<details class="numbers" id="numbers"{'' if coach else ' open'}>
  <summary><h2 id="numbers-title" class="h-md">The numbers behind this</h2><span class="muted small">scores, trend, recurring topics</span></summary>
  {tiles}{trend}
  <div class="chart-box">
    <div class="chart-head"><h3 class="sub-title">Trend</h3><p class="muted small">Dots are single interviews; the line is the
      average of the last 3, out of 10. Skills a round didn't test are left out.</p></div>
    <div class="picker" role="group" aria-label="Choose what to chart"><button type="button" class="pick" data-dim="overall" aria-pressed="true">Overall</button>{dim_buttons}</div>
    <div class="picker subs" role="group" aria-label="Choose a skill" hidden></div>
    <div class="chart-wrap"><div id="perf-chart" class="chart" role="img" aria-label="Interview scores over time">
      <noscript><p class="muted">The chart needs JavaScript; each past interview has its scores.</p></noscript></div></div>
    <p class="chart-status small" aria-live="polite"></p>
  </div>
  <div class="two">
    {f'<div><h3 class="sub-title">Topics that keep coming up</h3><ul class="weak-list">{weak}</ul></div>' if weak else ''}
    {f'<div><h3 class="sub-title">By round type</h3><ul class="kv-rows">{rounds}</ul></div>' if rounds else ''}
    {impressions_overview(perf.get("impressions"))}
  </div>
  <script type="application/json" id="perf-data">{data}</script>
</details>
<section class="coming"><div class="section-row"><h2 class="h-sm">Recent rounds</h2><a class="mono small" href="/valr/interviews/past">All past interviews →</a></div>
  <ul class="cu-list">{recent}</ul></section>"""
