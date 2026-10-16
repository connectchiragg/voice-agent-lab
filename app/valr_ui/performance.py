"""Performance: the coach's words first (valr-eval's narrative), what to work on this week,
then the numbers (three tiles, the trend chart, recurring topics) and the past rounds.

The chart markup (#perf-chart, .picker, #perf-data) is the one static/valr.js draws into.
"""
import json
from typing import Any

from app.valr_page import BADGE, OUTCOME, VERDICT, Clock, badge_chip, chip, e, score
from app.valr_ui import attention
from app.valr_ui.shell import page_head


def coach_block(perf: dict[str, Any]) -> str:
    c = perf.get("coach")
    focus = perf.get("focus_this_week") or []
    if c:
        strengths = "".join(f"<li>{e(s)}</li>" for s in c["strengths"])
        work = "".join(f'<li><p class="f-action">{e(w["action"])}</p>'
                       + (f'<p class="muted small">{e(w["why"])}</p>' if w["why"] else "") + "</li>" for w in c["work_on"])
        return f"""
<section class="coach card" aria-labelledby="coach-title">
  <p class="label">Your coach</p>
  <h2 id="coach-title" class="coach-head">{e(c['headline'] or 'How you are doing')}</h2>
  {''.join(f'<p class="coach-text">{e(p)}</p>' for p in c['narrative'])}
  <div class="coach-cols">
    {f'<div><h3 class="h-sm">Working well</h3><ul class="points ok">{strengths}</ul></div>' if strengths else ''}
    {f'<div><h3 class="h-sm">Work on next</h3><ul class="points work">{work}</ul></div>' if work else ''}
  </div>
</section>"""
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
        f'<span class="cu-what">{e(iv["company"] or "Unknown company")} <span class="muted small">{e(iv["round"])}</span></span>'
        f'<span class="cu-end"><span class="num">{score(iv["overall"])}</span>{chip(*OUTCOME[iv["outcome"]])}</span></a></li>'
        for iv in list(reversed(perf["interviews"]))[:5])
    return f"""{head}
{coach_block(perf)}
<section class="numbers" aria-labelledby="numbers-title">
  <h2 id="numbers-title" class="h-md">The numbers</h2>
  {tiles}
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
  </div>
  <script type="application/json" id="perf-data">{data}</script>
</section>
<section class="coming"><div class="section-row"><h2 class="h-sm">Recent rounds</h2><a class="mono small" href="/valr/interviews/past">All past interviews →</a></div>
  <ul class="cu-list">{recent}</ul></section>"""
