"""Runs the real interview scorer (app/profile/rubric.py) on the made-up eval set and checks
that its scores track performance. Progress is only as right as these scores.

    python scripts/eval_scoring.py                        # the profile gateway: Groq by default
    LLM_GATEWAY_PROFILE=anthropic python scripts/eval_scoring.py   # Claude Sonnet, to compare
    python scripts/eval_scoring.py --repeat 2             # also checks run-to-run stability

Checks (tests/scoring_sample.py has the interviews):
  scored        every interview is scored (no error, no reply cut off)
  whole         no transcript was shortened to fit the model's limits
  pair gap      strong beats weak by >= 2 overall and >= 2 on the dimension the pair tests
  n/a           a dimension the round never touched is N/A, not a guess
  negotiation   disclosing current pay scores <= 4 on information control; asking for the band >= 7
  long          strong answers in the middle of a long call score as they do in a short one
  stable        (--repeat) the same interview's overall moves by <= 1 between runs
Exits 1 when a check fails.
"""
import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / "projects/voice-agent-lab/.env")  # The main checkout's keys, if any.

from app import tenant  # noqa: E402
from app.profile import rubric  # noqa: E402
from app.profile.config import settings as load_settings  # noqa: E402
from app.profile.llm import AnthropicJsonLLM, LLMError  # noqa: E402
from tests import scoring_sample as sample  # noqa: E402


class Shortened(logging.Handler):
    """Counts the gateway's 'input was shortened' warnings (app/profile/llm.py)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        self.count += "shortened" in record.getMessage()


def score(meeting: dict, llm, model: str) -> dict:
    analysis = {"source": meeting["source"], "id": meeting["id"], "title": meeting["title"],
                "date": meeting["date"], "company": "Acme Voice", "round_type": None}
    return rubric.score_interview(meeting, analysis, llm, model, sample.CANDIDATE)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", nargs="*", help="case ids (default: all)")
    args = parser.parse_args()
    tenant.current.set(tenant.OWNER)
    model = load_settings().evaluator_model
    llm = AnthropicJsonLLM()
    shortened = Shortened()
    logging.getLogger().addHandler(shortened)
    cases = sample.cases()
    ids = args.only or list(cases)
    results: dict[str, list[dict]] = {}
    failures: list[str] = []
    times = []

    def check(ok: bool, what: str):
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            failures.append(what)

    for case_id in ids:
        for run in range(args.repeat):
            before = shortened.count
            started = time.perf_counter()
            try:
                out = score(cases[case_id], llm, model)
            except LLMError as error:
                print(f"{case_id} run {run + 1}: ERROR {error}")
                failures.append(f"{case_id}: not scored ({error})")
                continue
            times.append(time.perf_counter() - started)
            results.setdefault(case_id, []).append(out)
            dims = {d: (round(v, 1) if v is not None else None) for d, v in out["dimension_scores"].items()}
            print(f"{case_id} run {run + 1}: overall {out['overall_score']}  {dims}  "
                  f"{times[-1]:.0f} s{'  SHORTENED' if shortened.count > before else ''}")
            if shortened.count > before:
                failures.append(f"{case_id}: transcript shortened")

    first = {k: v[0] for k, v in results.items()}
    sub = lambda c, key: first[c]["scores"][key.split(".")[0]][key.split(".")[1]]["score"]  # noqa: E731
    print("\nChecks")
    for strong, weak, dim in sample.PAIRS:
        if strong in first and weak in first:
            gap = (first[strong]["overall_score"] or 0) - (first[weak]["overall_score"] or 0)
            check(gap >= sample.MIN_OVERALL_GAP, f"pair gap {strong} - {weak} overall {gap:+}")
            d_gap = (first[strong]["dimension_scores"][dim] or 0) - (first[weak]["dimension_scores"][dim] or 0)
            check(d_gap >= sample.MIN_DIMENSION_GAP, f"pair gap {strong} - {weak} {dim} {d_gap:+.1f}")
    for case_id in ("sd_weak", "sd_strong", "beh_weak", "beh_strong", "long_sd_strong"):
        if case_id in first:
            check(first[case_id]["dimension_scores"]["negotiation"] is None,
                  f"n/a {case_id}: negotiation (pay never came up)")
    for case_id in ("beh_weak", "beh_strong", "comp_leaky", "comp_strong"):
        if case_id in first:
            check(sub(case_id, "technical.system_design") is None, f"n/a {case_id}: system design")
    if "comp_leaky" in first:
        value = sub("comp_leaky", "negotiation.information_control")
        check(value is not None and value <= 4, f"negotiation comp_leaky information control {value} <= 4")
    if "comp_strong" in first:
        value = sub("comp_strong", "negotiation.information_control")
        check(value is not None and value >= 7, f"negotiation comp_strong information control {value} >= 7")
    if "long_sd_strong" in first and "sd_strong" in first:
        long_t = first["long_sd_strong"]["dimension_scores"]["technical"] or 0
        short_t = first["sd_strong"]["dimension_scores"]["technical"] or 0
        check(abs(long_t - short_t) <= 1.5 and long_t >= 7,
              f"long technical {long_t:.1f} vs short {short_t:.1f}")
    if args.repeat > 1:
        for case_id, runs in results.items():
            overall = [r["overall_score"] or 0 for r in runs]
            check(max(overall) - min(overall) <= 1, f"stable {case_id} overall {overall}")
    print(f"\n{len(failures)} failed; model {model}; {sum(len(v) for v in results.values())} scored; "
          f"p50 {statistics.median(times) if times else 0:.0f} s; {llm.ledger.summary()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
