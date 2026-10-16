"""The Performance page's trend math, takeaway wording and low-data states (app/valr_ui/trends.py,
charts.py, performance.py). All interviews here are made up."""
import re
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi", reason="the pages run in the app's venv")

from app import valr_page, valr_view  # noqa: E402
from app.valr_ui import charts, performance, trends  # noqa: E402
from tests.valr_sample import sample_view  # noqa: E402

LABELS = {k: valr_view.label(k) for k in trends.SKILLS}
CLOCK = valr_page.Clock("UTC", datetime(2026, 10, 16, tzinfo=timezone.utc))


def iv(i, overall=None, dims=None, imp=None, round_key="technical", outcome="unknown"):
    return {"id": f"fathom:{i}", "date": f"2026-09-{10 + i:02d}", "company": "Example Co", "round": "Technical",
            "round_key": round_key, "overall": overall, "dimensions": dims or {}, "impression": imp,
            "outcome": outcome, "do_next_time": [], "questions": []}


# ---------- the math ----------

def test_fitted_change_is_the_rise_of_the_fitted_line():
    assert trends.fitted_change([1, 2, 3, 4, 5]) == 4.0
    assert trends.fitted_change([5, 5, 5]) == 0.0
    assert trends.fitted_change([6, 4]) == -2.0
    assert trends.fitted_change([7]) == 0.0
    assert trends.fitted_change([5, 7, 5, 7, 5]) == 0.0  # Zig-zag: no trend.


def test_trend_uses_the_last_five_and_the_half_point_threshold():
    assert trends.trend([1, 1, 1, 5, 5, 5, 5, 5]) == {"dir": "flat", "change": 0.0, "n": 5}
    assert trends.trend([5, 5.25, 5.5])["dir"] == "up"  # +0.5 exactly counts.
    assert trends.trend([5, 5.2, 5.4])["dir"] == "flat"  # +0.4 does not.
    assert trends.trend([6, 5.75, 5.5])["dir"] == "down"
    assert trends.trend([5, 6]) == {"dir": None, "change": None, "n": 2}
    assert trends.trend([5, None, 6, None])["dir"] is None
    assert trends.trend([0.6, 0.7, 0.8], threshold=0.05)["dir"] == "up"


def test_rolling_average_of_three():
    assert trends.rolling([3, 6, 9, 3]) == [3.0, 4.5, 6.0, 6.0]
    assert trends.rolling([]) == []


# ---------- the wording ----------

def test_overall_takeaways():
    up = trends.overall([iv(i, v) for i, v in enumerate([4, 5, 6, 7, 8])])
    assert up["takeaway"] == "Your overall score is climbing: up 4.0 over your last 5 interviews."
    down = trends.overall([iv(i, v) for i, v in enumerate([8, 7, 6])])
    assert down["takeaway"] == "Your overall score is slipping: down 2.0 over your last 3 interviews."
    flat = trends.overall([iv(i, v) for i, v in enumerate([5, 6, 5, 6, 5.5])])
    assert flat["takeaway"] == "Your overall score is holding steady at about 5.5 out of 10."
    assert [p["avg"] for p in flat["points"]] == [5.0, 5.5, 5.33, 5.67, 5.5]


def test_skills_takeaway_names_the_biggest_mover_each_way():
    rows = lambda changes: [{"label": n, "trend": {"dir": d, "change": c}} for n, d, c in changes]  # noqa: E731
    assert trends.skills_takeaway(rows([("Technical", "up", 1.2), ("Communication", "up", 2.0),
                                        ("Pay conversations", "down", -0.9)])) == \
        "Communication is climbing; Pay conversations is slipping."
    assert trends.skills_takeaway(rows([("Technical", "up", 1.2), ("Clarity", "flat", 0.1)])) == \
        "Technical is climbing; the rest are holding steady."
    assert trends.skills_takeaway(rows([("Technical", "down", -1.2)])) == "Technical is slipping; nothing is climbing yet."
    assert trends.skills_takeaway(rows([("Technical", "flat", 0.0)])) == "All your skills are holding steady."
    assert trends.skills_takeaway(rows([("Technical", None, None)])) == "Needs 3+ interviews to show a trend."


def test_skills_rows_use_plain_names_and_skip_untested_skills():
    ivs = [iv(i, 5, {"technical": 4 + i, "negotiation": None}) for i in range(4)]
    out = trends.skills(ivs, LABELS)
    assert [r["label"] for r in out["rows"]] == ["Technical"]
    assert out["takeaway"] == "Technical is climbing; the rest are holding steady."


def test_reaction_takeaways_add_the_warm_vs_flat_split():
    imp = lambda score, warm, flat: {"score": score, "warm": warm, "flat": flat}  # noqa: E731
    cooling = trends.reactions([iv(i, imp=imp(s, 1, 5)) for i, s in enumerate([8, 6, 4])])
    assert cooling["takeaway"] == "Interviewers are cooling on you, and most of their reactions were flat (15 of 18)."
    warm = trends.reactions([iv(i, imp=imp(s, 4, 1)) for i, s in enumerate([4, 6, 8])])
    assert warm["takeaway"] == "Interviewers are warming to you, and most of their reactions were warm (12 of 15)."
    even = trends.reactions([iv(i, imp=imp(6, 2, 2)) for i in range(3)])
    assert even["takeaway"] == "Interviewer reactions are steady, and warm and flat reactions are about even."
    few = trends.reactions([iv(0, imp=imp(6, 2, 2))])
    assert few["takeaway"] == "Needs 3+ interviews with a transcript to show a trend."


def test_talk_takeaways_compare_the_last_three_with_the_healthy_band():
    imp = lambda share, secs: {"talk_share": share, "answer_seconds": secs}  # noqa: E731
    high = trends.talk([iv(i, imp=imp(0.8, 100)) for i in range(3)])
    assert high["takeaway"] == ("You're talking a lot: 80% of the words lately (aim for 50–72%), "
                                "and answers run long, about 100 seconds each.")
    ok = trends.talk([iv(0, imp=imp(0.9, 120))] + [iv(i, imp=imp(0.6, 50)) for i in range(1, 4)])
    assert ok["takeaway"] == "Your talk balance is healthy: 60% of the words lately, and answers stay under 90 seconds."
    low = trends.talk([iv(0, imp=imp(0.4, None))])
    assert low["takeaway"] == "You're talking less than ideal: 40% of the words lately (aim for 50–72%)."
    assert trends.talk([iv(0)])["takeaway"] == "Needs an interview with a transcript."


def test_funnel_uses_outcomes_and_offers_from_leads_when_available():
    ivs = [iv(0, outcome="advanced"), iv(1, outcome="rejected"), iv(2), iv(3, outcome="advanced")]
    without = trends.funnel(ivs, None)
    assert without["steps"] == [("Interviews", 4), ("Moved to the next round", 2)]
    assert without["takeaway"] == "2 of 4 interviews moved you to the next round. No word yet on 1."
    assert trends.funnel(ivs, 1)["takeaway"] == "2 of 4 interviews moved you to the next round; 1 offer so far. No word yet on 1."
    assert trends.funnel(ivs[:2], 0)["takeaway"] == "1 of 2 interviews moved you to the next round; no offers yet."
    leads = {"available": True, "leads": [{"stage": "offer"}, {"stage": "interviewing"}],
             "closed": [{"stage_raw": "closed-won"}, {"stage_raw": "closed-lost"}]}
    assert trends.offers_from_leads(leads) == 2
    assert trends.offers_from_leads({"available": False}) is None


def test_round_type_takeaway():
    ivs = [iv(0, 7, round_key="screen"), iv(1, 5, round_key="technical"), iv(2, 6, round_key="system_design"),
           iv(3, 6, round_key="hiring_manager")]
    out = trends.by_round(ivs)
    assert [(r["round"], r["avg"], r["n"]) for r in out["rows"]] == [("Screen", 7.0, 1), ("Technical", 5.5, 2),
                                                                      ("Hiring manager", 6.0, 1)]
    assert out["takeaway"] == "You score best in screen rounds (7.0) and lowest in technical rounds (5.5)."
    assert trends.by_round([iv(0, 6, round_key="screen"), iv(1, 6.2)])["takeaway"] == \
        "Your scores are about the same across round types."
    assert trends.by_round([iv(0, 6)])["rows"] and not charts.rounds_chart(trends.by_round([iv(0, 6)]))


def test_takeaways_follow_the_sample_data():
    ivs = sample_view()["performance"]["interviews"]
    t = trends.build(ivs, LABELS)
    assert t["overall"]["takeaway"].startswith("Your overall score is climbing")
    assert t["skills"]["takeaway"] == "Communication is climbing; Behavioural is slipping."
    assert t["talk"]["takeaway"].startswith("Your talk balance is healthy")


# ---------- the charts and the page ----------

def test_charts_are_accessible_and_handle_low_data():
    one = [iv(0, 6, {"technical": 6}, {"score": 7, "talk_share": 0.6, "answer_seconds": 40, "warm": 1, "flat": 1})]
    t = trends.build(one, LABELS)
    out = charts.all_charts(t)
    assert out.count('role="img"') == 4 and out.count('<table class="sr-only">') == 5
    assert "Needs 3+ interviews to show a trend." in out
    assert 'class="tc-avg"' not in out.split('id="chart-skills"')[0]  # No average line from one point.
    empty = charts.all_charts(trends.build([], LABELS))
    assert empty.count('class="tc-empty"') == 5 and "<svg class=\"tc-svg\"" not in empty


def test_overall_chart_marks_round_types_by_shape():
    ivs = [iv(0, 5, round_key="screen"), iv(1, 6), iv(2, 6, round_key="hiring_manager"), iv(3, 7, round_key="founder")]
    out = charts.overall_chart(trends.overall(ivs))
    for name in ("Screen", "Technical", "Hiring manager", "Founder", "Average of 3"):
        assert f"{name}</li>" in out
    assert out.count("<title>") == 5  # The chart's title and one per interview.


def test_page_renders_sample_data_without_jargon_or_pay_figures():
    view = sample_view()
    page = performance.page(view, CLOCK)
    text = re.sub(r"<[^>]+>", " ", page)
    assert text.index("Trends") < text.index("What it means") < text.index("Recent interviews") < text.index("All the numbers")
    for jargon in ("r =", "rubric", "schema", "stddev", "Correlation"):
        assert jargon not in text
    assert not re.search(r"\b\d+\s?(LPA|lakhs?|k\b)|\$\d", text)
    assert page.count('href="/valr/interviews/past/') == 5 and 'href="/valr/interviews/past">All past' in page


def test_where_you_stand_is_two_sentences_at_most():
    assert performance.sentences("One. Two! Three? Four.") == "One. Two!"
    view = sample_view()
    view["performance"]["coach"]["where_you_stand"] = "First point. Second point. Third point."
    page = performance.page(view, CLOCK)
    assert "First point. Second point." in page and "Third point." not in page


def test_low_data_and_empty_pages():
    view = sample_view()
    view["performance"]["interviews"] = view["performance"]["interviews"][:2]
    page = performance.page(view, CLOCK)
    assert "Needs 3+ interviews to show a trend." in page and 'id="chart-overall"' in page
    empty = sample_view(scenario="empty")
    assert "Your scores appear after your first recorded interview." in performance.page(empty, CLOCK)
