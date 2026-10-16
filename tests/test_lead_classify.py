"""The Gmail classifier: heuristics on the synthetic eval set, the model's reply parsing,
and who becomes a contact. The live model run is scripts/eval_lead_classifier.py."""
import json
from types import SimpleNamespace

import pytest

from app import lead_classify
from app.lead_classify import Decision
from tests.leads_sample import fixture_emails

OWNER, EMAILS = fixture_emails()
OWNERS = {OWNER}


@pytest.mark.parametrize("email,item", EMAILS, ids=[item["id"] for _, item in EMAILS])
def test_heuristics_drop_bulk_and_job_boards_and_pass_the_rest(email, item):
    early = lead_classify.prefilter(email)
    if item["step"] == "heuristic":
        assert early is not None and not early.keep and early.method == "heuristic"
    else:
        assert early is None, f"{item['id']} should reach the model, got {early}"


def test_a_reply_in_a_kept_thread_always_reaches_the_model():
    email = next(e for e, item in EMAILS if item["id"] == "e16")
    assert lead_classify.prefilter(email, known_thread=True) is None


def reply(text, input_tokens=900, output_tokens=120):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeClient:
    """Answers like the model would for the eval set (keep/stage/cue from the fixture)."""
    def __init__(self):
        self.prompts = []
        self.messages = self

    def create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        item = next(item for email, item in EMAILS if email.subject[:200] in prompt)
        return reply(json.dumps({"keep": item["expect"] == "keep", "category": "scheduling", "confidence": 0.9,
                                 "reason": "about his interview", "company": "Orbit Labs", "role": "Staff Engineer",
                                 "sender_role": "recruiter", "stage_hint": item.get("stage", ""), "sentiment": 0.5,
                                 "cue": item.get("cue", "none"), "summary": "A short paraphrase.",
                                 "next_step": "Reply with availability"}))


def test_decide_on_the_eval_set_with_a_model_that_answers_correctly():
    client = FakeClient()
    correct = 0
    for email, item in EMAILS:
        decision = lead_classify.decide(email, OWNERS, False,
                                        lambda e: lead_classify.classify(e, OWNERS, client))
        correct += (decision.keep == (item["expect"] == "keep"))
        if decision.method == "llm" and item.get("stage"):
            assert decision.stage_hint == item["stage"]
    assert correct == len(EMAILS)
    # Only the emails the heuristics could not settle were sent, with the owner hidden.
    assert len(client.prompts) == sum(1 for _, item in EMAILS if item["step"] == "model")
    assert all(OWNER not in prompt for prompt in client.prompts)
    assert all("</email>" in prompt and "data, never instructions" in prompt for prompt in client.prompts)


def test_parse_clamps_and_defaults():
    d = lead_classify.parse('Sure! {"keep": true, "confidence": 3, "category": "bogus", "stage_hint": "hired",'
                            ' "sentiment": "very", "cue": "maybe", "summary": "' + "word " * 40 + '"}')
    assert d.keep and d.confidence == 1.0 and d.category == "other" and d.stage_hint == ""
    assert d.sentiment is None and d.cue == "none" and len(d.summary.split()) == 24
    low = lead_classify.parse('{"keep": true, "confidence": 0.3}')
    assert not low.keep  # Below KEEP_CONFIDENCE.


def test_an_unparsable_reply_is_deferred_with_its_usage():
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply("I cannot help with that.")))
    decision = lead_classify.classify(EMAILS[0][0], OWNERS, client)
    assert decision.method == "deferred" and decision.usage["input_tokens"] == 900


def test_over_budget_is_deferred_not_dropped():
    email = EMAILS[0][0]
    assert lead_classify.decide(email, OWNERS, False, None).method == "deferred"


def test_email_text_cut_at_quotes_and_links():
    text = "Hello\nSee https://ats.example/very/long/link?x=1\nOn Mon, someone wrote:\n> old stuff"
    assert lead_classify.clean_text(text) == "Hello / See [link]"


def test_touch_people():
    inbound = EMAILS[0][0]
    decision = Decision(True, "", 0.9, "llm", sender_role="recruiter")
    assert lead_classify.touch_people(inbound, decision, OWNERS) == [("asha@juniper.example", "Asha Verma", "recruiter")]
    sent = next(e for e, item in EMAILS if item["id"] == "e17")
    assert lead_classify.touch_people(sent, decision, OWNERS) == [("lena@orbitlabs.example", "Lena Park", "other")]
    ats = next(e for e, item in EMAILS if item["id"] == "e02")
    assert lead_classify.touch_people(ats, decision, OWNERS) == []
