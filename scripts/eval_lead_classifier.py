"""Runs the real lead classifier (Claude Haiku via app/llm_gateway.py) on the synthetic eval set.

    python scripts/eval_lead_classifier.py            # needs OPENROUTER_API_KEY or ANTHROPIC_API_KEY

Prints, per email id, the step that decided, keep/drop against the expectation, the stage
and cue; then accuracy, tokens, cost and latency. The emails are all made up
(tests/fixtures/lead_emails.json).
"""
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / "projects/voice-agent-lab/.env")  # The main checkout's keys, if any.

from app import lead_classify, llm_gateway, research_jobs  # noqa: E402
from tests.leads_sample import fixture_emails  # noqa: E402


def main() -> None:
    owner, emails = fixture_emails()
    client = llm_gateway.shared().client(timeout=60)
    right, calls, usd, waits, tokens_in, tokens_out = 0, 0, 0.0, [], 0, 0
    stage_right = stage_total = 0
    for email, item in emails:
        started = time.perf_counter()
        decision = lead_classify.decide(email, {owner}, False,
                                        lambda e: lead_classify.classify(e, {owner}, client))
        if decision.usage:
            calls += 1
            waits.append(time.perf_counter() - started)
            tokens_in += decision.usage["input_tokens"]
            tokens_out += decision.usage["output_tokens"]
            usd += research_jobs.price(lead_classify.MODEL, decision.usage["input_tokens"],
                                       decision.usage["output_tokens"], decision.usage["cache_read"],
                                       decision.usage["cache_write"])
        ok = decision.keep == (item["expect"] == "keep")
        right += ok
        if item.get("stage") and decision.method == "llm":
            stage_total += 1
            stage_right += decision.stage_hint == item["stage"]
        print(f"{item['id']}  {decision.method:9}  {'keep' if decision.keep else 'drop'} "
              f"(expected {item['expect']}) {'ok ' if ok else 'MISS'}  conf {decision.confidence:.2f}  "
              f"stage {decision.stage_hint or '-':12} cue {decision.cue:8} {decision.category}")
    print(f"\naccuracy {right}/{len(emails)}; stage hints {stage_right}/{stage_total}; model calls {calls}; "
          f"tokens in {tokens_in}, out {tokens_out}; ${usd:.5f} total, "
          f"${usd / max(calls, 1):.6f} per model call; latency p50 "
          f"{statistics.median(waits) if waits else 0:.2f} s, max {max(waits) if waits else 0:.2f} s")


if __name__ == "__main__":
    main()
